"""Configuração local, carregada somente quando solicitada."""

from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Flags reservadas pelo DESIGN, ainda indisponíveis na fundação."""

    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
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

    @model_validator(mode='after')
    def reject_unavailable_features(self) -> Self:
        """Recusa ativação de capacidades que ainda não foram entregues."""
        flags = {
            'AI_ROUTING_ENABLED': self.ai_routing_enabled,
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
