"""Routing decisions and one complete local workflow, without external I/O."""

import json
import socket
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from agente_sped.application.analysis import AnalysisTimeoutError
from agente_sped.application.ingestion import analyze_catalogued_file
from agente_sped.application.routing import resolve_route, sanitize_question
from agente_sped.domain.routing import (
    AnalysisRequest,
    ProviderFailure,
    RoutingDecision,
    RoutingParameters,
)
from agente_sped.infra.analysis_catalog import load_analysis_catalog
from agente_sped.infra.report_store import load_report
from agente_sped.infra.settings import Settings
from agente_sped.interface import cli
from agente_sped.orchestration import graph

_REFERENCE = (
    Path(__file__).parent / 'fixtures/sped_icms_analysis_reference.txt'
)


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    destination = tmp_path_factory.mktemp('orchestration') / 'workspace'
    analyze_catalogued_file(_REFERENCE, destination, 'movement_by_cfop')
    return destination


@pytest.mark.parametrize(
    ('question', 'expected', 'analysis_id'),
    [
        (
            'créditos em uso ou consumo',
            'matched',
            'possible_credit_on_consumption_cfop',
        ),
        ('itens com NCM genérico', 'matched', 'generic_ncm_high_value'),
        ('Há crédito no CFOP 2556?', 'needs_clarification', None),
        ('Mostre produtos com NCM 99999999.', 'needs_clarification', None),
        (
            'Mostre documentos cancelados extemporaneamente.',
            'needs_clarification',
            None,
        ),
        ('Top cinco CFOPs de saída', 'needs_clarification', None),
        (
            'Concentração acima de setenta por cento',
            'needs_clarification',
            None,
        ),
        ('Mostre canceladas e analise PIS', 'needs_clarification', None),
    ],
)
def test_route_preserves_scope(question, expected, analysis_id):
    resolution = resolve_route(
        AnalysisRequest(question=question), load_analysis_catalog()
    )
    assert resolution.decision.status == expected
    assert resolution.decision.analysis_id == analysis_id


def test_explicit_confirmation_and_provider_input_are_bounded():
    catalog = load_analysis_catalog()
    provider = Mock()
    explicit = resolve_route(
        AnalysisRequest(
            analysis_id='possible_credit_on_consumption_cfop',
            question='Há crédito no CFOP 2556?',
        ),
        catalog,
        provider=provider,
    )
    assert explicit.source == 'manual'
    provider.route.assert_not_called()
    text = sanitize_question(
        'ACME 12.ABC.345/0001-99 contato fiscal@empresa.com '
        '(11) 99999-1234 IE 123.456.789.112 '
        'top 5 entre 2026-01-01 e 2026-01-31',
        catalog,
    )
    for private in ('acme', 'abc', 'empresa', '99999', '1234', '456', '789'):
        assert private not in text
    assert 'top 5' in text
    assert '2026-01-01' in text
    incomplete = sanitize_question(
        'acima de 1234567 ou limite de 10.000,00', catalog
    )
    assert '123456' not in incomplete
    assert '10.000' not in incomplete


