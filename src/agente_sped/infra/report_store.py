"""Snapshots privados e tipados, gravados atomicamente por run_id."""

import hashlib
import json
import math
import os
import re
from dataclasses import asdict, fields, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from uuid import UUID

from agente_sped.domain.reports import (
    MAX_NARRATIVE_BYTES,
    MAX_NARRATIVE_ROWS,
    MAX_REPORT_BYTES,
    MAX_REPORT_ROWS,
    NarrativePayload,
    ReportColumn,
    ReportPackage,
    ResultSnapshot,
    SuggestedAnalysis,
)


def _json_value(value: object) -> dict[str, str]:
    if isinstance(value, Decimal) and value.is_finite():
        return {'$decimal': str(value)}
    if type(value) is date:
        return {'$date': value.isoformat()}
    raise ValueError('Tipo de relatório não serializável.')


def encode_report_value(value: object) -> bytes:
    return json.dumps(
        value,
        default=_json_value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def _decode_object(pairs: list[tuple[str, Any]]) -> object:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Chave duplicada no snapshot.')
        result[key] = value
    if set(result) == {'$decimal'}:
        value = Decimal(result['$decimal'])
        if not value.is_finite():
            raise ValueError('Decimal não finito no snapshot.')
        return value
    if set(result) == {'$date'}:
        return date.fromisoformat(result['$date'])
    return result


def _shape(value: Any, model: type) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        field.name for field in fields(model)
    }:
        raise ValueError('Schema de snapshot incompatível.')
    return value


def _parameters(parameters: object) -> None:
    if not isinstance(parameters, dict):
        raise ValueError('Parâmetros de snapshot inválidos.')
    for name, value in parameters.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError('Nome de parâmetro de snapshot inválido.')
        if type(value) not in (str, int, date, Decimal):
            raise ValueError('Valor de parâmetro de snapshot inválido.')
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError('Parâmetro decimal não finito.')


def _validate_result(result: ResultSnapshot) -> None:
    if not all(
        type(value) is tuple
        for value in (result.rows, result.criteria, result.limitations)
    ):
        raise ValueError('Listas de resultado incompatíveis.')
    validate_run_id(result.run_id)
    for value in (
        result.analysis_catalog_sha256,
        result.source_sha256,
        result.layout_source_sha256,
        result.layout_metadata_sha256,
    ):
        if not isinstance(value, str) or not re.fullmatch(
            r'[0-9a-f]{64}', value
        ):
            raise ValueError('Hash de snapshot inválido.')
    for value in (
        result.analysis_id,
        result.version,
        result.catalog_version,
        result.file_id,
        result.layout_version,
    ):
        if not isinstance(value, str) or not value:
            raise ValueError('Identidade de snapshot inválida.')
    if any(
        type(n) is not int
        for n in (result.row_count, result.total_count, result.row_limit)
    ) or not (
        0
        <= result.row_count
        == len(result.rows)
        <= result.row_limit
        <= MAX_REPORT_ROWS
        and result.total_count >= result.row_count
        and result.row_limit >= 1
    ):
        raise ValueError('Contagens de snapshot inconsistentes.')
    if type(result.truncated) is not bool or result.truncated != (
        result.row_count < result.total_count
    ):
        raise ValueError('Truncamento de snapshot inconsistente.')
    expected = 'completed' if result.total_count else 'empty_result'
    if result.status != expected:
        raise ValueError('Estado de snapshot inconsistente.')
    for value in (result.timeout_seconds, result.elapsed_seconds):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError('Duração de snapshot inválida.')
    _parameters(result.parameters)


def _validate_rows(report: ReportPackage) -> None:
    names = [column.name for column in report.columns]
    if not names or len(names) != len(set(names)):
        raise ValueError('Colunas de snapshot inválidas.')
    sensitive = report.sensitive_columns
    if len(sensitive) != len(set(sensitive)) or not set(sensitive) <= set(
        names
    ):
        raise ValueError('Privacidade de snapshot incompatível.')
    types = {'string': str, 'integer': int, 'date': date, 'decimal': Decimal}
    for column in report.columns:
        if (
            not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', column.name)
            or column.value_type not in types
            or not isinstance(column.label, str)
            or column.format is not None
            and not isinstance(column.format, str)
        ):
            raise ValueError('Definição de coluna inválida.')
    for row in report.result.rows:
        if not isinstance(row, dict) or set(row) != set(names):
            raise ValueError('Linha incompatível com as colunas do snapshot.')
        for column in report.columns:
            value = row[column.name]
            if (
                value is not None
                and type(value) is not types[column.value_type]
            ):
                raise ValueError('Tipo de célula incompatível com o snapshot.')
            if isinstance(value, Decimal) and not value.is_finite():
                raise ValueError('Decimal de célula não finito.')


