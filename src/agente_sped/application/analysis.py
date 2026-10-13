"""Execute versioned catalog analyses against a local read-only workspace."""

import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Event, Timer
from typing import Annotated, Literal
from uuid import uuid4

import duckdb
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
)

from agente_sped.infra.analysis_catalog import (
    AnalysisCatalog,
    AnalysisDefinition,
    CatalogError,
    load_analysis_catalog,
)

_TECHNICAL_TYPES = {
    'file_id': 'VARCHAR',
    'record_id': 'BIGINT',
    'record_code': 'VARCHAR',
    'record_level': 'INTEGER',
    'source_line': 'BIGINT',
    'parent_record_id': 'BIGINT',
    'parent_record_code': 'VARCHAR',
}
_FIELD_TYPES = {
    'string': 'VARCHAR',
    'date': 'DATE',
    'decimal': 'DECIMAL(18,2)',
    'integer': 'BIGINT',
}
_METADATA_COLUMNS = (
    'file_id',
    'layout_version',
    'source_sha256',
    'period_start',
    'period_end',
    'layout_source_sha256',
    'layout_metadata_sha256',
)


class AnalysisError(ValueError):
    """An analysis request or its controlled execution is invalid."""


class AnalysisUnavailableError(AnalysisError):
    """The workspace cannot support a complete and reproducible analysis."""


class AnalysisTimeoutError(AnalysisError):
    """The complete read operation exceeded its configured deadline."""


def _date_input(value: object) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return date.fromisoformat(value)
    raise ValueError('Informe uma data real no formato ISO AAAA-MM-DD.')


def _decimal_input(value: object) -> Decimal:
    if type(value) not in (int, str, Decimal):
        raise ValueError('Informe Decimal, inteiro ou texto decimal exato.')
    try:
        decimal = Decimal(value)
    except InvalidOperation as error:
        raise ValueError('Número decimal inválido.') from error
    if not decimal.is_finite():
        raise ValueError('Número decimal deve ser finito.')
    return decimal


class _WorkspaceMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    file_id: Annotated[str, Field(min_length=1)]
    layout_version: Literal['020']
    source_sha256: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    period_start: date
    period_end: date
    layout_source_sha256: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    layout_metadata_sha256: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]


@dataclass(frozen=True)
class AnalysisResult:
    run_id: str
    analysis_id: str
    version: str
    catalog_version: str
    analysis_catalog_sha256: str
    parameters: dict[str, object]
    rows: tuple[dict[str, object], ...]
    row_count: int
    total_count: int
    truncated: bool
    status: Literal['completed', 'empty_result']
    file_id: str
    source_sha256: str
    layout_version: str
    layout_source_sha256: str
    layout_metadata_sha256: str
    row_limit: int
    timeout_seconds: float
    elapsed_seconds: float
    criteria: tuple[str, ...]
    limitations: tuple[str, ...]


def validate_execution_limits(
    catalog: AnalysisCatalog,
    definition: AnalysisDefinition,
    row_limit: int | None,
    timeout_seconds: float | None,
) -> tuple[int, float]:
    """Validate execution limits before opening or creating a workspace."""
    maximum_rows = min(
        catalog.runtime.max_result_rows, definition.result.row_limit
    )
    rows = maximum_rows if row_limit is None else row_limit
    if type(rows) is not int or not 1 <= rows <= maximum_rows:
        raise AnalysisError(
            f'row_limit deve ser inteiro entre 1 e {maximum_rows}.'
        )
    maximum_timeout = catalog.runtime.timeout_seconds
    seconds = maximum_timeout if timeout_seconds is None else timeout_seconds
    if (
        type(seconds) not in (int, float)
        or not 0 < seconds <= maximum_timeout
        or not math.isfinite(seconds)
    ):
        raise AnalysisError(
            f'timeout_seconds deve ser positivo e no máximo {maximum_timeout}.'
        )
    return rows, float(seconds)