def test_graph_executes_once_and_exports_saved_report(
    workspace, monkeypatch, capsys
):
    settings = Settings(_env_file=None)
    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    network = Mock(side_effect=AssertionError('External I/O is forbidden.'))
    monkeypatch.setattr(socket.socket, 'connect', network)
    monkeypatch.setattr(cli, 'Settings', lambda: settings)
    execute = Mock(wraps=graph.execute_analysis)
    monkeypatch.setattr(graph, 'execute_analysis', execute)
    assert (
        cli.main(
            [
                'ask',
                'Qual foi o valor das saídas por CFOP?',
                '--workspace',
                str(workspace),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    state = payload['state']
    assert state['status'] == 'completed'
    assert state['route'] == 'deterministic'
    assert state['run_id'] == state['report_id']
    assert (
        not {'question', 'rows', 'report', 'api_key', 'database'}
        & state.keys()
    )
    report = load_report(workspace, state['run_id'])
    assert report.result.total_count == report.result.row_count
    assert report.result.rows[0]['cfop'] == '5102'
    assert '135015.00' in report.summary
    assert report.suggestions
    execute.assert_called_once()
    monkeypatch.setattr(
        graph, 'execute_analysis', Mock(side_effect=AssertionError)
    )
    monkeypatch.setattr(
        graph, 'load_analysis_catalog', Mock(side_effect=AssertionError)
    )
    outcome = graph.run_export(workspace, state['run_id'], 'xlsx')
    assert outcome.state['status'] == 'exported'
    assert outcome.export.data_path.is_file()
    network.assert_not_called()


def test_graph_validates_before_execution_and_keeps_failure_distinct(
    workspace, monkeypatch
):
    settings = Settings(_env_file=None, ai_routing_enabled=True)
    provider = Mock()
    provider.route.side_effect = ProviderFailure('sanitized')
    failed = graph.run_agent(
        workspace,
        AnalysisRequest(question='Avalie a escrituração'),
        settings=settings,
        provider=provider,
    )
    assert failed.state['status'] == 'provider_failure'
    assert failed.options
    invalid = graph.run_agent(
        workspace,
        AnalysisRequest(
            analysis_id='movement_by_cfop', parameters={'top_n': 0}
        ),
        settings=settings,
        provider=provider,
    )
    assert invalid.state['status'] == 'needs_clarification'
    assert 'run_id' not in invalid.state
    provider.route.assert_called_once()
    provider.route.reset_mock()
    provider.route.side_effect = None
    provider.route.return_value = RoutingDecision(
        status='matched',
        analysis_id='movement_by_cfop',
        reason='catalog_match',
        parameters=RoutingParameters(top_n=1),
    )
    assisted = graph.run_agent(
        workspace,
        AnalysisRequest(question='Investigue a distribuição'),
        settings=settings,
        provider=provider,
    )
    assert assisted.state['status'] == 'completed'
    assert assisted.state['route'] == 'provider'
    assert assisted.state['parameters']['top_n'] == 1
    selected = load_report(workspace, assisted.state['run_id']).result
    assert selected.row_count == selected.total_count == 1
    assert not selected.truncated
    manual = graph.run_agent(
        workspace,
        AnalysisRequest(analysis_id='movement_by_cfop'),
        settings=settings,
        provider=provider,
    )
    assert manual.state['status'] == 'completed'
    provider.route.assert_called_once()
    monkeypatch.setattr(
        graph,
        'get_workspace_profile',
        Mock(side_effect=AnalysisTimeoutError('deadline')),
    )
    timed_out = graph.run_agent(
        workspace,
        AnalysisRequest(analysis_id='movement_by_cfop'),
        settings=settings,
    )
    assert timed_out.state['status'] == 'execution_error'
    assert timed_out.state['error_code'] == 'inspection_timeout'


def test_invalid_provider_parameters_and_suggestion_failure(
    workspace, monkeypatch
):
    settings = Settings(_env_file=None, ai_routing_enabled=True)
    provider = Mock()
    provider.route.return_value = RoutingDecision(
        status='matched',
        analysis_id='movement_by_cfop',
        reason='catalog_match',
        parameters=RoutingParameters(top_n=0),
    )
    invalid = graph.run_agent(
        workspace,
        AnalysisRequest(question='Investigue a distribuição'),
        settings=settings,
        provider=provider,
    )
    assert invalid.state['status'] == 'needs_clarification'
    assert 'run_id' not in invalid.state
    monkeypatch.setattr(
        graph,
        'build_suggestions',
        Mock(side_effect=AnalysisTimeoutError('deadline')),
    )
    completed = graph.run_agent(
        workspace,
        AnalysisRequest(analysis_id='movement_by_cfop'),
        settings=settings,
    )
    assert completed.state['status'] == 'completed'
    assert 'temporariamente' in completed.message
    assert load_report(workspace, completed.state['run_id']).suggestions == ()


def test_empty_result_outside_catalog_and_courtesy(workspace, monkeypatch):
    settings = Settings(_env_file=None)
    original = graph.execute_analysis

    def empty(*args, **kwargs):
        result = original(*args, **kwargs)
        return replace(
            result,
            rows=(),
            row_count=0,
            total_count=0,
            truncated=False,
            status='empty_result',
        )

    monkeypatch.setattr(graph, 'execute_analysis', empty)
    outcome = graph.run_agent(
        workspace,
        AnalysisRequest(analysis_id='movement_by_cfop'),
        settings=settings,
    )
    assert outcome.state['status'] == 'empty_result'
    assert load_report(workspace, outcome.state['run_id']).result.rows == ()
    for request, expected in (
        (AnalysisRequest(question='Obrigado!'), 'no_action'),
        (AnalysisRequest(analysis_id='arbitrary_sql'), 'out_of_catalog'),
    ):
        outcome = graph.run_agent(workspace, request, settings=settings)
        assert outcome.state['status'] == expected
        assert 'run_id' not in outcome.state
