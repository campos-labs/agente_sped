"""Synthetic ingestion checks: precision, provenance and safe failures."""

import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from agente_sped.application.parsing import (
    DEFAULT_MAX_FILE_BYTES,
    FileSizeLimitError,
    parse_sped,
)
from agente_sped.domain.layout import LayoutCatalog
from agente_sped.infra.layout_catalog import (
    build_layout_catalog,
    load_layout_catalog,
)
from agente_sped.infra.workspace import materialize_workspace

_REFERENCE_RECORD_COUNT = 146
_JANUARY_START = date(2026, 1, 1)
_JANUARY_END = date(2026, 1, 31)
_RELATIONS = ('c100', 'c190', 'c170', '0200', '0150', 'k230')
_EXPECTED_PARENTS = {
    '0001': '0000',
    '0150': '0001',
    '0200': '0001',
    'C001': '0000',
    'C100': 'C001',
    'C170': 'C100',
    'C190': 'C100',
    'K001': '0000',
    'K100': 'K001',
    'K230': 'K100',
}


@pytest.fixture(scope='module')
def compiled_catalog(tmp_path_factory):
    path = tmp_path_factory.mktemp('parsing-layout') / 'layout.duckdb'
    build_layout_catalog(path)
    return path


@pytest.fixture(scope='module')
def layout(compiled_catalog):
    return load_layout_catalog(compiled_catalog)


def _line(
    layout: LayoutCatalog, code: str, values: dict[int, str] | None = None
) -> str:
    values = values or {}
    definition = layout.records.get(code)
    maximum = max(
        [1, *values, *(f.position for f in definition.fields)]
        if definition
        else [1, *values]
    )
    parts = [code, *(values.get(i, '') for i in range(2, maximum + 1))]
    return '|' + '|'.join(parts) + '|'


def _source(
    tmp_path: Path,
    layout: LayoutCatalog,
    body: list[str],
    *,
    header: dict[int, str] | None = None,
) -> Path:
    head = {2: '020', 4: '01012026', 5: '31012026', 7: 'AB1234560001CD'}
    head.update(header or {})
    lines = [_line(layout, '0000', head), *body]
    path = tmp_path / 'synthetic.txt'
    path.write_bytes(('\r\n'.join(lines) + '\r\n').encode('latin-1'))
    return path


def _movement(
    layout: LayoutCatalog,
    *,
    amount: str = '1234567890123456,78',
    document_date: str = '02012026',
) -> list[str]:
    return [
        _line(layout, 'C001', {2: '0'}),
        _line(
            layout,
            'C100',
            {
                2: '0',
                3: '1',
                4: ' Órgão 0001 ',
                6: '00',
                10: document_date,
                12: amount,
            },
        ),
        _line(layout, 'C190', {2: '000', 3: '0102', 5: amount}),
    ]


def _production(layout: LayoutCatalog, quantity: str) -> list[str]:
    return [
        _line(layout, 'K001', {2: '0'}),
        _line(layout, 'K100', {2: '01012026', 3: '31012026'}),
        _line(
            layout,
            'K230',
            {
                2: '02012026',
                4: '000OP',
                5: '000ITEM',
                6: quantity,
            },
        ),
    ]


def test_reference_preserves_all_records_and_direct_parents(layout):
    path = Path(__file__).parent / 'fixtures/sped_icms_analysis_reference.txt'
    parsed = parse_sped(path, layout, 'reference-file')
    assert len(parsed.records) == _REFERENCE_RECORD_COUNT
    assert [r.record_id for r in parsed.records] == list(
        range(1, _REFERENCE_RECORD_COUNT + 1)
    )
    assert parsed.source_bytes == path.read_bytes()
    assert (
        parsed.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    )
    assert parsed.period_start == _JANUARY_START
    assert parsed.period_end == _JANUARY_END
    lookup = {r.record_id: r for r in parsed.records}
    current_document = None
    for record in parsed.records:
        assert record.source_line == record.record_id
        if record.record_code in _EXPECTED_PARENTS:
            expected = _EXPECTED_PARENTS[record.record_code]
            assert record.parent_record_code == expected
            assert lookup[record.parent_record_id].record_code == expected
            assert record.structural_errors == ()
        if record.record_code == 'C100':
            current_document = record
            assert record.parent_record_code == 'C001'
        if record.record_code == 'C190':
            assert record.parent_record_code == 'C100'
            assert lookup[record.parent_record_id] is current_document
            assert record.structural_errors == ()


