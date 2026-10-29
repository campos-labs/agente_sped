"""Ingestão local e execução de uma análise catalogada."""

import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from agente_sped.application.analysis import (
    AnalysisError,
    AnalysisResult,
    execute_analysis,
    validate_analysis_parameters,
    validate_execution_limits,
)
from agente_sped.application.parsing import (
    DEFAULT_MAX_FILE_BYTES,
    FileSizeLimitError,
    parse_sped,
)
from agente_sped.domain.layout import LayoutCatalog
from agente_sped.domain.records import ParsedFile
from agente_sped.infra.analysis_catalog import (
    CatalogError,
    load_analysis_catalog,
)
from agente_sped.infra.layout_catalog import (
    build_layout_catalog,
    load_layout_catalog,
)
from agente_sped.infra.workspace import Workspace, materialize_workspace


@contextmanager
def _source_layout(
    source: Path, workspace_dir: Path, max_file_bytes: int
) -> Iterator[tuple[ParsedFile, LayoutCatalog, Path]]:
    if workspace_dir.exists() or workspace_dir.is_symlink():
        raise FileExistsError('O diretório de workspace já existe.')
    if type(max_file_bytes) is not int or not 0 < max_file_bytes < sys.maxsize:
        raise ValueError('O limite de leitura deve ser um inteiro positivo.')
    with TemporaryDirectory(prefix='agente-sped-layout-') as temporary:
        catalog_path = Path(temporary) / 'layout_catalog.duckdb'
        build_layout_catalog(catalog_path)
        layout = load_layout_catalog(catalog_path)
        try:
            parsed = parse_sped(source, layout, max_file_bytes=max_file_bytes)
        except FileSizeLimitError as error:
            limit = error.max_file_bytes
            unit = 1024 * 1024
            description = (
                f'{limit // unit} MiB ({limit} bytes)'
                if limit % unit == 0
                else f'{limit} bytes'
            )
            raise ValueError(
                f'O arquivo excede o limite de leitura de {description}.'
            ) from error
        yield parsed, layout, catalog_path


def ingest_file(
    source: Path,
    workspace_dir: Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> Workspace:
    """Materialize um arquivo sem selecionar ou executar uma análise."""
    with _source_layout(source, workspace_dir, max_file_bytes) as data:
        parsed, layout, catalog_path = data
        return materialize_workspace(
            parsed,
            layout,
            workspace_dir,
            layout_catalog_path=catalog_path,
        )


def analyze_catalogued_file(  # noqa: PLR0913
    source: Path,
    workspace_dir: Path,
    analysis_id: str,
    parameters: Mapping[str, object] | None = None,
    *,
    row_limit: int | None = None,
    timeout_seconds: float | None = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> AnalysisResult:
    """Cria um workspace novo e executa somente o ID solicitado."""
    catalog = load_analysis_catalog()
    if not isinstance(analysis_id, str) or analysis_id not in catalog.analyses:
        raise ValueError('Análise não pertence ao catálogo.')
    definition = catalog.analyses[analysis_id]
    limit, seconds = validate_execution_limits(
        catalog, definition, row_limit, timeout_seconds
    )
    with _source_layout(source, workspace_dir, max_file_bytes) as data:
        parsed, layout, catalog_path = data
        effective = validate_analysis_parameters(
            definition,
            parameters,
            period_start=parsed.period_start,
            period_end=parsed.period_end,
        )
        workspace = materialize_workspace(
            parsed,
            layout,
            workspace_dir,
            layout_catalog_path=catalog_path,
        )
    try:
        return execute_analysis(
            workspace.database_path,
            analysis_id,
            effective,
            row_limit=limit,
            timeout_seconds=seconds,
        )
    except (AnalysisError, CatalogError) as error:
        raise type(error)(
            f'{error} O workspace foi criado e permanece disponível. '
            'Reutilize-o com run --workspace DIRETÓRIO --analysis ID.'
        ) from error
