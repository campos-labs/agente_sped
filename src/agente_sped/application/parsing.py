"""Interpret verified positions without changing their source values."""

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from agente_sped.domain.layout import (
    FieldDefinition,
    LayoutCatalog,
    RecordDefinition,
)
from agente_sped.domain.records import ParsedFile, ParsedRecord, Value

_DATE = re.compile(r'[0-9]{8}')
_DECIMAL = re.compile(r'[0-9]+(?:,[0-9]+)?')
_INTEGER = re.compile(r'[0-9]+')
_EXPECTED_PARENTS = {
    '0001': '0000',
    '0150': '0001',
    '0200': '0001',
    'C001': '0000',
    'C100': 'C001',
    'C170': 'C100',
    'C190': 'C100',
    'K001': '0000',
    'K100': 'K001',
    'K230': 'K100',
}
_SUPPORTED_VERSION = '020'
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024


class FileSizeLimitError(ValueError):
    """The source exceeds the caller's configured byte limit."""

    def __init__(self, max_file_bytes: int) -> None:
        self.max_file_bytes = max_file_bytes
        super().__init__('file_size_limit_exceeded')


def _coerce(raw: str, definition: FieldDefinition) -> Value:
    if raw == '':
        return None
    if definition.value_type == 'string':
        return raw
    if definition.value_type == 'date':
        if not _DATE.fullmatch(raw):
            raise ValueError('invalid_date')
        try:
            return date(int(raw[4:8]), int(raw[2:4]), int(raw[:2]))
        except ValueError as error:
            raise ValueError('invalid_date') from error
    if definition.value_type == 'decimal':
        if not _DECIMAL.fullmatch(raw):
            raise ValueError('invalid_decimal')
        fraction = raw.partition(',')[2]
        if (
            definition.decimals is not None
            and len(fraction) > definition.decimals
        ):
            raise ValueError('decimal_scale_exceeded')
        return Decimal(raw.replace(',', '.'))
    if definition.value_type == 'integer':
        if not _INTEGER.fullmatch(raw):
            raise ValueError('invalid_integer')
        return int(raw)
    raise ValueError('unsupported_field_type')


def _values(
    source_text: str,
) -> tuple[str, dict[int, str], tuple[str, ...]]:
    parts = source_text.split('|')
    errors = ()
    if source_text.startswith('|'):
        parts = parts[1:]
    else:
        errors = ('invalid_line_format',)
    if source_text.endswith('|'):
        parts = parts[:-1]
    elif 'invalid_line_format' not in errors:
        errors = ('invalid_line_format',)
    raw_values = dict(enumerate(parts, start=1))
    code = raw_values.get(1, '')
    if not re.fullmatch(r'[0-9A-Z]{4}', code):
        errors = (*errors, 'invalid_record_code')
    return code, raw_values, errors


@dataclass
class _Hierarchy:
    stack: list[ParsedRecord] = field(default_factory=list)

    def parent(
        self,
        code: str,
        definition: RecordDefinition | None,
        line_errors: tuple[str, ...],
    ) -> tuple[ParsedRecord | None, tuple[str, ...]]:
        if definition is None or line_errors:
            # An unknown level can close or open any branch. Retain only the
            # file root; a verified block opener can establish a new branch.
            self.stack = [r for r in self.stack if r.record_code == '0000']
            errors = ('unknown_record',) if definition is None else ()
            return None, errors
        level = definition.level
        if level is None:
            self.stack = [r for r in self.stack if r.record_code == '0000']
            return None, ('unknown_record_level',)
        while self.stack and self.stack[-1].record_level >= level:
            self.stack.pop()
        parent = self.stack[-1] if self.stack else None
        errors: tuple[str, ...] = ()
        if level > 0 and (parent is None or parent.record_level != level - 1):
            errors = ('hierarchy_level_gap',)
            parent = None
        expected = _EXPECTED_PARENTS.get(code)
        if expected is not None:
            if not definition.verified_structure:
                errors = (*errors, 'unverified_structure')
            if parent is None or parent.record_code != expected:
                errors = (*errors, 'unexpected_parent')
            elif parent.structural_errors:
                errors = (*errors, 'invalid_parent_structure')
        return parent, errors

    def remember(self, record: ParsedRecord) -> None:
        if record.record_level is not None and not any(
            e in record.structural_errors
            for e in ('invalid_line_format', 'invalid_record_code')
        ):
            self.stack.append(record)


