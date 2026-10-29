"""Interface fiscal local com sessões privadas e operações limitadas."""

import re
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from http import HTTPStatus
from pathlib import Path, PureWindowsPath
from threading import BoundedSemaphore
from typing import BinaryIO, Literal
from uuid import uuid4

from anyio import CancelScope
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agente_sped.application.ingestion import ingest_file
from agente_sped.application.parsing import DEFAULT_MAX_FILE_BYTES
from agente_sped.application.sessions import (
    Operation,
    SessionBusyError,
    SessionUnavailableError,
    WebSession,
    WorkspaceManager,
)
from agente_sped.domain.routing import AnalysisRequest
from agente_sped.infra.analysis_catalog import load_analysis_catalog
from agente_sped.infra.local_lock import SessionRootLock
from agente_sped.infra.report_store import load_report
from agente_sped.infra.settings import Settings
from agente_sped.interface.views import (
    EXPORT_NAME,
    display_value,
    workspace_context,
)
from agente_sped.orchestration.graph import run_agent, run_export

SESSION_COOKIE = 'agente_sped_session'
_ASSETS = Path(__file__).parent
_REQUEST_ID = re.compile(r'[0-9a-f]{32}')
_BODY_OVERHEAD = 64 * 1024
_MAX_FILENAME_LENGTH = 128
_MAX_PARAMETER_LENGTH = 80


class WebSettings(Settings):
    session_root: Path = Field(
        default_factory=lambda: Path.home() / '.agente-sped' / 'sessions',
        validation_alias='SPED_SESSION_ROOT',
    )
    retention_hours: int = Field(
        default=24, ge=1, le=720, validation_alias='SPED_RETENTION_HOURS'
    )


class OperationLimitError(ValueError):
    """O processo local já está usando sua capacidade de execução."""


class SessionDownload(FileResponse):
    """Mantenha o workspace em uso até terminar ou interromper o download."""

    def __init__(
        self, path: Path, manager: WorkspaceManager, session: WebSession
    ):
        super().__init__(path, filename='agente-sped-' + path.name)
        self._lease = manager.lease(session)

    async def __call__(self, scope, receive, send):
        await run_in_threadpool(self._lease.__enter__)
        try:
            await super().__call__(scope, receive, send)
        finally:
            with CancelScope(shield=True):
                await run_in_threadpool(self._lease.__exit__, None, None, None)


class LocalOperations:
    """Duas operações em execução, sem fila ou retomada automática."""

    def __init__(self, manager: WorkspaceManager) -> None:
        self.manager = manager
        self._capacity = BoundedSemaphore(2)
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix='sped'
        )

    def submit(
        self,
        session: WebSession,
        request_id: str,
        kind: Literal['upload', 'analysis', 'ask', 'export'],
        work: Callable[[WebSession], None],
        prepare: Callable[[WebSession], None] | None = None,
    ) -> None:
        if not self._capacity.acquire(blocking=False):
            raise OperationLimitError(
                'O ambiente está ocupado. Aguarde e consulte o estado '
                'antes de tentar novamente.'
            )
        try:
            with self.manager.lease(session):
                previous = session.metadata.operation
                if previous and previous.id == request_id:
                    self._capacity.release()
                    return
                if previous and previous.status == 'running':
                    raise SessionBusyError(
                        'Aguarde a operação em andamento; '
                        'não é necessário reenviar.'
                    )
                if prepare:
                    prepare(session)
                session.metadata.operation = Operation(
                    id=request_id, kind=kind, status='running'
                )
                self.manager.save(session)
            self._executor.submit(self._execute, session, work)
        except BaseException:
            self._capacity.release()
            raise

    def _execute(
        self, session: WebSession, work: Callable[[WebSession], None]
    ) -> None:
        # Polling and rendering never replace the worker's mutable metadata.
        session = WebSession(
            session.identifier,
            session.token,
            session.metadata.model_copy(deep=True),
            session.directory,
        )
        try:
            with self.manager.lease(session, wait=True):
                operation = session.metadata.operation
                assert operation is not None
                try:
                    work(session)
                    operation.status = 'completed'
                except Exception as error:
                    operation.status = 'failed'
                    operation.message = _operation_error_message(error)
                finally:
                    if operation.kind == 'upload':
                        session.source_path.unlink(missing_ok=True)
                    self.manager.save(session)
        finally:
            self._capacity.release()

    def close(self) -> None:
        self._executor.shutdown(wait=True)


