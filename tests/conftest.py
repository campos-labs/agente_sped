"""Fixtures de leitura compartilhadas entre módulos de teste."""

from pathlib import Path

import pytest

from agente_sped.infra.layout_catalog import build_layout_catalog


@pytest.fixture(scope='session')
def compiled_layout_catalog_path(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """Constrói um catálogo temporário compartilhado somente em leitura."""
    path = tmp_path_factory.mktemp('shared-layout') / 'layout_catalog.duckdb'
    build_layout_catalog(path)
    return path
