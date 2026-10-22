"""Contratos HTTP locais sobre os serviços e gabaritos existentes."""

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from shutil import copytree
from threading import Event
from time import monotonic
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from agente_sped.application.ingestion import ingest_file
from agente_sped.application.sessions import WebSession
from agente_sped.infra.analysis_catalog import load_analysis_catalog
from agente_sped.interface import web
from tests.test_analytical_core import _ANALYSIS_IDS, _reference_values

_ORIGIN = 'http://localhost'
_REFERENCE = (
    Path(__file__).parent / 'fixtures/sped_icms_analysis_reference.txt'
)


def _settings(root: Path) -> web.WebSettings:
    return web.WebSettings(
        _env_file=None,
        ai_routing_enabled=False,
        ai_narrative_enabled=False,
        exploratory_sql_enabled=False,
        gemini_api_key=None,
        session_root=root,
    )


@pytest.fixture(scope='module')
def reference_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    destination = tmp_path_factory.mktemp('web-reference') / 'workspace'
    ingest_file(_REFERENCE, destination)
    return destination


@dataclass
class Browser:
    client: TestClient

    @property
    def manager(self):
        return self.client.app.state.manager

    @property
    def session(self) -> WebSession:
        return self.manager.open(
            self.client.cookies.get(web.SESSION_COOKIE), touch=False
        )

    def form(self, **values: str) -> dict[str, str]:
        return {
            'csrf': self.session.csrf,
            'request_id': uuid4().hex,
            **values,
        }

    def post(self, path: str, data: dict[str, str], **kwargs):
        return self.client.post(
            path,
            data=data,
            headers={'Origin': _ORIGIN, 'HX-Request': 'true'},
            **kwargs,
        )

    def wait(self) -> str:
        deadline = monotonic() + 30
        pause = Event()
        while monotonic() < deadline:
            response = self.client.get(
                '/status', headers={'HX-Request': 'true'}
            )
            assert response.status_code == HTTPStatus.OK
            operation = self.session.metadata.operation
            if operation and operation.status != 'running':
                return response.text
            pause.wait(0.01)
        pytest.fail('A operação HTTP não chegou a um estado final em 30s.')

    def analyze(self, analysis_id: str) -> str:
        response = self.post('/analyze', self.form(analysis_id=analysis_id))
        assert response.status_code == HTTPStatus.ACCEPTED
        self.wait()
        operation = self.session.metadata.operation
        assert operation.status == 'completed'
        assert operation.outcome_status == 'completed'
        return self.session.metadata.run_ids[-1]


def _seed(browser: Browser, reference: Path) -> None:
    assert browser.client.get('/').status_code == HTTPStatus.OK
    session = browser.session
    with browser.manager.lease(session):
        copytree(reference, session.workspace_dir)
        session.metadata.workspace_ready = True
        session.metadata.source_name = 'escrituracao-sintetica.txt'
        browser.manager.save(session)


@pytest.fixture
def browser(tmp_path: Path, reference_workspace: Path) -> Iterator[Browser]:
    app = web.create_app(_settings(tmp_path / 'sessions'))
    with TestClient(app, base_url=_ORIGIN) as client:
        current = Browser(client)
        _seed(current, reference_workspace)
        yield current


@pytest.mark.parametrize('analysis_id', _ANALYSIS_IDS)
def test_http_analysis_matches_existing_reference(browser, analysis_id):
    run_id = browser.analyze(analysis_id)
    report = browser.manager.report(browser.session, run_id)
    expected = load_analysis_catalog().reference_validation['expected'][
        analysis_id
    ]
    assert _reference_values(report.result) == expected
    assert report.result.row_count == report.result.total_count
    assert not report.result.truncated
    page = browser.client.get(f'/runs/{run_id}')
    assert page.status_code == HTTPStatus.OK
    assert report.title in page.text