def validate_report(report: ReportPackage) -> None:
    if (
        not isinstance(report, ReportPackage)
        or not isinstance(report.result, ResultSnapshot)
        or not isinstance(report.narrative_payload, NarrativePayload)
        or any(
            type(value) is not tuple
            for value in (
                report.columns,
                report.sensitive_columns,
                report.suggestions,
            )
        )
        or not all(isinstance(c, ReportColumn) for c in report.columns)
        or not all(
            isinstance(s, SuggestedAnalysis) for s in report.suggestions
        )
    ):
        raise ValueError('Contrato de relatório inválido.')
    if report.schema_version != '1':
        raise ValueError('Versão de relatório incompatível.')
    if datetime.fromisoformat(report.created_at).utcoffset() is None:
        raise ValueError('Data de relatório sem fuso horário.')
    _validate_result(report.result)
    _validate_rows(report)
    for text in (
        report.title,
        report.summary,
        report.empty_message,
        *report.result.criteria,
        *report.result.limitations,
    ):
        if not isinstance(text, str):
            raise ValueError('Texto de relatório inválido.')
    payload = report.narrative_payload
    if (
        any(
            type(n) is not int
            for n in (
                payload.total_rows,
                payload.result_rows,
                payload.returned_rows,
            )
        )
        or type(payload.truncated) is not bool
        or payload.analysis_id != report.result.analysis_id
        or payload.total_rows != report.result.total_count
        or payload.result_rows != report.result.row_count
        or payload.returned_rows != len(payload.rows)
        or len(payload.rows) > min(report.result.row_count, MAX_NARRATIVE_ROWS)
        or payload.criteria != report.result.criteria
        or payload.limitations != report.result.limitations
        or payload.truncated
        != (payload.returned_rows < report.result.total_count)
        or len(encode_report_value(asdict(payload))) > MAX_NARRATIVE_BYTES
    ):
        raise ValueError('Payload narrativo incompatível.')
    for original, masked in zip(
        report.result.rows, payload.rows, strict=False
    ):
        expected = {
            name: '***'
            if name in report.sensitive_columns and value is not None
            else value
            for name, value in original.items()
        }
        if encode_report_value(masked) != encode_report_value(expected):
            raise ValueError('Payload narrativo sem mascaramento consistente.')
    for suggestion in report.suggestions:
        if not suggestion.analysis_id or not isinstance(suggestion.label, str):
            raise ValueError('Sugestão inválida.')
        _parameters(suggestion.parameters)


def validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not re.fullmatch(
        r'[0-9a-f]{32}', run_id
    ):
        raise ValueError('run_id UUID inválido.')
    if UUID(run_id).hex != run_id:
        raise ValueError('run_id UUID inválido.')


def private_directory(workspace_dir: Path, name: str) -> Path:
    if workspace_dir.is_symlink() or workspace_dir.is_junction():
        raise ValueError('Workspace de relatório inseguro.')
    root = workspace_dir.resolve(strict=True)
    if not root.is_dir() or name not in {'results', 'exports'}:
        raise ValueError('Workspace de relatório inválido.')
    target = root / name
    if (
        target.is_symlink()
        or target.is_junction()
        or target.exists()
        and not target.is_dir()
        or target.resolve().parent != root
    ):
        raise ValueError('Diretório de relatório inseguro.')
    target.mkdir(mode=0o700, exist_ok=True)
    return target


def _array(value: object) -> tuple:
    if not isinstance(value, list):
        raise ValueError('Lista de snapshot inválida.')
    return tuple(value)


def _restore(data: dict[str, Any]) -> ReportPackage:
    data = _shape(data, ReportPackage)
    result = _shape(data['result'], ResultSnapshot).copy()
    result['rows'] = _array(result['rows'])
    for name in ('criteria', 'limitations'):
        result[name] = _array(result[name])
    payload = _shape(data['narrative_payload'], NarrativePayload).copy()
    for name in ('rows', 'criteria', 'limitations'):
        payload[name] = _array(payload[name])
    return ReportPackage(
        **dict(
            data,
            result=ResultSnapshot(**result),
            columns=tuple(
                ReportColumn(**_shape(c, ReportColumn))
                for c in _array(data['columns'])
            ),
            sensitive_columns=_array(data['sensitive_columns']),
            narrative_payload=NarrativePayload(**payload),
            suggestions=tuple(
                SuggestedAnalysis(**_shape(s, SuggestedAnalysis))
                for s in _array(data['suggestions'])
            ),
        )
    )


def save_report(workspace_dir: Path, report: ReportPackage) -> Path:
    validate_report(report)
    payload = asdict(report)
    envelope = {
        'sha256': hashlib.sha256(encode_report_value(payload)).hexdigest(),
        'report': payload,
    }
    raw = encode_report_value(envelope)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError('Relatório excede o limite de bytes.')
    directory = private_directory(workspace_dir, 'results')
    target = directory / f'{report.run_id}.json'
    if target.exists():
        old = load_report(workspace_dir, report.run_id)
        if replace(old, suggestions=()) != replace(report, suggestions=()):
            raise ValueError('run_id já identifica outro snapshot.')
    temporary = None
    try:
        with NamedTemporaryFile(dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(raw)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def load_report(workspace_dir: Path, run_id: str) -> ReportPackage:
    validate_run_id(run_id)
    directory = private_directory(workspace_dir, 'results')
    path = directory / f'{run_id}.json'
    if (
        path.is_symlink()
        or path.is_junction()
        or path.resolve().parent != directory
    ):
        raise ValueError('Caminho de relatório inseguro.')
    with path.open('rb') as handle:
        raw = handle.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError('Relatório excede o limite de bytes.')
    try:
        envelope = json.loads(raw, object_pairs_hook=_decode_object)
        if not isinstance(envelope, dict) or set(envelope) != {
            'sha256',
            'report',
        }:
            raise ValueError('Envelope de relatório inválido.')
        actual = hashlib.sha256(
            encode_report_value(envelope['report'])
        ).hexdigest()
        if actual != envelope['sha256']:
            raise ValueError('Integridade do relatório divergente.')
        report = _restore(envelope['report'])
        if report.run_id != run_id:
            raise ValueError('Identidade de relatório divergente.')
        validate_report(report)
        return report
    except (KeyError, TypeError, ArithmeticError, RecursionError) as error:
        raise ValueError('Snapshot de relatório inválido.') from error
