"""Sessões locais recuperáveis; não retomam operações interrompidas."""

import hashlib
import math
import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock, get_ident
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agente_sped.domain.reports import ReportPackage
from agente_sped.infra.report_store import load_report, validate_run_id

_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
_IDENTIFIER = re.compile(r'[0-9a-f]{64}')
_MAX_METADATA_BYTES = 64 * 1024


class SessionBusyError(ValueError):
    """Uma operação ainda utiliza os dados da sessão."""


class SessionUnavailableError(ValueError):
    """Metadados locais incompatíveis ou indisponíveis."""


class Operation(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_assignment=True)

    id: str = Field(pattern=r'^[0-9a-f]{32}$')
    kind: Literal['upload', 'analysis', 'ask', 'export']
    status: Literal['running', 'completed', 'failed', 'interrupted']
    message: str | None = Field(default=None, max_length=500)
    outcome_status: str | None = Field(default=None, max_length=64)
    options: list[str] = Field(default_factory=list, max_length=8)


class SessionMetadata(BaseModel):
    """Somente referências e estado: sem perguntas, resultados ou caminhos."""

    model_config = ConfigDict(extra='forbid', validate_assignment=True)

    schema_version: Literal['1'] = '1'
    ingestion_version: Literal['1'] = '1'
    csrf: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$', repr=False)
    created_at: datetime
    last_activity: datetime
    workspace_ready: bool = False
    source_name: str | None = Field(default=None, min_length=1, max_length=128)
    run_ids: list[str] = Field(default_factory=list)
    operation: Operation | None = None

    @model_validator(mode='after')
    def validate_references(self) -> SessionMetadata:
        for moment in (self.created_at, self.last_activity):
            if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
                raise ValueError('Atividade da sessão deve usar UTC.')
        if self.last_activity < self.created_at:
            raise ValueError('Atividade anterior à criação da sessão.')
        if self.source_name is not None and (
            self.source_name in {'.', '..'}
            or re.search(r'[/\\\x00-\x1f]', self.source_name)
        ):
            raise ValueError('Nome de arquivo deve ser um basename simples.')
        if len(self.run_ids) != len(set(self.run_ids)):
            raise ValueError('Referência de execução duplicada.')
        for run_id in self.run_ids:
            validate_run_id(run_id)
        return self


@dataclass
class WebSession:
    identifier: str
    token: str = field(repr=False)
    metadata: SessionMetadata
    directory: Path = field(repr=False)

    @property
    def csrf(self) -> str:
        return self.metadata.csrf

    @property
    def workspace_dir(self) -> Path:
        path = self.directory / 'workspace'
        _safe_path(path)
        return path

    @property
    def source_path(self) -> Path:
        path = self.directory / 'upload.txt'
        _safe_path(path)
        return path


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_path(path: Path) -> None:
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise SessionUnavailableError('Caminho de sessão inseguro.')