def test_http_forms_origin_csrf_headers_and_html_escaping(browser):
    session = browser.session
    response = browser.client.get('/')
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['vary'] == 'HX-Request'
    assert "script-src 'self'" in response.headers['content-security-policy']
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie
    assert 'SameSite=strict' in cookie
    assert 'Max-Age=' in cookie
    assert str(session.directory) not in response.text
    assert (
        browser.post(
            '/analyze',
            browser.form(csrf='invalid', analysis_id='movement_by_cfop'),
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    assert (
        browser.client.post(
            '/analyze',
            data=browser.form(analysis_id='movement_by_cfop'),
            headers={'Origin': 'https://foreign.example'},
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    assert (
        browser.client.get(
            '/', headers={'Host': 'foreign.example'}
        ).status_code
        == HTTPStatus.BAD_REQUEST
    )
    with browser.manager.lease(session):
        session.metadata.source_name = '<script>.txt'
        browser.manager.save(session)
    escaped = browser.client.get('/')
    assert '&lt;script&gt;.txt' in escaped.text
    assert '<script>.txt' not in escaped.text
    assert browser.session.metadata.operation is None
    browser.client.cookies.clear()
    browser.client.cookies.set(
        web.SESSION_COOKIE,
        'tampered-cookie',
        domain='localhost.local',
        path='/',
    )
    unauthorized = browser.post(
        '/analyze',
        {
            'csrf': session.csrf,
            'request_id': uuid4().hex,
            'analysis_id': 'movement_by_cfop',
        },
    )
    assert unauthorized.status_code == HTTPStatus.FORBIDDEN
    assert session.directory.is_dir()
    incompatible = browser.session
    path = incompatible.directory / 'session.json'
    metadata = json.loads(path.read_text())
    metadata['ingestion_version'] = 'future'
    path.write_text(json.dumps(metadata))
    failure = browser.client.get('/status')
    assert failure.status_code == HTTPStatus.CONFLICT
    assert path.is_file()
    assert browser.session.identifier != incompatible.identifier


def test_mask_reveal_and_exports_reuse_snapshot_without_execution(
    browser, monkeypatch
):
    run_id = browser.analyze('cancelled_documents')
    workspace = browser.session.workspace_dir
    snapshot = workspace / 'results' / f'{run_id}.json'
    original = hashlib.sha256(snapshot.read_bytes()).digest()

    def forbidden_execution(*args, **kwargs):
        pytest.fail('Apresentação e exportação não podem executar análises.')

    monkeypatch.setattr(web, 'run_agent', forbidden_execution)
    masked = browser.client.get(f'/runs/{run_id}')
    assert '***' in masked.text
    assert '>2001<' not in masked.text
    revealed = browser.post(
        f'/runs/{run_id}/reveal', browser.form(show_sensitive='true')
    )
    assert revealed.status_code == HTTPStatus.OK
    assert '>2001<' in revealed.text
    assert '>2001<' not in browser.client.get(f'/runs/{run_id}').text
    report = browser.manager.report(browser.session, run_id)
    record_id = next(
        value
        for name, value in report.result.rows[0].items()
        if name == 'record_id' or name.endswith('_record_id')
    )
    evidence = browser.post(
        f'/runs/{run_id}/evidence/{record_id}', browser.form()
    )
    assert evidence.status_code == HTTPStatus.OK
    assert '|C100|' in evidence.text
    for format in ('csv', 'xlsx'):
        response = browser.post(
            f'/runs/{run_id}/exports', browser.form(format=format)
        )
        assert response.status_code == HTTPStatus.ACCEPTED
        page = browser.wait()
        paths = re.findall(r'href="(/downloads/[^\"]+)"', page)
        data_url = next(path for path in paths if path.endswith('.' + format))
        download = browser.client.get(data_url)
        assert download.status_code == HTTPStatus.OK
        assert 'attachment' in download.headers['content-disposition']
        if format == 'csv':
            rows = list(
                csv.DictReader(
                    io.StringIO(download.content.decode('utf-8-sig'))
                )
            )
            expected_count = browser.manager.report(
                browser.session, run_id
            ).result.row_count
            assert len(rows) == expected_count
            assert {row['num_doc'] for row in rows} == {'***'}
            context_url = next(
                path for path in paths if path.endswith('.context.json')
            )
            context = browser.client.get(context_url).json()
            assert context['run_id'] == run_id
            assert context['exported_rows'] == expected_count
            assert context['privacy_mode'] == 'masked'
        else:
            with ZipFile(io.BytesIO(download.content)) as workbook:
                definition = workbook.read('xl/workbook.xml').decode()
                assert 'name="Dados"' in definition
                assert 'name="Contexto"' in definition
    assert hashlib.sha256(snapshot.read_bytes()).digest() == original
    assert browser.session.metadata.run_ids == [run_id]


def test_other_session_cannot_open_reveal_export_or_download_run(browser):
    run_id = browser.analyze('cancelled_documents')
    previous = browser.session
    browser.client.cookies.clear()
    assert browser.client.get('/').status_code == HTTPStatus.OK
    assert browser.session.identifier != previous.identifier
    assert (
        browser.client.get(f'/runs/{run_id}').status_code
        == HTTPStatus.BAD_REQUEST
    )
    assert (
        browser.post(
            f'/runs/{run_id}/reveal', browser.form(show_sensitive='true')
        ).status_code
        == HTTPStatus.BAD_REQUEST
    )
    assert (
        browser.post(
            f'/runs/{run_id}/exports', browser.form(format='csv')
        ).status_code
        == HTTPStatus.BAD_REQUEST
    )
    assert (
        browser.client.get(
            f'/downloads/{run_id}.masked.{uuid4().hex}.csv'
        ).status_code
        == HTTPStatus.NOT_FOUND
    )
    assert previous.workspace_dir.is_dir()


def test_ask_distinguishes_generic_intent_and_restricted_scope(browser):
    response = browser.post(
        '/ask', browser.form(question='itens com NCM genérico')
    )
    assert response.status_code == HTTPStatus.ACCEPTED
    browser.wait()
    first_run = browser.session.metadata.run_ids[-1]
    report = browser.manager.report(browser.session, first_run)
    assert report.result.analysis_id == 'generic_ncm_high_value'
    response = browser.post(
        '/ask', browser.form(question='Mostre produtos com NCM 99999999.')
    )
    assert response.status_code == HTTPStatus.ACCEPTED
    clarification = browser.wait()
    operation = browser.session.metadata.operation
    assert operation.outcome_status == 'needs_clarification'
    assert operation.options == ['generic_ncm_high_value']
    assert 'Itens de alto valor com NCM genérico' in clarification
    assert browser.session.metadata.run_ids == [first_run]
    unknown = browser.post(
        '/ask', browser.form(question='Qual é o saldo da conta bancária?')
    )
    assert unknown.status_code == HTTPStatus.ACCEPTED
    browser.wait()
    assert (
        browser.session.metadata.operation.outcome_status
        == 'needs_clarification'
    )
    assert browser.session.metadata.run_ids == [first_run]


def test_polling_and_persistent_cookie_recover_saved_report_after_restart(
    tmp_path, reference_workspace
):
    settings = _settings(tmp_path / 'sessions')
    with TestClient(web.create_app(settings), base_url=_ORIGIN) as client:
        browser = Browser(client)
        _seed(browser, reference_workspace)
        run_id = browser.analyze('movement_by_cfop')
        activity = browser.session.metadata.last_activity
        for _ in range(2):
            assert client.get('/status').status_code == HTTPStatus.OK
        assert browser.session.metadata.last_activity == activity
        cookies = client.cookies
        identifier = browser.session.identifier
    with TestClient(
        web.create_app(settings), base_url=_ORIGIN, cookies=cookies
    ) as reopened:
        page = reopened.get('/')
        assert page.status_code == HTTPStatus.OK
        recovered = Browser(reopened)
        assert recovered.session.identifier == identifier
        assert recovered.session.metadata.run_ids == [run_id]
        assert 'Movimentação regular de saída por CFOP' in page.text
        manager = recovered.manager
        manager._clock = lambda: activity + timedelta(hours=25)
        expired = reopened.get('/')
        assert expired.status_code == HTTPStatus.OK
        assert recovered.session.identifier != identifier
        assert recovered.session.metadata.run_ids == []


def test_poll_during_worker_preserves_completion_and_request_idempotence(
    browser, monkeypatch
):
    entered, release = Event(), Event()
    original = web.run_agent
    calls = []

    def delayed(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(web, 'run_agent', delayed)
    form = browser.form(analysis_id='movement_by_cfop')
    try:
        assert (
            browser.post('/analyze', form).status_code == HTTPStatus.ACCEPTED
        )
        assert entered.wait(5)
        status = browser.client.get('/status', headers={'HX-Request': 'true'})
        assert status.status_code == HTTPStatus.OK
        assert 'aria-busy="true"' in status.text
        assert (
            browser.post(
                '/analyze', browser.form(analysis_id='movement_by_cfop')
            ).status_code
            == HTTPStatus.CONFLICT
        )
        clear = browser.post('/session/clear', browser.form())
        assert clear.status_code == HTTPStatus.CONFLICT
    finally:
        release.set()
    browser.wait()
    assert browser.session.metadata.operation.status == 'completed'
    assert len(browser.session.metadata.run_ids) == 1
    assert browser.post('/analyze', form).status_code == HTTPStatus.ACCEPTED
    browser.wait()
    assert len(calls) == 1
    assert len(browser.session.metadata.run_ids) == 1

    def crash(*args, **kwargs):
        raise RuntimeError('synthetic internal failure must not be exposed')

    monkeypatch.setattr(web, 'run_agent', crash)
    failure = browser.post(
        '/analyze', browser.form(analysis_id='movement_by_cfop')
    )
    assert failure.status_code == HTTPStatus.ACCEPTED
    page = browser.wait()
    assert browser.session.metadata.operation.status == 'failed'
    assert 'synthetic internal failure' not in page
    assert len(browser.session.metadata.run_ids) == 1


def test_upload_and_clear_use_existing_ingestion_contract(
    tmp_path, reference_workspace, monkeypatch
):
    calls = []

    def materialize(source, destination):
        calls.append(source.read_bytes())
        copytree(reference_workspace, destination)

    monkeypatch.setattr(web, 'ingest_file', materialize)
    app = web.create_app(_settings(tmp_path / 'sessions'))
    with TestClient(app, base_url=_ORIGIN) as client:
        browser = Browser(client)
        assert client.get('/').status_code == HTTPStatus.OK
        empty = browser.post(
            '/upload',
            browser.form(),
            files={'file': ('empty.txt', b'', 'text/plain')},
        )
        assert empty.status_code == HTTPStatus.BAD_REQUEST
        assert not browser.session.source_path.exists()
        assert browser.session.metadata.operation is None
        response = browser.post(
            '/upload',
            browser.form(),
            files={
                'file': (
                    '../../synthetic.txt',
                    _REFERENCE.read_bytes(),
                    'text/plain',
                )
            },
        )
        assert response.status_code == HTTPStatus.ACCEPTED
        browser.wait()
        session = browser.session
        assert session.metadata.workspace_ready
        assert session.metadata.source_name == 'synthetic.txt'
        assert not session.source_path.exists()
        assert calls == [_REFERENCE.read_bytes()]
        cleared = browser.post('/session/clear', browser.form())
        assert cleared.status_code == HTTPStatus.OK
        assert not session.directory.exists()
        assert browser.session.identifier != session.identifier
        assert not browser.session.metadata.workspace_ready


def test_body_limit_rejects_chunked_upload_without_content_length(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(web, 'DEFAULT_MAX_FILE_BYTES', 1024)
    monkeypatch.setattr(web, '_BODY_OVERHEAD', 256)
    app = web.create_app(_settings(tmp_path / 'sessions'))
    with TestClient(app, base_url=_ORIGIN) as client:
        browser = Browser(client)
        assert client.get('/').status_code == HTTPStatus.OK
        form = browser.form()
        boundary = 'synthetic-boundary'
        fields = ''.join(
            f'--{boundary}\r\nContent-Disposition: form-data; '
            f'name="{name}"\r\n\r\n{value}\r\n'
            for name, value in form.items()
        ).encode()
        file_header = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="synthetic.txt"\r\nContent-Type: text/plain\r\n\r\n'
        ).encode()
        chunks = [
            fields + file_header,
            b'x' * 2048,
            f'\r\n--{boundary}--\r\n'.encode(),
        ]
        request = client.build_request(
            'POST',
            '/upload',
            content=iter(chunks),
            headers={
                'Origin': _ORIGIN,
                'Content-Type': f'multipart/form-data; boundary={boundary}',
            },
        )
        assert 'content-length' not in request.headers
        response = client.send(request)
        assert response.status_code == HTTPStatus.CONTENT_TOO_LARGE
        assert not browser.session.metadata.workspace_ready
        assert browser.session.metadata.operation is None
        assert not browser.session.source_path.exists()
