"""Compile o snapshot CSV e sua cobertura explícita em um DuckDB local."""

import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, cast

import duckdb

from agente_sped.domain.layout import (
    FieldDefinition,
    LayoutCatalog,
    RecordDefinition,
    ValueType,
)

_DATA_PATH = Path(__file__).resolve().parents[1] / 'data' / 'layout'
_SOURCE_PATH = _DATA_PATH / 'sped_layout_fields.csv'
_METADATA_PATH = _DATA_PATH / 'metadata.json'
_VALUE_TYPES = {'string', 'date', 'decimal', 'integer'}
_CSV_COLUMN_COUNT = 33
_FIRST_EXPLICIT_POSITION = 2
_OFFLINE_CONFIG = {
    'enable_external_access': 'false',
    'autoinstall_known_extensions': 'false',
    'autoload_known_extensions': 'false',
}
_FIELD_COLUMNS = (
    'record_code, position, original_name, alias, value_type, decimals, '
    'verified, source_type, source_spec_type, spec_type, '
    'source_description, description, implicit'
)


@dataclass(frozen=True)
class _SourceField:
    position: int
    name: str
    source_type: str
    spec_type: str
    description: str


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Chave JSON duplicada: {key}')
        result[key] = value
    return result


def _metadata(raw: bytes) -> dict[str, Any]:
    metadata = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_keys)
    if (
        metadata['schema_version'] != 1
        or metadata['obligation'] != 'EFD_ICMS_IPI'
        or metadata['layout_version'] != '020'
    ):
        raise ValueError('Versão ou obrigação de metadados incompatível.')
    actual = _sha256(_canonical(metadata['corrections']))
    if actual != metadata['corrections_sha256']:
        raise ValueError('Hash das correções divergente.')
    return metadata


def _read_source(raw: bytes) -> dict[str, tuple[int, list[_SourceField]]]:
    rows = csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline=''))
    if next(rows) != ['sep=', '']:
        raise ValueError('Diretiva CSV esperada: sep=,.')
    header = next(rows)
    expected = {0: 'type_sped', 6: 'level', 13: 'index', 14: 'code'}
    if len(header) != _CSV_COLUMN_COUNT or any(
        header[k] != v for k, v in expected.items()
    ):
        raise ValueError('Cabeçalho do snapshot CSV incompatível.')
    records: dict[str, tuple[int, list[_SourceField]]] = {}
    for row in rows:
        if not row:
            continue
        if len(row) != len(header):
            raise ValueError('Quantidade de colunas CSV incompatível.')
        if row[0] != 'EFD-ICMS-IPI':
            continue
        code, level, position = row[2], int(row[6]), int(row[13])
        if row[12] != code or position < _FIRST_EXPLICIT_POSITION or level < 0:
            raise ValueError(f'Identidade posicional inválida: {code}.')
        if code not in records:
            records[code] = (
                level,
                [
                    _SourceField(
                        1, 'REG', 'implicit', 'C', 'Código do registro'
                    )
                ],
            )
        previous_level, fields = records[code]
        if previous_level != level:
            raise ValueError(f'Nível inconsistente: {code}.')
        if any(field.position == position for field in fields):
            raise ValueError(f'Posição duplicada: {code}.{position}.')
        fields.append(
            _SourceField(position, row[14], row[15], row[25], row[32])
        )
    if not records:
        raise ValueError('CSV sem registros EFD ICMS/IPI.')
    return records


def _corrections(metadata: dict[str, Any]) -> dict[tuple[str, int], Any]:
    corrections = {}
    for item in metadata['corrections']:
        key = item['record_code'], item['position']
        if key in corrections:
            raise ValueError(f'Correção duplicada: {key}.')
        corrections[key] = item
    return corrections


def _corrected(
    field: _SourceField, correction: dict[str, Any] | None
) -> tuple[str, str]:
    values = {'spec_type': field.spec_type, 'description': field.description}
    if correction:
        if correction['original_name'] != field.name:
            raise ValueError('Correção aponta para outro nome original.')
        for key, change in correction['changes'].items():
            if key not in values or change['from'] != values[key]:
                raise ValueError('Correção diverge do snapshot original.')
            if not isinstance(change['to'], str):
                raise ValueError('Correção descritiva deve ser texto.')
            values[key] = change['to']
    return values['spec_type'], values['description']