class WorkspaceManager:
    """Coordene um processo local com exclusão mútua por sessão."""

    def __init__(
        self,
        root: Path,
        *,
        retention_hours: float = 24,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if (
            isinstance(retention_hours, bool)
            or not math.isfinite(retention_hours)
            or retention_hours <= 0
        ):
            raise ValueError('Retenção deve ser um número positivo de horas.')
        self.root = root.expanduser().absolute()
        _safe_path(self.root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.retention = timedelta(hours=retention_hours)
        self._clock = clock
        self._locks: dict[str, Lock] = {}
        self._guard = Lock()
        self._held: dict[str, int] = {}
        self._recover_interrupted()
        self.cleanup()

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
            raise ValueError('O relógio da sessão deve usar UTC.')
        return moment

    def _lock(self, identifier: str) -> Lock:
        with self._guard:
            return self._locks.setdefault(identifier, Lock())

    def _directory(self, identifier: str) -> Path:
        if not _IDENTIFIER.fullmatch(identifier):
            raise SessionUnavailableError('Identidade de sessão inválida.')
        directory = self.root / identifier
        _safe_path(directory)
        return directory

    def _check_session(self, session: WebSession) -> None:
        expected = hashlib.sha256(session.token.encode('ascii')).hexdigest()
        if (
            expected != session.identifier
            or session.directory != self._directory(session.identifier)
        ):
            raise SessionUnavailableError('Referência de sessão inválida.')

    def _read(self, identifier: str) -> SessionMetadata:
        path = self._directory(identifier) / 'session.json'
        _safe_path(path)
        try:
            with self._guard, path.open('rb') as handle:
                raw = handle.read(_MAX_METADATA_BYTES + 1)
            if len(raw) > _MAX_METADATA_BYTES:
                raise ValueError('Metadados excedem o limite local.')
            return SessionMetadata.model_validate_json(raw)
        except (OSError, ValueError) as error:
            raise SessionUnavailableError(
                'Sessão incompatível ou indisponível; reinicie explicitamente.'
            ) from error

    def _write(self, identifier: str, metadata: SessionMetadata) -> None:
        raw = metadata.model_dump_json().encode('utf-8')
        if len(raw) > _MAX_METADATA_BYTES:
            raise ValueError('Metadados excedem o limite local.')
        SessionMetadata.model_validate_json(raw)
        directory = self._directory(identifier)
        target = directory / 'session.json'
        _safe_path(target)
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(dir=directory, delete=False) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            # Evita abertura concorrente durante a substituição no Windows.
            with self._guard:
                temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _expired(self, metadata: SessionMetadata) -> bool:
        return self._now() - metadata.last_activity >= self.retention

    @staticmethod
    def _running(metadata: SessionMetadata) -> bool:
        return bool(
            metadata.operation and metadata.operation.status == 'running'
        )

    def _remove(self, identifier: str) -> None:
        directory = self._directory(identifier)
        if not directory.exists():
            return
        self._remove_tree(directory)

    @staticmethod
    def _remove_tree(directory: Path) -> None:
        _safe_path(directory)
        for parent, directories, files in os.walk(directory):
            for name in (*directories, *files):
                _safe_path(Path(parent) / name)
        shutil.rmtree(directory)

    def _existing(self) -> Iterator[str]:
        for path in self.root.iterdir():
            if _IDENTIFIER.fullmatch(path.name):
                yield path.name

    def _recover_interrupted(self) -> None:
        # Somente na inicialização: polling nunca transforma uma execução
        # legítima em interrupção entre a reserva e o início do worker.
        for identifier in self._existing():
            try:
                metadata = self._read(identifier)
            except SessionUnavailableError:
                continue
            if metadata.operation and metadata.operation.status == 'running':
                metadata.operation.status = 'interrupted'
                metadata.operation.message = (
                    'A operação foi interrompida; execute-a novamente.'
                )
                workspace = self._directory(identifier) / 'workspace'
                if (
                    metadata.operation.kind == 'upload'
                    and not metadata.workspace_ready
                    and workspace.exists()
                ):
                    self._remove_tree(workspace)
                self._write(identifier, metadata)

    def _create(self) -> WebSession:
        token = secrets.token_urlsafe(32)
        identifier = hashlib.sha256(token.encode('ascii')).hexdigest()
        directory = self._directory(identifier)
        directory.mkdir(mode=0o700)
        now = self._now()
        metadata = SessionMetadata(
            csrf=secrets.token_urlsafe(32),
            created_at=now,
            last_activity=now,
        )
        self._write(identifier, metadata)
        return WebSession(identifier, token, metadata, directory)

    def open(self, token: str | None, *, touch: bool = True) -> WebSession:
        """Recupere pelo cookie opaco ou crie uma sessão nova."""
        if touch:
            self.cleanup()
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return self._create()
        identifier = hashlib.sha256(token.encode('ascii')).hexdigest()
        directory = self._directory(identifier)
        if not directory.exists():
            return self._create()
        lock = self._lock(identifier)
        acquired = lock.acquire(blocking=False)
        try:
            metadata = self._read(identifier)
            if self._expired(metadata):
                if not acquired or self._running(metadata):
                    raise SessionBusyError(
                        'A sessão expirou; aguarde a operação em andamento.'
                    )
                self._remove(identifier)
                return self._create()
            if acquired and touch:
                metadata.last_activity = max(
                    self._now(), metadata.last_activity
                )
                self._write(identifier, metadata)
            return WebSession(identifier, token, metadata, directory)
        finally:
            if acquired:
                lock.release()

    def read(self, session: WebSession) -> SessionMetadata:
        self._check_session(session)
        session.metadata = self._read(session.identifier)
        return session.metadata

    @contextmanager
    def lease(
        self, session: WebSession, *, wait: bool = False
    ) -> Iterator[WebSession]:
        """O worker reservado pode aguardar; as requisições não bloqueiam."""
        self._check_session(session)
        lock = self._lock(session.identifier)
        if not lock.acquire(blocking=wait):
            raise SessionBusyError(
                'Aguarde a operação em andamento na sessão.'
            )
        self._held[session.identifier] = get_ident()
        try:
            self.read(session)
            if self._expired(session.metadata) and not self._running(
                session.metadata
            ):
                raise SessionUnavailableError(
                    'A sessão expirou por inatividade.'
                )
            yield session
        finally:
            self._held.pop(session.identifier, None)
            lock.release()

    def save(self, session: WebSession) -> None:
        self._check_session(session)
        if self._held.get(session.identifier) != get_ident():
            raise RuntimeError('A gravação da sessão exige lease exclusivo.')
        session.metadata.last_activity = max(
            self._now(), session.metadata.last_activity
        )
        self._write(session.identifier, session.metadata)

    def report(self, session: WebSession, run_id: str) -> ReportPackage:
        validate_run_id(run_id)
        metadata = self.read(session)
        if self._expired(metadata) or run_id not in metadata.run_ids:
            raise SessionUnavailableError(
                'Execução não pertence à sessão ativa.'
            )
        _safe_path(session.workspace_dir)
        return load_report(session.workspace_dir, run_id)

    def delete(self, session: WebSession) -> bool:
        self._check_session(session)
        lock = self._lock(session.identifier)
        if not lock.acquire(blocking=False):
            raise SessionBusyError(
                'Aguarde a operação antes de limpar a sessão.'
            )
        try:
            existed = self._directory(session.identifier).exists()
            if existed and self._running(self._read(session.identifier)):
                raise SessionBusyError(
                    'Aguarde a operação antes de limpar a sessão.'
                )
            self._remove(session.identifier)
            return existed
        finally:
            lock.release()

    def cleanup(self) -> int:
        removed = 0
        for identifier in self._existing():
            lock = self._lock(identifier)
            if not lock.acquire(blocking=False):
                continue
            try:
                metadata = self._read(identifier)
                if self._expired(metadata) and not self._running(metadata):
                    self._remove(identifier)
                    removed += 1
            except SessionUnavailableError:
                # Arquivos incompatíveis não autorizam apagar dados locais.
                continue
            finally:
                lock.release()
        return removed