def test_latin1_strings_decimal_and_raw_are_independent(tmp_path, layout):
    path = _source(tmp_path, layout, _movement(layout))
    parsed = parse_sped(path, layout)
    header, _, document, analytical = parsed.records
    assert header.typed_values[7] == 'AB1234560001CD'
    assert document.source_text.encode('latin-1') in path.read_bytes()
    assert document.raw_values[4] == ' Órgão 0001 '
    assert document.typed_values[4] == ' Órgão 0001 '
    assert document.raw_values[12] == '1234567890123456,78'
    assert document.typed_values[12] == Decimal('1234567890123456.78')
    assert analytical.typed_values[3] == '0102'
    assert document.raw_values is not document.typed_values
    document.typed_values[12] = None
    assert document.raw_values[12] == '1234567890123456,78'


@pytest.mark.parametrize(
    ('amount', 'error'),
    [
        ('1.234,56', 'invalid_decimal'),
        ('1.23', 'invalid_decimal'),
        (' 1,23', 'invalid_decimal'),
        ('NaN', 'invalid_decimal'),
        ('-1,23', 'invalid_decimal'),
        ('1,234', 'decimal_scale_exceeded'),
    ],
)
def test_invalid_decimal_never_becomes_zero(tmp_path, layout, amount, error):
    parsed = parse_sped(
        _source(tmp_path, layout, _movement(layout, amount=amount)), layout
    )
    record = parsed.records[-1]
    assert record.raw_values[5] == amount
    assert record.typed_values[5] is None
    assert record.field_errors[5] == error


def test_real_dates_and_empty_values_without_pva_required_rules(
    tmp_path, layout
):
    parsed = parse_sped(
        _source(
            tmp_path,
            layout,
            _movement(layout, amount='', document_date='31022026'),
        ),
        layout,
    )
    document, analytical = parsed.records[-2:]
    assert document.raw_values[10] == '31022026'
    assert document.typed_values[10] is None
    assert document.field_errors == {10: 'invalid_date'}
    assert analytical.typed_values[5] is None
    assert analytical.field_errors == {}


@pytest.mark.parametrize(
    ('header', 'error'),
    [
        ({2: '019'}, 'unsupported_layout_version'),
        ({2: '020 '}, 'unsupported_layout_version'),
        ({4: '31022026'}, 'invalid_file_period'),
        ({4: '01022026'}, 'invalid_file_period'),
    ],
)
def test_rejects_unsupported_or_invalid_header(
    tmp_path, layout, header, error
):
    path = _source(tmp_path, layout, [], header=header)
    with pytest.raises(ValueError, match=error):
        parse_sped(path, layout)


def test_duplicate_header_rejected(tmp_path, layout):
    path = _source(
        tmp_path,
        layout,
        [_line(layout, '0000', {2: '020', 4: '01012026', 5: '31012026'})],
    )
    with pytest.raises(ValueError, match='invalid_file_header'):
        parse_sped(path, layout)


def test_size_limit_applied_to_bytes_actually_read(tmp_path, layout):
    path = _source(tmp_path, layout, _movement(layout))
    limit = 10
    assert DEFAULT_MAX_FILE_BYTES == 100 * 1024 * 1024
    with pytest.raises(
        FileSizeLimitError, match='file_size_limit_exceeded'
    ) as error:
        parse_sped(path, layout, max_file_bytes=limit)
    assert error.value.max_file_bytes == limit
    assert (
        parse_sped(
            path, layout, max_file_bytes=len(path.read_bytes())
        ).source_bytes
        == path.read_bytes()
    )


def test_duplicate_names_preserve_distinct_positions_without_interpretation(
    tmp_path, layout
):
    path = _source(
        tmp_path,
        layout,
        [
            _line(layout, 'E310', {8: '1,23', 16: '9,87'}),
            _line(layout, 'C170', {27: '0,65', 29: '1,23'}),
        ],
    )
    _, e310, c170 = parse_sped(path, layout).records
    assert e310.raw_values[8] == '1,23'
    assert e310.raw_values[16] == '9,87'
    assert e310.typed_values == {}
    assert c170.raw_values[27] == '0,65'
    assert c170.raw_values[29] == '1,23'
    assert c170.typed_values[1] == 'C170'
    assert c170.typed_values.keys().isdisjoint({27, 29})


def test_unknown_record_breaks_ambiguous_hierarchy(tmp_path, layout):
    body = _movement(layout)
    body.insert(-1, '|Z999|opaque|')
    parsed = parse_sped(_source(tmp_path, layout, body), layout)
    unknown, analytical = parsed.records[-2:]
    assert unknown.raw_values[2] == 'opaque'
    assert unknown.typed_values == {}
    assert unknown.parent_record_id is None
    assert unknown.structural_errors == ('unknown_record',)
    assert 'unexpected_parent' in analytical.structural_errors
    assert analytical.parent_record_code != 'C100'


