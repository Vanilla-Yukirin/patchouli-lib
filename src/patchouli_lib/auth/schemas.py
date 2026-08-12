from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchouli_lib.library.schemas import BoundedText, OpaqueId, ResourceName, TimestampMicros

CredentialSelector = Annotated[str, Field(min_length=22, max_length=22)]
CredentialVerifier = Annotated[bytes, Field(min_length=32, max_length=32, repr=False)]
RequestId = Annotated[
    str,
    Field(min_length=5, max_length=100, pattern=r"^req_[A-Za-z0-9_-]+$"),
]
AuditName = Annotated[str, Field(min_length=1, max_length=100)]
ResourceIdentifier = Annotated[str, Field(min_length=1, max_length=200)]
MAX_RFC3339_TIMESTAMP_MICROSECONDS = 253_402_300_799_999_999
CredentialExpiryMicros = Annotated[
    int,
    Field(ge=0, le=MAX_RFC3339_TIMESTAMP_MICROSECONDS),
]


class CallerKind(StrEnum):
    OPERATOR = "operator"
    AGENT = "agent"


class SectionAction(StrEnum):
    QUERY = "section:query"
    PAGE_READ = "page:read"
    ARCHIVE_WRITE = "archive:write"


class AuditOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AuthSchema(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        use_enum_values=False,
    )


class NewCaller(AuthSchema):
    id: OpaqueId
    library_id: OpaqueId
    kind: CallerKind
    name: ResourceName
    description: BoundedText = ""
    policy_version: Annotated[int, Field(ge=1)] = 1
    created_at: TimestampMicros
    updated_at: TimestampMicros
    disabled_at: TimestampMicros | None = None


class CallerRecord(NewCaller):
    pass


class NewCredential(AuthSchema):
    id: OpaqueId
    library_id: OpaqueId
    caller_id: OpaqueId
    selector: CredentialSelector = Field(repr=False)
    token_version: Annotated[int, Field(ge=1)]
    verifier: CredentialVerifier
    expires_at: CredentialExpiryMicros
    created_at: TimestampMicros
    updated_at: TimestampMicros
    last_used_at: TimestampMicros | None = None
    revoked_at: TimestampMicros | None = None
    rotated_at: TimestampMicros | None = None
    rotated_to_credential_id: OpaqueId | None = None


class StoredCredential(NewCredential):
    pass


class CredentialRecord(AuthSchema):
    id: OpaqueId
    library_id: OpaqueId
    caller_id: OpaqueId
    token_version: Annotated[int, Field(ge=1)]
    expires_at: CredentialExpiryMicros
    created_at: TimestampMicros
    updated_at: TimestampMicros
    last_used_at: TimestampMicros | None = None
    revoked_at: TimestampMicros | None = None
    rotated_at: TimestampMicros | None = None
    rotated_to_credential_id: OpaqueId | None = None


def credential_metadata(credential: StoredCredential) -> CredentialRecord:
    return CredentialRecord.model_validate(credential.model_dump(exclude={"selector", "verifier"}))


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class IssuedCredential:
    """One-time bearer value and separately persistable public metadata."""

    value: str = field(repr=False)
    credential: CredentialRecord

    def __repr__(self) -> str:
        return f"{type(self).__name__}(value=<redacted>, credential={self.credential!r})"

    def __str__(self) -> str:
        return repr(self)


class NewSectionGrant(AuthSchema):
    library_id: OpaqueId
    caller_id: OpaqueId
    section_id: OpaqueId
    action: SectionAction
    created_at: TimestampMicros


class SectionGrantRecord(NewSectionGrant):
    pass


class NewAuditEvent(AuthSchema):
    id: OpaqueId
    library_id: OpaqueId
    actor_caller_id: OpaqueId
    actor_credential_id: OpaqueId
    target_caller_id: OpaqueId | None = None
    section_id: OpaqueId | None = None
    section_action: SectionAction | None = None
    action: AuditName
    resource_type: AuditName
    resource_id: ResourceIdentifier
    outcome: AuditOutcome
    request_id: RequestId
    policy_version_before: Annotated[int, Field(ge=1)] | None = None
    policy_version_after: Annotated[int, Field(ge=1)] | None = None
    occurred_at: TimestampMicros

    @model_validator(mode="after")
    def validate_grant_identity(self) -> Self:
        is_grant_audit = self.action in {"auth.grant.add", "auth.grant.remove"}
        identity = (self.target_caller_id, self.section_id, self.section_action)
        if is_grant_audit and not all(value is not None for value in identity):
            raise ValueError("Grant audit identity must be complete and action-specific.")
        if not is_grant_audit and any(value is not None for value in identity):
            raise ValueError("Grant audit identity is only valid for grant events.")
        return self


class AuditEventRecord(NewAuditEvent):
    pass


class NewBootstrapMarker(AuthSchema):
    library_id: OpaqueId
    operator_caller_id: OpaqueId
    initial_credential_id: OpaqueId
    created_at: TimestampMicros


class BootstrapMarkerRecord(NewBootstrapMarker):
    pass


class AuthenticatedCaller(AuthSchema):
    caller: CallerRecord
    credential: CredentialRecord


class BootstrapGrant(AuthSchema):
    section_id: OpaqueId
    action: SectionAction


class OperatorBootstrap(AuthSchema):
    library_id: OpaqueId
    operator_name: ResourceName
    operator_description: BoundedText = ""
    credential_expires_at: CredentialExpiryMicros
    request_id: RequestId
    initial_grants: tuple[BootstrapGrant, ...] = ()


class LocalOperatorRecovery(AuthSchema):
    """Explicit local-only request; it intentionally accepts no bearer credential."""

    library_id: OpaqueId
    credential_expires_at: CredentialExpiryMicros
    request_id: RequestId


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class BootstrappedOperator:
    marker: BootstrapMarkerRecord
    caller: CallerRecord
    credential: IssuedCredential
    grants: tuple[SectionGrantRecord, ...]
    audit_event: AuditEventRecord

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(marker={self.marker!r}, caller={self.caller!r}, "
            f"credential={self.credential!r}, grants={self.grants!r}, "
            f"audit_event={self.audit_event!r})"
        )


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class RecoveredOperator:
    marker: BootstrapMarkerRecord
    caller: CallerRecord
    credential: IssuedCredential
    retired_credential_ids: tuple[str, ...]
    audit_event: AuditEventRecord

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(marker={self.marker!r}, caller={self.caller!r}, "
            f"credential={self.credential!r}, "
            f"retired_credential_ids={self.retired_credential_ids!r}, "
            f"audit_event={self.audit_event!r})"
        )


__all__ = [
    "AuditEventRecord",
    "AuditOutcome",
    "AuthenticatedCaller",
    "BootstrapMarkerRecord",
    "BootstrapGrant",
    "BootstrappedOperator",
    "CallerKind",
    "CallerRecord",
    "CredentialExpiryMicros",
    "CredentialRecord",
    "IssuedCredential",
    "LocalOperatorRecovery",
    "MAX_RFC3339_TIMESTAMP_MICROSECONDS",
    "NewAuditEvent",
    "NewBootstrapMarker",
    "NewCaller",
    "NewCredential",
    "NewSectionGrant",
    "OperatorBootstrap",
    "RecoveredOperator",
    "SectionAction",
    "SectionGrantRecord",
    "StoredCredential",
    "credential_metadata",
]
