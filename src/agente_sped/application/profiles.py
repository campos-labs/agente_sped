"""Leituras estruturais locais; não substituem as análises catalogadas."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from agente_sped.application.analysis import get_workspace_profile

_MAX_EVIDENCE_RECORDS = 200


def _database_path(workspace: Path) -> Path:
    database = workspace.absolute() / 'analysis.duckdb'
    if (
        not database.is_file()
        or not database.parent.is_dir()
        or any(
            path.is_symlink() or path.is_junction()
            for path in (database, *database.parents)
        )
    ):
        raise ValueError('Workspace indisponível ou inseguro.')
    return database


@contextmanager
def _connection(workspace: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    database = _database_path(workspace)
    connection = duckdb.connect(
        str(database),
        read_only=True,
        config={
            'enable_external_access': 'false',
            'autoinstall_known_extensions': 'false',
            'autoload_known_extensions': 'false',
        },
    )
    try:
        yield connection
    finally:
        connection.close()


def read_file_profile(workspace: Path) -> dict[str, object]:
    profile = get_workspace_profile(_database_path(workspace))
    with _connection(workspace) as connection:
        records = connection.execute(
            'SELECT record_code, count(*) FROM raw_records '
            'GROUP BY record_code ORDER BY record_code'
        ).fetchall()
        anomalies = connection.execute(
            'SELECT (SELECT count(*) FROM field_errors) '
            '+ (SELECT count(*) FROM record_errors)'
        ).fetchone()[0]
    return {
        'period_start': profile.period_start,
        'period_end': profile.period_end,
        'cod_ver': profile.layout_version,
        'source_sha256': profile.source_sha256,
        'record_count': sum(count for _, count in records),
        'anomaly_count': anomalies,
        'records': [{'code': code, 'count': count} for code, count in records],
    }


def read_record_evidence(
    workspace: Path, record_ids: set[int], *, reveal: bool = False
) -> list[dict[str, object]]:
    if not record_ids:
        return []
    if len(record_ids) > _MAX_EVIDENCE_RECORDS or any(
        type(n) is not int or n < 1 for n in record_ids
    ):
        raise ValueError('Referências de origem inválidas.')
    with _connection(workspace) as connection:
        rows = connection.execute(
            'SELECT record_id, record_code, source_line, parent_record_id, '
            'CASE WHEN $reveal AND length(source_text) <= 65536 '
            'THEN source_text END FROM raw_records '
            'WHERE record_id IN (SELECT unnest($ids)) ORDER BY source_line',
            {'ids': sorted(record_ids), 'reveal': reveal},
        ).fetchall()
    return [
        dict(
            zip(
                (
                    'record_id',
                    'record_code',
                    'source_line',
                    'parent_record_id',
                    'text',
                ),
                row,
                strict=True,
            )
        )
        for row in rows
    ]
