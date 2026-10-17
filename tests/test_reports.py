"""Relatórios tipados e exportação sem nova consulta analítica."""

import csv
import hashlib
import json
import tempfile
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from xlsxwriter.exceptions import FileCreateError

from agente_sped.application import analysis
from agente_sped.application.analysis import AnalysisResult
from agente_sped.application.reports import build_report, build_suggestions
from agente_sped.domain.reports import MAX_NARRATIVE_BYTES
from agente_sped.infra import report_store
from agente_sped.infra.analysis_catalog import load_analysis_catalog
from agente_sped.infra.exports import export_report
from agente_sped.infra.report_store import load_report, save_report

_CATALOG = load_analysis_catalog()
_LONG_DECIMAL = Decimal('1234567890123456.78')
_NAMESPACE = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def _result(
    analysis_id='possible_duplicate_documents',
    *,
    count=1,
    total=None,
    values=None,
    extra_limitations=(),
):
    definition = _CATALOG.analyses[analysis_id]
    defaults = {
        'string': '00123ABC',
        'integer': 1,
        'decimal': Decimal('1.00'),
        'date': date(2026, 1, 5),
    }
    row = {
        column.name: defaults[column.type]
        for column in definition.result.columns
    }
    row.update(values or {})
    parameters = {
        name: parameter.default
        for name, parameter in definition.parameters.items()
    }
    parameters.update(start_date=date(2026, 1, 1), end_date=date(2026, 1, 31))
    total = count if total is None else total
    return AnalysisResult(
        run_id=uuid4().hex,
        analysis_id=analysis_id,
        version=definition.version,
        catalog_version=_CATALOG.catalog_version,
        analysis_catalog_sha256=_CATALOG.source_sha256,
        parameters=parameters,
        rows=tuple(dict(row) for _ in range(count)),
        row_count=count,
        total_count=total,
        truncated=count < total,
        status='completed' if total else 'empty_result',
        file_id='synthetic',
        source_sha256='a' * 64,
        layout_version='020',
        layout_source_sha256='b' * 64,
        layout_metadata_sha256='c' * 64,
        row_limit=definition.result.row_limit,
        timeout_seconds=30.0,
        elapsed_seconds=0.01,
        criteria=tuple(definition.criteria),
        limitations=(*definition.limitations, *extra_limitations),
    )


def _report(workspace, **kwargs):
    return build_report(_result(**kwargs), _CATALOG, workspace_dir=workspace)


def _sheet_cells(path):
    with ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read('xl/sharedStrings.xml'))
        strings = [''.join(item.itertext()) for item in root]
        sheet = ElementTree.fromstring(
            archive.read('xl/worksheets/sheet1.xml')
        )
        result = {}
        for cell in sheet.findall('.//x:c', _NAMESPACE):
            value = cell.find('x:v', _NAMESPACE)
            if value is not None:
                kind = cell.attrib.get('t', 'n')
                result[cell.attrib['r']] = (
                    kind,
                    strings[int(value.text)] if kind == 's' else value.text,
                )
        assert not sheet.findall('.//x:f', _NAMESPACE)
        assert not sheet.findall('.//x:hyperlink', _NAMESPACE)
        return result


@pytest.mark.parametrize('analysis_id', _CATALOG.analysis_ids)
def test_each_catalogued_analysis_has_a_typed_snapshot_and_summary(
    tmp_path, analysis_id
):
    result = _result(analysis_id)
    report = build_report(result, _CATALOG, workspace_dir=tmp_path)
    assert report.result.analysis_id == analysis_id
    assert report.result.criteria == result.criteria
    assert report.result.limitations == result.limitations
    assert report.result.rows == result.rows
    assert report.summary
    save_report(tmp_path, report)
    assert load_report(tmp_path, result.run_id) == report


@pytest.mark.parametrize(
    ('total_operations', 'subtotal', 'coverage', 'coverage_text'),
    [
        (
            Decimal('135015.00'),
            Decimal('130000.00'),
            Decimal('96.29'),
            '96.29%',
        ),
        (Decimal('0.00'), Decimal('0.00'), None, 'não disponível'),
    ],
)
def test_p1_catalog_subtotal_is_distinct_from_saved_rows(
    tmp_path, total_operations, subtotal, coverage, coverage_text
):
    result = _result(
        'movement_by_cfop',
        total=3,
        values={
            'total_operacoes': total_operations,
            'displayed_subtotal': subtotal,
            'displayed_coverage_percent': coverage,
        },
    )
    report = build_report(result, _CATALOG, workspace_dir=tmp_path)
    assert (
        f'Subtotal do recorte de CFOPs catalogado: {subtotal}'
        in report.summary
    )
    assert f'cobertura: {coverage_text}.' in report.summary
    assert '1 de 3 linhas' in report.summary
    assert report.result.rows[0]['displayed_subtotal'] == subtotal
    assert report.result.rows[0]['displayed_coverage_percent'] == coverage