def test_c190_after_wrong_parent_is_anomaly(tmp_path, layout):
    body = [
        _line(layout, 'C001', {2: '0'}),
        _line(layout, 'C190', {3: '5102', 5: '10,00'}),
    ]
    analytical = parse_sped(_source(tmp_path, layout, body), layout).records[
        -1
    ]
    assert 'unexpected_parent' in analytical.structural_errors


def test_workspace_retains_source_catalog_and_exact_decimal(
    tmp_path, layout, compiled_catalog
):
    source = _source(tmp_path, layout, _movement(layout))
    parsed = parse_sped(source, layout, 'stable-id')
    destination = tmp_path / 'workspace'
    workspace = materialize_workspace(
        parsed, layout, destination, layout_catalog_path=compiled_catalog
    )
    assert (destination / 'source.txt').read_bytes() == source.read_bytes()
    assert (destination / 'layout_catalog.duckdb').read_bytes() == (
        compiled_catalog.read_bytes()
    )
    connection = duckdb.connect(str(workspace.database_path), read_only=True)
    try:
        assert connection.execute('SELECT vl_opr FROM C190').fetchone() == (
            Decimal('1234567890123456.78'),
        )
        raw, typed = connection.execute(
            'SELECT raw_values, typed_values FROM raw_records '
            "WHERE record_code='C190'"
        ).fetchone()
        assert json.loads(raw)['5'] == '1234567890123456,78'
        assert json.loads(typed)['5'] == '1234567890123456.78'
        assert connection.execute(
            'SELECT period_start, period_end, layout_source_sha256, '
            'layout_metadata_sha256 FROM workspace_metadata'
        ).fetchone() == (
            _JANUARY_START,
            _JANUARY_END,
            layout.source_sha256,
            layout.metadata_sha256,
        )
        assert connection.execute(
            'SELECT cfop, parent_record_id FROM C190'
        ).fetchone() == ('0102', parsed.records[-2].record_id)
    finally:
        connection.close()