def _schema(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    expected: Mapping[str, str],
) -> None:
    columns = dict(
        connection.execute(
            'SELECT column_name, data_type FROM information_schema.columns '
            "WHERE table_schema = 'main' AND table_name = $table",
            {'table': table},
        ).fetchall()
    )
    if any(columns.get(name) != value for name, value in expected.items()):
        raise AnalysisUnavailableError(
            f'Schema incompatível na relação {table}.'
        )


def _metadata(connection: duckdb.DuckDBPyConnection) -> _WorkspaceMetadata:
    expected = dict.fromkeys(_METADATA_COLUMNS, 'VARCHAR')
    expected.update(period_start='DATE', period_end='DATE')
    _schema(connection, 'workspace_metadata', expected)
    rows = connection.execute(
        'SELECT '
        + ', '.join(_METADATA_COLUMNS)
        + ' FROM workspace_metadata LIMIT 2'
    ).fetchall()
    if len(rows) != 1:
        raise AnalysisUnavailableError(
            'O workspace deve identificar exatamente um arquivo.'
        )
    try:
        metadata = _WorkspaceMetadata.model_validate(
            dict(zip(_METADATA_COLUMNS, rows[0], strict=True))
        )
    except ValidationError as error:
        raise AnalysisUnavailableError(
            'Metadados de workspace incompatíveis.'
        ) from error
    if metadata.period_start > metadata.period_end:
        raise AnalysisUnavailableError('Período do workspace inválido.')
    return metadata


def validate_analysis_parameters(
    definition: AnalysisDefinition,
    parameters: Mapping[str, object] | None,
    *,
    period_start: date,
    period_end: date,
) -> dict[str, object]:
    """Resolve defaults and validate parameters against the file period."""
    if parameters is not None and not isinstance(parameters, Mapping):
        raise AnalysisError('Parâmetros devem ser um mapeamento.')
    fields = {}
    profile = {'period_start': period_start, 'period_end': period_end}
    for name, parameter in definition.parameters.items():
        default = parameter.default
        if parameter.default_from is not None:
            attribute = parameter.default_from.removeprefix('file_profile.')
            default = profile[attribute]
        bounds = parameter.constraints
        if parameter.type == 'date':
            value_type = Annotated[date, BeforeValidator(_date_input)]
        elif parameter.type == 'integer':
            value_type = Annotated[
                int, Field(strict=True, ge=bounds.minimum, le=bounds.maximum)
            ]
        else:
            value_type = Annotated[
                Decimal,
                BeforeValidator(_decimal_input),
                Field(strict=True, ge=bounds.minimum, le=bounds.maximum),
            ]
        fields[name] = (value_type, default)
    contract = create_model(
        'AnalysisParameters',
        __config__=ConfigDict(
            extra='forbid', strict=True, validate_default=True
        ),
        **fields,
    )
    try:
        effective = contract.model_validate(
            dict(parameters or {})
        ).model_dump()
    except ValidationError as error:
        raise AnalysisError('Parâmetros da análise inválidos.') from error
    if effective['start_date'] > effective['end_date']:
        raise AnalysisError('Parâmetros de período fora de ordem.')
    if definition.execution.period_scope == 'file' and (
        effective['start_date'] != period_start
        or effective['end_date'] != period_end
    ):
        raise AnalysisError('A análise exige o período integral do arquivo.')
    return effective


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _field_schema(
    connection: duckdb.DuckDBPyConnection,
    record: str,
    names: list[str],
    *,
    required: bool,
) -> None:
    expected = dict(_TECHNICAL_TYPES)
    for name in names:
        alias = name.lower()
        interpreted = connection.execute(
            'SELECT position, value_type FROM interpreted_fields '
            'WHERE record_code = $record AND alias = $alias LIMIT 2',
            {'record': record, 'alias': alias},
        ).fetchall()
        if len(interpreted) != 1:
            raise AnalysisUnavailableError(
                f'Campo sem interpretação verificada: {record}.{name}.'
            )
        position, kind = interpreted[0]
        if position is None or position <= 1 or kind not in _FIELD_TYPES:
            raise AnalysisUnavailableError('Metadados de campo incompatíveis.')
        expected[alias] = (
            'DECIMAL(18,6)'
            if kind == 'decimal' and (record, alias) == ('K230', 'qtd_enc')
            else _FIELD_TYPES[kind]
        )
        if (
            required
            and connection.execute(
                'SELECT EXISTS(SELECT 1 FROM field_errors '
                'WHERE record_code = $record AND position = $position)',
                {'record': record, 'position': position},
            ).fetchone()[0]
        ):
            raise AnalysisUnavailableError(
                'Erros em campos necessários impedem a análise.'
            )
    _schema(connection, record.lower(), expected)


