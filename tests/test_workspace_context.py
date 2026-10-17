"""Workspace metadata and availability without analytical execution."""

from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from agente_sped.application import analysis
from agente_sped.application.analysis import (
    AnalysisTimeoutError,
    AnalysisUnavailableError,
    WorkspaceProfile,
    get_workspace_profile,
    inspect_analysis_availability,
)
from agente_sped.infra.analysis_catalog import load_analysis_catalog

_PERIOD_START = date(2026, 1, 1)
_PERIOD_END = date(2026, 1, 31)


@pytest.fixture
def workspace_database(tmp_path: Path) -> Path:
    path = tmp_path / 'analysis.duckdb'
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            'CREATE TABLE workspace_metadata ('
            'file_id VARCHAR, layout_version VARCHAR, source_sha256 VARCHAR, '
            'period_start DATE, period_end DATE, '
            'layout_source_sha256 VARCHAR, '
            'layout_metadata_sha256 VARCHAR)'
        )
        connection.execute(
            'INSERT INTO workspace_metadata VALUES (?, ?, ?, ?, ?, ?, ?)',
            [
                'synthetic',
                '020',
                'a' * 64,
                _PERIOD_START,
                _PERIOD_END,
                'b' * 64,
                'c' * 64,
            ],
        )
        connection.execute(
            'CREATE TABLE interpreted_fields ('
            'record_code VARCHAR, position INTEGER, alias VARCHAR, '
            'value_type VARCHAR)'
        )
        connection.execute(
            'INSERT INTO interpreted_fields VALUES '
            "('K230',2,'dt_ini_op','date'), "
            "('K230',4,'cod_doc_op','string'), "
            "('K230',5,'cod_item','string'), "
            "('K230',6,'qtd_enc','decimal')"
        )
        connection.execute(
            'CREATE TABLE record_errors ('
            'file_id VARCHAR, record_id BIGINT, record_code VARCHAR, '
            'error_code VARCHAR)'
        )
        connection.execute(
            'CREATE TABLE field_errors ('
            'file_id VARCHAR, record_id BIGINT, record_code VARCHAR, '
            'position INTEGER, error_code VARCHAR)'
        )
        connection.execute(
            'CREATE TABLE k230 ('
            'file_id VARCHAR, record_id BIGINT, record_code VARCHAR, '
            'record_level INTEGER, source_line BIGINT, '
            'parent_record_id BIGINT, parent_record_code VARCHAR, '
            'dt_ini_op DATE, cod_doc_op VARCHAR, cod_item VARCHAR, '
            'qtd_enc DECIMAL(18,6))'
        )
    return path


def test_profile_and_availability_are_read_only_without_analytical_sql(
    workspace_database, monkeypatch
):
    catalog = load_analysis_catalog()
    definition = catalog.analyses['block_k_activity']
    altered = definition.model_copy(
        update={
            'execution': definition.execution.model_copy(
                update={
                    'sql': "SELECT error('analytical SQL must not run')",
                }
            ),
        }
    )
    catalog = replace(
        catalog,
        analyses={
            **catalog.analyses,
            'block_k_activity': altered,
        },
    )
    connect = duckdb.connect
    calls = []

    def capture(*args, **kwargs):
        calls.append(kwargs)
        return connect(*args, **kwargs)

    monkeypatch.setattr(analysis.duckdb, 'connect', capture)
    before = sha256(workspace_database.read_bytes()).digest()
    profile = get_workspace_profile(workspace_database)
    assert isinstance(profile, WorkspaceProfile)
    assert profile.file_id == 'synthetic'
    assert profile.period_start == _PERIOD_START
    assert profile.period_end == _PERIOD_END
    status = inspect_analysis_availability(
        workspace_database, 'block_k_activity', catalog=catalog
    )
    assert status.available is True
    assert status.reason is None
    assert status.parameters == {
        'start_date': _PERIOD_START,
        'end_date': _PERIOD_END,
    }
    assert sha256(workspace_database.read_bytes()).digest() == before
    assert (
        calls
        == [
            {
                'read_only': True,
                'config': {
                    'enable_external_access': 'false',
                    'autoinstall_known_extensions': 'false',
                    'autoload_known_extensions': 'false',
                },
            }
        ]
        * 2
    )


@pytest.mark.parametrize(
    'case', ['missing_coverage', 'invalid_field', 'invalid_parameters']
)
def test_unavailable_inspection_returns_a_reason_without_running_analysis(
    workspace_database, case
):
    parameters = None
    if case == 'invalid_parameters':
        parameters = {'unknown_parameter': 'not accepted'}
    else:
        with duckdb.connect(str(workspace_database)) as connection:
            if case == 'missing_coverage':
                connection.execute(
                    "DELETE FROM interpreted_fields WHERE alias='qtd_enc'"
                )
            else:
                connection.execute(
                    'INSERT INTO k230 VALUES '
                    "('synthetic',1,'K230',3,1,2,'K100',"
                    "'2026-01-02','order','item',NULL)"
                )
                connection.execute(
                    'INSERT INTO field_errors VALUES '
                    "('synthetic',1,'K230',6,'invalid_decimal')"
                )
    status = inspect_analysis_availability(
        workspace_database, 'block_k_activity', parameters
    )
    assert status.available is False
    assert status.reason
    if case == 'invalid_parameters':
        assert status.parameters == {}
    else:
        assert status.parameters['start_date'] == _PERIOD_START


def test_profile_rejects_missing_or_ambiguous_workspace(
    workspace_database, tmp_path
):
    missing = tmp_path / 'missing.duckdb'
    with pytest.raises(AnalysisUnavailableError, match='indisponível'):
        get_workspace_profile(missing)
    assert not missing.exists()
    with duckdb.connect(str(workspace_database)) as connection:
        connection.execute(
            'INSERT INTO workspace_metadata SELECT * FROM workspace_metadata'
        )
    with pytest.raises(AnalysisUnavailableError, match='exatamente um'):
        get_workspace_profile(workspace_database)


@pytest.mark.parametrize('operation', ['profile', 'availability'])
def test_workspace_inspections_apply_deadline_and_release_connection(
    workspace_database, monkeypatch, operation
):
    moments = iter([0.0, 31.0])
    monkeypatch.setattr(
        analysis, 'time', SimpleNamespace(monotonic=lambda: next(moments))
    )
    reader = (
        get_workspace_profile
        if operation == 'profile'
        else lambda path: inspect_analysis_availability(
            path, 'block_k_activity'
        )
    )
    with pytest.raises(AnalysisTimeoutError, match='tempo limite'):
        reader(workspace_database)
    with duckdb.connect(str(workspace_database)) as connection:
        assert connection.execute('SELECT 1').fetchone() == (1,)
