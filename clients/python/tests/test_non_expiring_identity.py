from __future__ import annotations

from datetime import UTC, datetime

import pytest

from patchouli_client.errors import ProtocolError
from patchouli_client.models import WhoAmI


def _identity() -> dict[str, object]:
    return {
        "caller_id": "synthetic_caller",
        "credential_id": "synthetic_credential",
        "kind": "agent",
        "expires_at": None,
        "policy_version": 1,
        "grants": [],
        "policy_mode": "library_grants",
        "library_grants": [],
    }


def test_explicit_null_expiry_is_supported() -> None:
    assert WhoAmI.from_dict(_identity()).expires_at is None


def test_finite_expiry_remains_a_datetime() -> None:
    identity = _identity()
    identity["expires_at"] = "2030-01-01T00:00:00Z"
    assert WhoAmI.from_dict(identity).expires_at == datetime(2030, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("value", [0, "", False, "infinity"])
def test_invalid_expiry_is_not_treated_as_unlimited(value: object) -> None:
    identity = _identity()
    identity["expires_at"] = value
    with pytest.raises(ProtocolError):
        WhoAmI.from_dict(identity)


def test_missing_expiry_is_not_treated_as_unlimited() -> None:
    identity = _identity()
    del identity["expires_at"]
    with pytest.raises(ProtocolError):
        WhoAmI.from_dict(identity)
