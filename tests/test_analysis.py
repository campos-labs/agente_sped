from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from shutil import copyfile
from textwrap import dedent
from threading import Event, Thread

import duckdb
import pytest
from yaml.constructor import ConstructorError

from agente_sped.application import analysis
from agente_sped.application.analysis import (
    AnalysisError,
    AnalysisTimeoutError,
    AnalysisUnavailableError,
    execute_analysis,
)
from agente_sped.infra.analysis_catalog import (
    P1_ANALYSIS_ID,
    CatalogError,
    load_analysis_catalog,
)

_MAX_ROWS = 20
_REFERENCE = load_analysis_catalog().reference_validation['expected'][
    P1_ANALYSIS_ID
]
_SOURCE = files('agente_sped').joinpath('data/analysis_catalog.yml')
_TECHNICAL = (
    'file_id VARCHAR, record_id BIGINT, record_code VARCHAR, '
    'record_level INTEGER, source_line BIGINT, parent_record_id BIGINT, '
    'parent_record_code VARCHAR'
)


@pytest.fixture(scope='module')
def database_template(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp('analysis-template') / 'analysis.duckdb'
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            'CREATE TABLE workspace_metadata('
            'file_id VARCHAR, layout_version VARCHAR, source_sha256 VARCHAR, '
            'period_start DATE, period_end DATE, '
            'layout_source_sha256 VARCHAR, '
            'layout_metadata_sha256 VARCHAR)'
        )
        connection.execute(
            'INSERT INTO workspace_metadata VALUES '
            "('synthetic', '020', ?, '2026-01-01', '2026-01-31', ?, ?)",
            ['a' * 64, 'b' * 64, 'c' * 64],
        )
        connection.execute(
            'CREATE TABLE interpreted_fields('
            'record_code VARCHAR, position INTEGER, alias VARCHAR, '
            'value_type VARCHAR)'
        )
        connection.executemany(
            'INSERT INTO interpreted_fields VALUES (?, ?, ?, ?)',
            [
                ('C100', 2, 'ind_oper', 'string'),
                ('C100', 6, 'cod_sit', 'string'),
                ('C100', 10, 'dt_doc', 'date'),
                ('C190', 3, 'cfop', 'string'),
                ('C190', 5, 'vl_opr', 'decimal'),
            ],
        )
        connection.execute(
            'CREATE TABLE field_errors('
            'file_id VARCHAR, record_id BIGINT, record_code VARCHAR, '
            'position INTEGER, error_code VARCHAR)'
        )
        connection.execute(
            'CREATE TABLE record_errors('
            'file_id VARCHAR, record_id BIGINT, record_code VARCHAR, '
            'error_code VARCHAR)'
        )
        connection.execute(
            f'CREATE TABLE c100({_TECHNICAL}, ind_oper VARCHAR, '
            'cod_sit VARCHAR, dt_doc DATE)'
        )
        connection.execute(
            f'CREATE TABLE c190({_TECHNICAL}, cfop VARCHAR, '
            'vl_opr DECIMAL(18,2))'
        )
        connection.execute(
            'INSERT INTO c100 VALUES '
            "('synthetic',1,'C100',2,1,NULL,'C001','1','00','2026-01-05'),"
            "('synthetic',2,'C100',2,2,NULL,'C001','1','01','2026-01-05'),"
            "('synthetic',3,'C100',2,3,NULL,'C001','1','00','2026-01-05'),"
            "('synthetic',4,'C100',2,4,NULL,'C001','1','00','2026-01-05'),"
            "('synthetic',5,'C100',2,5,NULL,'C001','1','02','2026-01-05'),"
            "('synthetic',6,'C100',2,6,NULL,'C001','0','00','2026-01-05')"
        )
        connection.execute(
            'INSERT INTO c190 VALUES '
            "('synthetic',101,'C190',3,7,1,'C100','5102',70000.00),"
            "('synthetic',102,'C190',3,8,2,'C100','6102',40000.00),"
            "('synthetic',103,'C190',3,9,3,'C100','5405',20000.00),"
            "('synthetic',104,'C190',3,10,4,'C100','7102',5015.00),"
            "('synthetic',105,'C190',3,11,5,'C100','5102',99999.99),"
            "('synthetic',106,'C190',3,12,6,'C100','1102',99999.99)"
        )
        for field, value_type in {
            'ind_emit': 'VARCHAR',
            'cod_part': 'VARCHAR',
            'cod_mod': 'VARCHAR',
            'ser': 'VARCHAR',
            'num_doc': 'VARCHAR',
            'chv_nfe': 'VARCHAR',
            'vl_doc': 'DECIMAL(18,2)',
        }.items():
            connection.execute(f'ALTER TABLE c100 ADD {field} {value_type}')
        connection.execute(
            "UPDATE c100 SET ind_emit = CASE WHEN ind_oper='0' THEN '1' "
            "ELSE '0' END, cod_part='p', cod_mod='55', ser='1', "
            'num_doc=CAST(record_id AS VARCHAR), vl_doc=100.00'
        )
        for field in ('cst_icms', 'vl_bc_icms', 'vl_icms'):
            kind = 'VARCHAR' if field == 'cst_icms' else 'DECIMAL(18,2)'
            connection.execute(f'ALTER TABLE c190 ADD {field} {kind}')
        connection.execute(
            "UPDATE c190 SET cst_icms='000', vl_bc_icms=100, vl_icms=18"
        )
        for record, fields in {
            '0150': 'cod_part VARCHAR, nome VARCHAR',
            '0200': 'cod_item VARCHAR, descr_item VARCHAR, cod_ncm VARCHAR',
            'c170': 'cod_item VARCHAR, vl_item DECIMAL(18,2)',
            'k230': 'dt_ini_op DATE, cod_doc_op VARCHAR, cod_item VARCHAR, '
            'qtd_enc DECIMAL(18,6)',
        }.items():
            connection.execute(
                f'CREATE TABLE "{record}"({_TECHNICAL}, {fields})'
            )
        connection.execute(
            'INSERT INTO "0150" VALUES '
            "('synthetic',201,'0150',2,20,NULL,'0001','p','Participant')"
        )
        connection.execute(
            'INSERT INTO "0200" VALUES '
            "('synthetic',202,'0200',2,21,NULL,'0001','item','Item','99999999')"
        )
        connection.execute(
            'INSERT INTO c170 VALUES '
            "('synthetic',203,'C170',3,22,1,'C100','item',20000.00)"
        )
        connection.execute(
            'INSERT INTO k230 VALUES '
            "('synthetic',204,'K230',2,23,NULL,'K001',"
            "'2026-01-05','order','item',1.123456)"
        )
        connection.executemany(
            'INSERT INTO interpreted_fields VALUES (?, ?, ?, ?)',
            [
                ('C100', 3, 'ind_emit', 'string'),
                ('C100', 4, 'cod_part', 'string'),
                ('C100', 5, 'cod_mod', 'string'),
                ('C100', 7, 'ser', 'string'),
                ('C100', 8, 'num_doc', 'string'),
                ('C100', 9, 'chv_nfe', 'string'),
                ('C100', 12, 'vl_doc', 'decimal'),
                ('C190', 2, 'cst_icms', 'string'),
                ('C190', 6, 'vl_bc_icms', 'decimal'),
                ('C190', 7, 'vl_icms', 'decimal'),
                ('0150', 2, 'cod_part', 'string'),
                ('0150', 3, 'nome', 'string'),
                ('0200', 2, 'cod_item', 'string'),
                ('0200', 3, 'descr_item', 'string'),
                ('0200', 8, 'cod_ncm', 'string'),
                ('C170', 3, 'cod_item', 'string'),
                ('C170', 7, 'vl_item', 'decimal'),
                ('K230', 2, 'dt_ini_op', 'date'),
                ('K230', 4, 'cod_doc_op', 'string'),
                ('K230', 5, 'cod_item', 'string'),
                ('K230', 6, 'qtd_enc', 'decimal'),
            ],
        )
    return path