def _availability(
    connection: duckdb.DuckDBPyConnection,
    definition: AnalysisDefinition,
    metadata: _WorkspaceMetadata,
) -> None:
    availability = definition.availability
    _schema(
        connection,
        'interpreted_fields',
        {
            'record_code': 'VARCHAR',
            'position': 'INTEGER',
            'alias': 'VARCHAR',
            'value_type': 'VARCHAR',
        },
    )
    error_columns = {
        'file_id': 'VARCHAR',
        'record_id': 'BIGINT',
        'record_code': 'VARCHAR',
        'error_code': 'VARCHAR',
    }
    _schema(connection, 'record_errors', error_columns)
    _schema(
        connection, 'field_errors', dict(error_columns, position='INTEGER')
    )
    field_groups = (
        (availability.required_fields, True),
        (availability.optional_fields, False),
    )
    for groups, required in field_groups:
        for record, names in groups.items():
            _field_schema(connection, record, names, required=required)
            table = _quoted(record.lower())
            if required and availability.record_presence_required:
                present = connection.execute(
                    f'SELECT EXISTS(SELECT 1 FROM {table})'
                ).fetchone()[0]
                if not present:
                    raise AnalysisUnavailableError(
                        f'Sem ocorrências necessárias de {record}.'
                    )
            if connection.execute(
                f'SELECT EXISTS(SELECT 1 FROM {table} '
                'WHERE file_id IS DISTINCT FROM $file_id '
                'OR record_code IS DISTINCT FROM $record)',
                {'file_id': metadata.file_id, 'record': record},
            ).fetchone()[0]:
                raise AnalysisUnavailableError(
                    'Relações incompatíveis com o arquivo do workspace.'
                )
            if connection.execute(
                'SELECT EXISTS(SELECT position FROM interpreted_fields '
                'WHERE record_code = $record GROUP BY position '
                'HAVING COUNT(*) > 1)',
                {'record': record},
            ).fetchone()[0]:
                raise AnalysisUnavailableError(
                    'Posições interpretadas duplicadas impedem a análise.'
                )
    if connection.execute(
        'SELECT EXISTS(SELECT 1 FROM record_errors '
        'WHERE record_code = ANY($records))',
        {
            'records': (
                availability.required_records + availability.optional_records
            )
        },
    ).fetchone()[0]:
        raise AnalysisUnavailableError(
            'Erros estruturais nos registros utilizados impedem a análise.'
        )


def _select_sql(
    connection: duckdb.DuckDBPyConnection, definition: AnalysisDefinition
) -> tuple[str, set[str]]:
    sql = definition.execution.sql.strip()
    statements = connection.extract_statements(sql)
    if (
        len(statements) != 1
        or statements[0].type != duckdb.StatementType.SELECT
    ):
        raise CatalogError(
            'A análise exige exatamente uma instrução SELECT catalogada.'
        )
    names = set(statements[0].named_parameters)
    if not names <= set(definition.parameters):
        raise CatalogError(
            'SQL referencia parâmetro não declarado no catálogo.'
        )
    return sql.removesuffix(';'), names


def _has_rows(
    connection: duckdb.DuckDBPyConnection,
    query: str,
    parameters: dict[str, object],
) -> bool:
    sql = f'SELECT EXISTS(\n{query}\n)'
    names = connection.extract_statements(sql)[0].named_parameters
    bound = {name: parameters[name] for name in names}
    return connection.execute(sql, bound).fetchone()[0]


