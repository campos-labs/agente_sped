"""Projeções HTML de snapshots; sem cálculos fiscais ou credenciais."""

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import quote

from agente_sped.application.analysis import (
    AnalysisError,
    inspect_analysis_availability,
)
from agente_sped.application.profiles import (
    read_file_profile,
    read_record_evidence,
)
from agente_sped.application.sessions import WebSession, WorkspaceManager
from agente_sped.domain.reports import ReportPackage
from agente_sped.infra.analysis_catalog import AnalysisCatalog, CatalogError

_PAGE_SIZE = 100
EXPORT_NAME = re.compile(
    r'([0-9a-f]{32})\.(masked|raw)\.([0-9a-f]{32})\.(csv|xlsx|context\.json)'
)


def _display_timestamp(value: str) -> str:
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() is None:
        raise ValueError('Data do resultado sem fuso explícito.')
    return moment.astimezone(UTC).strftime('%d/%m/%Y %H:%M:%S UTC')


def display_value(value: object) -> str:
    if value is None:
        return '—'
    if isinstance(value, Decimal):
        text = format(value, 'f')
        integral, dot, fractional = text.partition('.')
        sign = '-' if integral.startswith('-') else ''
        digits = integral.lstrip('-')
        groups = [
            digits[max(0, end - 3) : end] for end in range(len(digits), 0, -3)
        ]
        return (
            sign
            + '.'.join(reversed(groups))
            + (',' + fractional if dot else '')
        )
    if type(value) is date:
        return value.strftime('%d/%m/%Y')
    return str(value)


def source_ids(
    rows: tuple[dict[str, object], ...] | list[dict[str, object]],
) -> set[int]:
    return {
        value
        for row in rows
        for name, value in row.items()
        if (name == 'record_id' or name.endswith('_record_id'))
        and type(value) is int
        and value > 0
    }


def available_exports(
    session: WebSession, run_id: str
) -> list[dict[str, str | None]]:
    directory = session.workspace_dir / 'exports'
    if (
        not directory.exists()
        or directory.is_symlink()
        or directory.is_junction()
    ):
        return []
    candidates = []
    for path in directory.iterdir():
        match = EXPORT_NAME.fullmatch(path.name)
        if not match or match[1] != run_id or match[4] not in ('csv', 'xlsx'):
            continue
        if path.is_symlink() or path.is_junction() or not path.is_file():
            continue
        candidates.append((path.stat().st_mtime_ns, path.name, path, match))
    exports = []
    for _, _, path, match in sorted(candidates, reverse=True)[:6]:
        context = path.with_suffix('.context.json')
        exports.append(
            {
                'label': 'Excel' if match[4] == 'xlsx' else 'CSV',
                'format': match[4],
                'privacy_mode': match[2],
                'data_url': '/downloads/' + quote(path.name),
                'context_url': '/downloads/' + quote(context.name)
                if match[4] == 'csv'
                and context.is_file()
                and not context.is_symlink()
                and not context.is_junction()
                else None,
            }
        )
    return exports