@pytest.fixture
def database(tmp_path: Path, database_template: Path) -> Path:
    path = tmp_path / 'analysis.duckdb'
    copyfile(database_template, path)
    return path


def _change_database(path: Path, sql: str) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(sql)


def _with_sql(sql: str):
    catalog = load_analysis_catalog()
    execution = catalog.p1.execution.model_copy(update={'sql': sql})
    p1 = catalog.p1.model_copy(update={'execution': execution})
    return replace(
        catalog, analyses=dict(catalog.analyses, movement_by_cfop=p1)
    )


def test_catalog_preserves_decimals_sql_and_source_hash():
    catalog = load_analysis_catalog()
    expected = catalog.reference_validation['expected'][P1_ANALYSIS_ID]
    assert isinstance(expected['total_operations'], Decimal)
    assert expected['total_operations'] == Decimal('135015.00')
    assert catalog.source_sha256 == sha256(_SOURCE.read_bytes()).hexdigest()
    source_sql = (
        _SOURCE.read_text(encoding='utf-8')
        .split('      sql: |\n', 1)[1]
        .split('    result:\n', 1)[0]
    )
    assert catalog.p1.execution.sql == dedent(source_sql)


@pytest.mark.parametrize(
    'content',
    [
        'catalog_version: 1\ncatalog_version: 2\n',
        'base: &base {key: 1}\nmerged: {<<: *base, key: 2}\n',
        'name: &name collision\nvalues: {*name: 1, collision: 2}\n',
    ],
)
def test_yaml_rejects_duplicate_keys_including_aliases(tmp_path, content):
    path = tmp_path / 'catalog.yml'
    path.write_text(content, encoding='utf-8')
    with pytest.raises(CatalogError) as error:
        load_analysis_catalog(path)
    assert isinstance(error.value.__cause__, ConstructorError)


