"""Positional records and their interpretation, independent of storage."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

type Value = str | date | Decimal | int | None


@dataclass(frozen=True)
class ParsedRecord:
    file_id: str
    record_id: int
    record_code: str
    record_level: int | None
    source_line: int
    parent_record_id: int | None
    parent_record_code: str | None
    source_text: str
    raw_values: dict[int, str]
    typed_values: dict[int, Value]
    field_errors: dict[int, str]
    structural_errors: tuple[str, ...]


@dataclass(frozen=True)
class ParsedFile:
    file_id: str
    source_sha256: str
    source_bytes: bytes
    layout_version: str
    period_start: date
    period_end: date
    records: tuple[ParsedRecord, ...]
