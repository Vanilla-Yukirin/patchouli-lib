from __future__ import annotations

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from patchouli_lib.auth.schemas import (
    MAX_RFC3339_TIMESTAMP_MICROSECONDS,
    SectionAction,
)
from patchouli_lib.library.schemas import BoundedText, OpaqueId, ResourceName

CredentialTtlSeconds = Annotated[
    int,
    Field(gt=0, le=MAX_RFC3339_TIMESTAMP_MICROSECONDS // 1_000_000),
]


class AdminActionInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class BootstrapInput(AdminActionInput):
    library_name: ResourceName
    section_name: ResourceName
    section_description: BoundedText = ""
    book_name: ResourceName
    book_summary: BoundedText = ""
    operator_name: ResourceName
    operator_description: BoundedText = ""
    credential_ttl_seconds: CredentialTtlSeconds


class RecoverOperatorInput(AdminActionInput):
    library_name: ResourceName
    credential_ttl_seconds: CredentialTtlSeconds


class ProvisionAgentInput(AdminActionInput):
    library_name: ResourceName
    section_name: ResourceName
    agent_name: ResourceName
    agent_description: BoundedText = ""
    credential_ttl_seconds: CredentialTtlSeconds
    grants: tuple[SectionAction, ...] = Field(min_length=1, max_length=len(SectionAction))
    operator_token: SecretStr = Field(min_length=1, max_length=256, repr=False)

    @field_validator("operator_token", mode="before")
    @classmethod
    def reject_padded_operator_token(cls, value: object) -> object:
        if isinstance(value, str) and value != value.strip():
            raise ValueError("Operator credential must not contain whitespace.")
        return value

    @model_validator(mode="after")
    def require_distinct_grants(self) -> Self:
        if len(set(self.grants)) != len(self.grants):
            raise ValueError("Agent grants must be distinct.")
        return self


class RevokeAgentCredentialInput(AdminActionInput):
    library_name: ResourceName
    caller_id: OpaqueId
    credential_id: OpaqueId
    operator_token: SecretStr = Field(min_length=1, max_length=256, repr=False)

    @field_validator("operator_token", mode="before")
    @classmethod
    def reject_padded_operator_token(cls, value: object) -> object:
        if isinstance(value, str) and value != value.strip():
            raise ValueError("Operator credential must not contain whitespace.")
        return value


__all__ = [
    "BootstrapInput",
    "ProvisionAgentInput",
    "RecoverOperatorInput",
    "RevokeAgentCredentialInput",
]