def test_yaml_does_not_construct_arbitrary_python_objects(tmp_path):
    path = tmp_path / 'catalog.yml'
    path.write_text('!!python/object:builtins.object {}', encoding='utf-8')
    with pytest.raises(CatalogError):
        load_analysis_catalog(path)


@pytest.mark.parametrize(
    ('original', 'replacement'),
    [
        (
            'catalog_version:',
            'unknown_root: true\ncatalog_version:',
        ),
        (
            '    execution:\n      sql:',
            '    execution:\n      unknown: true\n      sql:',
        ),
        ('top_n: {type: integer', 'top_n: {unknown: true, type: integer'),
        ('default: 50.0, label:', 'default: 50.0, unknown: true, label:'),
        ('      row_limit: 20', '      unknown: true\n      row_limit: 20'),
        (
            'sensitive_columns: []',
            'sensitive_columns: [missing_result_column]',
        ),
        ('sensitive_columns: []', 'sensitive_columns: [cfop, cfop]'),
    ],
)
def test_catalog_rejects_invalid_operational_contracts(
    tmp_path, original, replacement
):
    path = tmp_path / 'catalog.yml'
    source = _SOURCE.read_text(encoding='utf-8')
    assert original in source
    path.write_text(source.replace(original, replacement, 1), encoding='utf-8')
    with pytest.raises(CatalogError):
        load_analysis_catalog(path)


def test_decimal_constructor_preserves_precision(tmp_path):
    path = tmp_path / 'catalog.yml'
    exact = '135015.12345678901234567890123456789'
    path.write_text(
        _SOURCE.read_text(encoding='utf-8').replace('135015.00', exact),
        encoding='utf-8',
    )
    catalog = load_analysis_catalog(path)
    expected = catalog.reference_validation['expected'][P1_ANALYSIS_ID]
    assert expected['total_operations'] == Decimal(exact)


def test_p1_defaults_preserve_parameters_precision_and_provenance(database):
    result = execute_analysis(database, P1_ANALYSIS_ID)
    assert result.truncated is False
    assert result.status == 'completed'
    assert result.parameters == {
        'start_date': date(2026, 1, 1),
        'end_date': date(2026, 1, 31),
        'top_n': _REFERENCE['row_count'],
    }
    assert isinstance(result.rows[0]['total_operacao'], Decimal)
    assert result.rows[0]['participation_percent'] == Decimal('51.85')
    assert result.rows[0]['displayed_coverage_percent'] == Decimal('96.29')
    assert (
        result.analysis_catalog_sha256
        == sha256(_SOURCE.read_bytes()).hexdigest()
    )
    assert result.source_sha256 == 'a' * 64
    assert result.layout_source_sha256 == 'b' * 64
    assert result.layout_metadata_sha256 == 'c' * 64


def test_row_limit_preserves_count_and_catalog_aggregates(database):
    result = execute_analysis(database, P1_ANALYSIS_ID, row_limit=1)
    assert result.row_count == 1
    assert result.total_count == _REFERENCE['row_count']
    assert result.truncated is True
    assert (
        result.rows[0]['displayed_subtotal']
        == _REFERENCE['displayed_subtotal']
    )