def _field_type(
    field: _SourceField, coverage: dict[str, Any]
) -> tuple[ValueType, int | None, bool]:
    verified = coverage.get(str(field.position))
    if verified is None:
        return 'string', None, False
    value_type, decimals = verified['value_type'], verified['decimals']
    if (
        verified['original_name'] != field.name
        or value_type not in _VALUE_TYPES
    ):
        raise ValueError('Cobertura diverge do campo original.')
    if value_type == 'decimal':
        if type(decimals) is not int or decimals < 0:
            raise ValueError('Escala decimal inválida.')
    elif decimals is not None:
        raise ValueError('Escala informada para campo não decimal.')
    return cast(ValueType, value_type), decimals, True


def _compile_rows(
    raw: bytes, metadata: dict[str, Any]
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    source_records = _read_source(raw)
    corrections = _corrections(metadata)
    coverage = metadata['verified_records']
    if set(coverage) - source_records.keys():
        raise ValueError('Cobertura referencia registro ausente.')
    record_rows, field_rows = [], []
    for code, (level, fields) in sorted(source_records.items()):
        verified = code in coverage
        record_coverage = coverage.get(code, {})
        if verified and record_coverage['level'] != level:
            raise ValueError(f'Nível verificado divergente: {code}.')
        field_coverage = record_coverage.get('fields', {})
        positions = {str(field.position) for field in fields}
        if set(field_coverage) - positions:
            raise ValueError(f'Cobertura referencia campo ausente: {code}.')
        record_rows.append((code, level, verified))
        name_counts = Counter(field.name.casefold() for field in fields)
        aliases = set()
        for field in sorted(fields, key=lambda item: item.position):
            alias = field.name.casefold()
            if name_counts[alias] > 1:
                alias += f'__p{field.position:03d}'
            if (
                not re.fullmatch(r'[a-z_][a-z0-9_]*', alias)
                or alias in aliases
            ):
                raise ValueError(
                    f'Alias inválido ou duplicado: {code}.{alias}.'
                )
            aliases.add(alias)
            correction = corrections.pop((code, field.position), None)
            spec_type, description = _corrected(field, correction)
            value_type, decimals, field_verified = _field_type(
                field, field_coverage
            )
            field_rows.append(
                (
                    code,
                    field.position,
                    field.name,
                    alias,
                    value_type,
                    decimals,
                    field_verified,
                    field.source_type,
                    field.spec_type,
                    spec_type,
                    field.description,
                    description,
                    field.position == 1,
                )
            )
    if corrections:
        raise ValueError('Correção referencia campo ausente.')
    return record_rows, field_rows


def _catalog(
    metadata: dict[str, str],
    record_rows: list[tuple[Any, ...]],
    field_rows: list[tuple[Any, ...]],
) -> LayoutCatalog:
    fields: dict[str, list[FieldDefinition]] = {r[0]: [] for r in record_rows}
    for row in field_rows:
        fields[row[0]].append(
            FieldDefinition(
                metadata['obligation'],
                metadata['layout_version'],
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
            )
        )
    records = {
        code: RecordDefinition(code, level, verified, tuple(fields[code]))
        for code, level, verified in record_rows
    }
    return LayoutCatalog(
        metadata['obligation'],
        metadata['layout_version'],
        metadata['source_sha256'],
        metadata['metadata_sha256'],
        records,
    )


def _insert_compiled_rows(
    connection: duckdb.DuckDBPyConnection,
    table: Literal['records', 'fields'],
    rows: list[tuple[Any, ...]],
    types: tuple[str, ...],
) -> None:
    # Um parâmetro evita conversões Python opcionais para cada célula.
    columns = ', '.join(
        f"CAST(json_extract_string(value, '$[{index}]') AS {kind})"
        for index, kind in enumerate(types)
    )
    connection.execute(
        f'INSERT INTO {table} SELECT {columns} '
        'FROM json_each(CAST(? AS JSON))',
        [json.dumps(rows, ensure_ascii=False, allow_nan=False)],
    )


def build_layout_catalog(
    path: Path,
    *,
    source_path: Path | None = None,
    metadata_path: Path | None = None,
) -> LayoutCatalog:
    """Compile as fontes offline e substitua o destino atomicamente."""
    raw = (source_path or _SOURCE_PATH).read_bytes()
    metadata_raw = (metadata_path or _METADATA_PATH).read_bytes()
    source_metadata = _metadata(metadata_raw)
    if _sha256(raw) != source_metadata['source']['sha256']:
        raise ValueError('Hash do CSV diverge da proveniência controlada.')
    record_rows, field_rows = _compile_rows(raw, source_metadata)
    metadata = {
        'schema_version': '1',
        'obligation': source_metadata['obligation'],
        'layout_version': source_metadata['layout_version'],
        'source_sha256': _sha256(raw),
        'metadata_sha256': _sha256(metadata_raw),
        'corrections_sha256': source_metadata['corrections_sha256'],
        'content_sha256': _sha256(_canonical([record_rows, field_rows])),
        'metadata_json': metadata_raw.decode('utf-8'),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.layout-', dir=path.parent) as staging:
        staged_path = Path(staging) / 'layout_catalog.duckdb'
        with duckdb.connect(
            str(staged_path), config=_OFFLINE_CONFIG
        ) as connection:
            connection.execute('BEGIN TRANSACTION')
            connection.execute(
                'CREATE TABLE catalog_metadata '
                '(key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)'
            )
            connection.execute(
                'CREATE TABLE records (record_code VARCHAR PRIMARY KEY, '
                'level INTEGER NOT NULL, verified_structure BOOLEAN NOT NULL)'
            )
            connection.execute(
                'CREATE TABLE fields ('
                'record_code VARCHAR NOT NULL, position INTEGER NOT NULL, '
                'original_name VARCHAR NOT NULL, alias VARCHAR NOT NULL, '
                'value_type VARCHAR NOT NULL, decimals INTEGER, '
                'verified BOOLEAN NOT NULL, source_type VARCHAR NOT NULL, '
                'source_spec_type VARCHAR NOT NULL, '
                'spec_type VARCHAR NOT NULL, '
                'source_description VARCHAR NOT NULL, '
                'description VARCHAR NOT NULL, implicit BOOLEAN NOT NULL, '
                'PRIMARY KEY (record_code, position), '
                'UNIQUE (record_code, alias))'
            )
            connection.executemany(
                'INSERT INTO catalog_metadata VALUES (?, ?)',
                list(metadata.items()),
            )
            _insert_compiled_rows(
                connection,
                'records',
                record_rows,
                ('VARCHAR', 'INTEGER', 'BOOLEAN'),
            )
            _insert_compiled_rows(
                connection,
                'fields',
                field_rows,
                (
                    'VARCHAR',
                    'INTEGER',
                    'VARCHAR',
                    'VARCHAR',
                    'VARCHAR',
                    'INTEGER',
                    'BOOLEAN',
                    'VARCHAR',
                    'VARCHAR',
                    'VARCHAR',
                    'VARCHAR',
                    'VARCHAR',
                    'BOOLEAN',
                ),
            )
            connection.execute('COMMIT')
        staged_path.replace(path)
    return _catalog(metadata, record_rows, field_rows)


def load_layout_catalog(path: Path) -> LayoutCatalog:
    """Leia o compilado e confira seus hashes antes de expor definições."""
    with duckdb.connect(
        str(path), read_only=True, config=_OFFLINE_CONFIG
    ) as connection:
        metadata = dict(
            connection.execute('SELECT * FROM catalog_metadata').fetchall()
        )
        record_rows = connection.execute(
            'SELECT record_code, level, verified_structure FROM records '
            'ORDER BY record_code'
        ).fetchall()
        field_rows = connection.execute(
            f'SELECT {_FIELD_COLUMNS} FROM fields '
            'ORDER BY record_code, position'
        ).fetchall()
    if metadata.get('schema_version') != '1':
        raise ValueError('Versão de catálogo compilado incompatível.')
    metadata_raw = metadata['metadata_json'].encode('utf-8')
    if _sha256(metadata_raw) != metadata['metadata_sha256']:
        raise ValueError('Hash dos metadados compilados divergente.')
    source_metadata = _metadata(metadata_raw)
    for key in ('obligation', 'layout_version', 'corrections_sha256'):
        if metadata[key] != source_metadata[key]:
            raise ValueError(f'Metadado compilado divergente: {key}.')
    if metadata['source_sha256'] != source_metadata['source']['sha256']:
        raise ValueError('Hash de origem compilado divergente.')
    content_hash = _sha256(_canonical([record_rows, field_rows]))
    if content_hash != metadata['content_sha256']:
        raise ValueError('Hash do conteúdo compilado divergente.')
    return _catalog(metadata, record_rows, field_rows)