def test_workspace_materializes_empty_relations(tmp_path, layout):
    parsed = parse_sped(_source(tmp_path, layout, []), layout)
    workspace = materialize_workspace(parsed, layout, tmp_path / 'empty')
    connection = duckdb.connect(str(workspace.database_path), read_only=True)
    try:
        for relation in _RELATIONS:
            assert connection.execute(
                f'SELECT count(*) FROM "{relation}"'
            ).fetchone() == (0,)
        assert connection.execute(
            'SELECT value_type FROM interpreted_fields '
            "WHERE record_code='C190' AND alias='vl_opr'"
        ).fetchone() == ('decimal',)
        assert connection.execute(
            "SELECT count(*) FROM interpreted_fields WHERE record_code='E310'"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_workspace_persists_field_and_structural_errors(tmp_path, layout):
    body = _movement(layout, amount='invalid')
    body.insert(-1, '|Z999|unknown|')
    parsed = parse_sped(_source(tmp_path, layout, body), layout)
    workspace = materialize_workspace(parsed, layout, tmp_path / 'errors')
    connection = duckdb.connect(str(workspace.database_path), read_only=True)
    try:
        assert connection.execute('SELECT vl_opr FROM C190').fetchone() == (
            None,
        )
        assert connection.execute(
            'SELECT position,error_code FROM field_errors '
            "WHERE record_code='C190'"
        ).fetchone() == (5, 'invalid_decimal')
        assert connection.execute(
            'SELECT count(*) FROM record_errors '
            "WHERE record_code='C190' AND error_code='unexpected_parent'"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_workspace_refuses_existing_directory_without_changes(
    tmp_path, layout
):
    parsed = parse_sped(_source(tmp_path, layout, []), layout)
    destination = tmp_path / 'existing'
    destination.mkdir()
    marker = destination / 'keep.txt'
    marker.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        materialize_workspace(parsed, layout, destination)
    assert list(destination.iterdir()) == [marker]
    assert marker.read_bytes() == b'keep'


@pytest.mark.parametrize('record_code', ['C190', 'K230'])
def test_workspace_rolls_back_unrepresentable_decimal(
    tmp_path, layout, record_code
):
    body = (
        _movement(layout, amount='10000000000000000,00')
        if record_code == 'C190'
        else _production(layout, '1000000000000,000000')
    )
    parsed = parse_sped(
        _source(tmp_path, layout, body),
        layout,
    )
    destination = tmp_path / 'failed'
    with pytest.raises(ValueError, match='unrepresentable_decimal'):
        materialize_workspace(parsed, layout, destination)
    assert not destination.exists()


def test_new_relations_preserve_strings_quantity_scale_and_unverified_fields(
    tmp_path, layout
):
    quantity = '123456789012,123456'
    body = [
        _line(layout, '0001', {2: '0'}),
        _line(layout, '0150', {2: '000PART', 3: ' Órgão sintético '}),
        _line(
            layout,
            '0200',
            {
                2: '000ITEM',
                3: 'Descrição sintética',
                8: '01012100',
            },
        ),
        *_movement(layout, amount='123,45')[:-1],
        _line(
            layout,
            'C170',
            {
                3: '000ITEM',
                7: '123,45',
                27: 'raw-only',
                29: 'also-raw',
            },
        ),
        *_production(layout, quantity),
    ]
    parsed = parse_sped(_source(tmp_path, layout, body), layout)
    workspace = materialize_workspace(
        parsed, layout, tmp_path / 'six-relations'
    )
    lookup = {record.record_code: record for record in parsed.records}
    assert lookup['C170'].parent_record_id == lookup['C100'].record_id
    assert lookup['K230'].parent_record_id == lookup['K100'].record_id
    assert lookup['K100'].typed_values == {1: 'K100'}
    assert lookup['C170'].raw_values[27] == 'raw-only'
    assert lookup['C170'].typed_values.keys().isdisjoint({27, 29})
    with duckdb.connect(str(workspace.database_path), read_only=True) as con:
        assert con.execute('SELECT cod_part, nome FROM "0150"').fetchone() == (
            '000PART',
            ' Órgão sintético ',
        )
        assert con.execute(
            'SELECT cod_item, descr_item, cod_ncm FROM "0200"'
        ).fetchone() == ('000ITEM', 'Descrição sintética', '01012100')
        assert con.execute(
            'SELECT cod_item, vl_item FROM c170'
        ).fetchone() == (
            '000ITEM',
            Decimal('123.45'),
        )
        assert con.execute(
            'SELECT dt_ini_op, cod_doc_op, cod_item, qtd_enc FROM k230'
        ).fetchone() == (
            date(2026, 1, 2),
            '000OP',
            '000ITEM',
            Decimal('123456789012.123456'),
        )
        assert con.execute(
            'SELECT data_type FROM information_schema.columns '
            "WHERE table_name='k230' AND column_name='qtd_enc'"
        ).fetchone() == ('DECIMAL(18,6)',)
    invalid = _source(tmp_path, layout, _production(layout, '1,1234567'))
    record = parse_sped(invalid, layout).records[-1]
    assert record.raw_values[6] == '1,1234567'
    assert record.typed_values[6] is None
    assert record.field_errors[6] == 'decimal_scale_exceeded'


def test_extra_verified_position_preserved_and_flagged(tmp_path, layout):
    body = _movement(layout)
    maximum = max(f.position for f in layout.records['C190'].fields)
    body[-1] = body[-1] + 'unexpected|'
    record = parse_sped(_source(tmp_path, layout, body), layout).records[-1]
    assert record.raw_values[maximum + 1] == 'unexpected'
    assert 'unexpected_field_position' in record.structural_errors
    assert maximum + 1 not in record.typed_values


def test_extra_unverified_position_preserved_without_interpretation(
    tmp_path, layout
):
    path = _source(tmp_path, layout, [_line(layout, '1310', {11: '123456'})])
    record = parse_sped(path, layout).records[-1]
    assert record.raw_values[11] == '123456'
    assert record.typed_values == {}
    assert 'unexpected_field_position' not in record.structural_errors


def test_workspace_rolls_back_runtime_failure(tmp_path, layout, monkeypatch):
    parsed = parse_sped(_source(tmp_path, layout, []), layout)
    destination = tmp_path / 'failed'

    def failure(*args, **kwargs):
        raise RuntimeError('controlled_failure')

    monkeypatch.setattr('agente_sped.infra.workspace.duckdb.connect', failure)
    with pytest.raises(RuntimeError, match='controlled_failure'):
        materialize_workspace(parsed, layout, destination)
    assert not destination.exists()


def test_workspace_checks_source_hash_before_creating_directory(
    tmp_path, layout
):
    parsed = parse_sped(_source(tmp_path, layout, []), layout)
    corrupted = replace(parsed, source_bytes=b'different')
    destination = tmp_path / 'failed'
    with pytest.raises(ValueError, match='source_hash_mismatch'):
        materialize_workspace(corrupted, layout, destination)
    assert not destination.exists()