def _operation_error_message(error: Exception) -> str:
    # Somente códigos conhecidos recebem texto público; nunca expor a exceção.
    if type(error) is ValueError:
        if str(error) in {
            'unsupported_layout_version',
            'unsupported_obligation',
        }:
            return (
                'Arquivo não suportado. Envie EFD ICMS/IPI com COD_VER=020. '
                'EFD-Contribuições e leiautes antigos não são aceitos.'
            )
        if str(error) == 'invalid_file_header':
            return (
                'Cabeçalho do TXT inválido ou incompatível. Envie EFD '
                'ICMS/IPI com COD_VER=020 e registro 0000 válido.'
            )
    return (
        'Não foi possível concluir a operação. Confira o arquivo e os '
        'parâmetros; resultados anteriores permanecem disponíveis.'
    )


def _copy_upload(session: WebSession, source: BinaryIO, filename: str) -> None:
    if session.metadata.workspace_ready or session.workspace_dir.exists():
        raise ValueError(
            'Limpe a sessão antes de carregar outra escrituração.'
        )
    name = PureWindowsPath(filename).name
    if (
        not name.casefold().endswith('.txt')
        or len(name) > _MAX_FILENAME_LENGTH
    ):
        raise ValueError('Selecione um arquivo TXT EFD ICMS/IPI.')
    if session.source_path.exists() or session.source_path.is_symlink():
        raise ValueError(
            'Já existe um upload nesta sessão; limpe a sessão para reiniciar.'
        )
    total = 0
    try:
        with session.source_path.open('xb') as target:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > DEFAULT_MAX_FILE_BYTES:
                    raise HTTPException(
                        413, 'O TXT excede o limite de 100 MiB.'
                    )
                target.write(chunk)
        if not total:
            raise ValueError('O arquivo TXT está vazio.')
        session.metadata.source_name = name
    except BaseException:
        session.source_path.unlink(missing_ok=True)
        raise


def _parameters(form: FormData, analysis_id: str) -> dict[str, str | int]:
    catalog = load_analysis_catalog()
    definition = catalog.analyses.get(analysis_id)
    if definition is None:
        raise ValueError('Escolha uma análise do catálogo.')
    parameters = {}
    controls = {'csrf', 'request_id', 'analysis_id'}
    if set(form) - controls - definition.parameters.keys():
        raise ValueError('A análise não aceita os parâmetros informados.')
    for name, definition in definition.parameters.items():
        value = form.get(name)
        if not value:
            continue
        if not isinstance(value, str) or len(value) > _MAX_PARAMETER_LENGTH:
            raise ValueError('Parâmetro inválido.')
        parameters[name] = (
            int(value) if definition.type == 'integer' else value
        )
    return parameters


