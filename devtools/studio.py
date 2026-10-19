"""Optional Studio demo using only the synthetic fixture and real graph."""

import json
import os
import re
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from langgraph.graph.state import CompiledStateGraph
from langgraph_sdk.runtime import ServerRuntime
from langsmith import tracing_context
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from agente_sped.application.ingestion import analyze_catalogued_file
from agente_sped.domain.routing import (
    AnalysisRequest,
    ProviderFailure,
    RoutingDecision,
)
from agente_sped.infra.analysis_catalog import load_analysis_catalog
from agente_sped.infra.gemini import ensure_private_runtime
from agente_sped.orchestration.graph import _build_graph, _Execution

ROOT = Path(__file__).resolve().parents[1]
_CATALOG = load_analysis_catalog()
_RUN_PATH = re.compile(r'/threads/([^/]+)/runs/stream|/runs/stream')
_INITIAL = {'action': 'analysis', 'status': 'pending'}
_MESSAGE = (
    'Use uma thread nova e a entrada {"action":"analysis","status":"pending"}.'
)


class DemoRequests(BaseHTTPMiddleware):
    """Permit fresh runs; deny state editing, replay and other writes."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self.used_threads: set[str] = set()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method in {'GET', 'OPTIONS'}:
            return await call_next(request)
        path = request.url.path
        allowed_read = path.endswith(('/search', '/count', '/history'))
        if request.method == 'POST' and allowed_read:
            return await call_next(request)
        if request.method != 'POST':
            return JSONResponse({'detail': _MESSAGE}, status_code=403)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
            if path == '/threads':
                if not set(body) <= {'thread_id', 'metadata', 'if_exists'}:
                    raise ValueError
                return await call_next(request)
            match = _RUN_PATH.fullmatch(path)
            if match is None or not _new_run(body):
                raise ValueError
            if thread_id := match.group(1):
                if thread_id in self.used_threads:
                    raise ValueError
                # No await between checking and reserving this thread.
                self.used_threads.add(thread_id)
        except ValueError, TypeError:
            return JSONResponse({'detail': _MESSAGE}, status_code=403)
        return await call_next(request)


def _new_run(body: dict[str, object]) -> bool:
    config = body.get('config') or {}
    if not isinstance(config, dict):
        return False
    return (
        body.get('input') == _INITIAL
        and body.get('langsmith_tracing') in (None, False)
        and not any(
            body.get(key)
            for key in (
                'command',
                'checkpoint',
                'checkpoint_id',
                'interrupt_before',
                'interrupt_after',
                'webhook',
                'thread_id',
                'context',
            )
        )
        and not config.get('configurable')
        and set(config) <= {'configurable', 'tags', 'recursion_limit'}
    )


app = Starlette(middleware=[Middleware(DemoRequests)])


class FailedProvider:
    def route(
        self, question: str, catalog: list[dict[str, object]]
    ) -> RoutingDecision:
        raise ProviderFailure('Falha sintética do provedor.')


@asynccontextmanager
async def _graph(
    scenario: str, runtime: ServerRuntime
) -> AsyncIterator[CompiledStateGraph]:
    if runtime.access_context == 'threads.update':
        raise ValueError(_MESSAGE)
    requests = {
        'p1': AnalysisRequest(
            question='Qual foi o valor das saídas por CFOP?'
        ),
        'clarification': AnalysisRequest(question='Há crédito no CFOP 2556?'),
        'provider_failure': AnalysisRequest(
            question='Investigue a distribuição das saídas por CFOP com top 2'
        ),
    }
    session = os.environ.get('SPED_STUDIO_ROOT')
    if session is None:
        raise ValueError('Inicie com python devtools/studio.py.')
    workspace = Path(session) / 'workspace'
    execution = _Execution(
        workspace=workspace,
        catalog=_CATALOG,
        request=requests[scenario],
        provider=FailedProvider() if scenario == 'provider_failure' else None,
    )
    with tracing_context(enabled=False, parent=False):
        ensure_private_runtime()
        yield _build_graph(execution)


def p1(
    runtime: ServerRuntime,
) -> AbstractAsyncContextManager[CompiledStateGraph]:
    return _graph('p1', runtime)


def clarification(
    runtime: ServerRuntime,
) -> AbstractAsyncContextManager[CompiledStateGraph]:
    return _graph('clarification', runtime)


def provider_failure(
    runtime: ServerRuntime,
) -> AbstractAsyncContextManager[CompiledStateGraph]:
    return _graph('provider_failure', runtime)


def _private_environment() -> None:
    for name in list(os.environ):
        if name.startswith(
            (
                'LANGCHAIN_',
                'LANGSMITH_',
                'LANGGRAPH_',
                'GOOGLE_',
                'GEMINI_',
                'OTEL_',
                'DD_',
            )
        ):
            os.environ.pop(name, None)
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        os.environ.pop(name, None)
    os.environ.update(
        {
            'LANGSMITH_TRACING': 'false',
            'LANGCHAIN_TRACING_V2': 'false',
            'LANGGRAPH_CLI_NO_ANALYTICS': '1',
            'LANGGRAPH_NO_VERSION_CHECK': '1',
            'OTEL_ENABLED': 'false',
            'OTEL_SDK_DISABLED': 'true',
            'AI_ROUTING_ENABLED': 'false',
            'AI_NARRATIVE_ENABLED': 'false',
            'EXPLORATORY_SQL_ENABLED': 'false',
            'LOG_COLOR': 'false',
            'NO_PROXY': 'localhost,127.0.0.1,::1',
        }
    )


def _local_only(event: str, arguments: tuple) -> None:
    if event not in {'socket.connect', 'socket.getaddrinfo'}:
        return
    host = arguments[1][0] if event == 'socket.connect' else arguments[0]
    if host not in {
        'localhost',
        '127.0.0.1',
        '::1',
        b'localhost',
        b'127.0.0.1',
        b'::1',
    }:
        raise PermissionError(
            'A demonstração permite somente conexões locais.'
        )


def main() -> None:
    _private_environment()
    sys.addaudithook(_local_only)
    from langgraph_cli.cli import cli  # noqa: PLC0415

    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', 2024))
    previous = Path.cwd()
    with TemporaryDirectory(prefix='agente-sped-studio-') as temporary:
        session = Path(temporary)
        analyze_catalogued_file(
            ROOT / 'tests/fixtures/sped_icms_analysis_reference.txt',
            session / 'workspace',
            'movement_by_cfop',
        )
        config = json.loads((ROOT / 'langgraph.json').read_text('utf-8'))
        config['dependencies'] = [str(ROOT)]
        config['graphs'] = {
            name: (ROOT / path.removeprefix('./')).as_posix()
            for name, path in config['graphs'].items()
        }
        config['http']['app'] = (
            ROOT / config['http']['app'].removeprefix('./')
        ).as_posix()
        config_path = session / 'langgraph.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        os.environ['SPED_STUDIO_ROOT'] = str(session)
        try:
            os.chdir(session)
            cli(
                args=[
                    'dev',
                    '--host',
                    '127.0.0.1',
                    '--port',
                    '2024',
                    '--no-browser',
                    '--no-reload',
                    '--config',
                    str(config_path),
                ]
            )
        finally:
            os.chdir(previous)
            os.environ.pop('SPED_STUDIO_ROOT', None)


if __name__ == '__main__':
    main()
