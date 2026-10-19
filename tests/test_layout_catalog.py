"""Proveniência, resolução posicional e limites da cobertura 020."""

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from agente_sped.domain.layout import LayoutCatalog
from agente_sped.infra import layout_catalog
from agente_sped.infra.layout_catalog import (
    build_layout_catalog,
    load_layout_catalog,
)

_SOURCE_HASH = (
    '63ab6b2bcedcbf698c3e29889aee537590f16d9e04a68e4796d9733b1d5ede93'
)
_RESEARCH_COMMIT = '8ec26d42a0caa2b13134bf94811d1d4b8962ee40'
_CSV_COLUMN_COUNT = 33
_ORIGINAL_RECORD_COUNT = 269
_COMPILED_FIELD_COUNT = 2138
_VERIFIED_FIELD_COUNT = 66
_C190_VL_OPR_POSITION = 5
_MONETARY_SCALE = 2
_C100_VL_MERC_POSITION = 16


@pytest.fixture(scope='module')
def compiled_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp('layout') / 'layout_catalog.duckdb'
    build_layout_catalog(path)
    return path


@pytest.fixture(scope='module')
def catalog(compiled_path: Path) -> LayoutCatalog:
    return load_layout_catalog(compiled_path)


def _write_metadata(path: Path, metadata: dict[str, Any]) -> Path:
    corrections = json.dumps(
        metadata['corrections'],
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    metadata['corrections_sha256'] = hashlib.sha256(corrections).hexdigest()
    path.write_bytes(json.dumps(metadata, ensure_ascii=False).encode('utf-8'))
    return path


def _modified_source(
    tmp_path: Path, extra_row: list[str]
) -> tuple[Path, Path]:
    rows = list(
        csv.reader(
            io.StringIO(
                layout_catalog._SOURCE_PATH.read_bytes().decode('utf-8-sig')
            )
        )
    )
    rows.append(extra_row)
    buffer = io.StringIO(newline='')
    csv.writer(buffer, lineterminator='\n').writerows(rows)
    source_path = tmp_path / 'source.csv'
    source_path.write_bytes(buffer.getvalue().encode('utf-8'))
    metadata = json.loads(layout_catalog._METADATA_PATH.read_bytes())
    metadata['source']['sha256'] = hashlib.sha256(
        source_path.read_bytes()
    ).hexdigest()
    return source_path, _write_metadata(tmp_path / 'metadata.json', metadata)


def _c170_row(position: int) -> list[str]:
    rows = csv.reader(
        io.StringIO(
            layout_catalog._SOURCE_PATH.read_bytes().decode('utf-8-sig')
        )
    )
    return next(
        row
        for row in rows
        if len(row) == _CSV_COLUMN_COUNT
        and row[0] == 'EFD-ICMS-IPI'
        and row[2] == 'C170'
        and row[13] == str(position)
    )


def test_original_snapshot_and_provenance(catalog: LayoutCatalog) -> None:
    raw = layout_catalog._SOURCE_PATH.read_bytes()
    metadata_raw = layout_catalog._METADATA_PATH.read_bytes()
    metadata = json.loads(metadata_raw)
    assert hashlib.sha256(raw).hexdigest() == _SOURCE_HASH
    assert catalog.source_sha256 == _SOURCE_HASH
    assert catalog.metadata_sha256 == hashlib.sha256(metadata_raw).hexdigest()
    assert metadata['source']['commit'] == _RESEARCH_COMMIT
    assert metadata['source']['path'] == (
        'docs/rfcs/_archive/004-agente_sped/research/data/sped_layout_fields.csv'
    )
    assert catalog.obligation == 'EFD_ICMS_IPI'
    assert catalog.layout_version == '020'
    assert len(catalog.records) == _ORIGINAL_RECORD_COUNT
    assert (
        sum(len(record.fields) for record in catalog.records.values())
        == _COMPILED_FIELD_COUNT
    )
    assert all(
        record.fields[0].original_name == 'REG'
        and record.fields[0].position == 1
        for record in catalog.records.values()
    )


def test_roundtrip_and_verified_application_types(
    tmp_path: Path, catalog: LayoutCatalog
) -> None:
    path = tmp_path / 'layout.duckdb'
    rebuilt = build_layout_catalog(path)
    assert rebuilt == catalog == load_layout_catalog(path)
    assert catalog.resolve('0000', 'COD_VER').value_type == 'string'
    assert catalog.resolve('0000', 4).value_type == 'date'
    assert catalog.resolve('0000', 5).value_type == 'date'
    assert catalog.resolve('0000', 7).value_type == 'string'
    assert catalog.resolve('C100', 'DT_DOC').value_type == 'date'
    assert catalog.resolve('C100', 'COD_SIT').value_type == 'string'
    assert catalog.resolve('C100', 'NUM_DOC').value_type == 'string'
    assert catalog.resolve('C100', 'CHV_NFE').value_type == 'string'
    assert catalog.resolve('C190', 'CFOP').value_type == 'string'
    amount = catalog.resolve('C190', 'vl_opr')
    assert amount.position == _C190_VL_OPR_POSITION
    assert amount.value_type == 'decimal'
    assert amount.decimals == _MONETARY_SCALE
    additional = [
        ('C170', 3, 'COD_ITEM', 'string', None),
        ('C170', 7, 'VL_ITEM', 'decimal', 2),
        ('0200', 2, 'COD_ITEM', 'string', None),
        ('0200', 3, 'DESCR_ITEM', 'string', None),
        ('0200', 8, 'COD_NCM', 'string', None),
        ('0150', 2, 'COD_PART', 'string', None),
        ('0150', 3, 'NOME', 'string', None),
        ('K230', 2, 'DT_INI_OP', 'date', None),
        ('K230', 4, 'COD_DOC_OP', 'string', None),
        ('K230', 5, 'COD_ITEM', 'string', None),
        ('K230', 6, 'QTD_ENC', 'decimal', 6),
    ]
    for code, position, name, value_type, decimals in additional:
        field = catalog.resolve(code, name)
        assert (field.position, field.value_type, field.decimals) == (
            position,
            value_type,
            decimals,
        )


@pytest.mark.parametrize(
    ('original_name', 'position', 'alias'),
    [
        ('ALIQ_PIS', 27, 'aliq_pis__p027'),
        ('ALIQ_PIS', 29, 'aliq_pis__p029'),
        ('ALIQ_COFINS', 33, 'aliq_cofins__p033'),
        ('ALIQ_COFINS', 35, 'aliq_cofins__p035'),
    ],
)
def test_repeated_official_names_require_position_or_unique_alias(
    catalog: LayoutCatalog, original_name: str, position: int, alias: str
) -> None:
    with pytest.raises(ValueError, match='Nome ambíguo'):
        catalog.resolve('C170', original_name, require_verified=False)
    by_alias = catalog.resolve('C170', alias, require_verified=False)
    assert by_alias == catalog.resolve(
        'C170', position, require_verified=False
    )
    assert by_alias.original_name == original_name
    assert by_alias.position == position
    assert by_alias.alias == alias
    assert by_alias.value_type == 'string'
    assert by_alias.verified is False
    with pytest.raises(ValueError, match='Campo não verificado'):
        catalog.resolve('C170', alias)


def test_unverified_records_and_fields_stay_outside_typed_coverage(
    catalog: LayoutCatalog,
) -> None:
    assert {
        code
        for code, record in catalog.records.items()
        if record.verified_structure
    } == {
        '0000',
        '0001',
        '0150',
        '0200',
        'C001',
        'C100',
        'C170',
        'C190',
        'K001',
        'K100',
        'K230',
    }
    assert (
        sum(
            field.verified
            for record in catalog.records.values()
            for field in record.fields
        )
        == _VERIFIED_FIELD_COUNT
    )
    assert (
        catalog.resolve('C100', 'VL_MERC').position == _C100_VL_MERC_POSITION
    )
    with pytest.raises(ValueError, match='Campo não verificado'):
        catalog.resolve('0000', 'COD_FIN')
    with pytest.raises(ValueError, match='Campo não verificado'):
        catalog.resolve('0150', 'CNPJ')
    assert catalog.records['E310'].verified_structure is False
    assert all(
        not field.verified and field.value_type == 'string'
        for field in catalog.records['E310'].fields
    )
    with pytest.raises(ValueError, match='Nome ambíguo'):
        catalog.resolve('E310', 'VL_TOT_CRED_FCP', require_verified=False)
    with pytest.raises(KeyError, match='Campo desconhecido'):
        catalog.resolve('C190', 'not_a_field')


def test_corrections_keep_original_metadata_and_do_not_rewrite_csv(
    compiled_path: Path,
) -> None:
    with duckdb.connect(str(compiled_path), read_only=True) as connection:
        rows = connection.execute(
            'SELECT record_code, position, source_spec_type, spec_type, '
            'verified '
            "FROM fields WHERE (record_code='0000' AND position=7) "
            "OR (record_code='0150' AND position=5) "
            "OR (record_code='C100' AND position=9) ORDER BY record_code"
        ).fetchall()
        assert rows == [
            ('0000', 7, 'N', 'C', True),
            ('0150', 5, 'N', 'C', False),
            ('C100', 9, 'N', 'C', True),
        ]
        descriptions = connection.execute(
            'SELECT position, source_description, description FROM fields '
            "WHERE record_code='C100' AND position IN (13,17) "
            'ORDER BY position'
        ).fetchall()
        assert all(
            original != corrected for _, original, corrected in descriptions
        )
        assert '2 - Outros' in descriptions[0][2]
        assert 'remetente (CIF)' in descriptions[1][2]
        assert '4 - Transporte próprio' in descriptions[1][2]
    assert (
        hashlib.sha256(layout_catalog._SOURCE_PATH.read_bytes()).hexdigest()
        == _SOURCE_HASH
    )


def test_duplicate_positions_fail_without_replacing_destination(
    tmp_path: Path,
) -> None:
    source, metadata = _modified_source(tmp_path, _c170_row(27))
    destination = tmp_path / 'layout.duckdb'
    destination.write_bytes(b'previous-reviewed-artifact')
    with pytest.raises(ValueError, match=r'Posição duplicada: C170\.27'):
        build_layout_catalog(
            destination, source_path=source, metadata_path=metadata
        )
    assert destination.read_bytes() == b'previous-reviewed-artifact'


def test_alias_collisions_fail_instead_of_renaming_fields(
    tmp_path: Path,
) -> None:
    extra = _c170_row(27)
    extra[13], extra[14] = '39', 'ALIQ_PIS__p027'
    source, metadata = _modified_source(tmp_path, extra)
    with pytest.raises(ValueError, match='Alias inválido ou duplicado'):
        build_layout_catalog(
            tmp_path / 'layout.duckdb',
            source_path=source,
            metadata_path=metadata,
        )


def test_source_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / 'source.csv'
    source.write_bytes(layout_catalog._SOURCE_PATH.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='Hash do CSV'):
        build_layout_catalog(tmp_path / 'layout.duckdb', source_path=source)


def test_corrections_must_match_the_original_snapshot(tmp_path: Path) -> None:
    metadata = json.loads(layout_catalog._METADATA_PATH.read_bytes())
    metadata['corrections'][0]['changes']['spec_type']['from'] = 'X'
    metadata_path = _write_metadata(tmp_path / 'metadata.json', metadata)
    with pytest.raises(ValueError, match='Correção diverge do snapshot'):
        build_layout_catalog(
            tmp_path / 'layout.duckdb', metadata_path=metadata_path
        )


def test_corrections_hash_and_json_duplicate_keys_are_rejected(
    tmp_path: Path,
) -> None:
    metadata = json.loads(layout_catalog._METADATA_PATH.read_bytes())
    metadata['corrections_sha256'] = '0' * 64
    metadata_path = tmp_path / 'metadata.json'
    metadata_path.write_bytes(json.dumps(metadata).encode('utf-8'))
    with pytest.raises(ValueError, match='Hash das correções'):
        build_layout_catalog(
            tmp_path / 'layout.duckdb', metadata_path=metadata_path
        )
    metadata_path.write_bytes(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError, match='Chave JSON duplicada'):
        build_layout_catalog(
            tmp_path / 'layout.duckdb', metadata_path=metadata_path
        )


def test_modified_compiled_fields_are_rejected(
    tmp_path: Path, compiled_path: Path
) -> None:
    altered = tmp_path / 'altered.duckdb'
    altered.write_bytes(compiled_path.read_bytes())
    with duckdb.connect(str(altered)) as connection:
        connection.execute(
            "UPDATE fields SET value_type='integer' "
            "WHERE record_code='C100' AND position=6"
        )
    with pytest.raises(ValueError, match='Hash do conteúdo compilado'):
        load_layout_catalog(altered)