def test_date_objects_and_filtered_empty_result(database):
    result = execute_analysis(
        database,
        P1_ANALYSIS_ID,
        {
            'start_date': date(2026, 2, 1),
            'end_date': '2026-02-28',
            'top_n': _MAX_ROWS,
        },
    )
    assert result.rows == ()
    assert result.total_count == 0
    assert result.truncated is False
    assert result.status == 'empty_result'


@pytest.mark.parametrize(
    'parameters',
    [
        {'top_n': True},
        {'top_n': '3'},
        {'top_n': 3.0},
        {'top_n': 0},
        {'top_n': 21},
        {'start_date': '2026-02-30'},
        {'start_date': '20260101'},
        {'start_date': datetime(2026, 1, 1)},
        {'start_date': None},
        {'start_date': '2026-02-01', 'end_date': '2026-01-01'},
        {'arbitrary': 'ignored'},
        [],
    ],
)
def test_invalid_parameters_are_explicit(database, parameters):
    with pytest.raises(AnalysisError, match='Parâmetros'):
        execute_analysis(database, P1_ANALYSIS_ID, parameters)


@pytest.mark.parametrize('row_limit', [False, '1', 1.0, 0, 21, -1])
def test_invalid_row_limit_is_rejected(database, row_limit):
    with pytest.raises(AnalysisError, match='row_limit'):
        execute_analysis(database, P1_ANALYSIS_ID, row_limit=row_limit)


@pytest.mark.parametrize(
    'timeout', [True, '1', 0, -1, 31, 10**1000, float('inf'), float('nan')]
)
def test_invalid_timeout_is_rejected(database, timeout):
    with pytest.raises(AnalysisError, match='timeout_seconds'):
        execute_analysis(database, P1_ANALYSIS_ID, timeout_seconds=timeout)


def test_unknown_analysis_is_rejected(database):
    with pytest.raises(AnalysisError, match='não pertence'):
        execute_analysis(database, 'SELECT 1')


@pytest.mark.parametrize(
    'mutation',
    [
        'DROP TABLE c190',
        'ALTER TABLE c190 ALTER vl_opr TYPE DOUBLE',
        "DELETE FROM interpreted_fields WHERE alias = 'cfop'",
        "UPDATE interpreted_fields SET position = 0 WHERE alias = 'cfop'",
        "UPDATE interpreted_fields SET value_type = 'string' "
        "WHERE alias = 'vl_opr'",
        'INSERT INTO interpreted_fields SELECT * FROM interpreted_fields '
        "WHERE alias = 'cfop'",
        'DELETE FROM c190',
        "UPDATE c190 SET file_id = 'another-file' WHERE record_id = 101",
        'INSERT INTO workspace_metadata SELECT * FROM workspace_metadata',
        "UPDATE workspace_metadata SET layout_version = '019'",
    ],
)
def test_incompatible_schema_coverage_or_occurrences_prevent_analysis(
    database, mutation
):
    _change_database(database, mutation)
    with pytest.raises(AnalysisUnavailableError):
        execute_analysis(database, P1_ANALYSIS_ID)


@pytest.mark.parametrize(
    ('analysis_id', 'mutation'),
    [
        (
            P1_ANALYSIS_ID,
            "INSERT INTO field_errors VALUES ('synthetic',1,'C100',10,"
            "'invalid_date')",
        ),
        (
            P1_ANALYSIS_ID,
            "INSERT INTO field_errors VALUES ('synthetic',101,'C190',5,"
            "'invalid_decimal')",
        ),
        (
            P1_ANALYSIS_ID,
            "INSERT INTO record_errors VALUES ('synthetic',101,'C190',"
            "'field_count')",
        ),
        (
            P1_ANALYSIS_ID,
            "INSERT INTO record_errors VALUES ('synthetic',1,'C100',"
            "'invalid_parent')",
        ),
        (
            'cancelled_documents',
            "INSERT INTO record_errors VALUES ('synthetic',201,'0150',"
            "'unexpected_parent')",
        ),
    ],
)
def test_relevant_anomalies_block_execution(database, analysis_id, mutation):
    _change_database(database, mutation)
    with pytest.raises(AnalysisUnavailableError, match='Erros'):
        execute_analysis(database, analysis_id)


def test_unrelated_field_anomalies_remain_outside_p1_contract(database):
    _change_database(
        database,
        "INSERT INTO field_errors VALUES ('synthetic',1,'C100',12,"
        "'invalid_decimal')",
    )
    assert (
        execute_analysis(database, P1_ANALYSIS_ID).row_count
        == _REFERENCE['row_count']
    )


