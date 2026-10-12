"""Verifica a instalação sem iniciar o protótipo ou serviços externos."""

import argparse
import sys
from collections.abc import Sequence
from importlib.metadata import version

from pydantic import ValidationError

from agente_sped.infra.settings import Settings


def main(argv: Sequence[str] | None = None) -> int:
    """Informa o estágio disponível e valida a configuração local."""
    parser = argparse.ArgumentParser(
        prog='agente-sped',
        description='Verifica a fundação técnica do Agente SPED.',
    )
    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {version("agente-sped")}',
    )
    parser.parse_args(argv)

    try:
        Settings()
    except ValidationError as error:
        messages = (
            item['msg']
            for item in error.errors(include_input=False, include_url=False)
        )
        print('Configuração inválida: ' + '; '.join(messages), file=sys.stderr)
        return 2

    print('Agente SPED Fiscal: fundação técnica instalada.')
    print(
        'O processamento fiscal e a interface serão entregues '
        'nas próximas FEATs.'
    )
    return 0
