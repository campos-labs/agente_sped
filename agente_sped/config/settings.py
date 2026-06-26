from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
    GOOGLE_API_KEY: str
    MODEL: str = "gemma-4-26b-a4b-it"
    MODEL_PROVIDER: str = "google-genai"
    MAX_RETRIES: int = 3
    TEMPERATURE: float = 0.0


settings = Settings()
