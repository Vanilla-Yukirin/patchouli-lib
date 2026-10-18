from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from patchouli_lib.auth.library_policy import LibraryAction
from patchouli_lib.auth.schemas import (
    MAX_RFC3339_TIMESTAMP_MICROSECONDS,
    SectionAction,
)
from patchouli_lib.content.schemas import StrongPageETag
from patchouli_lib.identifiers import parse_occurrence_time
from patchouli_lib.library.schemas import BoundedText, OpaqueId, ResourceName, TimestampMicros

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
    library_description: BoundedText = ""
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


class MasterCredentialLifetimeInput(AdminActionInput):
    never_expires: bool = False
    credential_ttl_seconds: CredentialTtlSeconds | None = None

    @model_validator(mode="before")
    @classmethod
    def ignore_duration_only_when_explicitly_unlimited(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("never_expires") in (True, "true", "on", "1"):
            return {**value, "credential_ttl_seconds": None}
        return value

    @model_validator(mode="after")
    def require_duration_when_limited(self) -> Self:
        if not self.never_expires and self.credential_ttl_seconds is None:
            raise ValueError("A finite credential requires a positive lifetime.")
        return self


class MasterLibraryScopeInput(AdminActionInput):
    read_mode: Literal["include", "exclude"] = "include"
    write_mode: Literal["include", "exclude"] = "include"
    read_libraries: tuple[OpaqueId, ...] = ()
    write_libraries: tuple[OpaqueId, ...] = ()

    @model_validator(mode="after")
    def require_distinct_selections(self) -> Self:
        for values in (self.read_libraries, self.write_libraries):
            if len(set(values)) != len(values):
                raise ValueError("Library selections must be distinct.")
        return self


class MasterProvisionAgentInput(MasterCredentialLifetimeInput, MasterLibraryScopeInput):
    home_library_id: OpaqueId | None = None
    agent_name: ResourceName
    agent_description: BoundedText = ""
    grants: tuple[MasterLibraryGrantInput, ...] = ()

    @model_validator(mode="after")
    def require_distinct_grants(self) -> Self:
        pairs = {(grant.library_id, grant.action) for grant in self.grants}
        if len(pairs) != len(self.grants):
            raise ValueError("Library grants must be distinct.")
        if self.grants and self.model_fields_set.intersection(
            {"read_mode", "write_mode", "read_libraries", "write_libraries"}
        ):
            raise ValueError("Legacy grants cannot be combined with scope selections.")
        return self


class MasterSetAgentLibraryScopeInput(MasterLibraryScopeInput):
    expected_scope_revision: Annotated[int, Field(ge=0)]


class MasterUpdateAgentInput(AdminActionInput):
    name: ResourceName
    description: BoundedText
    expected_updated_at: TimestampMicros


class MasterRotateAgentCredentialInput(MasterCredentialLifetimeInput):
    pass


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


class MasterTagBatchChange(AdminActionInput):
    model_config = ConfigDict(str_strip_whitespace=False)

    library_id: OpaqueId
    tag_id: OpaqueId
    expected_name: Annotated[str, Field(min_length=1, max_length=100)]
    new_name: Annotated[str, Field(min_length=1, max_length=100)] | None = None


class MasterTagBatchInput(AdminActionInput):
    operation: Literal["rename", "delete"]
    changes: tuple[MasterTagBatchChange, ...] = Field(min_length=1, max_length=200)
    confirm_delete: bool = False

    @model_validator(mode="after")
    def require_explicit_distinct_changes(self) -> Self:
        if len({(item.library_id, item.tag_id) for item in self.changes}) != len(self.changes):
            raise ValueError("Each Tag may appear only once in a batch.")
        if self.operation == "delete":
            if not self.confirm_delete or any(item.new_name is not None for item in self.changes):
                raise ValueError("Tag deletion requires confirmation and no replacement names.")
        elif any(item.new_name is None for item in self.changes):
            raise ValueError("Every renamed Tag requires a replacement name.")
        return self


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


class MasterUpdatePageTitleInput(AdminActionInput):
    title: Annotated[str, Field(min_length=1)]
    expected_updated_at: TimestampMicros

    @field_validator("title")
    @classmethod
    def reject_nul_title(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Page title must not contain NUL characters.")
        return value


class MasterCorrectOccurrenceInput(AdminActionInput):
    """A required, explicit-timezone declaration and exact current precondition."""

    occurred_at: Annotated[str, Field(min_length=1, max_length=64)]
    expected_etag: StrongPageETag

    @field_validator("occurred_at")
    @classmethod
    def require_valid_declaration(cls, value: str) -> str:
        parse_occurrence_time(value)
        return value

    @field_validator("expected_etag", mode="before")
    @classmethod
    def reject_padded_precondition(cls, value: object) -> object:
        if isinstance(value, str) and value != value.strip():
            raise ValueError("ETag must not contain surrounding whitespace.")
        return value


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


class MasterDeletePageFormInput(MasterRestoreArchiveFormInput):
    """Explicit confirmation and current ETag for a reversible Page deletion."""

    confirm_delete: Literal["yes"]


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
    "MasterDeletePageFormInput",
    "BootstrapInput",
    "ProvisionAgentInput",
    "RecoverOperatorInput",
    "RevokeAgentCredentialInput",
    "TagFormInput",
    "MasterTagFormInput",
    "MasterTagBatchChange",
    "MasterTagBatchInput",
    "PageTagFormInput",
    "MasterPageTagFormInput",
    "MasterRestoreArchiveFormInput",
    "RestoreArchiveFormInput",
    "MasterLibraryGrantInput",
    "MasterProvisionAgentInput",
    "MasterUpdateAgentInput",
    "MasterRotateAgentCredentialInput",
    "MasterSetAgentLibraryGrantsInput",
    "MasterLibraryScopeInput",
    "MasterSetAgentLibraryScopeInput",
]