def test_empty_result_keeps_dynamic_indetermination(tmp_path):
    warning = (
        'No arquivo, 2 K230 sem data impedem concluir ausência de atividade.'
    )
    result = _result('block_k_activity', count=0, extra_limitations=(warning,))
    report = build_report(result, _CATALOG, workspace_dir=tmp_path)
    assert _CATALOG.analyses['block_k_activity'].result.empty_message in (
        report.summary
    )
    assert warning in report.summary
    assert warning in report.narrative_payload.limitations


def test_narrative_is_masked_bounded_and_does_not_mutate_source(tmp_path):
    result = _result(
        'cancelled_documents',
        count=30,
        total=50,
        values={
            'num_doc': '0009001',
            'cod_mod': 'x' * 10000,
        },
    )
    report = build_report(result, _CATALOG, workspace_dir=tmp_path)
    payload = report.narrative_payload
    assert payload.returned_rows < len(result.rows)
    assert payload.total_rows == result.total_count
    assert payload.result_rows == result.row_count
    assert payload.truncated is True
    assert all(row['num_doc'] == '***' for row in payload.rows)
    assert result.rows[0]['num_doc'] == '0009001'
    assert len(report_store.encode_report_value(asdict(payload))) <= (
        MAX_NARRATIVE_BYTES
    )


def test_export_reuses_snapshot_without_database_catalog_or_executor(
    tmp_path,
    monkeypatch,
):
    report = _report(tmp_path)
    save_report(tmp_path, report)

    def forbidden(*args, **kwargs):
        raise AssertionError(
            'Exportação não pode recalcular ou carregar catálogo'
        )

    monkeypatch.setattr(analysis, 'execute_analysis', forbidden)
    monkeypatch.setattr(
        'agente_sped.infra.analysis_catalog.load_analysis_catalog', forbidden
    )
    assert not (tmp_path / 'analysis.duckdb').exists()
    receipt = export_report(tmp_path, report.run_id, 'csv')
    assert receipt.data_path.is_file()
    assert receipt.context_path.is_file()
    assert load_report(tmp_path, report.run_id) == report


def test_csv_masks_by_default_and_records_formula_transformations(tmp_path):
    report = _report(
        tmp_path,
        values={
            'cod_part': '=1+1',
            'num_doc': '0009001',
            'minimum_document_value': _LONG_DECIMAL,
        },
    )
    save_report(tmp_path, report)
    masked = export_report(tmp_path, report.run_id, 'csv')
    raw = export_report(tmp_path, report.run_id, 'csv', show_sensitive=True)
    with masked.data_path.open(encoding='utf-8-sig', newline='') as handle:
        row = next(csv.DictReader(handle))
        assert row['cod_part'] == row['num_doc'] == '***'
    with raw.data_path.open(encoding='utf-8-sig', newline='') as handle:
        row = next(csv.DictReader(handle))
        assert row['cod_part'] == '\t=1+1'
        assert row['num_doc'] == '0009001'
        assert row['minimum_document_value'] == str(_LONG_DECIMAL)
    context = json.loads(raw.context_path.read_text(encoding='utf-8'))
    assert context['transformed_cells'] == raw.transformed_cells == 1
    assert context['privacy_mode'] == 'raw'
    assert context['source_sha256'] == report.result.source_sha256
    assert (
        load_report(tmp_path, report.run_id).result.rows[0]['cod_part']
        == '=1+1'
    )


def test_xlsx_keeps_decimal_and_ids_as_text_and_counts_numeric(tmp_path):
    report = _report(
        tmp_path,
        values={
            'cod_part': '=1+1',
            'num_doc': '0009001',
            'minimum_document_value': _LONG_DECIMAL,
            'occurrence_count': 2,
        },
    )
    save_report(tmp_path, report)
    receipt = export_report(
        tmp_path, report.run_id, 'xlsx', show_sensitive=True
    )
    cells = _sheet_cells(receipt.data_path)
    columns = {
        column.name: index for index, column in enumerate(report.columns)
    }

    def cell(name):
        return cells[f'{chr(65 + columns[name])}2']

    assert cell('cod_part') == ('s', '=1+1')
    assert cell('num_doc') == ('s', '0009001')
    assert cell('minimum_document_value') == ('s', str(_LONG_DECIMAL))
    assert cell('occurrence_count') == ('n', '2')
    assert cell('dt_doc') == ('s', '2026-01-05')
    assert receipt.context_path is None


def test_xlsx_refuses_cell_truncation_and_removes_failed_artifact(tmp_path):
    report = _report(tmp_path, values={'cod_part': 'x' * 32768})
    save_report(tmp_path, report)
    with pytest.raises(ValueError, match='limite de célula'):
        export_report(tmp_path, report.run_id, 'xlsx', show_sensitive=True)
    assert list((tmp_path / 'exports').iterdir()) == []


