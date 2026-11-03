"""Limites e privacidade nas fronteiras locais, sem repetir ingestões."""

import json
from dataclasses import dataclass
from datetime import date

import pytest

from agente_sped.application import ingestion
from agente_sped.application.analysis import AnalysisUnavailableError
from agente_sped.domain.layout import LayoutCatalog
from agente_sped.domain.records import ParsedFile
from agente_sped.infra.analysis_catalog import CatalogError
from agente_sped.infra.workspace import Workspace
from agente_sped.interface import cli

_CLI_ERROR = 2


@pytest.mark.parametrize('host', ['0.0.0.0', '192.168.1.10'])
def test_serve_rejects_network_bind(host, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(['serve', '--host', host])
    assert error.value.code == _CLI_ERROR
    assert 'invalid choice' in capsys.readouterr().err


@pytest.mark.parametrize('host', [None, '::1'])
def test_serve_uses_loopback_only(host, tmp_path, monkeypatch):
    import uvicorn  # noqa: PLC0415

    calls = []
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kw: calls.append(kw))
    arguments = ['serve', '--session-root', str(tmp_path / 'sessions')]
    if host is not None:
        arguments.extend(['--host', host])
    assert cli.main(arguments) == 0
    assert calls[0]['host'] == (host or '127.0.0.1')
    assert calls[0]['workers'] == 1
    assert calls[0]['proxy_headers'] is False
    assert calls[0]['access_log'] is False


def test_service_reports_applied_read_limit(tmp_path, monkeypatch):
    source = tmp_path / 'source.txt'
    source.write_bytes(b'x' * 64)
    layout = LayoutCatalog('EFD_ICMS_IPI', '020', '0' * 64, '1' * 64, {})
    monkeypatch.setattr(ingestion, 'build_layout_catalog', lambda _: layout)
    monkeypatch.setattr(ingestion, 'load_layout_catalog', lambda _: layout)
    with pytest.raises(ValueError, match='limite de leitura de 32 bytes'):
        ingestion.analyze_catalogued_file(
            source,
            tmp_path / 'workspace',
            'movement_by_cfop',
            max_file_bytes=32,
        )
    assert not (tmp_path / 'workspace').exists()


@dataclass(frozen=True)
class _LocalResult:
    analysis_id: str
    rows: tuple[dict[str, object], ...]


def test_cli_propagates_limit_and_masks_sensitive_columns(
    tmp_path, monkeypatch, capsys
):
    calls = []
    result = _LocalResult(
        'cancelled_documents', ({'num_doc': '2001', 'cod_sit': '02'},)
    )

    def ingest(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(cli, 'analyze_catalogued_file', ingest)
    monkeypatch.setattr(
        cli, 'execute_analysis', lambda *args, **kwargs: result
    )
    common = [
        '--workspace',
        str(tmp_path / 'workspace'),
        '--analysis',
        'cancelled_documents',
    ]
    assert (
        cli.main(['analyze', 'synthetic.txt', *common, '--max-file-mib', '1'])
        == 0
    )
    assert calls[0][1]['max_file_bytes'] == 1024 * 1024
    assert calls[0][0][3] == {}
    assert json.loads(capsys.readouterr().out)['rows'][0]['num_doc'] == '***'
    assert cli.main(['run', *common, '--show-sensitive']) == 0
    assert json.loads(capsys.readouterr().out)['rows'][0]['num_doc'] == '2001'
    assert len(calls) == 1


@pytest.mark.parametrize(
    'error_type', [AnalysisUnavailableError, CatalogError]
)
def test_cli_preserves_ingestion_after_query_failure_and_explains_reuse(
    tmp_path, monkeypatch, capsys, error_type
):
    parsed = ParsedFile(
        file_id='synthetic',
        source_sha256='0' * 64,
        source_bytes=b'synthetic-source',
        layout_version='020',
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        records=(),
    )
    layout = LayoutCatalog('EFD_ICMS_IPI', '020', '0' * 64, '1' * 64, {})
    monkeypatch.setattr(ingestion, 'build_layout_catalog', lambda _: layout)
    monkeypatch.setattr(ingestion, 'load_layout_catalog', lambda _: layout)
    monkeypatch.setattr(ingestion, 'parse_sped', lambda *a, **kw: parsed)

    def materialize(parsed, layout, destination, **kwargs):
        destination.mkdir()
        database = destination / 'analysis.duckdb'
        database.write_bytes(b'synthetic-database')
        return Workspace(
            database,
            parsed.file_id,
            parsed.source_sha256,
            parsed.period_start,
            parsed.period_end,
        )

    def unavailable(*args, **kwargs):
        raise error_type('Dados necessários incompletos.')

    monkeypatch.setattr(ingestion, 'materialize_workspace', materialize)
    monkeypatch.setattr(ingestion, 'execute_analysis', unavailable)
    workspace = tmp_path / 'workspace'
    arguments = ['analyze', 'synthetic.txt', '--workspace', str(workspace)]
    assert cli.main(arguments) == _CLI_ERROR
    failure = capsys.readouterr()
    assert 'Dados necessários incompletos' in failure.err
    assert 'workspace foi criado e permanece disponível' in failure.err
    assert 'run --workspace' in failure.err
    database = workspace / 'analysis.duckdb'
    assert database.read_bytes() == b'synthetic-database'
    assert cli.main(arguments) == _CLI_ERROR
    assert 'já existe' in capsys.readouterr().err
    monkeypatch.setattr(
        cli,
        'execute_analysis',
        lambda *a, **kw: _LocalResult('movement_by_cfop', ()),
    )
    assert cli.main(['run', '--workspace', str(workspace)]) == 0
    assert json.loads(capsys.readouterr().out)['rows'] == []
    assert database.read_bytes() == b'synthetic-database'