@pytest.mark.parametrize(
    'mutation',
    [
        'UPDATE c100 SET ind_oper = NULL WHERE record_id = 1',
        'UPDATE c100 SET cod_sit = NULL WHERE record_id = 1',
        'UPDATE c100 SET dt_doc = NULL WHERE record_id = 1',
        'UPDATE c190 SET cfop = NULL WHERE record_id = 101',
        'UPDATE c190 SET vl_opr = NULL WHERE record_id = 101',
        'UPDATE c190 SET parent_record_id = 999 WHERE record_id = 101',
    ],
)
def test_incomplete_required_candidates_are_not_silently_ignored(
    database, mutation
):
    _change_database(database, mutation)
    with pytest.raises(AnalysisUnavailableError, match='Dados necessários'):
        execute_analysis(database, P1_ANALYSIS_ID)


def test_blanks_outside_selected_regular_outputs_do_not_block_p1(database):
    _change_database(
        database, 'UPDATE c100 SET dt_doc = NULL WHERE record_id IN (5,6)'
    )
    _change_database(
        database,
        'UPDATE c190 SET cfop = NULL, vl_opr = NULL '
        'WHERE parent_record_id IN (5,6)',
    )
    assert (
        execute_analysis(database, P1_ANALYSIS_ID).row_count
        == _REFERENCE['row_count']
    )


def test_read_only_connection_disables_external_access_and_extensions(
    database, monkeypatch
):
    connect = duckdb.connect
    calls = []

    def capture(*args, **kwargs):
        calls.append(kwargs)
        return connect(*args, **kwargs)

    monkeypatch.setattr(analysis.duckdb, 'connect', capture)
    execute_analysis(database, P1_ANALYSIS_ID)
    assert calls == [
        {
            'read_only': True,
            'config': {
                'enable_external_access': 'false',
                'autoinstall_known_extensions': 'false',
                'autoload_known_extensions': 'false',
            },
        }
    ]


@pytest.mark.parametrize(
    'sql', ['DELETE FROM c100', 'SELECT 1; SELECT 2', 'SELECT $not_declared']
)
def test_duckdb_parser_rejects_non_select_or_multiple_statements(
    database, monkeypatch, sql
):
    monkeypatch.setattr(
        analysis, 'load_analysis_catalog', lambda: _with_sql(sql)
    )
    with pytest.raises(CatalogError, match='SELECT|parâmetro'):
        execute_analysis(database, P1_ANALYSIS_ID)


def test_read_only_connection_rejects_writes(database, monkeypatch):
    def attempt_write(connection, *_args):
        connection.execute('DELETE FROM c100')

    before = sha256(database.read_bytes()).hexdigest()
    monkeypatch.setattr(analysis, '_query_result', attempt_write)
    with pytest.raises(AnalysisError, match='Falha controlada'):
        execute_analysis(database, P1_ANALYSIS_ID)
    assert sha256(database.read_bytes()).hexdigest() == before


def test_external_file_access_is_blocked(database, monkeypatch, tmp_path):
    secret = tmp_path / 'private.txt'
    secret.write_text('synthetic private content', encoding='utf-8')

    def attempt_external_read(connection, *_args):
        connection.execute('SELECT * FROM read_text(?)', [str(secret)])

    monkeypatch.setattr(analysis, '_query_result', attempt_external_read)
    with pytest.raises(AnalysisError, match='Falha controlada'):
        execute_analysis(database, P1_ANALYSIS_ID)


def test_deadline_includes_complete_execution_without_timing_assumptions(
    database, monkeypatch
):
    moments = iter([0.0, 31.0])
    monkeypatch.setattr(analysis.time, 'monotonic', lambda: next(moments))
    with pytest.raises(AnalysisTimeoutError, match='tempo limite'):
        execute_analysis(database, P1_ANALYSIS_ID)


