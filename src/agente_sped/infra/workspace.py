"""Create one private, reconstructible DuckDB workspace per source file."""

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb

from agente_sped.domain.layout import FieldDefinition, LayoutCatalog
from agente_sped.domain.records import ParsedFile, ParsedRecord, Value
from agente_sped.infra.layout_catalog import load_layout_catalog

_TECHNICAL_TYPES = {
    'file_id': 'VARCHAR',
    'record_id': 'BIGINT',
    'record_code': 'VARCHAR',
    'record_level': 'INTEGER',
    'source_line': 'BIGINT',
    'parent_record_id': 'BIGINT',
    'parent_record_code': 'VARCHAR',
}
_VALUE_TYPES = {
    'string': 'VARCHAR',
    'date': 'DATE',
    'integer': 'BIGINT',
}
_RELATIONS = ('C100', 'C190', 'C170', '0200', '0150', 'K230')
_DECIMAL_PRECISION = 18
_BIGINT_MIN = -(2**63)
_BIGINT_MAX = 2**63 - 1


@dataclass(frozen=True)
class Workspace:
    database_path: Path
    file_id: str
    source_sha256: str
    period_start: date
    period_end: date


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _technical_values(record: ParsedRecord) -> list[Value]:
    return [getattr(record, name) for name in _TECHNICAL_TYPES]


def _json_value(value: Value) -> str | int | None:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _status(record: ParsedRecord) -> str:
    if record.structural_errors:
        return 'structural_error'
    if record.field_errors:
        return 'field_error'
    return 'partially_interpreted' if record.typed_values else 'raw_only'


def _verified_fields(
    layout: LayoutCatalog, code: str
) -> tuple[FieldDefinition, ...]:
    if code not in layout.records:
        raise ValueError('missing_workspace_relation')
    fields = tuple(f for f in layout.records[code].fields if f.verified)
    names = [f.alias.casefold() for f in fields]
    if len(names) != len(set(names)) or set(names) & _TECHNICAL_TYPES.keys():
        raise ValueError('invalid_workspace_alias')
    return fields


def _decimal_scale(definition: FieldDefinition) -> int:
    scale = definition.decimals
    if type(scale) is not int or not 0 <= scale <= _DECIMAL_PRECISION:
        raise ValueError('unsupported_decimal_scale')
    return scale


def _storage_type(definition: FieldDefinition) -> str:
    if definition.value_type == 'decimal':
        return f'DECIMAL({_DECIMAL_PRECISION},{_decimal_scale(definition)})'
    return _VALUE_TYPES[definition.value_type]


def _storage_value(value: Value, definition: FieldDefinition) -> Value:
    if value is None:
        return None
    if definition.value_type == 'decimal':
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError('unrepresentable_decimal')
        scale = _decimal_scale(definition)
        limit = Decimal(1).scaleb(_DECIMAL_PRECISION - scale)
        if value.copy_abs() >= limit:
            raise ValueError('unrepresentable_decimal')
        _, digits, exponent = value.as_tuple()
        # Extra zeroes are exact; nonzero fractional digits beyond the column
        # scale would be rounded by DuckDB and must never reach its cast.
        excess = max(0, -exponent - scale)
        if excess and any(digits[-excess:]):
            raise ValueError('unrepresentable_decimal')
    elif definition.value_type == 'integer':
        if type(value) is not int or not _BIGINT_MIN <= value <= _BIGINT_MAX:
            raise ValueError('unrepresentable_integer')
    elif definition.value_type == 'date':
        if type(value) is not date:
            raise ValueError('unrepresentable_date')
    elif definition.value_type == 'string':
        if not isinstance(value, str):
            raise ValueError('unrepresentable_string')
    else:
        raise ValueError('unsupported_field_type')
    return value


def _create_tables(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        'CREATE TABLE workspace_metadata ('
        'file_id VARCHAR, layout_version VARCHAR, source_sha256 VARCHAR, '
        'period_start DATE, period_end DATE, layout_source_sha256 VARCHAR, '
        'layout_metadata_sha256 VARCHAR)'
    )
    connection.execute(
        'CREATE TABLE interpreted_fields ('
        'record_code VARCHAR, position INTEGER, alias VARCHAR, '
        'value_type VARCHAR)'
    )
    connection.execute(
        'CREATE TABLE field_errors ('
        'file_id VARCHAR, record_id BIGINT, record_code VARCHAR, '
        'position INTEGER, error_code VARCHAR)'
    )
    connection.execute(
        'CREATE TABLE record_errors ('
        'file_id VARCHAR, record_id BIGINT, record_code VARCHAR, '
        'error_code VARCHAR)'
    )
    technical = ', '.join(
        f'{_identifier(name)} {kind}'
        for name, kind in _TECHNICAL_TYPES.items()
    )
    connection.execute(
        f'CREATE TABLE raw_records ({technical}, source_text VARCHAR, '
        'raw_values JSON, typed_values JSON, status VARCHAR)'
    )


