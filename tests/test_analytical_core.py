"""Regressão das oito análises sobre uma única ingestão sintética."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from shutil import copyfile

import duckdb
import pytest

from agente_sped.application.analysis import (
    AnalysisResult,
    AnalysisUnavailableError,
    execute_analysis,
)
from agente_sped.application.ingestion import analyze_catalogued_file
from agente_sped.application.parsing import parse_sped
from agente_sped.domain.layout import LayoutCatalog
from agente_sped.infra.analysis_catalog import load_analysis_catalog
from agente_sped.infra.layout_catalog import build_layout_catalog
from agente_sped.infra.workspace import materialize_workspace
from agente_sped.interface.cli import main

_FIXTURE = Path(__file__).parent / 'fixtures/sped_icms_analysis_reference.txt'
_MINIMAL = Path(__file__).parent / 'fixtures/cancelled_document_minimal.txt'
_DATA = Path(__file__).parents[1] / 'src/agente_sped/data'
_ORIGINAL_CATALOG_HASH = (
    '66a0a2fa7c8d2e7866b4a5b230910fd441cdb3be5cdcbdcacbad51dd3e31b7fa'
)
_ORIGINAL_FIXTURE_HASH = (
    'a80d8ea3271be3a735e3b14f2af0914408556e8647a2728128dea4929f892307'
)
_ORIGINAL_FIXTURE_BYTES = 9214
_MINIMAL_RECORD_COUNT = 3
_CLI_ERROR = 2
_ANALYSIS_IDS = (
    'movement_by_cfop',
    'cancelled_documents',
    'block_k_activity',
    'supplier_concentration',
    'possible_duplicate_documents',
    'document_sequence_gaps',
    'generic_ncm_high_value',
    'possible_credit_on_consumption_cfop',
)

type ReferenceRun = tuple[Path, LayoutCatalog, Path]


@pytest.fixture(scope='module')
def reference_run(tmp_path_factory: pytest.TempPathFactory) -> ReferenceRun:
    root = tmp_path_factory.mktemp('analytical-core')
    catalog_path = root / 'layout_catalog.duckdb'
    layout = build_layout_catalog(catalog_path)
    parsed = parse_sped(_FIXTURE, layout)
    workspace = materialize_workspace(
        parsed, layout, root / 'workspace', layout_catalog_path=catalog_path
    )
    return workspace.database_path.parent, layout, catalog_path


def _reference_values(result: AnalysisResult) -> dict[str, object]:
    rows = result.rows
    observed: dict[str, object] = {'row_count': result.row_count}
    match result.analysis_id:
        case 'movement_by_cfop':
            observed.update(
                total_operations=rows[0]['total_operacoes'],
                displayed_subtotal=rows[0]['displayed_subtotal'],
                top_cfops=[
                    {'cfop': row['cfop'], 'total': row['total_operacao']}
                    for row in rows
                ],
            )
            assert all(
                isinstance(row['total_operacao'], Decimal) for row in rows
            )
            assert all(
                row['displayed_coverage_percent'] == Decimal('96.29')
                for row in rows
            )
        case 'cancelled_documents':
            observed['document_numbers'] = [row['num_doc'] for row in rows]
        case 'block_k_activity':
            observed.update(
                total_distinct_orders=rows[0]['total_distinct_orders'],
                daily_distinct_orders={
                    row['dt_ini_op'].isoformat(): row['distinct_order_count']
                    for row in rows
                },
            )
            assert all(
                row['total_distinct_orders']
                == rows[0]['total_distinct_orders']
                for row in rows
            )
        case 'supplier_concentration':
            observed.update(
                supplier=rows[0]['cod_part'],
                supplier_total=rows[0]['supplier_total'],
                total_purchases=rows[0]['total_purchases'],
            )
        case 'possible_duplicate_documents':
            observed.update(
                participant=rows[0]['cod_part'],
                series=rows[0]['ser'],
                document_number=rows[0]['num_doc'],
                occurrence_count=rows[0]['occurrence_count'],
            )
        case 'document_sequence_gaps':
            observed['gaps'] = [
                {
                    'series': row['ser'],
                    'first_missing_number': row['first_missing_number'],
                    'last_missing_number': row['last_missing_number'],
                }
                for row in rows
            ]
        case 'generic_ncm_high_value':
            observed.update(
                item_code=rows[0]['cod_item'],
                ncm=rows[0]['cod_ncm'],
                item_value=rows[0]['vl_item'],
            )
        case 'possible_credit_on_consumption_cfop':
            observed.update(
                cfops=sorted(row['cfop'] for row in rows),
                total_icms=sum(row['vl_icms'] for row in rows),
            )
        case _:
            raise AssertionError('Análise sem gabarito de regressão.')
    return observed


@pytest.mark.parametrize('analysis_id', _ANALYSIS_IDS)
def test_catalogued_analysis_matches_its_operational_reference(
    reference_run: ReferenceRun, analysis_id: str
) -> None:
    workspace, _, _ = reference_run
    catalog = load_analysis_catalog()
    expected = catalog.reference_validation['expected'][analysis_id]
    result = execute_analysis(workspace / 'analysis.duckdb', analysis_id)
    assert result.row_count == result.total_count == expected['row_count']
    assert result.truncated is False
    assert result.status == 'completed'
    assert _reference_values(result) == expected
    assert result.catalog_version == '1.1.1'
    assert result.version == {
        'cancelled_documents': '1.1.0',
        'block_k_activity': '1.1.0',
        'possible_duplicate_documents': '1.1.0',
        'document_sequence_gaps': '1.1.1',
    }.get(analysis_id, '1.0.0')
    assert result.source_sha256 == _ORIGINAL_FIXTURE_HASH
    assert result.analysis_catalog_sha256 == catalog.source_sha256
    assert (workspace / 'source.txt').read_bytes() == _FIXTURE.read_bytes()
    assert (workspace / 'layout_catalog.duckdb').is_file()


def test_presentation_limit_keeps_catalog_totals_and_exact_count(
    reference_run: ReferenceRun,
) -> None:
    workspace, _, _ = reference_run
    limit = 2
    expected = load_analysis_catalog().reference_validation['expected'][
        'movement_by_cfop'
    ]
    result = execute_analysis(
        workspace / 'analysis.duckdb', 'movement_by_cfop', row_limit=limit
    )
    assert result.row_count == limit
    assert result.total_count == expected['row_count']
    assert result.truncated is True
    assert [row['cfop'] for row in result.rows] == ['5102', '6102']
    assert result.rows[0]['displayed_subtotal'] == Decimal('130000.00')
    assert result.rows[0]['displayed_coverage_percent'] == Decimal('96.29')


def test_execution_does_not_modify_workspace_database(
    reference_run: ReferenceRun,
) -> None:
    workspace, _, _ = reference_run
    database = workspace / 'analysis.duckdb'
    before = hashlib.sha256(database.read_bytes()).digest()
    execute_analysis(database, 'movement_by_cfop', {'top_n': 1})
    assert hashlib.sha256(database.read_bytes()).digest() == before


def test_cli_rejects_invalid_parameters_without_consuming_workspace(
    reference_run: ReferenceRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, catalog_path = reference_run
    monkeypatch.setattr(
        'agente_sped.application.ingestion.build_layout_catalog',
        lambda destination: copyfile(catalog_path, destination),
    )
    for name in (
        'GOOGLE_API_KEY',
        'GEMINI_API_KEY',
        'AI_ROUTING_ENABLED',
        'AI_NARRATIVE_ENABLED',
        'EXPLORATORY_SQL_ENABLED',
    ):
        monkeypatch.delenv(name, raising=False)
    workspace = tmp_path / 'workspace'
    arguments = [
        'analyze',
        str(_FIXTURE),
        '--analysis',
        'movement_by_cfop',
        '--workspace',
        str(workspace),
    ]
    assert main([*arguments, '--top-n', '0']) == _CLI_ERROR
    rejected = capsys.readouterr()
    assert rejected.out == ''
    assert 'Parâmetros da análise inválidos' in rejected.err
    assert not workspace.exists()
    code = main(arguments)
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ''
    result = json.loads(captured.out)
    assert result['rows'][0]['total_operacoes'] == '135015.00'
    assert result['rows'][0]['displayed_coverage_percent'] == '96.29'
    assert result['analysis_id'] == 'movement_by_cfop'


def test_existing_workspace_is_preserved(reference_run: ReferenceRun) -> None:
    workspace, _, _ = reference_run
    original = (workspace / 'source.txt').read_bytes()
    database = workspace / 'analysis.duckdb'
    original_database_hash = hashlib.sha256(database.read_bytes()).digest()
    with pytest.raises(FileExistsError):
        analyze_catalogued_file(_FIXTURE, workspace, 'movement_by_cfop')
    assert (workspace / 'source.txt').read_bytes() == original
    assert (
        hashlib.sha256(database.read_bytes()).digest()
        == original_database_hash
    )


def test_research_snapshot_and_original_fixture_remain_unchanged() -> None:
    original = _DATA / 'research/analysis_catalog.original.yml'
    provenance = json.loads(
        (_DATA / 'research/catalog_provenance.json').read_bytes()
    )
    assert (
        hashlib.sha256(original.read_bytes()).hexdigest()
        == provenance['original_catalog']['sha256']
        == _ORIGINAL_CATALOG_HASH
    )
    assert (
        hashlib.sha256(_FIXTURE.read_bytes()).hexdigest()
        == provenance['original_fixture']['sha256']
        == _ORIGINAL_FIXTURE_HASH
    )
    assert len(_FIXTURE.read_bytes()) == _ORIGINAL_FIXTURE_BYTES
    assert (
        hashlib.sha256(
            (_DATA / 'analysis_catalog.yml').read_bytes()
        ).hexdigest()
        == (provenance['operational_catalog']['sha256'])
    )
    assert (
        len((_DATA / 'analysis_catalog.yml').read_bytes())
        == (provenance['operational_catalog']['byte_length'])
    )
    assert provenance['operational_catalog']['catalog_version'] == '1.1.1'


def test_sequence_gaps_preserves_and_ignores_irrelevant_invalid_date(
    reference_run: ReferenceRun,
    tmp_path: Path,
) -> None:
    _, layout, catalog_path = reference_run
    lines = _FIXTURE.read_bytes().splitlines(keepends=True)
    for index, line in enumerate(lines):
        fields = line.split(b'|')
        if fields[1:2] == [b'C100'] and fields[8] == b'6002':
            fields[10] = b'31022026'
            lines[index] = b'|'.join(fields)
            break
    else:
        raise AssertionError('Documento de referência não encontrado.')
    source = tmp_path / 'invalid-document-date.txt'
    source.write_bytes(b''.join(lines))
    parsed = parse_sped(source, layout)
    cancelled = next(
        record
        for record in parsed.records
        if record.record_code == 'C100' and record.raw_values[8] == '6002'
    )
    assert cancelled.raw_values[10] == '31022026'
    assert cancelled.typed_values[10] is None
    assert cancelled.field_errors == {10: 'invalid_date'}
    workspace = materialize_workspace(
        parsed,
        layout,
        tmp_path / 'invalid-date-workspace',
        layout_catalog_path=catalog_path,
    )
    result = execute_analysis(
        workspace.database_path, 'document_sequence_gaps'
    )
    catalog = load_analysis_catalog()
    assert (
        _reference_values(result)
        == (catalog.reference_validation['expected']['document_sequence_gaps'])
    )
    assert result.catalog_version == result.version == '1.1.1'
    with duckdb.connect(
        str(workspace.database_path), read_only=True
    ) as database:
        assert database.execute(
            'SELECT position, error_code FROM field_errors '
            'WHERE record_id = ?',
            [cancelled.record_id],
        ).fetchall() == [(10, 'invalid_date')]
    for analysis_id in ('cancelled_documents', 'movement_by_cfop'):
        with pytest.raises(
            AnalysisUnavailableError, match='campos necessários'
        ):
            execute_analysis(workspace.database_path, analysis_id)


def test_cancelled_record_preserves_exception_fields_and_absent_values(
    reference_run: ReferenceRun,
    tmp_path: Path,
) -> None:
    _, layout, catalog_path = reference_run
    parsed = parse_sped(_MINIMAL, layout)
    assert len(parsed.records) == _MINIMAL_RECORD_COUNT
    assert [record.record_code for record in parsed.records] == [
        '0000',
        'C001',
        'C100',
    ]
    cancelled = parsed.records[-1]
    assert {
        position for position, value in cancelled.raw_values.items() if value
    } == {1, 2, 3, 5, 6, 7, 8, 9}
    assert cancelled.raw_values[10] == cancelled.raw_values[12] == ''
    assert cancelled.typed_values[10] is None
    assert cancelled.typed_values[12] is None
    assert not cancelled.field_errors
    assert not cancelled.structural_errors
    workspace = materialize_workspace(
        parsed, layout, tmp_path / 'minimal', layout_catalog_path=catalog_path
    )
    result = execute_analysis(workspace.database_path, 'cancelled_documents')
    assert result.row_count == result.total_count == 1
    assert result.truncated is False
    assert result.parameters['start_date'] == parsed.period_start
    assert result.parameters['end_date'] == parsed.period_end
    row = result.rows[0]
    assert row['num_doc'] == '9001'
    assert row['cod_sit'] == '02'
    assert row['document_status'] == 'Cancelado'
    assert row['dt_doc'] is None
    assert row['vl_doc'] is None
    assert row['cod_part'] is None
    assert row['participant_name'] is None