def test_timer_actively_interrupts_query_and_cleans_up(database, monkeypatch):
    interrupted = Event()
    timers = []
    cleanup = []
    connect = duckdb.connect
    active_query = 'SELECT SUM(i % 7) FROM range(1000000000000) AS source(i)'

    class ControlledTimer:
        def __init__(self, seconds, callback):
            self.callback = callback
            self.worker = None
            timers.append(self)

        def start(self):
            pass

        def fire(self):
            self.worker = Thread(target=self.callback, daemon=True)
            self.worker.start()

        def cancel(self):
            cleanup.append('cancel')

        def join(self):
            if self.worker is not None:
                self.worker.join(timeout=2)
                assert not self.worker.is_alive()
            cleanup.append('join')

    class ControlledConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            if sql == active_query:
                timers[0].fire()
            return self.connection.execute(sql, *args)

        def extract_statements(self, sql):
            return self.connection.extract_statements(sql)

        def interrupt(self):
            self.connection.interrupt()
            interrupted.set()

        def close(self):
            self.connection.close()
            cleanup.append('close')

    def controlled_query(connection, *_args):
        connection.execute(active_query).fetchone()
        raise AssertionError('DuckDB não interrompeu a consulta ativa.')

    monkeypatch.setattr(analysis, 'Timer', ControlledTimer)
    monkeypatch.setattr(
        analysis.duckdb,
        'connect',
        lambda *args, **kwargs: ControlledConnection(connect(*args, **kwargs)),
    )
    monkeypatch.setattr(analysis, '_query_result', controlled_query)
    with pytest.raises(AnalysisTimeoutError, match='tempo limite'):
        execute_analysis(database, P1_ANALYSIS_ID)
    assert interrupted.is_set()
    assert cleanup == ['cancel', 'join', 'close']


@pytest.mark.parametrize(
    'value', [True, 50.0, 'NaN', Decimal('Infinity'), '-1', '101']
)
def test_decimal_parameter_rejects_inexact_or_out_of_range_values(
    database, value
):
    with pytest.raises(AnalysisError, match='Parâmetros'):
        execute_analysis(
            database, 'supplier_concentration', {'threshold_percent': value}
        )


def test_exact_decimal_parameter_and_six_digit_quantity_are_preserved(
    database,
):
    result = execute_analysis(
        database,
        'supplier_concentration',
        {'threshold_percent': '99.1234567890123456789'},
    )
    assert result.parameters['threshold_percent'] == Decimal(
        '99.1234567890123456789'
    )
    assert len(result.rows) == 1
    quantity = execute_analysis(database, 'block_k_activity')
    assert quantity.rows[0]['total_finished_quantity'] == Decimal('1.123456')


@pytest.mark.parametrize(
    'analysis_id', ['cancelled_documents', 'document_sequence_gaps']
)
def test_file_scope_binds_only_used_parameters_and_rejects_partial_period(
    database, analysis_id
):
    result = execute_analysis(database, analysis_id)
    assert result.parameters['start_date'] == date(2026, 1, 1)
    assert result.parameters['end_date'] == date(2026, 1, 31)
    with pytest.raises(AnalysisError, match='período integral'):
        execute_analysis(database, analysis_id, {'start_date': '2026-01-02'})


def test_nullable_cancelled_details_and_absent_optional_participants_are_valid(
    database,
):
    _change_database(
        database, 'UPDATE c100 SET dt_doc=NULL, vl_doc=NULL WHERE record_id=5'
    )
    _change_database(database, 'DELETE FROM "0150"')
    result = execute_analysis(database, 'cancelled_documents')
    assert result.row_count == 1
    assert result.rows[0]['dt_doc'] is None
    assert result.rows[0]['vl_doc'] is None
    assert result.rows[0]['participant_name'] is None
    _change_database(
        database,
        'INSERT INTO c100 SELECT * REPLACE '
        '(record_id + 1000 AS record_id, source_line + 1000 AS source_line) '
        'FROM c100 WHERE record_id=5',
    )
    duplicates = execute_analysis(database, 'possible_duplicate_documents')
    assert duplicates.rows == ()
    assert duplicates.status == 'empty_result'
    assert any('2 C100 sem DT_DOC' in note for note in duplicates.limitations)
    _change_database(database, "UPDATE c100 SET num_doc='6' WHERE record_id=4")
    gaps = execute_analysis(database, 'document_sequence_gaps')
    assert [
        (row['first_missing_number'], row['last_missing_number'])
        for row in gaps.rows
    ] == [(4, 4)]


