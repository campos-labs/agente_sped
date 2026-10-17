"""Typed requests and provider proposals; no SQL or filesystem authority."""

from datetime import date
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderFailure(ValueError):
    """An optional provider could not return a usable routing proposal."""


class RoutingParameters(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    start_date: str | None = None
    end_date: str | None = None
    top_n: int | None = None
    max_gap_size: int | None = None
    threshold_percent: str | None = None
    min_item_value: str | None = None


class RoutingDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    status: Literal['matched', 'needs_clarification', 'out_of_catalog']
    analysis_id: str | None = None
    parameters: RoutingParameters = Field(default_factory=RoutingParameters)
    reason: Literal[
        'catalog_match', 'ambiguous', 'unsupported', 'scope_confirmation'
    ]

    @model_validator(mode='after')
    def check_decision(self) -> Self:
        if (self.status == 'matched') != (self.analysis_id is not None):
            raise ValueError('Uma rota executável exige uma análise.')
        if self.status != 'matched' and self.parameters.model_dump(
            exclude_none=True
        ):
            raise ValueError('Uma rota não executável não propõe parâmetros.')
        return self


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    question: str | None = Field(default=None, min_length=1, max_length=2048)
    analysis_id: str | None = Field(default=None, min_length=1, max_length=80)
    parameters: dict[str, str | int | Decimal | date] = Field(
        default_factory=dict, max_length=6
    )

    @model_validator(mode='after')
    def check_input(self) -> Self:
        if self.analysis_id is None and not (self.question or '').strip():
            raise ValueError('Informe pergunta ou seleção explícita.')
        return self
