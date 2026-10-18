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
        }
    )

    assert not absent.admin_enabled
    assert not blank.admin_enabled


def test_admin_console_accepts_legacy_password_with_signing_secret() -> None:
    configured = Settings.model_validate(
        {
            "environment": "production",
            "retrieval_cursor_signing_secret": "r" * 32,
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "s" * 32,
        }
    )

    assert configured.admin_enabled
    assert _ADMIN_PASSWORD_HASH not in repr(configured)
    assert "s" * 32 not in repr(configured)


def test_admin_console_accepts_signing_secret_without_legacy_password() -> None:
    configured = Settings.model_validate(
        {
            "environment": "production",
            "retrieval_cursor_signing_secret": "r" * 32,
            "admin_session_signing_secret": "s" * 32,
        }
    )

    assert configured.admin_enabled
    assert configured.admin_password_hash is None
    assert "s" * 32 not in repr(configured)


@pytest.mark.parametrize("proof", [None, "", SecretStr("")])
def test_admin_setup_token_has_no_default_and_blank_values_disable_it(
    proof: str | SecretStr | None,
) -> None:
    settings = Settings.model_validate({"environment": "test", "admin_setup_token": proof})
    assert settings.admin_setup_token is None
    assert not settings.admin_enabled


@pytest.mark.parametrize("proof", ["p" * 32, "p" * 1_024, "界" * 11])
def test_admin_setup_token_is_separate_optional_redacted_utf8_material(proof: str) -> None:
    settings = Settings.model_validate(
        {
            "environment": "test",
            "admin_setup_token": proof,
            "admin_session_signing_secret": "s" * 32,
        }
    )
    assert isinstance(settings.admin_setup_token, SecretStr)
    assert settings.admin_setup_token.get_secret_value() == proof
    assert proof not in repr(settings)


@pytest.mark.parametrize("proof", ["p" * 31, "p" * 1_025, "界" * 342, "p" * 32 + "\ud800"])
def test_admin_setup_token_rejects_invalid_utf8_byte_length_without_echo(proof: str) -> None:
    with pytest.raises(ValidationError) as error:
        Settings.model_validate(
            {
                "environment": "test",
                "admin_setup_token": proof,
                "admin_session_signing_secret": "s" * 32,
            }
        )
    assert proof not in str(error.value)


@pytest.mark.parametrize("signing_secret", [None, "same synthetic shared secret material 0001"])
def test_admin_setup_token_requires_an_independent_session_secret(
    signing_secret: str | None,
) -> None:
    proof = "same synthetic shared secret material 0001"
    with pytest.raises(ValidationError, match="admin setup token") as error:
        Settings.model_validate(
            {
                "environment": "test",
                "admin_setup_token": proof,
                "admin_session_signing_secret": signing_secret,
            }
        )
    assert proof not in str(error.value)


def test_admin_setup_token_is_loaded_from_prefixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "synthetic environment setup material 0001"
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    monkeypatch.setenv("PATCHOULI_ADMIN_SETUP_TOKEN", proof)
    monkeypatch.setenv("PATCHOULI_ADMIN_SESSION_SIGNING_SECRET", "s" * 32)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.admin_setup_token is not None
    assert settings.admin_setup_token.get_secret_value() == proof
    assert proof not in repr(settings)


@pytest.mark.parametrize(
    "values",
    [
        {"admin_password_hash": _ADMIN_PASSWORD_HASH},
        {
            "admin_password_hash": "short",
            "admin_session_signing_secret": "s" * 32,
        },
        {
            "admin_password_hash": _ADMIN_PASSWORD_HASH,
            "admin_session_signing_secret": "short",
        },
    ],
)
def test_admin_console_rejects_incomplete_or_unsafe_configuration(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"environment": "test", **values})


def test_admin_http_cookie_option_defaults_off_and_remains_explicit() -> None:
    values = {
        "environment": "production",
        "retrieval_cursor_signing_secret": "r" * 32,
        "admin_password_hash": _ADMIN_PASSWORD_HASH,
        "admin_session_signing_secret": "s" * 32,
    }
    assert not Settings.model_validate(values).admin_allow_private_http
    configured = Settings.model_validate({**values, "admin_allow_private_http": True})
    assert configured.admin_allow_private_http
    assert configured.admin_enabled


@pytest.mark.parametrize(
    "legacy_value", ["http://100.64.0.7:8080", "https://old.example.invalid", "not-an-origin"]
)
def test_legacy_origin_environment_is_ignored(
    monkeypatch: pytest.MonkeyPatch, legacy_value: str
) -> None:
    monkeypatch.setenv("PATCHOULI_ADMIN_ORIGIN", legacy_value)
    monkeypatch.setenv("PATCHOULI_ADMIN_PASSWORD_HASH", _ADMIN_PASSWORD_HASH)
    monkeypatch.setenv("PATCHOULI_ADMIN_SESSION_SIGNING_SECRET", "s" * 32)
    configured = Settings()
    assert configured.admin_enabled
    assert "admin_origin" not in Settings.model_fields