def test_known_empty_k230_is_available_but_unverified_quantity_is_not(
    database,
):
    _change_database(database, 'UPDATE k230 SET dt_ini_op=NULL')
    undated = execute_analysis(database, 'block_k_activity')
    assert undated.status == 'empty_result'
    assert (
        'K230 sem DT_INI_OP no arquivo, excluídos do filtro por data: 1.'
        in undated.limitations
    )
    assert (
        'Nenhum K230 elegível com data de início no período selecionado; '
        'os 1 registros sem data no arquivo impedem concluir ausência de '
        'atividade.' in undated.limitations
    )
    _change_database(database, 'DELETE FROM k230')
    empty = execute_analysis(database, 'block_k_activity')
    assert empty.status == 'empty_result'
    assert (
        'K230 sem DT_INI_OP no arquivo, excluídos do filtro por data: 0.'
        in empty.limitations
    )
    assert (
        'Nenhum K230 elegível com data de início no período selecionado.'
        in empty.limitations
    )
    _change_database(
        database,
        "DELETE FROM interpreted_fields WHERE record_code='K230' "
        "AND alias='qtd_enc'",
    )
    with pytest.raises(
        AnalysisUnavailableError, match='interpretação verificada'
    ):
        execute_analysis(database, 'block_k_activity')


@pytest.mark.parametrize(
    ('analysis_id', 'mutation'),
    [
        ('block_k_activity', 'UPDATE k230 SET qtd_enc=NULL'),
        (
            'supplier_concentration',
            'UPDATE c100 SET vl_doc=NULL WHERE record_id=6',
        ),
        (
            'supplier_concentration',
            'UPDATE c100 SET cod_part=NULL WHERE record_id=6',
        ),
        (
            'supplier_concentration',
            "UPDATE c100 SET cod_part='   ' WHERE record_id=6",
        ),
        ('generic_ncm_high_value', 'UPDATE c170 SET vl_item=NULL'),
        (
            'possible_credit_on_consumption_cfop',
            "UPDATE c190 SET cfop='1556',vl_icms=NULL WHERE record_id=106",
        ),
    ],
)
def test_missing_values_needed_for_selection_or_totals_are_explicit(
    database, analysis_id, mutation
):
    _change_database(database, mutation)
    with pytest.raises(AnalysisUnavailableError, match='Dados necessários'):
        execute_analysis(database, analysis_id)


@pytest.mark.parametrize(
    ('analysis_id', 'record'),
    [('supplier_concentration', '0150'), ('generic_ncm_high_value', '0200')],
)
def test_duplicate_dimension_keys_cannot_amplify_results(
    database, analysis_id, record
):
    _change_database(
        database, f'INSERT INTO "{record}" SELECT * FROM "{record}"'
    )
    with pytest.raises(AnalysisUnavailableError, match='amplificação'):
        execute_analysis(database, analysis_id)


def test_duplicate_group_requires_values_but_unique_null_is_allowed(
    database,
):
    _change_database(database, 'UPDATE c100 SET vl_doc=NULL WHERE record_id=3')
    assert (
        execute_analysis(database, 'possible_duplicate_documents').row_count
        == 0
    )
    _change_database(
        database, "UPDATE c100 SET num_doc='1', vl_doc=NULL WHERE record_id=2"
    )
    with pytest.raises(AnalysisUnavailableError, match='grupo duplicado'):
        execute_analysis(database, 'possible_duplicate_documents')


def test_generic_ncm_cannot_be_determined_without_corresponding_item(database):
    _change_database(database, "UPDATE c170 SET cod_item='missing'")
    with pytest.raises(AnalysisUnavailableError, match='determinar NCM'):
        execute_analysis(database, 'generic_ncm_high_value')
    _change_database(database, "UPDATE c170 SET cod_item='item'")
    for missing_ncm in ('NULL', "''", "'   '"):
        _change_database(database, f'UPDATE "0200" SET cod_ncm={missing_ncm}')
        with pytest.raises(
            AnalysisUnavailableError, match='Dados necessários'
        ):
            execute_analysis(database, 'generic_ncm_high_value')


def test_missing_or_invalid_database_has_controlled_error(tmp_path):
    with pytest.raises(AnalysisUnavailableError, match='indisponível'):
        execute_analysis(tmp_path / 'missing.duckdb', P1_ANALYSIS_ID)
    invalid = tmp_path / 'invalid.duckdb'
    invalid.write_text('not a DuckDB file', encoding='utf-8')
    with pytest.raises(AnalysisUnavailableError, match='incompatível'):
        execute_analysis(invalid, P1_ANALYSIS_ID)
