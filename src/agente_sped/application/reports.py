"""Apresente resultados salvos sem reconstruir cálculos catalogados."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from agente_sped.application import analysis
from agente_sped.application.analysis import AnalysisResult
from agente_sped.domain.reports import (
    MAX_NARRATIVE_BYTES,
    MAX_NARRATIVE_ROWS,
    NarrativePayload,
    ReportColumn,
    ReportPackage,
    ResultSnapshot,
    SuggestedAnalysis,
)
from agente_sped.infra.analysis_catalog import (
    AnalysisCatalog,
    AnalysisDefinition,
    load_analysis_catalog,
)
from agente_sped.infra.report_store import encode_report_value, validate_report


def _summary(result: AnalysisResult, definition: AnalysisDefinition) -> str:
    if not result.rows:
        text = definition.result.empty_message
    else:
        first = result.rows[0]
        match result.analysis_id:
            case 'movement_by_cfop':
                coverage = first['displayed_coverage_percent']
                coverage_text = (
                    'não disponível' if coverage is None else f'{coverage}%'
                )
                text = (
                    f'Total regular de saída: {first["total_operacoes"]}. '
                    'Subtotal do recorte de CFOPs catalogado: '
                    f'{first["displayed_subtotal"]}; '
                    f'cobertura: {coverage_text}.'
                )
            case 'cancelled_documents':
                text = (
                    f'{result.total_count} documentos de saída com '
                    'cancelamento comum ou extemporâneo na escrituração.'
                )
            case 'block_k_activity':
                text = (
                    f'{first["total_distinct_orders"]} ordens distintas '
                    f'no período; {result.total_count} datas de início '
                    'com K230 elegível.'
                )
            case 'supplier_concentration':
                text = (
                    f'{result.total_count} fornecedores acima do limite. '
                    'Total das compras consideradas: '
                    f'{first["total_purchases"]}.'
                )
            case 'possible_duplicate_documents':
                text = (
                    f'{result.total_count} grupos de possíveis duplicidades. '
                    'A triagem não comprova duplicidade documental.'
                )
            case 'document_sequence_gaps':
                text = (
                    f'{result.total_count} possíveis intervalos ausentes '
                    'dentro do limite configurado, por modelo e série. '
                    'A presença de um número não valida sua situação fiscal.'
                )
            case 'generic_ncm_high_value':
                text = (
                    f'{result.total_count} itens com NCM tratado '
                    'como genérico '
                    'e valor acima do limite informado.'
                )
            case 'possible_credit_on_consumption_cfop':
                text = (
                    f'{result.total_count} linhas candidatas à investigação '
                    'de crédito em CFOP 1556 ou 2556, sem conclusão fiscal.'
                )
            case _:
                raise ValueError('Análise sem síntese determinística.')
    return _summary_context(text, result, definition)


def _summary_context(
    text: str, result: AnalysisResult, definition: AnalysisDefinition
) -> str:
    text += f' Linhas disponíveis nesta execução: {result.row_count}.'
    if result.truncated:
        text += (
            f' Resultado truncado: {result.row_count} de '
            f'{result.total_count} linhas; a exportação usa somente as salvas.'
        )
    dynamic = [
        item
        for item in result.limitations
        if item not in definition.limitations
    ]
    if dynamic:
        text += ' ' + ' '.join(dynamic)
    return text


def _narrative_payload(
    result: ResultSnapshot, definition: AnalysisDefinition
) -> NarrativePayload:
    sensitive = definition.privacy.sensitive_columns
    limit = min(definition.narrative.input.max_rows, MAX_NARRATIVE_ROWS)
    rows = tuple(
        {
            name: '***' if name in sensitive and value is not None else value
            for name, value in row.items()
        }
        for row in result.rows[:limit]
    )
    while True:
        payload = NarrativePayload(
            analysis_id=result.analysis_id,
            rows=rows,
            total_rows=result.total_count,
            result_rows=result.row_count,
            returned_rows=len(rows),
            truncated=len(rows) < result.total_count,
            criteria=result.criteria,
            limitations=result.limitations,
        )
        if len(encode_report_value(asdict(payload))) <= MAX_NARRATIVE_BYTES:
            return payload
        if not rows:
            raise ValueError('Contexto narrativo excede o limite de bytes.')
        rows = rows[:-1]


def build_report(
    result: AnalysisResult, catalog: AnalysisCatalog, *, workspace_dir: Path
) -> ReportPackage:
    if not workspace_dir.is_dir():
        raise ValueError('Workspace de relatório indisponível.')
    definition = catalog.analyses[result.analysis_id]
    if (
        result.analysis_catalog_sha256 != catalog.source_sha256
        or result.catalog_version != catalog.catalog_version
        or result.version != definition.version
    ):
        raise ValueError(
            'Resultado e catálogo pertencem a versões diferentes.'
        )
    snapshot = ResultSnapshot(**asdict(result))
    report = ReportPackage(
        schema_version='1',
        created_at=datetime.now(UTC).isoformat(),
        result=snapshot,
        title=definition.title,
        columns=tuple(
            ReportColumn(column.name, column.type, column.label, column.format)
            for column in definition.result.columns
        ),
        sensitive_columns=tuple(definition.privacy.sensitive_columns),
        summary=_summary(result, definition),
        empty_message=definition.result.empty_message,
        narrative_payload=_narrative_payload(snapshot, definition),
    )
    validate_report(report)
    return report


def build_suggestions(
    report: ReportPackage,
    *,
    workspace_dir: Path,
    catalog: AnalysisCatalog | None = None,
) -> ReportPackage:
    """Inspecione destinos disponíveis; nenhuma análise é executada."""
    catalog = catalog or load_analysis_catalog()
    if catalog.source_sha256 != report.result.analysis_catalog_sha256:
        raise ValueError('Sugestões exigem o catálogo da execução.')
    database = workspace_dir / 'analysis.duckdb'
    profile = analysis.get_workspace_profile(database)
    if (
        profile.file_id != report.result.file_id
        or profile.source_sha256 != report.result.source_sha256
        or profile.layout_version != report.result.layout_version
        or profile.layout_source_sha256 != report.result.layout_source_sha256
        or profile.layout_metadata_sha256
        != report.result.layout_metadata_sha256
    ):
        raise ValueError('Sugestões pertencem a outro workspace.')
    source = catalog.analyses[report.result.analysis_id]
    suggestions = []
    for item in source.suggestions:
        target = catalog.analyses[item.analysis_id]
        parameters = {
            name: value
            for name, value in report.result.parameters.items()
            if name in target.parameters
        }
        label = item.label
        if target.execution.period_scope == 'file':
            parameters.update(
                start_date=profile.period_start, end_date=profile.period_end
            )
            label += ' (período integral da escrituração)'
        status = analysis.inspect_analysis_availability(
            database, item.analysis_id, parameters, catalog=catalog
        )
        if status.available:
            suggestions.append(
                SuggestedAnalysis(item.analysis_id, label, status.parameters)
            )
    updated = replace(report, suggestions=tuple(suggestions))
    validate_report(updated)
    return updated
