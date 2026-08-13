import pytest
from pydantic import SecretStr, ValidationError

from patchouli_lib.config import Settings


def test_rejects_non_sqlite_database() -> None:
    with pytest.raises(ValidationError, match="supports SQLite only"):
        Settings.model_validate({"database_url": "postgresql://example.invalid/db"})


def test_cursor_secret_is_optional_outside_production_and_redacted() -> None:
    without_retrieval = Settings.model_validate({"environment": "test"})
    assert without_retrieval.retrieval_cursor_signing_secret is None

    with_retrieval = Settings.model_validate(
        {
            "environment": "test",
            "retrieval_cursor_signing_secret": "s" * 32,
        }
    )
    secret = with_retrieval.retrieval_cursor_signing_secret
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == "s" * 32
    assert "s" * 32 not in repr(with_retrieval)


@pytest.mark.parametrize("secret", [None, "", "s" * 31])
def test_production_requires_strong_cursor_secret(secret: str | None) -> None:
    values: dict[str, object] = {"environment": "production"}
    if secret is not None:
        values["retrieval_cursor_signing_secret"] = secret

    with pytest.raises(ValidationError, match="retrieval cursor signing secret") as exc_info:
        Settings.model_validate(values)
    if secret:
        assert secret not in str(exc_info.value)


def test_cursor_secret_length_is_measured_as_utf8_bytes() -> None:
    settings = Settings.model_validate(
        {
            "environment": "production",
            "retrieval_cursor_signing_secret": "界" * 11,
        }
    )
    assert settings.retrieval_cursor_signing_secret is not None


def test_cursor_secret_is_loaded_from_prefixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "production")
    monkeypatch.setenv("PATCHOULI_RETRIEVAL_CURSOR_SIGNING_SECRET", "e" * 32)

    settings = Settings()

    assert settings.retrieval_cursor_signing_secret is not None
    assert settings.retrieval_cursor_signing_secret.get_secret_value() == "e" * 32