def _guard_values(
    connection: duckdb.DuckDBPyConnection,
    relation: str,
    columns: tuple[str, ...],
    predicate: str,
    parameters: dict[str, object],
) -> None:
    missing = ' OR '.join(
        f"NULLIF(TRIM(CAST({column} AS VARCHAR)), '') IS NULL"
        for column in columns
    )
    if _has_rows(
        connection,
        f'SELECT 1 FROM {relation} WHERE ({predicate}) AND ({missing})',
        parameters,
    ):
        raise AnalysisUnavailableError(
            'Dados necessários incompletos impedem a análise.'
        )


def _join_integrity(
    connection: duckdb.DuckDBPyConnection,
    definition: AnalysisDefinition,
    parameters: dict[str, object],
) -> None:
    availability = definition.availability
    relations = availability.required_records + availability.optional_records
    for record in relations:
        dimension_key = {'0150': 'cod_part', '0200': 'cod_item'}.get(record)
        if dimension_key is not None and _has_rows(
            connection,
            f'SELECT {dimension_key} FROM {_quoted(record.lower())} '
            f'WHERE {dimension_key} IS NOT NULL GROUP BY {dimension_key} '
            'HAVING COUNT(*) > 1',
            parameters,
        ):
            raise AnalysisUnavailableError(
                'Chaves de cadastro duplicadas impedem cálculo '
                'sem amplificação.'
            )
        if record not in ('C170', 'C190'):
            continue
        child = record.lower()
        if _has_rows(
            connection,
            f'SELECT 1 FROM {child} LEFT JOIN c100 '
            f'ON {child}.file_id = c100.file_id '
            f'AND {child}.parent_record_id = c100.record_id '
            'WHERE c100.record_id IS NULL '
            f"OR {child}.parent_record_code IS DISTINCT FROM 'C100'",
            parameters,
        ):
            raise AnalysisUnavailableError(
                'Dados necessários sem vínculo estrutural impedem a análise.'
            )


def _readiness(
    connection: duckdb.DuckDBPyConnection,
    analysis_id: str,
    definition: AnalysisDefinition,
    parameters: dict[str, object],
) -> None:
    # These checks reject incomplete calculations, without classifying fiscal
    # correctness or replacing any of the selection criteria in the catalog.
    document_fields = {
        'movement_by_cfop': ('ind_oper', 'cod_sit'),
        'cancelled_documents': ('ind_oper', 'cod_sit'),
        'supplier_concentration': ('ind_oper', 'ind_emit', 'cod_sit'),
        'document_sequence_gaps': ('ind_oper', 'ind_emit', 'cod_sit'),
        'generic_ncm_high_value': ('cod_sit',),
        'possible_credit_on_consumption_cfop': ('ind_oper', 'cod_sit'),
    }
    if analysis_id in document_fields:
        _guard_values(
            connection,
            'c100',
            document_fields[analysis_id],
            'TRUE',
            parameters,
        )
    date_candidates = {
        'movement_by_cfop': "ind_oper = '1' AND cod_sit IN ('00', '01')",
        'supplier_concentration': (
            "ind_oper = '0' AND ind_emit = '1' AND cod_sit IN ('00', '01')"
        ),
        'generic_ncm_high_value': "cod_sit IN ('00', '01')",
        'possible_credit_on_consumption_cfop': (
            "ind_oper = '0' AND cod_sit IN ('00', '01')"
        ),
    }
    if analysis_id in date_candidates:
        _guard_values(
            connection,
            'c100',
            ('dt_doc',),
            date_candidates[analysis_id],
            parameters,
        )
    _join_integrity(connection, definition, parameters)
    period = 'c100.dt_doc BETWEEN $start_date AND $end_date'
    regular = "c100.cod_sit IN ('00', '01')"
    detail_join = (
        'c190 INNER JOIN c100 ON c190.file_id = c100.file_id '
        'AND c190.parent_record_id = c100.record_id'
    )
    if analysis_id == 'movement_by_cfop':
        _guard_values(
            connection,
            detail_join,
            ('c190.cfop', 'c190.vl_opr'),
            f"c100.ind_oper = '1' AND {regular} AND {period}",
            parameters,
        )
    elif analysis_id == 'block_k_activity':
        _guard_values(
            connection,
            'k230',
            ('qtd_enc',),
            'dt_ini_op BETWEEN $start_date AND $end_date',
            parameters,
        )
    elif analysis_id == 'supplier_concentration':
        _guard_values(
            connection,
            'c100',
            ('vl_doc',),
            f"ind_oper = '0' AND ind_emit = '1' AND {regular} AND {period} "
            "AND NULLIF(TRIM(cod_part), '') IS NOT NULL",
            parameters,
        )
    elif analysis_id == 'possible_duplicate_documents':
        if _has_rows(
            connection,
            'SELECT 1 FROM c100 WHERE '
            + period
            + ' GROUP BY file_id, cod_part, cod_mod, '
            "COALESCE(NULLIF(TRIM(ser), ''), 'SEM_SERIE'), "
            'num_doc, dt_doc, ind_oper HAVING COUNT(*) > 1 '
            'AND COUNT(vl_doc) <> COUNT(*)',
            parameters,
        ):
            raise AnalysisUnavailableError(
                'Dados necessários de valor incompletos no grupo duplicado.'
            )
    elif analysis_id == 'generic_ncm_high_value':
        _item_readiness(connection, regular + ' AND ' + period, parameters)
    elif analysis_id == 'possible_credit_on_consumption_cfop':
        candidate = f"c100.ind_oper = '0' AND {regular} AND {period}"
        _guard_values(
            connection, detail_join, ('c190.cfop',), candidate, parameters
        )
        _guard_values(
            connection,
            detail_join,
            ('c190.vl_icms',),
            candidate + " AND c190.cfop IN ('1556', '2556')",
            parameters,
        )


