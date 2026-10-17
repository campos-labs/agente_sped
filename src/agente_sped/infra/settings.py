"""Configuração local, carregada somente quando solicitada."""

from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Capacidades opcionais, sem exigir credenciais no caminho local."""

    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    ai_routing_enabled: bool = Field(
        default=False, validation_alias='AI_ROUTING_ENABLED'
    )
    ai_narrative_enabled: bool = Field(
        default=False, validation_alias='AI_NARRATIVE_ENABLED'
    )
    exploratory_sql_enabled: bool = Field(
        default=False, validation_alias='EXPLORATORY_SQL_ENABLED'
    )
    gemini_api_key: SecretStr | None = Field(
        default=None, validation_alias='GEMINI_API_KEY', repr=False
    )
    gemini_model: str = Field(
        default='gemini-3.5-flash-lite',
        validation_alias='GEMINI_MODEL',
        min_length=1,
        max_length=128,
        pattern=r'^gemini-[a-zA-Z0-9._-]+$',
    )
    gemini_timeout_seconds: float = Field(
        default=30,
        validation_alias='GEMINI_TIMEOUT_SECONDS',
        ge=1,
        le=60,
        allow_inf_nan=False,
    )

    @field_validator('gemini_api_key', mode='before')
    @classmethod
    def empty_key_is_absent(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator('gemini_timeout_seconds', mode='before')
    @classmethod
    def reject_boolean_timeout(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError('O timeout deve ser um número de segundos.')
        return value

    @model_validator(mode='after')
    def reject_unavailable_features(self) -> Self:
        """Recusa ativação de capacidades que ainda não foram entregues."""
        flags = {
            'AI_NARRATIVE_ENABLED': self.ai_narrative_enabled,
            'EXPLORATORY_SQL_ENABLED': self.exploratory_sql_enabled,
        }
        enabled = [name for name, value in flags.items() if value]
        if enabled:
            raise ValueError(
                'Recursos ainda indisponíveis nesta fundação: '
                + ', '.join(enabled)
            )
        return self
