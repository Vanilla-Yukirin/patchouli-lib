from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from patchouli_lib.api.authentication import AuthenticatedRequestContext
from patchouli_lib.api.contracts import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    RFC3339UTC,
    OpaqueIdentifier,
    WireModel,
)
from patchouli_lib.auth.schemas import CallerKind, SectionAction

MAX_CONTENT_BYTES = 2 * 1024 * 1024
MAX_QUERY_BYTES = 4_096
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

CapabilityName = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9:_-]*$"),
]
ApiVersion = Annotated[str, Field(min_length=1, max_length=16, pattern=r"^v[1-9][0-9]*$")]
RetentionDescription = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$"),
]


class CapabilityConfiguration(BaseModel):
    """Immutable integrator-owned advertisement for optional implemented behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_versions: tuple[ApiVersion, ...] = ("v1",)
    features: tuple[CapabilityName, ...] = ()
    content_mutation_idempotency: bool = False
    successful_replay_retention: RetentionDescription = "unsupported"

    @field_validator("api_versions", "features")
    @classmethod
    def require_sorted_unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("Capability values must be sorted and unique.")
        return value


DEFAULT_CAPABILITY_CONFIGURATION = CapabilityConfiguration()


class ApiLimits(WireModel):
    max_content_bytes: Annotated[int, Field(ge=1)] = MAX_CONTENT_BYTES
    default_page_size: Annotated[int, Field(ge=1)] = DEFAULT_PAGE_LIMIT
    max_page_size: Annotated[int, Field(ge=1)] = MAX_PAGE_LIMIT
    max_query_bytes: Annotated[int, Field(ge=1)] = MAX_QUERY_BYTES


class IdempotencySupport(WireModel):
    content_mutations: bool
    successful_replay_retention: RetentionDescription


class CapabilitiesResponse(WireModel):
    api_versions: tuple[ApiVersion, ...]
    features: tuple[CapabilityName, ...]
    limits: ApiLimits
    idempotency: IdempotencySupport


class EffectiveSectionGrant(WireModel):
    section_id: OpaqueIdentifier
    actions: tuple[SectionAction, ...]


class WhoAmIResponse(WireModel):
    caller_id: OpaqueIdentifier
    credential_id: OpaqueIdentifier
    kind: CallerKind
    expires_at: RFC3339UTC
    policy_version: Annotated[int, Field(ge=1)]
    grants: tuple[EffectiveSectionGrant, ...]


def capabilities_response(configuration: CapabilityConfiguration) -> CapabilitiesResponse:
    return CapabilitiesResponse(
        api_versions=configuration.api_versions,
        features=configuration.features,
        limits=ApiLimits(),
        idempotency=IdempotencySupport(
            content_mutations=configuration.content_mutation_idempotency,
            successful_replay_retention=configuration.successful_replay_retention,
        ),
    )


def _timestamp_datetime(timestamp_microseconds: int) -> datetime:
    return _EPOCH + timedelta(microseconds=timestamp_microseconds)


def _effective_grants(context: AuthenticatedRequestContext) -> tuple[EffectiveSectionGrant, ...]:
    if context.authenticated.caller.kind is CallerKind.OPERATOR:
        return ()

    grouped: dict[str, set[SectionAction]] = {}
    for grant in context.grants:
        grouped.setdefault(grant.section_id, set()).add(grant.action)
    return tuple(
        EffectiveSectionGrant(
            section_id=section_id,
            actions=tuple(sorted(actions, key=str)),
        )
        for section_id, actions in sorted(grouped.items())
    )


def whoami_response(context: AuthenticatedRequestContext) -> WhoAmIResponse:
    authenticated = context.authenticated
    return WhoAmIResponse(
        caller_id=authenticated.caller.id,
        credential_id=authenticated.credential.id,
        kind=authenticated.caller.kind,
        expires_at=_timestamp_datetime(authenticated.credential.expires_at),
        policy_version=authenticated.caller.policy_version,
        grants=_effective_grants(context),
    )


__all__ = [
    "DEFAULT_CAPABILITY_CONFIGURATION",
    "MAX_CONTENT_BYTES",
    "MAX_QUERY_BYTES",
    "ApiLimits",
    "CapabilitiesResponse",
    "CapabilityConfiguration",
    "EffectiveSectionGrant",
    "IdempotencySupport",
    "WhoAmIResponse",
    "capabilities_response",
    "whoami_response",
]
