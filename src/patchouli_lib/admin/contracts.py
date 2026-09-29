from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from patchouli_lib.auth.library_policy import LibraryAction
from patchouli_lib.auth.schemas import (
    MAX_RFC3339_TIMESTAMP_MICROSECONDS,
    SectionAction,
)
from patchouli_lib.content.schemas import StrongPageETag
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


class MasterLibraryGrantInput(AdminActionInput):
    library_id: OpaqueId
    action: LibraryAction


class MasterProvisionAgentInput(AdminActionInput):
    home_library_id: OpaqueId
    agent_name: ResourceName
    agent_description: BoundedText = ""
    credential_ttl_seconds: CredentialTtlSeconds
    grants: tuple[MasterLibraryGrantInput, ...] = ()

    @model_validator(mode="after")
    def require_distinct_grants(self) -> Self:
        pairs = {(grant.library_id, grant.action) for grant in self.grants}
        if len(pairs) != len(self.grants):
            raise ValueError("Library grants must be distinct.")
        return self


class MasterRotateAgentCredentialInput(AdminActionInput):
    credential_ttl_seconds: CredentialTtlSeconds


class MasterSetAgentLibraryGrantsInput(AdminActionInput):
    expected_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    read: bool = False
    write: bool = False


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


class MasterTagFormInput(AdminActionInput):
    name: Annotated[str, Field(min_length=1, max_length=100)]


class TagFormInput(MasterTagFormInput):
    operator_token: SecretStr = Field(min_length=1, max_length=256, repr=False)

    @field_validator("operator_token", mode="before")
    @classmethod
    def reject_padded_operator_token(cls, value: object) -> object:
        if isinstance(value, str) and value != value.strip():
            raise ValueError("Operator credential must not contain whitespace.")
        return value


class MasterPageTagFormInput(AdminActionInput):
    tag_id: OpaqueId
    operation: Literal["attach", "detach"]


class PageTagFormInput(MasterPageTagFormInput):
    operator_token: SecretStr = Field(min_length=1, max_length=256, repr=False)

    @field_validator("operator_token", mode="before")
    @classmethod
    def reject_padded_operator_token(cls, value: object) -> object:
        if isinstance(value, str) and value != value.strip():
            raise ValueError("Operator credential must not contain whitespace.")
        return value


class MasterRestoreArchiveFormInput(AdminActionInput):
    """Conditional restore submitted by the authenticated master session."""

    expected_etag: StrongPageETag

    @field_validator("expected_etag", mode="before")
    @classmethod
    def reject_padded_precondition(cls, value: object) -> object:
        if isinstance(value, str) and value != value.strip():
            raise ValueError("Conditional restore values must not contain padding.")
        return value

    @field_validator("expected_etag")
    @classmethod
    def require_current_etag(cls, value: str) -> str:
        if not value.startswith('"page-v2-'):
            raise ValueError("A current Page ETag is required.")
        return value


class RestoreArchiveFormInput(MasterRestoreArchiveFormInput):
    """Legacy one-request Operator credential and idempotent restore values."""

    idempotency_key: Annotated[str, Field(min_length=1, max_length=256)]
    operator_token: SecretStr = Field(min_length=1, max_length=256, repr=False)

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def reject_padded_idempotency_key(cls, value: object) -> object:
        if isinstance(value, str) and value != value.strip():
            raise ValueError("Conditional restore values must not contain padding.")
        return value

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
    "TagFormInput",
    "MasterTagFormInput",
    "PageTagFormInput",
    "MasterPageTagFormInput",
    "MasterRestoreArchiveFormInput",
    "RestoreArchiveFormInput",
    "MasterLibraryGrantInput",
    "MasterProvisionAgentInput",
    "MasterRotateAgentCredentialInput",
    "MasterSetAgentLibraryGrantsInput",
]