def create_app(settings: WebSettings | None = None) -> FastAPI:  # noqa: PLR0915
    settings = settings if settings is not None else WebSettings()
    catalog = load_analysis_catalog()
    owner = SessionRootLock(settings.session_root)
    try:
        manager = WorkspaceManager(
            settings.session_root, retention_hours=settings.retention_hours
        )
    except BaseException:
        owner.close()
        raise
    jobs = LocalOperations(manager)
    templates = Jinja2Templates(directory=str(_ASSETS / 'templates'))
    templates.env.filters['value'] = display_value

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await run_in_threadpool(jobs.close)
            owner.close()

    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        telemetry={
            'tracing': False,
            'metrics': False,
            'logs': False,
            'operation_spans': False,
            'auto_configure': False,
        },
    )
    app.state.manager = manager
    app.state.operations = jobs
    app.state.owner = owner
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_size=DEFAULT_MAX_FILE_BYTES + _BODY_OVERHEAD,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=['127.0.0.1', 'localhost', '[::1]'],
        www_redirect=False,
    )
    app.mount(
        '/static', StaticFiles(directory=_ASSETS / 'static'), name='static'
    )

    @app.middleware('http')
    async def local_requests(request: Request, call_next):
        origin = request.headers.get('origin')
        expected = f'{request.url.scheme}://{request.headers.get("host", "")}'
        if request.method not in ('GET', 'HEAD') and origin != expected:
            response = HTMLResponse(
                '<div role="alert">Origem da solicitação recusada.</div>',
                status_code=403,
            )
        else:
            response = await call_next(request)
            if response.status_code == HTTPStatus.CONTENT_TOO_LARGE:
                response = await controlled_error(
                    request,
                    HTTPException(
                        HTTPStatus.CONTENT_TOO_LARGE,
                        'O arquivo excede o limite de upload. '
                        'Envie um TXT de até 100 MiB.',
                    ),
                )
        response.headers.update(
            {
                'Cache-Control': 'no-store',
                'Vary': 'HX-Request',
                'Content-Security-Policy': (
                    "default-src 'self'; script-src 'self'; "
                    "style-src 'self'; connect-src 'self'; img-src 'self'; "
                    "object-src 'none'; base-uri 'none'; "
                    "frame-ancestors 'none'; form-action 'self'"
                ),
                'Referrer-Policy': 'no-referrer',
                'X-Content-Type-Options': 'nosniff',
            }
        )
        return response

    async def session_for(
        request: Request, *, touch: bool = True
    ) -> WebSession:
        return await run_in_threadpool(
            partial(
                manager.open, request.cookies.get(SESSION_COOKIE), touch=touch
            )
        )

    def render(
        request: Request,
        session: WebSession,
        *,
        status_code: int = 200,
        message: str | None = None,
        **options,
    ) -> HTMLResponse:
        context = workspace_context(manager, session, catalog, **options)
        context.update(
            request_id=uuid4().hex, ai_enabled=settings.ai_routing_enabled
        )
        if message:
            context.update(
                message=message,
                level='error'
                if status_code >= HTTPStatus.BAD_REQUEST
                else 'info',
            )
        name = (
            'workspace.html'
            if request.headers.get('hx-request') == 'true'
            else 'layout.html'
        )
        response = templates.TemplateResponse(
            request=request,
            name=name,
            context=context,
            status_code=status_code,
        )
        seconds = max(
            0,
            int(
                (
                    session.metadata.last_activity - datetime.now(UTC)
                ).total_seconds()
                + settings.retention_hours * 3600
            ),
        )
        response.set_cookie(
            SESSION_COOKIE,
            session.token,
            max_age=seconds,
            httponly=True,
            samesite='strict',
        )
        return response

    async def action_session(
        request: Request, form: FormData
    ) -> tuple[WebSession, str]:
        session = await session_for(request)
        csrf = form.get('csrf')
        if not isinstance(csrf, str) or not secrets.compare_digest(
            csrf, session.csrf
        ):
            raise HTTPException(
                403,
                'A sessão expirou ou a solicitação não foi autorizada. '
                'Reabra a página.',
            )
        request_id = form.get('request_id')
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(
            request_id
        ):
            raise HTTPException(
                400, 'Solicitação inválida. Atualize a página.'
            )
        return session, request_id

    async def post_form(request: Request) -> FormData:
        form = await request.form(
            max_files=0, max_fields=12, max_part_size=4096
        )
        if len(form) != len(form.multi_items()):
            raise HTTPException(400, 'Campos duplicados não são aceitos.')
        return form

    @app.exception_handler(StarletteHTTPException)
    async def controlled_error(
        request: Request, error: StarletteHTTPException
    ):
        try:
            session = await session_for(request, touch=False)
            return await run_in_threadpool(
                partial(
                    render,
                    request,
                    session,
                    status_code=error.status_code,
                    message=str(error.detail),
                )
            )
        except SessionUnavailableError:
            session = await run_in_threadpool(manager.open, None)
            return await run_in_threadpool(
                partial(
                    render,
                    request,
                    session,
                    status_code=409,
                    message=(
                        'A sessão anterior está incompatível ou '
                        'indisponível. Seus arquivos foram preservados; '
                        'carregue o TXT para iniciar outra sessão.'
                    ),
                )
            )
        except SessionBusyError:
            return HTMLResponse(
                '<p role="status">Aguarde a operação em andamento '
                'e reabra a página.</p>',
                status_code=409,
            )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        request: Request, _error: RequestValidationError
    ):
        return await controlled_error(
            request, HTTPException(422, 'Revise os parâmetros da solicitação.')
        )

    @app.exception_handler(ValueError)
    async def invalid_operation(request: Request, error: ValueError):
        code = (
            HTTPStatus.CONFLICT
            if isinstance(error, (SessionBusyError, OperationLimitError))
            else HTTPStatus.BAD_REQUEST
        )
        message = (
            'Aguarde a operação em andamento e consulte o estado '
            'antes de reenviar.'
            if code == HTTPStatus.CONFLICT
            else 'Não foi possível atender a solicitação. Confira os '
            'parâmetros e a sessão.'
        )
        return await controlled_error(request, HTTPException(code, message))

    @app.get('/', response_class=HTMLResponse)
    async def home(request: Request):
        await run_in_threadpool(manager.cleanup)
        session = await session_for(request)
        return await run_in_threadpool(render, request, session)

    @app.get('/status', response_class=HTMLResponse)
    async def status(
        request: Request, run_id: str | None = None, page: int = 1
    ):
        session = await session_for(request, touch=False)
        return await run_in_threadpool(
            partial(render, request, session, run_id=run_id, page=page)
        )

    @app.post('/upload', response_class=HTMLResponse)
    async def upload(request: Request):
        async with request.form(
            max_files=1, max_fields=2, max_part_size=512
        ) as form:
            if len(form) != len(form.multi_items()):
                raise HTTPException(400, 'Campos duplicados não são aceitos.')
            session, request_id = await action_session(request, form)
            source = form.get('file')
            if not isinstance(source, UploadFile):
                raise HTTPException(400, 'Selecione um arquivo TXT.')

            def ingest(current: WebSession) -> None:
                ingest_file(current.source_path, current.workspace_dir)
                current.metadata.workspace_ready = True
                current.metadata.operation.message = (
                    'Escrituração carregada. Selecione uma análise '
                    'ou faça uma pergunta.'
                )

            await run_in_threadpool(
                jobs.submit,
                session,
                request_id,
                'upload',
                ingest,
                partial(
                    _copy_upload,
                    source=source.file,
                    filename=source.filename or '',
                ),
            )
        return await run_in_threadpool(
            partial(render, request, session, status_code=202)
        )

    async def analyze_request(request: Request, *, ask: bool):
        form = await post_form(request)
        session, request_id = await action_session(request, form)
        if not session.metadata.workspace_ready:
            raise HTTPException(
                409, 'Carregue uma escrituração antes de analisar.'
            )
        if ask:
            if set(form) - {'csrf', 'request_id', 'question'}:
                raise HTTPException(
                    400, 'Envie a pergunta sem seleção de análise.'
                )
            command = AnalysisRequest(question=form.get('question'))
        else:
            analysis_id = str(form.get('analysis_id', ''))
            command = AnalysisRequest(
                analysis_id=analysis_id,
                parameters=_parameters(form, analysis_id),
            )

        def execute(current: WebSession) -> None:
            outcome = run_agent(
                current.workspace_dir, command, settings=settings
            )
            operation = current.metadata.operation
            route_label = {
                'manual': 'Seleção direta. ',
                'deterministic': 'Roteamento sem IA. ',
                'provider': 'Roteamento por IA. ',
            }.get(outcome.state.get('route'), '')
            operation.message = (
                route_label + (outcome.message or 'Resultado disponível.')
            )[:500]
            operation.outcome_status = outcome.state['status']
            operation.options = [
                option.analysis_id for option in outcome.options
            ]
            if outcome.state['status'] in ('completed', 'empty_result') and (
                identity := outcome.state.get('report_id')
            ):
                load_report(current.workspace_dir, identity)
                current.metadata.run_ids.append(identity)

        await run_in_threadpool(
            jobs.submit,
            session,
            request_id,
            'ask' if ask else 'analysis',
            execute,
        )
        return await run_in_threadpool(
            partial(render, request, session, status_code=202)
        )

    @app.post('/analyze', response_class=HTMLResponse)
    async def analyze(request: Request):
        return await analyze_request(request, ask=False)

    @app.post('/ask', response_class=HTMLResponse)
    async def ask(request: Request):
        return await analyze_request(request, ask=True)

    @app.get('/runs/{run_id}', response_class=HTMLResponse)
    async def saved_report(request: Request, run_id: str, page: int = 1):
        session = await session_for(request)
        return await run_in_threadpool(
            partial(render, request, session, run_id=run_id, page=page)
        )

    @app.post('/runs/{run_id}/reveal', response_class=HTMLResponse)
    async def reveal(request: Request, run_id: str):
        form = await post_form(request)
        session, _ = await action_session(request, form)
        return await run_in_threadpool(
            partial(
                render,
                request,
                session,
                run_id=run_id,
                revealed=form.get('show_sensitive') == 'true',
            )
        )

    @app.post(
        '/runs/{run_id}/evidence/{record_id}', response_class=HTMLResponse
    )
    async def evidence(request: Request, run_id: str, record_id: int):
        form = await post_form(request)
        session, _ = await action_session(request, form)
        return await run_in_threadpool(
            partial(
                render, request, session, run_id=run_id, evidence_id=record_id
            )
        )

    @app.post('/runs/{run_id}/exports', response_class=HTMLResponse)
    async def exports(request: Request, run_id: str):
        form = await post_form(request)
        session, request_id = await action_session(request, form)
        await run_in_threadpool(manager.report, session, run_id)
        format = form.get('format')
        if format not in ('csv', 'xlsx'):
            raise HTTPException(400, 'Escolha CSV ou Excel.')

        def export(current: WebSession) -> None:
            outcome = run_export(
                current.workspace_dir,
                run_id,
                format,
                show_sensitive=form.get('show_sensitive') == 'true',
            )
            if outcome.state['status'] != 'exported':
                raise ValueError('Exportação indisponível.')
            current.metadata.operation.message = (
                'Exportação pronta para download.'
            )

        await run_in_threadpool(
            jobs.submit, session, request_id, 'export', export
        )
        return await run_in_threadpool(
            partial(render, request, session, run_id=run_id, status_code=202)
        )

    @app.get('/downloads/{filename}')
    async def download(request: Request, filename: str):
        session = await session_for(request)
        match = EXPORT_NAME.fullmatch(filename)
        if not match or match[1] not in session.metadata.run_ids:
            raise HTTPException(404, 'Arquivo não disponível nesta sessão.')
        path = session.workspace_dir / 'exports' / filename
        if (
            any(
                p.is_symlink() or p.is_junction()
                for p in (path, path.parent, session.workspace_dir)
            )
            or not path.is_file()
        ):
            raise HTTPException(404, 'Arquivo não disponível nesta sessão.')
        return SessionDownload(path, manager, session)

    @app.post('/session/clear', response_class=HTMLResponse)
    async def clear(request: Request):
        form = await post_form(request)
        session, _ = await action_session(request, form)
        await run_in_threadpool(manager.delete, session)
        replacement = await run_in_threadpool(manager.open, None)
        return await run_in_threadpool(
            partial(
                render,
                request,
                replacement,
                message='Sessão limpa. Você pode carregar outra escrituração.',
            )
        )

    return app
