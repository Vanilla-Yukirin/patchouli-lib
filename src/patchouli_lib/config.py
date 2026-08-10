from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

Environment = Literal["development", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PATCHOULI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "PatchouliLib"
    environment: Environment = "development"
    database_url: str = "sqlite:///./data/patchouli.db"
    log_level: str = "info"

    @field_validator("database_url")
    @classmethod
    def require_sqlite(cls, value: str) -> str:
        url = make_url(value)
        if url.get_backend_name() != "sqlite":
            message = "The bootstrap implementation supports SQLite only."
            raise ValueError(message)
        return value
