"""Exporte somente o snapshot identificado, sem consultar o workspace."""

import csv
import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4

import xlsxwriter
from xlsxwriter.worksheet import Worksheet

from agente_sped.domain.records import Value
from agente_sped.domain.reports import (
    ExportReceipt,
    ReportColumn,
    ReportPackage,
)
from agente_sped.infra.report_store import load_report, private_directory

_EXCEL_CELL_LIMIT = 32767
_COUNT_COLUMNS = {
    'distinct_order_count',
    'record_count',
    'total_distinct_orders',
    'document_count',
    'occurrence_count',
    'distinct_access_key_count',
    'distinct_status_count',
    'missing_count',
}
_FORMULA_STARTS = '=+-@＝＋－＠'


def _text(value: object) -> str:
    if value is None:
        return ''
    if isinstance(value, Decimal):
        return format(value, 'f')
    if type(value) is date:
        return value.isoformat()
    return str(value)


def _json_value(value: object) -> str:
    if isinstance(value, Decimal) or type(value) is date:
        return _text(value)
    raise ValueError('Tipo de contexto não serializável.')


def _projection(
    report: ReportPackage, show_sensitive: bool
) -> tuple[dict[str, Value], ...]:
    return tuple(
        {
            name: '***'
            if not show_sensitive
            and name in report.sensitive_columns
            and value is not None
            else value
            for name, value in row.items()
        }
        for row in report.result.rows
    )


def _context(report: ReportPackage, raw: bool) -> dict[str, object]:
    context = asdict(report.result)
    context.pop('rows')
    context.update(
        report_schema_version=report.schema_version,
        title=report.title,
        created_at=report.created_at,
        summary=report.summary,
        columns=[asdict(column) for column in report.columns],
        privacy_mode='raw' if raw else 'masked',
        decimal_policy='texto decimal exato; sem conversão para float',
        identifier_policy='texto no XLSX; CSV não declara tipos de célula',
        null_policy='célula vazia; o snapshot preserva None',
        exported_rows=report.result.row_count,
        complete_result=not report.result.truncated,
    )
    return context


def _csv_export(
    path: Path, report: ReportPackage, rows: tuple[dict[str, Value], ...]
) -> int:
    transformed = 0
    with path.open('x', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
        writer.writerow([column.name for column in report.columns])
        for row in rows:
            cells = []
            for column in report.columns:
                value = row[column.name]
                text = _text(value)
                if isinstance(value, str) and (
                    text.startswith(('\t', '\r', '\n'))
                    or text.lstrip(' \t\r\n').startswith(
                        tuple(_FORMULA_STARTS)
                    )
                ):
                    text = '\t' + text
                    transformed += 1
                cells.append(text)
            writer.writerow(cells)
    return transformed


def _write_string(sheet: Worksheet, row: int, column: int, value: str) -> None:
    if len(value) > _EXCEL_CELL_LIMIT:
        raise ValueError('Texto excede o limite de célula do Excel.')
    if sheet.write_string(row, column, value) != 0:
        raise ValueError('Falha ao escrever célula de texto no Excel.')


def _write_value(
    sheet: Worksheet, row: int, index: int, value: Value, column: ReportColumn
) -> None:
    if value is None:
        return
    if type(value) is int and column.name in _COUNT_COLUMNS:
        if abs(value) >= 10**15:
            raise ValueError('Contagem excede a precisão numérica do Excel.')
        if sheet.write_number(row, index, value) != 0:
            raise ValueError('Falha ao escrever contagem no Excel.')
    else:
        _write_string(sheet, row, index, _text(value))


def _xlsx_export(
    path: Path,
    report: ReportPackage,
    rows: tuple[dict[str, Value], ...],
    context: dict[str, object],
) -> None:
    with (
        path.open('xb') as handle,
        xlsxwriter.Workbook(
            handle,
            {
                'in_memory': True,
                'strings_to_formulas': False,
                'strings_to_urls': False,
                'strings_to_numbers': False,
            },
        ) as workbook,
    ):
        data = workbook.add_worksheet('Dados')
        for index, column in enumerate(report.columns):
            _write_string(data, 0, index, column.label)
        for row_index, values in enumerate(rows, start=1):
            for column_index, column in enumerate(report.columns):
                _write_value(
                    data, row_index, column_index, values[column.name], column
                )
        data.freeze_panes(1, 0)
        metadata = workbook.add_worksheet('Contexto')
        for row_index, (key, value) in enumerate(context.items()):
            _write_string(metadata, row_index, 0, key)
            text = json.dumps(value, ensure_ascii=False, default=_json_value)
            _write_string(metadata, row_index, 1, text)


def export_report(
    workspace_dir: Path,
    run_id: str,
    format: Literal['csv', 'xlsx'],
    *,
    show_sensitive: bool = False,
) -> ExportReceipt:
    if format not in ('csv', 'xlsx') or type(show_sensitive) is not bool:
        raise ValueError('Formato ou modo de exportação inválido.')
    report = load_report(workspace_dir, run_id)
    directory = private_directory(workspace_dir, 'exports')
    identity = uuid4().hex
    mode = 'raw' if show_sensitive else 'masked'
    path = directory / f'{run_id}.{mode}.{identity}.{format}'
    context_path = (
        path.with_suffix('.context.json') if format == 'csv' else None
    )
    rows = _projection(report, show_sensitive)
    context = _context(report, show_sensitive)
    transformed = 0
    try:
        if format == 'csv':
            transformed = _csv_export(path, report, rows)
            context.update(
                formula_policy=(
                    'Textos iniciados por fórmula/controle recebem tabulação '
                    'dentro do campo entre aspas. '
                    'Isso altera o texto exportado; '
                    'não há garantia universal entre editores de planilha.'
                ),
                transformed_cells=transformed,
                csv_type_note=(
                    'Importe códigos como texto. Aspas CSV não impedem '
                    'conversões automáticas de tipo pelo Excel.'
                ),
            )
            with context_path.open('x', encoding='utf-8') as handle:
                json.dump(
                    context,
                    handle,
                    default=_json_value,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
        else:
            context.update(
                formula_policy='Todos os textos são células literais.'
            )
            _xlsx_export(path, report, rows, context)
    except BaseException:
        path.unlink(missing_ok=True)
        if context_path is not None:
            context_path.unlink(missing_ok=True)
        raise
    return ExportReceipt(
        run_id=run_id,
        format=format,
        data_path=path,
        context_path=context_path,
        row_count=report.result.row_count,
        total_count=report.result.total_count,
        truncated=report.result.truncated,
        privacy_mode=mode,
        transformed_cells=transformed,
    )
