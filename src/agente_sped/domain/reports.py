"""Contratos locais de relatório, independentes dos adaptadores."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agente_sped.domain.records import Value

MAX_REPORT_ROWS = 5000
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_NARRATIVE_ROWS = 20
MAX_NARRATIVE_BYTES = 32 * 1024


@dataclass(frozen=True)
class ResultSnapshot:
    run_id: str
    analysis_id: str
    version: str
    catalog_version: str
    analysis_catalog_sha256: str
    parameters: dict[str, object]
    rows: tuple[dict[str, Value], ...]
    row_count: int
    total_count: int
    truncated: bool
    status: Literal['completed', 'empty_result']
    file_id: str
    source_sha256: str
    layout_version: str
    layout_source_sha256: str
    layout_metadata_sha256: str
    row_limit: int
    timeout_seconds: float
    elapsed_seconds: float
    criteria: tuple[str, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class ReportColumn:
    name: str
    value_type: Literal['string', 'decimal', 'integer', 'date']
    label: str
    format: str | None


@dataclass(frozen=True)
class SuggestedAnalysis:
    analysis_id: str
    label: str
    parameters: dict[str, object]


@dataclass(frozen=True)
class NarrativePayload:
    analysis_id: str
    rows: tuple[dict[str, Value], ...]
    total_rows: int
    result_rows: int
    returned_rows: int
    truncated: bool
    criteria: tuple[str, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class ReportPackage:
    schema_version: str
    created_at: str
    result: ResultSnapshot
    title: str
    columns: tuple[ReportColumn, ...]
    sensitive_columns: tuple[str, ...]
    summary: str
    empty_message: str
    narrative_payload: NarrativePayload
    suggestions: tuple[SuggestedAnalysis, ...] = ()

    @property
    def run_id(self) -> str:
        return self.result.run_id


@dataclass(frozen=True)
class ExportReceipt:
    run_id: str
    format: Literal['csv', 'xlsx']
    data_path: Path
    context_path: Path | None
    row_count: int
    total_count: int
    truncated: bool
    privacy_mode: Literal['masked', 'raw']
    transformed_cells: int
