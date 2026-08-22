"""Tests for administrative web form contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from patchouli_lib.admin.contracts import (
    ProvisionAgentInput,
    RevokeAgentCredentialInput,
)


def _provision_values() -> dict[str, object]:
    return {
        "library_name": "Synthetic Library",
        "section_name": "Synthetic Section",
        "agent_name": "Synthetic Agent",
        "credential_ttl_seconds": 3600,
        "grants": ("section:query",),
        "operator_token": "plb1.synthetic-operator-token",
    }


def test_provision_contract_rejects_duplicate_grants() -> None:
    values = _provision_values()
    values["grants"] = ("section:query", "section:query")

    with pytest.raises(ValidationError, match="must be distinct"):
        ProvisionAgentInput.model_validate(values)


def test_operator_credentials_reject_surrounding_whitespace() -> None:
    provision_values = _provision_values()
    provision_values["operator_token"] = " plb1.synthetic-operator-token"

    with pytest.raises(ValidationError, match="must not contain whitespace"):
        ProvisionAgentInput.model_validate(provision_values)
    with pytest.raises(ValidationError, match="must not contain whitespace"):
        RevokeAgentCredentialInput.model_validate(
            {
                "library_name": "Synthetic Library",
                "caller_id": "caller_synthetic",
                "credential_id": "credential_synthetic",
                "operator_token": "plb1.synthetic-operator-token ",
            }
        )