def report_view(
    report: ReportPackage,
    catalog: AnalysisCatalog,
    *,
    page: int = 1,
    revealed: bool = False,
) -> dict[str, object]:
    result = report.result
    pages = max(1, (result.row_count + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(1, min(page, pages))
    start = (page - 1) * _PAGE_SIZE
    rows = result.rows[start : start + _PAGE_SIZE]
    definition = catalog.analyses.get(result.analysis_id)
    compatible = result.analysis_catalog_sha256 == catalog.source_sha256
    return {
        'run_id': report.run_id,
        'analysis_id': result.analysis_id,
        'title': report.title,
        'status': result.status,
        'status_label': 'Concluída'
        if result.status == 'completed'
        else 'Sem resultados',
        'summary': [report.summary],
        'criteria': result.criteria,
        'limitations': (
            *result.limitations,
            *(
                ()
                if compatible
                else (
                    'Resultado de catálogo anterior; reprocessamento '
                    'requer ação explícita.',
                )
            ),
        ),
        'parameters': result.parameters,
        'parameter_labels': {
            name: parameter.label
            for name, parameter in definition.parameters.items()
        }
        if definition and compatible
        else {},
        'columns': report.columns,
        'rows': [
            [
                '***'
                if not revealed
                and column.name in report.sensitive_columns
                and row[column.name] is not None
                else row[column.name]
                for column in report.columns
            ]
            for row in rows
        ],
        'returned_rows': len(rows),
        'saved_rows': result.row_count,
        'total_rows': result.total_count,
        'truncated': result.truncated,
        'sensitive': bool(report.sensitive_columns),
        'revealed': revealed,
        'suggestions': report.suggestions if compatible else (),
        'sql': definition.execution.sql if definition and compatible else None,
        'catalog_version': result.catalog_version,
        'analysis_version': result.version,
        'layout_version': result.layout_version,
        'source_sha256': result.source_sha256,
        'created_at': _display_timestamp(report.created_at),
        'page': page,
        'pages': pages,
        'page_start': start + 1 if rows else 0,
        'page_end': start + len(rows),
        'prev_url': f'/runs/{report.run_id}?page={page - 1}'
        if page > 1
        else None,
        'next_url': f'/runs/{report.run_id}?page={page + 1}'
        if page < pages
        else None,
        'evidence_ids': source_ids(rows),
    }


def _catalog_view(
    catalog: AnalysisCatalog,
    session: WebSession,
    profile: dict[str, object] | None,
    selected: ReportPackage | None,
) -> list[dict[str, object]]:
    entries = []
    for identity, definition in catalog.analyses.items():
        available = False
        technical_error = False
        reason = 'Carregue uma escrituração para verificar a disponibilidade.'
        if profile is not None:
            parameters = (
                selected.result.parameters
                if selected and selected.result.analysis_id == identity
                else None
            )
            try:
                status = inspect_analysis_availability(
                    session.workspace_dir / 'analysis.duckdb',
                    identity,
                    parameters,
                    catalog=catalog,
                )
                available, reason = status.available, status.reason
            except AnalysisError, CatalogError:
                technical_error = True
                reason = (
                    'Falha técnica ao verificar a disponibilidade. '
                    'Tente novamente; isto não indica ausência de dados.'
                )
        scope = definition.execution.period_scope
        entries.append(
            {
                'analysis_id': identity,
                'title': definition.title,
                'description': definition.description,
                'available': available,
                'unavailable_reason': reason,
                'availability_label': 'verificação falhou'
                if technical_error
                else 'indisponível',
                'period_scope': 'full_file' if scope == 'file' else scope,
            }
        )
    return entries


def _parameter_fields(
    catalog: AnalysisCatalog,
    profile: dict[str, object] | None,
    selected: ReportPackage | None,
) -> list[dict[str, object]]:
    fields: dict[str, dict[str, object]] = {}
    selected_id = selected.result.analysis_id if selected else None
    for identity, definition in catalog.analyses.items():
        for name, parameter in definition.parameters.items():
            if (
                definition.execution.period_scope == 'file'
                and parameter.type == 'date'
            ):
                continue
            value = parameter.default
            if parameter.default_from and profile:
                attribute = parameter.default_from.removeprefix(
                    'file_profile.'
                )
                value = profile[attribute]
            if selected and identity == selected_id:
                value = selected.result.parameters.get(name, value)
            bounds = parameter.constraints
            variant = {
                'label': parameter.label,
                'type': parameter.type,
                'value': str(value) if value is not None else '',
                'required': parameter.required,
                'min': str(bounds.minimum)
                if bounds.minimum is not None
                else None,
                'max': str(bounds.maximum)
                if bounds.maximum is not None
                else None,
                'step': '1' if parameter.type == 'integer' else 'any',
            }
            field = fields.setdefault(
                name, {'name': name, 'analysis_ids': [], 'variants': {}}
            )
            field['analysis_ids'].append(identity)
            field['variants'][identity] = variant
            if 'value' not in field or identity == selected_id:
                field.update(variant)
    return list(fields.values())


def _clarification_options(
    identities: list[str],
    catalog: AnalysisCatalog,
    entries: list[dict[str, object]],
) -> list[dict[str, object]]:
    availability = {entry['analysis_id']: entry for entry in entries}
    return [
        {
            'analysis_id': identity,
            'title': catalog.analyses[identity].title,
            'criteria': catalog.analyses[identity].criteria,
            'available': availability[identity]['available'],
            'message': availability[identity]['unavailable_reason'],
        }
        for identity in identities
        if identity in availability
    ]


def workspace_context(  # noqa: PLR0913
    manager: WorkspaceManager,
    session: WebSession,
    catalog: AnalysisCatalog,
    *,
    run_id: str | None = None,
    page: int = 1,
    revealed: bool = False,
    evidence_id: int | None = None,
) -> dict[str, object]:
    """Project explicit HTTP view controls without persisting reveal flags.

    Page, reveal and evidence identify independent, request-scoped choices;
    keeping them explicit avoids putting sensitive visibility in the session.
    """
    metadata = manager.read(session)
    operation = metadata.operation
    busy = bool(operation and operation.status == 'running')
    profile = None
    if metadata.workspace_ready and not busy:
        profile = read_file_profile(session.workspace_dir)
        profile['filename'] = metadata.source_name or 'Escrituração carregada'
    history = []
    for identity in reversed(metadata.run_ids[-5:]):
        previous = manager.report(session, identity)
        history.append(
            {
                'run_id': identity,
                'title': previous.title,
                'status': 'Concluída'
                if previous.result.status == 'completed'
                else 'Sem resultados',
                'created_at': _display_timestamp(previous.created_at),
            }
        )
    report = None
    saved = None
    exports = []
    non_result = bool(
        operation
        and (
            operation.outcome_status
            in {
                'needs_clarification',
                'out_of_catalog',
                'provider_failure',
                'no_action',
                'analysis_unavailable',
                'execution_error',
            }
            or operation.kind in {'analysis', 'ask'}
            and operation.status in {'failed', 'interrupted'}
        )
    )
    identity = run_id or (
        metadata.run_ids[-1] if metadata.run_ids and not non_result else None
    )
    if identity:
        saved = manager.report(session, identity)
        report = report_view(saved, catalog, page=page, revealed=revealed)
        report['evidence'] = (
            read_record_evidence(
                session.workspace_dir,
                set(sorted(report.pop('evidence_ids'))[:20]),
            )
            if not busy
            else []
        )
        if evidence_id is not None:
            if evidence_id not in source_ids(saved.result.rows):
                raise ValueError('Origem não pertence ao resultado.')
            evidence = read_record_evidence(
                session.workspace_dir, {evidence_id}, reveal=True
            )
            if not evidence or evidence[0]['text'] is None:
                raise ValueError(
                    'Linha original indisponível para apresentação.'
                )
            report['original_evidence'] = evidence[0]
        exports = available_exports(session, identity)
    message = operation.message if operation and operation.message else None
    catalog_view = _catalog_view(catalog, session, profile, saved)
    return {
        'csrf': session.csrf,
        'status_url': f'/status?run_id={run_id}&page={report["page"]}'
        if run_id and report
        else '/status',
        'can_clear': bool(
            metadata.workspace_ready
            or metadata.source_name
            or metadata.operation
        ),
        'profile': profile,
        'catalog': catalog_view,
        'parameter_fields': _parameter_fields(catalog, profile, saved),
        'selection': {
            'analysis_id': report['analysis_id'] if report else '',
            'parameters': saved.result.parameters if saved else {},
        },
        'report': report,
        'history': history,
        'exports': exports,
        'operation': {
            'status': operation.status,
            'label': {
                'upload': 'Lendo a escrituração',
                'analysis': 'Executando análise',
                'ask': 'Interpretando a pergunta',
                'export': 'Preparando exportação',
            }[operation.kind],
            'phase': 'Aguarde. Fechar a aba não cancela a operação.',
        }
        if operation
        else None,
        'message': message,
        'level': 'error'
        if operation
        and (
            operation.status in ('failed', 'interrupted')
            or operation.outcome_status
            in ('execution_error', 'provider_failure')
        )
        else 'warning'
        if operation and operation.outcome_status == 'analysis_unavailable'
        else 'info',
        'options': _clarification_options(
            operation.options, catalog, catalog_view
        )
        if operation
        else [],
    }
