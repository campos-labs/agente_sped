"""Definições posicionais e cobertura verificada do leiaute."""

from dataclasses import dataclass
from typing import Literal

type ValueType = Literal['string', 'date', 'decimal', 'integer']


@dataclass(frozen=True)
class FieldDefinition:
    obligation: str
    layout_version: str
    record_code: str
    position: int
    original_name: str
    alias: str
    value_type: ValueType
    decimals: int | None
    verified: bool


@dataclass(frozen=True)
class RecordDefinition:
    code: str
    level: int
    verified_structure: bool
    fields: tuple[FieldDefinition, ...]


@dataclass(frozen=True)
class LayoutCatalog:
    obligation: str
    layout_version: str
    source_sha256: str
    metadata_sha256: str
    records: dict[str, RecordDefinition]

    def resolve(
        self,
        record_code: str,
        reference: str | int,
        require_verified: bool = True,
    ) -> FieldDefinition:
        """Resolva posição, nome original inequívoco ou alias posicional."""
        record = self.records[record_code.upper()]
        if isinstance(reference, int) and not isinstance(reference, bool):
            matches = [f for f in record.fields if f.position == reference]
        elif isinstance(reference, str):
            name = reference.casefold()
            matches = [
                f for f in record.fields if f.original_name.casefold() == name
            ]
            if len(matches) > 1:
                raise ValueError(
                    f'Nome ambíguo: {record.code}.{reference}; use posição '
                    'ou alias.'
                )
            if not matches:
                matches = [f for f in record.fields if f.alias == name]
        else:
            raise TypeError(
                'A referência deve ser um nome ou posição inteira.'
            )
        if not matches:
            raise KeyError(f'Campo desconhecido: {record.code}.{reference}')
        field = matches[0]
        if require_verified and not field.verified:
            raise ValueError(
                f'Campo não verificado: {record.code}.{field.original_name}'
                f' (posição {field.position}).'
            )
        return field