def _item_readiness(
    connection: duckdb.DuckDBPyConnection,
    candidate: str,
    parameters: dict[str, object],
) -> None:
    relation = (
        'c170 INNER JOIN c100 ON c170.file_id = c100.file_id '
        'AND c170.parent_record_id = c100.record_id '
        'LEFT JOIN "0200" AS item ON c170.file_id = item.file_id '
        'AND c170.cod_item = item.cod_item'
    )
    if _has_rows(
        connection,
        'SELECT 1 FROM '
        + relation
        + ' WHERE '
        + candidate
        + ' AND item.record_id IS NULL',
        parameters,
    ):
        raise AnalysisUnavailableError(
            'Não é possível determinar NCM de item '
            'sem cadastro correspondente.'
        )
    _guard_values(
        connection,
        relation,
        ('c170.vl_item',),
        candidate + " AND item.cod_ncm IN ('00000000', '99999999')",
        parameters,
    )


def _query_result(
    connection: duckdb.DuckDBPyConnection,
    definition: AnalysisDefinition,
    parameters: dict[str, object],
    row_limit: int,
) -> tuple[tuple[dict[str, object], ...], int]:
    sql, names = _select_sql(connection, definition)
    bound = {name: parameters[name] for name in names}
    bound['runtime_row_limit'] = row_limit
    cursor = connection.execute(
        'SELECT catalog_result.*, COUNT(*) OVER () AS __total_count '
        f'FROM (\n{sql}\n) AS catalog_result LIMIT $runtime_row_limit',
        bound,
    )
    names = [column[0] for column in cursor.description[:-1]]
    if names != [column.name for column in definition.result.columns]:
        raise CatalogError(
            'Resultado SQL incompatível com as colunas do catálogo.'
        )
    values = cursor.fetchall()
    total_count = values[0][-1] if values else 0
    rows = tuple(dict(zip(names, row[:-1], strict=True)) for row in values)
    return rows, total_count