def _store_metadata(
    connection: duckdb.DuckDBPyConnection,
    parsed: ParsedFile,
    layout: LayoutCatalog,
) -> None:
    connection.execute(
        'INSERT INTO workspace_metadata VALUES (?, ?, ?, ?, ?, ?, ?)',
        [
            parsed.file_id,
            parsed.layout_version,
            parsed.source_sha256,
            parsed.period_start,
            parsed.period_end,
            layout.source_sha256,
            layout.metadata_sha256,
        ],
    )
    fields = [
        (code, item.position, item.alias, item.value_type)
        for code, record in layout.records.items()
        for item in record.fields
        if item.verified
    ]
    if fields:
        connection.executemany(
            'INSERT INTO interpreted_fields VALUES (?, ?, ?, ?)', fields
        )


def _store_raw_records(
    connection: duckdb.DuckDBPyConnection, parsed: ParsedFile
) -> None:
    for record in parsed.records:
        connection.execute(
            'INSERT INTO raw_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [
                *_technical_values(record),
                record.source_text,
                json.dumps(record.raw_values, ensure_ascii=False),
                json.dumps(
                    {
                        k: _json_value(v)
                        for k, v in record.typed_values.items()
                    },
                    ensure_ascii=False,
                ),
                _status(record),
            ],
        )
        for position, error in record.field_errors.items():
            connection.execute(
                'INSERT INTO field_errors VALUES (?, ?, ?, ?, ?)',
                [
                    parsed.file_id,
                    record.record_id,
                    record.record_code,
                    position,
                    error,
                ],
            )
        for error in record.structural_errors:
            connection.execute(
                'INSERT INTO record_errors VALUES (?, ?, ?, ?)',
                [parsed.file_id, record.record_id, record.record_code, error],
            )


def _store_relations(
    connection: duckdb.DuckDBPyConnection,
    parsed: ParsedFile,
    layout: LayoutCatalog,
) -> None:
    technical = [
        f'{_identifier(name)} {kind}'
        for name, kind in _TECHNICAL_TYPES.items()
    ]
    for code in _RELATIONS:
        fields = _verified_fields(layout, code)
        declarations = [
            f'{_identifier(item.alias)} {_storage_type(item)}'
            for item in fields
        ]
        columns = ', '.join([*technical, *declarations])
        table = _identifier(code.lower())
        connection.execute(f'CREATE TABLE {table} ({columns})')
        count = len(_TECHNICAL_TYPES) + len(fields)
        placeholders = ', '.join('?' for _ in range(count))
        for record in parsed.records:
            if record.record_code != code:
                continue
            values = [
                _storage_value(record.typed_values.get(item.position), item)
                for item in fields
            ]
            connection.execute(
                f'INSERT INTO {table} VALUES ({placeholders})',
                [*_technical_values(record), *values],
            )


def _remove_failed_workspace(path: Path) -> None:
    if path.is_symlink() or path.resolve() != path:
        raise ValueError('unsafe_workspace_cleanup')
    shutil.rmtree(path)


def materialize_workspace(
    parsed: ParsedFile,
    layout: LayoutCatalog,
    workspace_dir: Path,
    *,
    layout_catalog_path: Path | None = None,
) -> Workspace:
    if hashlib.sha256(parsed.source_bytes).hexdigest() != parsed.source_sha256:
        raise ValueError('source_hash_mismatch')
    if parsed.layout_version != layout.layout_version:
        raise ValueError('workspace_layout_mismatch')
    for code in _RELATIONS:
        _verified_fields(layout, code)
    catalog_bytes = None
    if layout_catalog_path is not None:
        saved = load_layout_catalog(layout_catalog_path)
        if (
            saved.source_sha256 != layout.source_sha256
            or saved.metadata_sha256 != layout.metadata_sha256
            or saved.layout_version != layout.layout_version
            or saved.obligation != layout.obligation
        ):
            raise ValueError('workspace_layout_mismatch')
        catalog_bytes = layout_catalog_path.read_bytes()
    # Resolve the existing parent, not a possibly preexisting final symlink.
    workspace_dir = workspace_dir.parent.resolve() / workspace_dir.name
    workspace_dir.mkdir(mode=0o700, exist_ok=False)
    try:
        (workspace_dir / 'source.txt').write_bytes(parsed.source_bytes)
        if catalog_bytes is not None:
            (workspace_dir / 'layout_catalog.duckdb').write_bytes(
                catalog_bytes
            )
        database_path = workspace_dir / 'analysis.duckdb'
        connection = duckdb.connect(
            str(database_path),
            config={
                'enable_external_access': 'false',
                'autoinstall_known_extensions': 'false',
                'autoload_known_extensions': 'false',
            },
        )
        try:
            connection.execute('BEGIN TRANSACTION')
            _create_tables(connection)
            _store_metadata(connection, parsed, layout)
            _store_raw_records(connection, parsed)
            _store_relations(connection, parsed, layout)
            connection.execute('COMMIT')
        finally:
            connection.close()
    except BaseException:
        _remove_failed_workspace(workspace_dir)
        raise
    return Workspace(
        database_path=database_path,
        file_id=parsed.file_id,
        source_sha256=parsed.source_sha256,
        period_start=parsed.period_start,
        period_end=parsed.period_end,
    )