def test_xlsx_packaging_failure_closes_output_without_temporary_xml(
    tmp_path,
    monkeypatch,
):
    report = _report(tmp_path, values={'cod_part': 'synthetic-private-value'})
    save_report(tmp_path, report)
    zip_handles = []

    def no_temporary_xml(*args, **kwargs):
        raise AssertionError('XLSX não deve criar XML temporário em disco.')

    def fail_packaging(archive, *args, **kwargs):
        zip_handles.append((archive, archive.fp))
        raise OSError('synthetic-disk-failure')

    monkeypatch.setattr(tempfile, 'mkstemp', no_temporary_xml)
    monkeypatch.setattr(ZipFile, 'writestr', fail_packaging)
    with pytest.raises(
        FileCreateError, match='synthetic-disk-failure'
    ) as error:
        export_report(tmp_path, report.run_id, 'xlsx', show_sensitive=True)
    # Keep the failed SDK traceback alive while checking application cleanup.
    assert error.value.__traceback__ is not None
    assert zip_handles
    assert all(handle.closed for _, handle in zip_handles)
    assert list((tmp_path / 'exports').iterdir()) == []
    # The SDK retains the ZipFile on its traceback; release this injected
    # failure's in-memory objects after checking the owned OS descriptor.
    for archive, _ in zip_handles:
        with suppress(ValueError):
            archive.close()


@pytest.mark.parametrize('invalidity', ['checksum', 'type'])
def test_load_rejects_corrupted_or_wrongly_typed_snapshot(
    tmp_path, invalidity
):
    report = _report(tmp_path)
    path = save_report(tmp_path, report)
    envelope = json.loads(path.read_bytes())
    if invalidity == 'checksum':
        envelope['report']['summary'] = 'alterado'
    else:
        envelope['report']['result']['row_count'] = True
        envelope['sha256'] = hashlib.sha256(
            report_store.encode_report_value(envelope['report'])
        ).hexdigest()
    path.write_text(json.dumps(envelope), encoding='utf-8')
    with pytest.raises(ValueError, match='Integridade|Contagens'):
        load_report(tmp_path, report.run_id)


def test_store_rejects_traversal_junction_and_oversized_snapshot(
    tmp_path,
    monkeypatch,
):
    report = _report(tmp_path)
    with pytest.raises(ValueError, match='UUID'):
        load_report(tmp_path, '../outside')
    original = Path.is_junction
    monkeypatch.setattr(Path, 'is_junction', lambda p: p.name == 'results')
    with pytest.raises(ValueError, match='inseguro'):
        save_report(tmp_path, report)
    monkeypatch.setattr(Path, 'is_junction', original)
    monkeypatch.setattr(report_store, 'MAX_REPORT_BYTES', 100)
    with pytest.raises(ValueError, match='limite de bytes'):
        save_report(tmp_path, report)


def test_same_run_cannot_replace_authoritative_result(tmp_path):
    report = _report(tmp_path)
    save_report(tmp_path, report)
    conflict = replace(
        report, result=replace(report.result, source_sha256='d' * 64)
    )
    with pytest.raises(ValueError, match='outro snapshot'):
        save_report(tmp_path, conflict)
    assert load_report(tmp_path, report.run_id) == report


def test_suggestions_inspect_availability_without_executing_targets(
    tmp_path,
    monkeypatch,
):
    result = _result('movement_by_cfop')
    result = replace(
        result,
        parameters=dict(
            result.parameters,
            start_date=date(2026, 1, 10),
            end_date=date(2026, 1, 15),
        ),
    )
    report = build_report(result, _CATALOG, workspace_dir=tmp_path)
    save_report(tmp_path, report)
    profile = SimpleNamespace(
        file_id=result.file_id,
        source_sha256=result.source_sha256,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        layout_version=result.layout_version,
        layout_source_sha256=result.layout_source_sha256,
        layout_metadata_sha256=result.layout_metadata_sha256,
    )
    calls = []
    monkeypatch.setattr(analysis, 'get_workspace_profile', lambda _: profile)

    def inspect(path, analysis_id, parameters, *, catalog):
        calls.append(analysis_id)
        return SimpleNamespace(
            available=analysis_id == 'cancelled_documents',
            reason=None,
            parameters=parameters,
        )

    monkeypatch.setattr(analysis, 'inspect_analysis_availability', inspect)
    updated = build_suggestions(
        report, workspace_dir=tmp_path, catalog=_CATALOG
    )
    assert report.suggestions == ()
    assert len(calls) == len(_CATALOG.p1.suggestions)
    assert len(updated.suggestions) == 1
    suggestion = updated.suggestions[0]
    assert suggestion.analysis_id == 'cancelled_documents'
    assert suggestion.parameters == {
        'start_date': profile.period_start,
        'end_date': profile.period_end,
    }
    assert 'período integral' in suggestion.label
    save_report(tmp_path, updated)
    assert load_report(tmp_path, report.run_id) == updated
