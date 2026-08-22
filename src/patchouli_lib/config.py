from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from patchouli_lib.admin.passwords import parse_password_hash

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
    admin_password_hash: SecretStr | None = None
    admin_session_signing_secret: SecretStr | None = None
    admin_origin: str | None = None
    admin_session_ttl_seconds: Annotated[int, Field(ge=300, le=86_400)] = 1_800

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

    @field_validator(
        "admin_password_hash",
        "admin_session_signing_secret",
        "admin_origin",
        mode="before",
    )
    @classmethod
    def normalize_optional_admin_values(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("admin_password_hash")
    @classmethod
    def require_valid_admin_password_hash(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        parse_password_hash(value.get_secret_value())
        return value

    @field_validator("admin_session_signing_secret")
    @classmethod
    def require_strong_admin_session_secret(
        cls,
        value: SecretStr | None,
    ) -> SecretStr | None:
        if value is None:
            return None
        length = len(value.get_secret_value().encode("utf-8"))
        if length < 32 or length > 1_024:
            message = "The admin session signing secret must contain 32 to 1024 UTF-8 bytes."
            raise ValueError(message)
        return value

    @field_validator("admin_origin")
    @classmethod
    def require_exact_admin_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        try:
            _ = parsed.port
        except ValueError:
            raise ValueError(
                "The admin origin must be one exact HTTP(S) origin without credentials."
            ) from None
        if (
            value != value.strip()
            or parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            message = "The admin origin must be one exact HTTP(S) origin without credentials."
            raise ValueError(message)
        return value.removesuffix("/")

    @model_validator(mode="after")
    def validate_environment_secrets(self) -> "Settings":
        if self.environment == "production" and self.retrieval_cursor_signing_secret is None:
            message = "The retrieval cursor signing secret is required in production."
            raise ValueError(message)
        admin_values = (
            self.admin_password_hash,
            self.admin_session_signing_secret,
            self.admin_origin,
        )
        if any(value is not None for value in admin_values) and not all(
            value is not None for value in admin_values
        ):
            message = (
                "Admin password hash, session signing secret, and origin must be set together."
            )
            raise ValueError(message)
        if (
            self.environment == "production"
            and self.admin_origin is not None
            and not self.admin_origin.startswith("https://")
        ):
            message = "The admin origin must use HTTPS in production."
            raise ValueError(message)
        return self

    @property
    def admin_enabled(self) -> bool:
        return self.admin_password_hash is not None
