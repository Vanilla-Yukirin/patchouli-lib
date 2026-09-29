"""Unactivated per-credential Library policy evaluation.

Callers must still authenticate the bearer token before using this data-layer
helper. Legacy Section policy is deliberately a marker, not a grant.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from sqlalchemy import Connection, select

from patchouli_lib.auth.models import (
    Caller,
    Credential,
    CredentialLibraryGrant,
    CredentialLibraryPolicy,
)


class LibraryAction(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class LegacySectionPolicy:
    """An existing credential still needs the existing Section authorization path."""

    mode: Literal["legacy_section"] = "legacy_section"


@dataclass(frozen=True, slots=True)
class LibraryGrantPolicy:
    """Independent per-Library rights. Missing grants deny both actions."""

    read: bool
    write: bool
    mode: Literal["library_grants"] = "library_grants"

    def allows(self, action: LibraryAction) -> bool:
        if action is LibraryAction.READ:
            return self.read
        if action is LibraryAction.WRITE:
            return self.write
        raise ValueError("Unknown Library action.")


LibraryPolicy = LegacySectionPolicy | LibraryGrantPolicy


def resolve_library_policy(
    connection: Connection,
    *,
    credential_id: str,
    caller_id: str,
    home_library_id: str,
    target_library_id: str,
    active_at: int,
) -> LibraryPolicy | None:
    """Resolve policy only for this exact active Agent credential identity.

    ``None`` means an invalid, inactive, or mismatched identity. A legacy marker
    does not authorize anything by itself; existing Section checks remain
    responsible for those credentials. This module is not wired into routes.
    """

    if active_at < 0:
        raise ValueError("Evaluation time must be nonnegative.")
    valid_credential = (
        select(Credential.id)
        .join(
            Caller,
            (Caller.id == Credential.caller_id) & (Caller.library_id == Credential.library_id),
        )
        .where(
            Credential.id == credential_id,
            Credential.caller_id == caller_id,
            Credential.library_id == home_library_id,
            Credential.created_at <= active_at,
            Credential.expires_at > active_at,
            Credential.revoked_at.is_(None),
            Credential.rotated_at.is_(None),
            Caller.kind == "agent",
            Caller.disabled_at.is_(None),
        )
    )
    if connection.execute(valid_credential).scalar_one_or_none() is None:
        return None

    policy_exists = select(CredentialLibraryPolicy.credential_id).where(
        CredentialLibraryPolicy.credential_id == credential_id,
        CredentialLibraryPolicy.caller_id == caller_id,
        CredentialLibraryPolicy.home_library_id == home_library_id,
        CredentialLibraryPolicy.mode == "library_grants",
    )
    if connection.execute(policy_exists).scalar_one_or_none() is None:
        return LegacySectionPolicy()

    actions = connection.execute(
        select(CredentialLibraryGrant.action).where(
            CredentialLibraryGrant.credential_id == credential_id,
            CredentialLibraryGrant.caller_id == caller_id,
            CredentialLibraryGrant.home_library_id == home_library_id,
            CredentialLibraryGrant.target_library_id == target_library_id,
        )
    ).scalars()
    granted = frozenset(actions)
    return LibraryGrantPolicy(read="read" in granted, write="write" in granted)


__all__ = [
    "LegacySectionPolicy",
    "LibraryAction",
    "LibraryGrantPolicy",
    "LibraryPolicy",
    "resolve_library_policy",
]
