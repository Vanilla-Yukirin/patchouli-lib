from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

Environment = Literal["development", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PATCHOULI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "PatchouliLib"
    environment: Environment = "development"
    database_url: str = "sqlite:///./data/patchouli.db"
    log_level: str = "info"
    retrieval_cursor_signing_secret: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def require_sqlite(cls, value: str) -> str:
        url = make_url(value)
        if url.get_backend_name() != "sqlite":
            message = "The bootstrap implementation supports SQLite only."
            raise ValueError(message)
        return value

    @field_validator("retrieval_cursor_signing_secret")
    @classmethod
    def require_strong_cursor_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        if len(value.get_secret_value().encode("utf-8")) < 32:
            message = "The retrieval cursor signing secret must contain at least 32 bytes."
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def require_cursor_secret_in_production(self) -> "Settings":
        if self.environment == "production" and self.retrieval_cursor_signing_secret is None:
            message = "The retrieval cursor signing secret is required in production."
            raise ValueError(message)
        return self
