"""Small synchronous graph with local results outside the graph state."""

from contextvars import Context
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from agente_sped.application.analysis import (
    AnalysisError,
    AnalysisResult,
    AnalysisTimeoutError,
    AnalysisUnavailableError,
    execute_analysis,
    get_workspace_profile,
    inspect_analysis_availability,
    validate_analysis_parameters,
    validate_execution_limits,
)
from agente_sped.application.reports import build_report, build_suggestions
from agente_sped.application.routing import (
    RouteResolution,
    RoutingProvider,
    is_simple_action,
    merged_parameters,
    resolve_route,
)
from agente_sped.domain.reports import ExportReceipt, ReportPackage
from agente_sped.domain.routing import AnalysisRequest, ProviderFailure
from agente_sped.infra.analysis_catalog import (
    AnalysisCatalog,
    CatalogError,
    load_analysis_catalog,
)
from agente_sped.infra.exports import export_report
from agente_sped.infra.gemini import (
    GeminiRoutingProvider,
    ensure_private_runtime,
)
from agente_sped.infra.report_store import save_report
from agente_sped.infra.settings import Settings

type RunStatus = Literal[
    'pending',
    'routed',
    'validated',
    'executed',
    'reported',
    'completed',
    'empty_result',
    'out_of_catalog',
    'needs_clarification',
    'analysis_unavailable',
    'execution_error',
    'provider_failure',
    'no_action',
    'exported',
]


class _InvalidParameters(ValueError):
    """Distinguish request validation from technical inspection errors."""


class AgentState(TypedDict, total=False):
    request_id: str
    workspace_id: str
    action: Literal['analysis', 'export']
    analysis_id: str
    parameters: dict[str, str | int]
    route: Literal['manual', 'deterministic', 'provider', 'none']
    status: RunStatus
    run_id: str
    report_id: str
    export_id: str
    error_code: str


@dataclass(frozen=True)
class AnalysisOption:
    analysis_id: str
    title: str
    criteria: tuple[str, ...]
    available: bool
    message: str | None


@dataclass(frozen=True)
class WorkflowOutcome:
    state: AgentState
    message: str
    options: tuple[AnalysisOption, ...] = ()
    export: ExportReceipt | None = None


@dataclass
class _Execution:
    workspace: Path
    catalog: AnalysisCatalog | None
    request: AnalysisRequest | None = None
    provider: RoutingProvider | None = None
    row_limit: int | None = None
    timeout_seconds: float | None = None
    resolution: RouteResolution | None = None
    effective: dict[str, object] = field(default_factory=dict)
    result: AnalysisResult | None = None
    report: ReportPackage | None = None
    message: str = ''
    options: tuple[AnalysisOption, ...] = ()
    export_format: Literal['csv', 'xlsx'] = 'csv'
    show_sensitive: bool = False
    receipt: ExportReceipt | None = None

    @property
    def database(self) -> Path:
        return self.workspace / 'analysis.duckdb'

    @property
    def analysis_catalog(self) -> AnalysisCatalog:
        assert self.catalog is not None
        return self.catalog

    def offer_options(self, analysis_ids: tuple[str, ...]) -> None:
        options = []
        for analysis_id in analysis_ids:
            definition = self.analysis_catalog.analyses[analysis_id]
            try:
                availability = inspect_analysis_availability(
                    self.database, analysis_id, catalog=self.analysis_catalog
                )
                available = availability.available
            except AnalysisError, CatalogError:
                available = False
            options.append(
                AnalysisOption(
                    analysis_id,
                    definition.title,
                    tuple(definition.criteria),
                    available,
                    None if available else 'Indisponível neste workspace.',
                )
            )
        self.options = tuple(options)


