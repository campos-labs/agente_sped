"""Análises locais catalogadas, sem SQL fornecido pelo consumidor."""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path

from duckdb import Error as DuckDBError
from pydantic import ValidationError

from agente_sped.application.analysis import AnalysisResult, execute_analysis
from agente_sped.application.ingestion import analyze_catalogued_file
from agente_sped.application.parsing import DEFAULT_MAX_FILE_BYTES
from agente_sped.infra.analysis_catalog import (
    P1_ANALYSIS_ID,
    load_analysis_catalog,
)
from agente_sped.infra.settings import Settings


def _json_value(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError('Tipo de resultado não serializável.')


def _payload(
    result: AnalysisResult, *, show_sensitive: bool
) -> dict[str, object]:
    payload = asdict(result)
    if not show_sensitive:
        definition = load_analysis_catalog().analyses[result.analysis_id]
        for row in payload['rows']:
            for name in definition.privacy.sensitive_columns:
                if row.get(name) is not None:
                    row[name] = '***'
    return payload


def _parameters(arguments: argparse.Namespace) -> dict[str, object]:
    return {
        name: getattr(arguments, name)
        for name in (
            'start_date',
            'end_date',
            'top_n',
            'threshold_percent',
            'max_gap_size',
            'min_item_value',
        )
        if getattr(arguments, name, None) is not None
    }


def _execute(arguments: argparse.Namespace) -> int:
    parameters = _parameters(arguments)
    try:
        if arguments.command == 'analyze':
            result = analyze_catalogued_file(
                arguments.source,
                arguments.workspace,
                arguments.analysis,
                parameters,
                row_limit=arguments.max_rows,
                timeout_seconds=arguments.timeout_seconds,
                max_file_bytes=arguments.max_file_mib * 1024 * 1024,
            )
        else:
            result = execute_analysis(
                arguments.workspace / 'analysis.duckdb',
                arguments.analysis,
                parameters,
                row_limit=arguments.max_rows,
                timeout_seconds=arguments.timeout_seconds,
            )
    except DuckDBError:
        print('Falha no processamento local do DuckDB.', file=sys.stderr)
        return 2
    except FileExistsError:
        print(
            'O diretório de workspace já existe. '
            'Reutilize-o com run --workspace DIRETÓRIO --analysis ID.',
            file=sys.stderr,
        )
        return 2
    except OSError:
        print('Arquivo ou workspace indisponível.', file=sys.stderr)
        return 2
    except ValueError as error:
        print(f'Análise indisponível: {error}', file=sys.stderr)
        return 2
    print(
        json.dumps(
            _payload(result, show_sensitive=arguments.show_sensitive),
            ensure_ascii=False,
            indent=2,
            default=_json_value,
        )
    )
    return 0


def _analysis_arguments(
    parser: argparse.ArgumentParser,
    *,
    default_analysis: str | None = P1_ANALYSIS_ID,
) -> None:
    parser.add_argument(
        '--analysis',
        default=default_analysis,
        help='ID catalogado; seleção explícita confirma seus critérios.',
    )
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--start-date')
    parser.add_argument('--end-date')
    parser.add_argument('--top-n', type=int)
    parser.add_argument('--threshold-percent', type=Decimal)
    parser.add_argument('--max-gap-size', type=int)
    parser.add_argument('--min-item-value', type=Decimal)
    parser.add_argument('--max-rows', type=int)
    parser.add_argument('--timeout-seconds', type=float)
    parser.add_argument(
        '--show-sensitive',
        action='store_true',
        help='Exibe explicitamente as colunas sensíveis do resultado local.',
    )


def _saved_report_payload(
    workspace: Path, run_id: str, *, show_sensitive: bool
) -> dict[str, object]:
    from agente_sped.infra.report_store import load_report  # noqa: PLC0415

    report = load_report(workspace, run_id)
    payload = asdict(report)
    if not show_sensitive:
        for row in payload['result']['rows']:
            for name in report.sensitive_columns:
                if row.get(name) is not None:
                    row[name] = '***'
    return payload


def _workflow(arguments: argparse.Namespace, settings: Settings) -> int:
    # Core commands need not import the optional orchestration stack.
    from agente_sped.domain.routing import AnalysisRequest  # noqa: PLC0415
    from agente_sped.orchestration.graph import (  # noqa: PLC0415
        run_agent,
        run_export,
    )

    try:
        if arguments.command == 'report':
            payload = _saved_report_payload(
                arguments.workspace,
                arguments.run_id,
                show_sensitive=arguments.show_sensitive,
            )
            code = 0
        else:
            if arguments.command == 'export':
                outcome = run_export(
                    arguments.workspace,
                    arguments.run_id,
                    arguments.format,
                    show_sensitive=arguments.show_sensitive,
                )
            else:
                outcome = run_agent(
                    arguments.workspace,
                    AnalysisRequest(
                        question=arguments.question,
                        analysis_id=arguments.analysis,
                        parameters=_parameters(arguments),
                    ),
                    settings=settings,
                    row_limit=arguments.max_rows,
                    timeout_seconds=arguments.timeout_seconds,
                )
            payload = asdict(outcome)
            if outcome.state['status'] in ('completed', 'empty_result'):
                payload['report'] = _saved_report_payload(
                    arguments.workspace,
                    outcome.state['run_id'],
                    show_sensitive=arguments.show_sensitive,
                )
            code = (
                2
                if outcome.state['status']
                in (
                    'execution_error',
                    'analysis_unavailable',
                    'provider_failure',
                )
                else 0
            )
    except OSError, ValueError:
        print('Solicitação ou resultado local indisponível.', file=sys.stderr)
        return 2
    print(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_value)
    )
    return code


