"""Explicit unlimited mode never results from an empty or invalid duration."""

import pytest
from pydantic import ValidationError

from patchouli_lib.admin.contracts import MasterRotateAgentCredentialInput
from patchouli_lib.admin.pages import AdminLocale, _credential_expiry_label, _credential_lifetime


@pytest.mark.parametrize("seconds", [31_536_000, 315_360_000])
def test_finite_duration(seconds: int) -> None:
    value = MasterRotateAgentCredentialInput(credential_ttl_seconds=seconds)
    assert value.credential_ttl_seconds == seconds
    assert not value.never_expires


@pytest.mark.parametrize("seconds", [None, "", "infinity", 0, -1])
def test_invalid_duration_is_not_infinite(seconds: object) -> None:
    with pytest.raises(ValidationError):
        MasterRotateAgentCredentialInput.model_validate({"credential_ttl_seconds": seconds})


@pytest.mark.parametrize("seconds", [None, "", "ignored", 0, 315_360_000])
def test_explicit_unlimited_ignores_seconds(seconds: object) -> None:
    value = MasterRotateAgentCredentialInput.model_validate(
        {"never_expires": "true", "credential_ttl_seconds": seconds}
    )
    assert value.never_expires
    assert value.credential_ttl_seconds is None


def test_lifetime_controls_and_expiry_labels() -> None:
    labels: tuple[tuple[AdminLocale, str], ...] = (("zh-CN", "永不过期"), ("en", "Never expires"))
    for locale, label in labels:
        html = _credential_lifetime(locale)
        assert 'name="never_expires" value="true"' in html
        assert 'value="31536000"' in html
        assert label in html
        assert _credential_expiry_label(None, locale) == label
        assert "1970" in _credential_expiry_label(1_000_000, locale)