def _build_graph(execution: _Execution):  # noqa: PLR0915
    def route_intent(state: AgentState) -> AgentState:
        request = execution.request
        assert request is not None
        if request.analysis_id is None and is_simple_action(request.question):
            execution.message = 'Nenhuma análise foi solicitada.'
            return {'status': 'no_action', 'route': 'none'}
        try:
            resolution = resolve_route(
                request,
                execution.analysis_catalog,
                provider=execution.provider,
            )
        except ProviderFailure:
            execution.message = (
                'O provedor de roteamento não está disponível. '
                'Selecione uma análise para continuar sem IA.'
            )
            execution.offer_options(execution.analysis_catalog.analysis_ids)
            return {
                'status': 'provider_failure',
                'route': 'provider',
                'error_code': 'routing_provider_failed',
            }
        execution.resolution = resolution
        execution.message = resolution.message
        decision = resolution.decision
        if decision.status != 'matched':
            execution.offer_options(resolution.options)
            return {'status': decision.status, 'route': resolution.source}
        return {
            'analysis_id': decision.analysis_id,
            'status': 'routed',
            'route': resolution.source,
        }

    def validate_contract(state: AgentState) -> AgentState:
        try:
            profile = get_workspace_profile(execution.database)
            definition = execution.analysis_catalog.analyses[
                state['analysis_id']
            ]
            assert execution.resolution is not None
            assert execution.request is not None
            try:
                validate_execution_limits(
                    execution.analysis_catalog,
                    definition,
                    execution.row_limit,
                    execution.timeout_seconds,
                )
                execution.effective = validate_analysis_parameters(
                    definition,
                    merged_parameters(
                        execution.request, execution.resolution.decision
                    ),
                    period_start=profile.period_start,
                    period_end=profile.period_end,
                )
            except AnalysisError as error:
                raise _InvalidParameters from error
            available = inspect_analysis_availability(
                execution.database,
                state['analysis_id'],
                execution.effective,
                catalog=execution.analysis_catalog,
            )
            if not available.available:
                raise AnalysisUnavailableError('Dados incompletos.')
        except AnalysisTimeoutError:
            execution.message = 'O prazo da inspeção local foi excedido.'
            return {
                'status': 'execution_error',
                'error_code': 'inspection_timeout',
            }
        except AnalysisUnavailableError:
            execution.message = 'Dados necessários indisponíveis no workspace.'
            return {
                'status': 'analysis_unavailable',
                'error_code': 'workspace_unavailable',
            }
        except _InvalidParameters:
            execution.message = (
                'Parâmetros ou limites inválidos. Confira os critérios e '
                'selecione parâmetros permitidos.'
            )
            return {
                'status': 'needs_clarification',
                'error_code': 'invalid_analysis_parameters',
            }
        except AnalysisError, CatalogError:
            execution.message = 'A inspeção local não pôde ser concluída.'
            return {
                'status': 'execution_error',
                'error_code': 'inspection_failed',
            }
        return {
            'workspace_id': profile.file_id,
            'parameters': {
                name: value if type(value) is int else str(value)
                for name, value in execution.effective.items()
            },
            'status': 'validated',
        }

    def execute_catalog(state: AgentState) -> AgentState:
        try:
            execution.result = execute_analysis(
                execution.database,
                state['analysis_id'],
                execution.effective,
                row_limit=execution.row_limit,
                timeout_seconds=execution.timeout_seconds,
            )
        except AnalysisUnavailableError:
            execution.message = 'Dados necessários indisponíveis no workspace.'
            return {
                'status': 'analysis_unavailable',
                'error_code': 'workspace_unavailable',
            }
        except AnalysisError, CatalogError, OSError:
            execution.message = 'A execução local não pôde ser concluída.'
            return {
                'status': 'execution_error',
                'error_code': 'analysis_failed',
            }
        return {'run_id': execution.result.run_id, 'status': 'executed'}

    def make_report(state: AgentState) -> AgentState:
        assert execution.result is not None
        execution.report = build_report(
            execution.result,
            execution.analysis_catalog,
            workspace_dir=execution.workspace,
        )
        return {'report_id': state['run_id'], 'status': 'reported'}

    def suggest_next(state: AgentState) -> AgentState:
        assert execution.report is not None
        assert execution.result is not None
        try:
            execution.report = build_suggestions(
                execution.report,
                workspace_dir=execution.workspace,
                catalog=execution.analysis_catalog,
            )
            execution.message = 'Relatório salvo; sugestões aguardam seleção.'
        except ValueError, OSError:
            execution.message = (
                'Relatório salvo; sugestões temporariamente indisponíveis.'
            )
        save_report(execution.workspace, execution.report)
        return {'status': execution.result.status}

    def export_saved(state: AgentState) -> AgentState:
        execution.receipt = export_report(
            execution.workspace,
            state['run_id'],
            execution.export_format,
            show_sensitive=execution.show_sensitive,
        )
        execution.message = 'Exportação criada a partir do resultado salvo.'
        return {'status': 'exported', 'export_id': state['request_id']}

    graph = StateGraph(AgentState)
    graph.add_node('ROUTE_INTENT', route_intent)
    graph.add_node('VALIDATE_CONTRACT', validate_contract)
    graph.add_node('EXECUTE_CATALOG', execute_catalog)
    graph.add_node('BUILD_REPORT', make_report)
    graph.add_node('SUGGEST_NEXT', suggest_next)
    graph.add_node('EXPORT_REPORT', export_saved)
    graph.add_conditional_edges(
        START,
        lambda state: state['action'],
        {'analysis': 'ROUTE_INTENT', 'export': 'EXPORT_REPORT'},
    )
    for source, target, success in (
        ('ROUTE_INTENT', 'VALIDATE_CONTRACT', 'routed'),
        ('VALIDATE_CONTRACT', 'EXECUTE_CATALOG', 'validated'),
        ('EXECUTE_CATALOG', 'BUILD_REPORT', 'executed'),
        ('BUILD_REPORT', 'SUGGEST_NEXT', 'reported'),
    ):
        graph.add_conditional_edges(
            source,
            lambda state, expected=success: state['status'] == expected,
            {True: target, False: END},
        )
    graph.add_edge('SUGGEST_NEXT', END)
    graph.add_edge('EXPORT_REPORT', END)
    return graph.compile(checkpointer=False, debug=False)