def _interpret(
    raw_values: dict[int, str], definition: RecordDefinition | None
) -> tuple[dict[int, Value], dict[int, str]]:
    typed_values: dict[int, Value] = {}
    errors: dict[int, str] = {}
    if definition is None:
        return typed_values, errors
    for item in definition.fields:
        if not item.verified:
            continue
        if item.position not in raw_values:
            typed_values[item.position] = None
            errors[item.position] = 'missing_field_position'
            continue
        try:
            typed_values[item.position] = _coerce(
                raw_values[item.position], item
            )
        except ValueError as error:
            typed_values[item.position] = None
            errors[item.position] = str(error)
    return typed_values, errors


def _shape_errors(
    raw_values: dict[int, str], definition: RecordDefinition | None
) -> tuple[str, ...]:
    if definition is None or not definition.verified_structure:
        return ()
    positions = {item.position for item in definition.fields}
    if raw_values.keys() - positions:
        return ('unexpected_field_position',)
    return ()


def _header(records: list[ParsedRecord]) -> ParsedRecord:
    headers = [r for r in records if r.record_code == '0000']
    if len(headers) != 1 or not records or headers[0] is not records[0]:
        raise ValueError('invalid_file_header')
    header = headers[0]
    if header.structural_errors:
        raise ValueError('invalid_file_header')
    if header.raw_values.get(2) != _SUPPORTED_VERSION:
        raise ValueError('unsupported_layout_version')
    return header


def parse_sped(
    path: Path,
    layout: LayoutCatalog,
    file_id: str | None = None,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> ParsedFile:
    if layout.layout_version != _SUPPORTED_VERSION:
        raise ValueError('unsupported_layout_version')
    if layout.obligation != 'EFD_ICMS_IPI':
        raise ValueError('unsupported_obligation')
    identity = file_id if file_id is not None else str(uuid.uuid4())
    if not identity:
        raise ValueError('invalid_file_id')
    if max_file_bytes <= 0:
        raise ValueError('invalid_file_size_limit')
    with path.open('rb') as handle:
        source_bytes = handle.read(max_file_bytes + 1)
    if len(source_bytes) > max_file_bytes:
        raise FileSizeLimitError(max_file_bytes)
    lines = source_bytes.split(b'\n')
    if lines and lines[-1] == b'':
        lines.pop()
    hierarchy = _Hierarchy()
    records: list[ParsedRecord] = []
    for source_line, source in enumerate(lines, start=1):
        source_text = source.removesuffix(b'\r').decode('latin-1')
        code, raw_values, line_errors = _values(source_text)
        definition = layout.records.get(code)
        line_errors = (*line_errors, *_shape_errors(raw_values, definition))
        parent, hierarchy_errors = hierarchy.parent(
            code, definition, line_errors
        )
        typed_values, field_errors = _interpret(raw_values, definition)
        record = ParsedRecord(
            file_id=identity,
            record_id=len(records) + 1,
            record_code=code,
            record_level=definition.level if definition else None,
            source_line=source_line,
            parent_record_id=parent.record_id if parent else None,
            parent_record_code=parent.record_code if parent else None,
            source_text=source_text,
            raw_values=raw_values,
            typed_values=typed_values,
            field_errors=field_errors,
            structural_errors=tuple(
                dict.fromkeys((*line_errors, *hierarchy_errors))
            ),
        )
        records.append(record)
        hierarchy.remember(record)
    header = _header(records)
    start = header.typed_values.get(4)
    end = header.typed_values.get(5)
    if type(start) is not date or type(end) is not date or start > end:
        raise ValueError('invalid_file_period')
    return ParsedFile(
        file_id=identity,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_bytes=source_bytes,
        layout_version=_SUPPORTED_VERSION,
        period_start=start,
        period_end=end,
        records=tuple(records),
    )