def _scope_limitations(
    connection: duckdb.DuckDBPyConnection,
    analysis_id: str,
    total_count: int,
) -> tuple[str, ...]:
    if analysis_id == 'block_k_activity':
        missing = connection.execute(
            'SELECT COUNT(*) FROM k230 WHERE dt_ini_op IS NULL'
        ).fetchone()[0]
        notes = [
            'K230 sem DT_INI_OP no arquivo, excluídos do filtro por data: '
            f'{missing}.'
        ]
        if total_count == 0:
            note = (
                'Nenhum K230 elegível com data de início no período '
                'selecionado'
            )
            if missing:
                note += (
                    f'; os {missing} registros sem data no arquivo impedem '
                    'concluir ausência de atividade'
                )
            notes.append(note + '.')
        if missing:
            notes.append(
                'Os registros sem data não foram atribuídos ao período; '
                'nenhuma data foi inferida.'
            )
        return tuple(notes)
    if analysis_id == 'possible_duplicate_documents':
        missing = connection.execute(
            'SELECT COUNT(*) FROM c100 WHERE dt_doc IS NULL'
        ).fetchone()[0]
        if missing:
            return (
                f'No arquivo, {missing} C100 sem DT_DOC foram excluídos do '
                'filtro por data; a detecção de duplicidades cobre somente '
                'documentos com data no período.',
            )
    return ()


def execute_analysis(
    database_path: Path,
    analysis_id: str,
    parameters: Mapping[str, object] | None = None,
    *,
    row_limit: int | None = None,
    timeout_seconds: float | None = None,
) -> AnalysisResult:
    """Run a catalog ID; never accept SQL from callers."""
    catalog = load_analysis_catalog()
    if not isinstance(analysis_id, str) or analysis_id not in catalog.analyses:
        raise AnalysisError('Análise não pertence ao catálogo.')
    definition = catalog.analyses[analysis_id]
    limit, seconds = validate_execution_limits(
        catalog, definition, row_limit, timeout_seconds
    )
    if not isinstance(database_path, Path) or not database_path.is_file():
        raise AnalysisUnavailableError('Banco do workspace indisponível.')
    started = time.monotonic()
    interrupted = Event()
    try:
        connection = duckdb.connect(
            str(database_path),
            read_only=True,
            config={
                'enable_external_access': 'false',
                'autoinstall_known_extensions': 'false',
                'autoload_known_extensions': 'false',
            },
        )
    except duckdb.Error as error:
        raise AnalysisUnavailableError(
            'Banco do workspace incompatível.'
        ) from error
    finished = Event()

    def interrupt() -> None:
        interrupted.set()
        # Metadata and analysis queries share the same overall deadline.
        while not finished.is_set():
            connection.interrupt()
            finished.wait(0.01)

    timer = Timer(seconds, interrupt)
    timer.daemon = True
    timer.start()
    try:
        metadata = _metadata(connection)
        effective = validate_analysis_parameters(
            definition,
            parameters,
            period_start=metadata.period_start,
            period_end=metadata.period_end,
        )
        _availability(connection, definition, metadata)
        _readiness(connection, analysis_id, definition, effective)
        rows, total_count = _query_result(
            connection, definition, effective, limit
        )
        scope_limitations = _scope_limitations(
            connection, analysis_id, total_count
        )
        elapsed = time.monotonic() - started
        if interrupted.is_set() or elapsed >= seconds:
            raise AnalysisTimeoutError(
                'O tempo limite da análise foi excedido.'
            )
    except duckdb.InterruptException as error:
        raise AnalysisTimeoutError(
            'O tempo limite da análise foi excedido.'
        ) from error
    except duckdb.Error as error:
        raise AnalysisError(
            'Falha controlada ao consultar o workspace.'
        ) from error
    finally:
        finished.set()
        timer.cancel()
        timer.join()
        connection.close()
    return AnalysisResult(
        run_id=uuid4().hex,
        analysis_id=analysis_id,
        version=definition.version,
        catalog_version=catalog.catalog_version,
        analysis_catalog_sha256=catalog.source_sha256,
        parameters=effective,
        rows=rows,
        row_count=len(rows),
        total_count=total_count,
        truncated=len(rows) < total_count,
        status='completed' if total_count else 'empty_result',
        file_id=metadata.file_id,
        source_sha256=metadata.source_sha256,
        layout_version=metadata.layout_version,
        layout_source_sha256=metadata.layout_source_sha256,
        layout_metadata_sha256=metadata.layout_metadata_sha256,
        row_limit=limit,
        timeout_seconds=seconds,
        elapsed_seconds=elapsed,
        criteria=tuple(definition.criteria),
        limitations=(*definition.limitations, *scope_limitations),
    )
