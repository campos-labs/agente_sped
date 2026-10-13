"""Load the versioned catalog without changing its SQL or reference values."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, ScalarNode

P1_ANALYSIS_ID = 'movement_by_cfop'
_RECORD_CODE_LENGTH = 4


class CatalogError(ValueError):
    """The packaged catalog does not satisfy its operational contract."""


class _CatalogLoader(yaml.SafeLoader):
    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[object, object]:
        self.flatten_mapping(node)
        result: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as error:
                raise ConstructorError(
                    None, None, 'Chave YAML inválida.', key_node.start_mark
                ) from error
            if duplicate:
                raise ConstructorError(
                    None, None, 'Chave YAML duplicada.', key_node.start_mark
                )
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _decimal(loader: _CatalogLoader, node: ScalarNode) -> Decimal:
    text = loader.construct_scalar(node).replace('_', '')
    try:
        number = Decimal(text)
    except InvalidOperation as error:
        raise CatalogError('Número decimal YAML inválido.') from error
    if not number.is_finite():
        raise CatalogError('O catálogo exige números decimais finitos.')
    return number


_CatalogLoader.add_constructor('tag:yaml.org,2002:float', _decimal)


class _Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


PositiveInt = Annotated[int, Field(strict=True, gt=0)]


class ParameterBounds(_Contract):
    minimum: int | Decimal | None = None
    maximum: int | Decimal | None = None

    @model_validator(mode='after')
    def check_order(self) -> 'ParameterBounds':
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError('Limites do parâmetro fora de ordem.')
        return self


class ParameterDefinition(_Contract):
    type: Literal['date', 'integer', 'decimal']
    required: bool
    default_from: (
        Literal['file_profile.period_start', 'file_profile.period_end'] | None
    ) = None
    default: int | Decimal | None = None
    label: str
    constraints: ParameterBounds = Field(default_factory=ParameterBounds)

    @model_validator(mode='after')
    def check_default(self) -> 'ParameterDefinition':
        if self.type == 'date':
            if self.default_from is None or self.default is not None:
                raise ValueError('Parâmetro de data exige default do perfil.')
            return self
        if self.default_from is not None or self.default is None:
            raise ValueError('Parâmetro numérico exige default explícito.')
        numbers = (
            self.default,
            self.constraints.minimum,
            self.constraints.maximum,
        )
        if self.type == 'integer' and any(
            type(value) is not int for value in numbers if value is not None
        ):
            raise ValueError(
                'Parâmetro inteiro exige limites e default inteiros.'
            )
        if any(
            isinstance(value, Decimal) and not value.is_finite()
            for value in numbers
        ):
            raise ValueError('Parâmetro decimal exige valores finitos.')
        if (
            self.constraints.minimum is not None
            and self.default < self.constraints.minimum
        ) or (
            self.constraints.maximum is not None
            and self.default > self.constraints.maximum
        ):
            raise ValueError('Default fora dos limites do parâmetro.')
        return self


class Availability(_Contract):
    required_records: list[str]
    required_fields: dict[str, list[str]]
    record_presence_required: bool = True
    empty_result_is_valid: bool
    optional_records: list[str] = Field(default_factory=list)
    optional_fields: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode='after')
    def check_records(self) -> 'Availability':
        records = self.required_records + self.optional_records
        if len(records) != len(set(records)) or any(
            not record.isalnum() or len(record) != _RECORD_CODE_LENGTH
            for record in records
        ):
            raise ValueError('Registros ausentes, duplicados ou inválidos.')
        if set(self.required_fields) != set(self.required_records) or set(
            self.optional_fields
        ) != set(self.optional_records):
            raise ValueError(
                'Campos devem corresponder aos registros declarados.'
            )
        for names in (
            *self.required_fields.values(),
            *self.optional_fields.values(),
        ):
            if (
                not names
                or len(names) != len(set(names))
                or any(not name.replace('_', '').isalnum() for name in names)
            ):
                raise ValueError('Campos vazios, duplicados ou inválidos.')
        return self


class Execution(_Contract):
    engine: Literal['duckdb']
    mode: Literal['read_only']
    sql: Annotated[str, Field(min_length=1)]
    period_scope: Literal['document', 'file'] = 'document'


class ResultColumn(_Contract):
    name: str
    type: Literal['string', 'decimal', 'integer', 'date']
    label: str
    format: str | None = None


class ResultDefinition(_Contract):
    shape: Literal['table']
    row_limit: PositiveInt
    columns: list[ResultColumn]
    empty_message: str

    @model_validator(mode='after')
    def check_columns(self) -> 'ResultDefinition':
        names = [column.name for column in self.columns]
        if not names or len(names) != len(set(names)):
            raise ValueError('Colunas de resultado vazias ou duplicadas.')
        return self


class Routing(_Contract):
    clarification_required: bool
    utterances: list[str]


class NarrativeInput(_Contract):
    mode: str
    max_rows: PositiveInt
    include_total_count: bool
    overflow_behavior: str
    group_by: list[str] = Field(default_factory=list)


class Narrative(_Contract):
    input: NarrativeInput
    instruction: str
    must_not: list[str]


class Privacy(_Contract):
    default_mode: Literal['masked']
    protected_targets: list[str]
    ui_default: Literal['masked']
    raw_export_requires_explicit_action: Literal[True]
    sensitive_columns: list[str]


class Suggestion(_Contract):
    analysis_id: str
    label: str


class AnalysisDefinition(_Contract):
    version: str
    status: str
    display_order: int
    title: str
    description: str
    routing: Routing
    availability: Availability
    parameters: dict[str, ParameterDefinition]
    criteria: list[str]
    execution: Execution
    result: ResultDefinition
    privacy: Privacy
    narrative: Narrative
    suggestions: list[Suggestion]
    limitations: list[str]

    @model_validator(mode='after')
    def check_parameters(self) -> 'AnalysisDefinition':
        for name, source in (
            ('start_date', 'file_profile.period_start'),
            ('end_date', 'file_profile.period_end'),
        ):
            parameter = self.parameters.get(name)
            if (
                parameter is None
                or parameter.type != 'date'
                or parameter.default_from != source
            ):
                raise ValueError(
                    'Período deve derivar dos limites do arquivo.'
                )
        if any(
            not name.isascii()
            or not name.isidentifier()
            or name == 'runtime_row_limit'
            for name in self.parameters
        ):
            raise ValueError('Nome de parâmetro inválido ou reservado.')
        return self


class Enforcement(_Contract):
    require_single_file: Literal[True]
    require_single_statement: Literal[True]
    allowed_statement_roots: list[Literal['SELECT', 'WITH']]
    apply_result_row_limit_before_materialization: Literal[True]
    apply_timeout_in_executor: Literal[True]


class TechnicalColumns(_Contract):
    all: list[str]
    hierarchical: list[str]


class RelationContract(_Contract):
    naming: Literal['lowercase_record_code']
    examples: list[str]
    technical_columns: TechnicalColumns
    normalized_types: dict[str, str]
    empty_relation_policy: Literal['create_from_layout']
    notes: list[str]


class RuntimeDefinition(_Contract):
    engine: Literal['duckdb']
    execution_scope: Literal['single_file']
    parameter_style: Literal['duckdb_named']
    statement_type: Literal['select']
    access_mode: Literal['read_only']
    timeout_seconds: PositiveInt
    max_result_rows: PositiveInt
    enforcement: Enforcement
    relation_contract: RelationContract


class LoaderDefinition(_Contract):
    apply_defaults_before_validation: Literal[True]
    merge_strategy: Literal['recursive']
    fail_on_unknown_keys: Literal[True]
    fail_on_duplicate_analysis_ids: Literal[True]


class SpedDefinition(_Contract):
    type: Literal['EFD_ICMS_IPI']
    supported_layouts: list[Literal['020']]


class _CatalogDocument(_Contract):
    schema_version: Literal['1']
    catalog_version: str
    product_release: str
    catalog_id: str
    language: str
    sped: SpedDefinition
    loader: LoaderDefinition
    runtime: RuntimeDefinition
    defaults: dict[str, object]
    parameter_definitions: dict[str, object]
    reference_validation: dict[str, object]
    analyses: dict[str, dict[str, object]]


@dataclass(frozen=True)
class AnalysisCatalog:
    catalog_version: str
    source_sha256: str
    runtime: RuntimeDefinition
    analyses: dict[str, AnalysisDefinition]
    analysis_ids: tuple[str, ...]
    reference_validation: dict[str, object]

    @property
    def p1(self) -> AnalysisDefinition:
        return self.analyses[P1_ANALYSIS_ID]


def _merge_defaults(
    defaults: Mapping[str, object], definition: Mapping[str, object]
) -> dict[str, object]:
    merged = dict(defaults)
    for key, value in definition.items():
        previous = merged.get(key)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_defaults(previous, value)
        else:
            merged[key] = value
    return merged


def load_analysis_catalog(path: Path | None = None) -> AnalysisCatalog:
    """Load source metadata and validate every cataloged analysis contract."""
    source = (
        path
        if path is not None
        else files('agente_sped').joinpath('data/analysis_catalog.yml')
    )
    try:
        source_bytes = source.read_bytes()
        raw = yaml.load(source_bytes.decode('utf-8'), Loader=_CatalogLoader)
        document = _CatalogDocument.model_validate(raw)
        analyses = {
            analysis_id: AnalysisDefinition.model_validate(
                _merge_defaults(document.defaults, definition)
            )
            for analysis_id, definition in document.analyses.items()
        }
    except (
        OSError,
        UnicodeError,
        yaml.YAMLError,
        ValidationError,
        KeyError,
        RecursionError,
    ) as error:
        raise CatalogError(
            'Catálogo analítico inválido ou indisponível.'
        ) from error
    known_ids = tuple(document.analyses)
    if any(
        item.analysis_id not in known_ids
        for definition in analyses.values()
        for item in definition.suggestions
    ):
        raise CatalogError(
            'Sugestão aponta para uma análise ausente do catálogo.'
        )
    return AnalysisCatalog(
        catalog_version=document.catalog_version,
        source_sha256=sha256(source_bytes).hexdigest(),
        runtime=document.runtime,
        analyses=analyses,
        analysis_ids=known_ids,
        reference_validation=document.reference_validation,
    )
