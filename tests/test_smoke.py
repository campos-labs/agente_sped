"""Smoke tests de importação, contratos locais, configuração e CLI."""

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from agente_sped.domain.routing import AnalysisRequest, RoutingDecision
from agente_sped.infra.settings import Settings
from agente_sped.interface.cli import main

_DEFAULT_TIMEOUT_SECONDS = 30


def test_package_import_has_no_network_side_effects(tmp_path: Path) -> None:
    script = """
import socket

def deny_network(*args, **kwargs):
    raise AssertionError('Package import attempted network access.')

socket.socket.connect = deny_network
socket.create_connection = deny_network

import agente_sped
import agente_sped.interface.cli

assert agente_sped.__doc__
"""
    environment = os.environ.copy()
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=environment,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''


def test_settings_have_local_defaults_without_credentials() -> None:
    settings = Settings(_env_file=None)

    assert settings.ai_routing_enabled is False
    assert settings.ai_narrative_enabled is False
    assert settings.exploratory_sql_enabled is False
    assert settings.gemini_api_key is None
    assert settings.gemini_model == 'gemini-3.5-flash-lite'
    assert settings.gemini_timeout_seconds == _DEFAULT_TIMEOUT_SECONDS


def test_routing_contracts_accept_a_valid_local_request() -> None:
    request = AnalysisRequest(analysis_id='movement_by_cfop')
    decision = RoutingDecision(
        status='matched',
        analysis_id=request.analysis_id,
        reason='catalog_match',
    )

    assert request.analysis_id == 'movement_by_cfop'
    assert decision.status == 'matched'


def test_routing_contract_rejects_unavailable_shape() -> None:
    with pytest.raises(ValidationError):
        AnalysisRequest(analysis_id='movement_by_cfop', unexpected='value')


@pytest.mark.parametrize(
    ('argument', 'expected'),
    [('--help', 'usage: agente-sped'), ('--version', 'agente-sped ')],
)
def test_cli_help_and_version_are_local(
    argument: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        main([argument])

    assert error.value.code == 0
    assert expected in capsys.readouterr().out


def test_cli_help_and_version_do_not_open_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_network(*args: object, **kwargs: object) -> None:
        raise AssertionError('CLI smoke test attempted network access.')

    monkeypatch.setattr(socket.socket, 'connect', deny_network)
    monkeypatch.setattr(socket, 'create_connection', deny_network)

    for argument in ('--help', '--version'):
        with pytest.raises(SystemExit) as error:
            main([argument])
        assert error.value.code == 0