def _invoke(execution: _Execution, initial: AgentState) -> WorkflowOutcome:
    def isolated() -> WorkflowOutcome:
        with tracing_context(enabled=False, parent=False):
            try:
                ensure_private_runtime()
                final = _build_graph(execution).invoke(
                    initial, config={'callbacks': [], 'recursion_limit': 12}
                )
            except ProviderFailure:
                final = {
                    **initial,
                    'status': 'execution_error',
                    'error_code': 'private_runtime_configuration',
                }
                execution.message = (
                    'Configuração de rastreamento incompatível com '
                    'execução privada.'
                )
            except ValueError, OSError:
                final = {
                    **initial,
                    'status': 'execution_error',
                    'error_code': 'report_or_export_failed',
                }
                execution.message = (
                    'Não foi possível concluir o relatório ou exportação. '
                    'O workspace foi preservado.'
                )
            return WorkflowOutcome(
                final, execution.message, execution.options, execution.receipt
            )

    return Context().run(isolated)


def run_agent(  # noqa: PLR0913 -- request and independent runtime controls
    workspace_dir: Path,
    request: AnalysisRequest,
    *,
    settings: Settings | None = None,
    provider: RoutingProvider | None = None,
    row_limit: int | None = None,
    timeout_seconds: float | None = None,
) -> WorkflowOutcome:
    """Run one explicit user action; provider use is optional and lazy."""
    settings = settings if settings is not None else Settings()
    if not settings.ai_routing_enabled:
        provider = None
    elif provider is None:
        provider = GeminiRoutingProvider(settings)
    execution = _Execution(
        workspace=workspace_dir,
        catalog=load_analysis_catalog(),
        request=request,
        provider=provider,
        row_limit=row_limit,
        timeout_seconds=timeout_seconds,
    )
    return _invoke(
        execution,
        {
            'request_id': uuid4().hex,
            'action': 'analysis',
            'status': 'pending',
        },
    )


def run_export(
    workspace_dir: Path,
    run_id: str,
    format: Literal['csv', 'xlsx'],
    *,
    show_sensitive: bool = False,
) -> WorkflowOutcome:
    """Export an identified saved run; do not reopen DuckDB or call AI."""
    execution = _Execution(
        workspace=workspace_dir,
        catalog=None,
        export_format=format,
        show_sensitive=show_sensitive,
    )
    return _invoke(
        execution,
        {
            'request_id': uuid4().hex,
            'action': 'export',
            'status': 'pending',
            'run_id': run_id,
            'report_id': run_id,
        },
    )