def _positive_mib(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            'Informe MiB inteiros positivos.'
        ) from error
    if number <= 0:
        raise argparse.ArgumentTypeError('Informe MiB inteiros positivos.')
    return number


def main(argv: Sequence[str] | None = None) -> int:
    """Valida a configuração e executa a ação local solicitada."""
    parser = argparse.ArgumentParser(
        prog='agente-sped',
        description='Agente SPED: análise local catalogada e rastreável.',
    )
    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {version("agente-sped")}',
    )
    commands = parser.add_subparsers(dest='command')
    analyze = commands.add_parser(
        'analyze',
        help='Interpreta o TXT em um workspace novo e executa uma análise.',
    )
    analyze.add_argument('source', type=Path)
    analyze.add_argument(
        '--max-file-mib',
        type=_positive_mib,
        default=DEFAULT_MAX_FILE_BYTES // (1024 * 1024),
        help='Limite de leitura em MiB (padrão: 100).',
    )
    _analysis_arguments(analyze)
    run = commands.add_parser(
        'run', help='Executa uma análise reutilizando um workspace existente.'
    )
    _analysis_arguments(run)
    ask = commands.add_parser(
        'ask', help='Orquestra uma pergunta ou seleção e salva seu relatório.'
    )
    ask.add_argument('question', nargs='?')
    _analysis_arguments(ask, default_analysis=None)
    for name, help_text in (
        ('report', 'Reabre um relatório salvo, sem executar a análise.'),
        ('export', 'Exporta somente as linhas de um relatório salvo.'),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--workspace', type=Path, required=True)
        command.add_argument('--run-id', required=True)
        command.add_argument('--show-sensitive', action='store_true')
        if name == 'export':
            command.add_argument(
                '--format', choices=('csv', 'xlsx'), required=True
            )
    arguments = parser.parse_args(argv)

    try:
        settings = Settings()
    except ValidationError as error:
        messages = (
            item['msg']
            for item in error.errors(include_input=False, include_url=False)
        )
        print('Configuração inválida: ' + '; '.join(messages), file=sys.stderr)
        return 2

    if arguments.command in ('analyze', 'run'):
        return _execute(arguments)
    if arguments.command in ('ask', 'report', 'export'):
        return _workflow(arguments, settings)

    print('Agente SPED Fiscal: instalação local disponível.')
    print(
        'Ingestão: analyze ARQUIVO --workspace DIRETÓRIO_NOVO. '
        'Nova consulta: run --workspace DIRETÓRIO --analysis ID. '
        'Relatórios: ask --workspace DIRETÓRIO --analysis ID. '
        'A interface gráfica permanece indisponível.'
    )
    return 0
