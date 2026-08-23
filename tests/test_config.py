import pytest
from pydantic import SecretStr, ValidationError

from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.config import Settings

_ADMIN_PASSWORD_HASH = hash_password(
    "synthetic-password",
    salt_factory=lambda size: b"s" * size,
    iterations=300_000,
)


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


def test_admin_console_is_disabled_when_all_admin_values_are_absent_or_blank() -> None:
    absent = Settings.model_validate({"environment": "test"})
    blank = Settings.model_validate(
        {
            "environment": "test",
            "admin_password_hash": "",
            "admin_session_signing_secret": "",
            "admin_origin": "",
        }
    )

    assert not absent.admin_enabled
    assert not blank.admin_enabled


def test_admin_console_requires_complete_redacted_configuration() -> None:
    configured = Settings.model_validate(
        {
            "environment": "production",
            "retrieval_cursor_signing_secret": "r" * 32,
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
            "admin_origin": "https://admin.example.invalid/",
        }
    )

    assert configured.admin_enabled
    assert configured.admin_origin == "https://admin.example.invalid"
    assert _ADMIN_PASSWORD_HASH not in repr(configured)
    assert "s" * 32 not in repr(configured)


@pytest.mark.parametrize(
    ("configured_origin", "expected_origin"),
    [
        ("HTTPS://Admin.Example.Invalid/", "https://admin.example.invalid"),
        ("https://admin.example.invalid:443", "https://admin.example.invalid"),
        ("http://Admin.Example.Invalid:80/", "http://admin.example.invalid"),
        ("https://Admin.Example.Invalid:8443/", "https://admin.example.invalid:8443"),
    ],
)
def test_admin_origin_is_normalized_to_browser_serialization(
    configured_origin: str,
    expected_origin: str,
) -> None:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
            "admin_origin": configured_origin,
        }
    )

    assert settings.admin_origin == expected_origin


def test_admin_origin_rejects_non_ascii_hostname() -> None:
    with pytest.raises(ValidationError, match="ASCII"):
        Settings.model_validate(
            {
                "environment": "test",
                "admin_password_hash": _ADMIN_PASSWORD_HASH,
                "admin_session_signing_secret": "s" * 32,
                "admin_origin": "https://bücher.example.invalid",
            }
        )


@pytest.mark.parametrize(
    "values",
    [
        {"admin_password_hash": _ADMIN_PASSWORD_HASH},
        {
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
        },
        {
            "admin_password_hash": "short",
            "admin_session_signing_secret": "s" * 32,
            "admin_origin": "https://admin.example.invalid",
        },
        {
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "short",
            "admin_origin": "https://admin.example.invalid",
        },
        {
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
            "admin_origin": "https://user@example.invalid",
        },
        {
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
            "admin_origin": "https://admin.example.invalid/path",
        },
        {
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
            "admin_origin": "https://admin.example.invalid:invalid",
        },
        {
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
            "admin_origin": " https://admin.example.invalid",
        },
    ],
)
def test_admin_console_rejects_incomplete_or_unsafe_configuration(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"environment": "test", **values})


def test_production_admin_console_requires_https_origin() -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings.model_validate(
            {
                "environment": "production",
                "retrieval_cursor_signing_secret": "r" * 32,
                "admin_password_hash": _ADMIN_PASSWORD_HASH,
                "admin_session_signing_secret": "s" * 32,
                "admin_origin": "http://admin.example.invalid",
            }
        )
