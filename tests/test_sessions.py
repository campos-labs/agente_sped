"""Retenção, isolamento e recuperação de referências locais de sessão."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from agente_sped.application import sessions
from agente_sped.application.ingestion import ingest_file
from agente_sped.application.sessions import (
    Operation,
    SessionBusyError,
    SessionUnavailableError,
    WorkspaceManager,
)

_FIXTURE = Path(__file__).parent / 'fixtures/sped_icms_analysis_reference.txt'


@pytest.fixture
def clock():
    moments = [datetime(2026, 1, 1, tzinfo=UTC)]
    return moments


def test_session_reopens_with_opaque_cookie_and_no_token_on_disk(tmp_path):
    manager = WorkspaceManager(tmp_path)
    session = manager.open(None)
    with manager.lease(session):
        session.metadata.run_ids.append(uuid4().hex)
        manager.save(session)
    reopened = WorkspaceManager(tmp_path).open(session.token, touch=False)
    assert reopened.identifier == session.identifier
    assert reopened.metadata.run_ids == session.metadata.run_ids
    assert reopened.csrf == session.csrf
    assert (
        session.identifier
        == hashlib.sha256(session.token.encode('ascii')).hexdigest()
    )
    assert (
        session.token not in (session.directory / 'session.json').read_text()
    )
    assert session.token not in repr(session)
    assert session.csrf not in repr(session)


def test_retention_uses_activity_and_polling_does_not_renew(tmp_path, clock):
    manager = WorkspaceManager(tmp_path, clock=lambda: clock[0])
    session = manager.open(None)
    clock[0] += timedelta(hours=23)
    assert manager.open(session.token).identifier == session.identifier
    clock[0] += timedelta(hours=23)
    assert (
        manager.open(session.token, touch=False).identifier
        == session.identifier
    )
    clock[0] += timedelta(hours=1)
    fresh = manager.open(session.token, touch=False)
    assert fresh.identifier != session.identifier
    assert not session.directory.exists()


def test_busy_session_isolated_from_cleanup_and_other_session(tmp_path, clock):
    manager = WorkspaceManager(tmp_path, clock=lambda: clock[0])
    first, second = manager.open(None), manager.open(None)
    with manager.lease(first):
        with pytest.raises(SessionBusyError), manager.lease(first):
            pytest.fail('Uma sessão não pode executar duas escritas.')
        with manager.lease(second):
            manager.save(second)
        with pytest.raises(SessionBusyError):
            manager.delete(first)
        clock[0] += timedelta(hours=24)
        with pytest.raises(SessionBusyError, match='expirou'):
            manager.open(first.token, touch=False)
        assert manager.cleanup() == 1
        assert first.directory.exists()
    assert manager.cleanup() == 1


def test_recovery_marks_interruption_and_discards_incomplete_ingestion(
    tmp_path,
):
    manager = WorkspaceManager(tmp_path)
    session = manager.open(None)
    with manager.lease(session):
        session.metadata.operation = Operation(
            id=uuid4().hex, kind='upload', status='running'
        )
        session.workspace_dir.mkdir()
        (session.workspace_dir / 'analysis.duckdb').write_bytes(b'incomplete')
        manager.save(session)
    reopened = WorkspaceManager(tmp_path).open(session.token)
    assert reopened.metadata.operation.status == 'interrupted'
    assert not reopened.metadata.workspace_ready
    assert not reopened.workspace_dir.exists()


def test_reserved_operation_is_not_deleted_before_worker_starts(
    tmp_path, clock
):
    manager = WorkspaceManager(tmp_path, clock=lambda: clock[0])
    session = manager.open(None)
    with manager.lease(session):
        session.metadata.operation = Operation(
            id=uuid4().hex, kind='analysis', status='running'
        )
        manager.save(session)
    with pytest.raises(SessionBusyError):
        manager.delete(session)
    clock[0] += timedelta(hours=24)
    assert manager.cleanup() == 0
    with pytest.raises(SessionBusyError, match='expirou'):
        manager.open(session.token, touch=False)
    assert session.directory.exists()


def test_atomic_save_and_idempotent_clear(tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path)
    session = manager.open(None)
    original = (session.directory / 'session.json').read_bytes()

    def interrupted_replace(*args):
        raise OSError('simulated failure before atomic replacement')

    with manager.lease(session):
        session.metadata.operation = Operation(
            id=uuid4().hex, kind='analysis', status='failed'
        )
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'replace', interrupted_replace)
            with pytest.raises(OSError, match='simulated'):
                manager.save(session)
    assert (session.directory / 'session.json').read_bytes() == original
    assert list(session.directory.iterdir()) == [
        session.directory / 'session.json'
    ]
    assert manager.delete(session)
    assert not manager.delete(session)


def test_metadata_validation_does_not_silently_reset_local_data(tmp_path):
    manager = WorkspaceManager(tmp_path)
    session = manager.open(None)
    metadata_path = session.directory / 'session.json'
    value = json.loads(metadata_path.read_text())
    value['ingestion_version'] = 'future'
    metadata_path.write_text(json.dumps(value))
    with pytest.raises(SessionUnavailableError, match='incompatível'):
        manager.open(session.token)
    assert manager.cleanup() == 0
    assert session.directory.exists()


def test_polling_waits_for_atomic_metadata_replacement(tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path)
    session = manager.open(None)
    reader_session = manager.open(session.token, touch=False)
    target = session.directory / 'session.json'
    replacing, release, reading, finished = Event(), Event(), Event(), Event()
    original_replace, original_open = Path.replace, Path.open

    def replace(path, destination):
        if destination == target:
            replacing.set()
            assert release.wait(5)
        return original_replace(path, destination)

    def open_path(path, *args, **kwargs):
        if path == target and replacing.is_set() and not release.is_set():
            raise PermissionError('Synthetic Windows replacement conflict')
        return original_open(path, *args, **kwargs)

    def write():
        with manager.lease(session):
            session.metadata.source_name = 'synthetic.txt'
            manager.save(session)

    def read():
        reading.set()
        try:
            return manager.read(reader_session)
        finally:
            finished.set()

    monkeypatch.setattr(Path, 'replace', replace)
    monkeypatch.setattr(Path, 'open', open_path)
    with ThreadPoolExecutor(max_workers=2) as workers:
        writer = workers.submit(write)
        assert replacing.wait(5)
        reader = workers.submit(read)
        try:
            assert reading.wait(5)
            assert not finished.wait(0.05)
        finally:
            release.set()
        writer.result(timeout=5)
        assert reader.result(timeout=5).source_name == 'synthetic.txt'


def test_paths_and_linked_data_are_not_followed(tmp_path):
    manager = WorkspaceManager(tmp_path / 'sessions')
    session = manager.open('../../outside')
    outside = tmp_path / 'outside'
    outside.mkdir()
    marker = outside / 'keep.txt'
    marker.write_text('preserve')
    try:
        (session.directory / 'linked').symlink_to(
            outside, target_is_directory=True
        )
    except OSError:
        pytest.skip(
            'O sistema não permite criar links simbólicos neste ambiente.'
        )
    with pytest.raises(SessionUnavailableError, match='inseguro'):
        manager.delete(session)
    assert marker.read_text() == 'preserve'


def test_report_access_requires_run_belonging_to_active_session(
    tmp_path, monkeypatch
):
    manager = WorkspaceManager(tmp_path)
    session = manager.open(None)
    allowed, foreign = uuid4().hex, uuid4().hex
    with manager.lease(session):
        session.metadata.run_ids.append(allowed)
        manager.save(session)
    sentinel = object()
    calls = []

    def load(workspace, run_id):
        calls.append((workspace, run_id))
        return sentinel

    monkeypatch.setattr(sessions, 'load_report', load)
    with pytest.raises(SessionUnavailableError, match='não pertence'):
        manager.report(session, foreign)
    assert not calls
    assert manager.report(session, allowed) is sentinel
    assert calls == [(session.workspace_dir, allowed)]
    with pytest.raises(ValueError, match='UUID'):
        manager.report(session, '../other')


def test_ingestion_without_analysis_preserves_database_and_rejects_overwrite(
    tmp_path,
):
    destination = tmp_path / 'workspace'
    workspace = ingest_file(_FIXTURE, destination)
    assert workspace.database_path.is_file()
    assert workspace.period_start.isoformat() == '2026-01-01'
    assert (destination / 'source.txt').read_bytes() == _FIXTURE.read_bytes()
    with pytest.raises(FileExistsError, match='já existe'):
        ingest_file(_FIXTURE, destination)
