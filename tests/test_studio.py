"""Optional demo checks; the default suite does not require Studio packages."""

import asyncio
import importlib.util
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip('langgraph_api', reason='Grupo opcional studio.')
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from agente_sped.application.ingestion import (  # noqa: E402
    analyze_catalogued_file,
)
from agente_sped.infra.analysis_catalog import (
    load_analysis_catalog,  # noqa: E402
)
from agente_sped.infra.report_store import load_report  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    'studio_demo', _ROOT / 'devtools/studio.py'
)
assert _SPEC is not None
assert _SPEC.loader is not None
studio = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(studio)


@pytest.fixture
def demo_client():
    async def accepted(request):
        return JSONResponse({'accepted': True})

    app = Starlette(
        routes=[Route('/{path:path}', accepted, methods=['POST'])],
        middleware=[Middleware(studio.DemoRequests)],
    )
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    'change',
    [
        {'input': None},
        {'command': {'resume': 'continue'}},
        {'checkpoint_id': 'previous'},
        {'interrupt_after': ['VALIDATE_CONTRACT']},
        {'langsmith_tracing': True},
        {'webhook': 'https://example.com'},
        {'config': {'configurable': {'checkpoint_id': 'previous'}}},
    ],
)
def test_unsupported_run_never_reaches_server(demo_client, change):
    response = demo_client.post(
        '/threads/new/runs/stream',
        json={'input': {'action': 'analysis', 'status': 'pending'}, **change},
    )
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_only_new_threads_and_no_state_edits(demo_client):
    body = {'input': {'action': 'analysis', 'status': 'pending'}}
    path = '/threads/first/runs/stream'
    studio_body = {**body, 'config': {'configurable': {'thread_id': 'first'}}}
    assert (
        demo_client.post(path, json=studio_body).status_code == HTTPStatus.OK
    )
    assert (
        demo_client.post(path, json=body).status_code == HTTPStatus.FORBIDDEN
    )
    assert (
        demo_client.post('/threads/second/runs/stream', json=body).status_code
        == HTTPStatus.OK
    )
    assert (
        demo_client.post(
            '/threads/third/runs/stream', json=studio_body
        ).status_code
        == HTTPStatus.FORBIDDEN
    )
    for path in ('/threads/first/state', '/threads/first/copy', '/runs/batch'):
        assert (
            demo_client.post(path, json={}).status_code == HTTPStatus.FORBIDDEN
        )
    imported = demo_client.post(
        '/threads', json={'supersteps': [{'values': {}}]}
    )
    assert imported.status_code == HTTPStatus.FORBIDDEN


@pytest.fixture(scope='module')
def demo_workspace(tmp_path_factory):
    root = tmp_path_factory.mktemp('studio')
    analyze_catalogued_file(
        _ROOT / 'tests/fixtures/sped_icms_analysis_reference.txt',
        root / 'workspace',
        'movement_by_cfop',
    )
    return root


def test_real_graph_scenarios_and_p1_reference(demo_workspace, monkeypatch):
    monkeypatch.setenv('SPED_STUDIO_ROOT', str(demo_workspace))
    runtime = SimpleNamespace(access_context='threads.create_run')

    async def run(factory):
        async with factory(runtime) as graph:
            return await graph.ainvoke(
                {'action': 'analysis', 'status': 'pending'},
                config={'callbacks': [], 'recursion_limit': 12},
            )

    completed = asyncio.run(run(studio.p1))
    report = load_report(demo_workspace / 'workspace', completed['run_id'])
    expected = load_analysis_catalog().reference_validation['expected'][
        'movement_by_cfop'
    ]
    assert completed['status'] == 'completed'
    assert report.result.row_count == expected['row_count']
    assert [
        (row['cfop'], row['total_operacao']) for row in report.result.rows
    ] == [(row['cfop'], row['total']) for row in expected['top_cfops']]
    clarified = asyncio.run(run(studio.clarification))
    failed = asyncio.run(run(studio.provider_failure))
    assert clarified['status'] == 'needs_clarification'
    assert failed['status'] == 'provider_failure'
    assert 'run_id' not in clarified
    assert 'run_id' not in failed


def test_network_guard_rejects_remote_connections():
    with pytest.raises(PermissionError, match='conexões locais'):
        studio._local_only('socket.connect', (None, ('8.8.8.8', 443)))
    studio._local_only('socket.connect', (None, ('127.0.0.1', 2024)))


def test_launcher_disables_inherited_credentials_and_telemetry(monkeypatch):
    environment = {
        'GEMINI_API_KEY': 'synthetic',
        'LANGSMITH_API_KEY': 'synthetic',
        'LANGSMITH_TRACING': 'true',
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'https://example.com',
    }
    monkeypatch.setattr(studio.os, 'environ', environment)
    studio._private_environment()
    assert 'GEMINI_API_KEY' not in environment
    assert 'LANGSMITH_API_KEY' not in environment
    assert 'OTEL_EXPORTER_OTLP_ENDPOINT' not in environment
    assert environment['LANGSMITH_TRACING'] == 'false'
    assert environment['LANGGRAPH_CLI_NO_ANALYTICS'] == '1'
