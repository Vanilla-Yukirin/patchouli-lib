"""Per-credential Library policy evaluation.

Callers must still authenticate the bearer token before using this data-layer
helper. Legacy Section policy is deliberately a marker, not a grant.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from sqlalchemy import Connection, or_, select

from patchouli_lib.auth.models import (
    Caller,
    Credential,
    CredentialLibraryGrant,
    CredentialLibraryPolicy,
)
from patchouli_lib.library.models import Library


class LibraryAction(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class LegacySectionPolicy:
    """An existing credential still needs the existing Section authorization path."""

    mode: Literal["legacy_section"] = "legacy_section"


@dataclass(frozen=True, slots=True)
class LibraryGrantPolicy:
    """Effective independent per-Library rights after scope evaluation."""

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


def target_library_grants_digest(
    home_library_id: str,
    caller_id: str,
    credential_id: str,
    target_library_id: str,
    actions: Iterable[LibraryAction],
    revision: int,
) -> str:
    """Stable comparison token for one target Library's read/write state."""

    if revision < 0:
        raise ValueError("Grant revision must be nonnegative.")
    state = [
        home_library_id,
        caller_id,
        credential_id,
        target_library_id,
        sorted(action.value for action in actions),
        revision,
    ]
    serialized = json.dumps(state, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(b"patchouli-target-library-grants-v1\0" + serialized).hexdigest()


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
    responsible for those credentials.
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
            or_(Credential.expires_at.is_(None), Credential.expires_at > active_at),
            Credential.revoked_at.is_(None),
            Credential.rotated_at.is_(None),
            Caller.kind == "agent",
            Caller.disabled_at.is_(None),
        )
    )
    if connection.execute(valid_credential).scalar_one_or_none() is None:
        return None

    policy_statement = select(
        CredentialLibraryPolicy.read_mode, CredentialLibraryPolicy.write_mode
    ).where(
        CredentialLibraryPolicy.credential_id == credential_id,
        CredentialLibraryPolicy.caller_id == caller_id,
        CredentialLibraryPolicy.home_library_id == home_library_id,
        CredentialLibraryPolicy.mode == "library_grants",
    )
    policy = connection.execute(policy_statement).one_or_none()
    if policy is None:
        return LegacySectionPolicy()

    if (
        connection.execute(
            select(Library.id).where(Library.id == target_library_id)
        ).scalar_one_or_none()
        is None
    ):
        return LibraryGrantPolicy(read=False, write=False)

    actions = connection.execute(
        select(CredentialLibraryGrant.action).where(
            CredentialLibraryGrant.credential_id == credential_id,
            CredentialLibraryGrant.caller_id == caller_id,
            CredentialLibraryGrant.home_library_id == home_library_id,
            CredentialLibraryGrant.target_library_id == target_library_id,
        )
    ).scalars()
    selected = frozenset(actions)
    return LibraryGrantPolicy(
        read=library_scope_allows(policy.read_mode, "read" in selected),
        write=library_scope_allows(policy.write_mode, "write" in selected),
    )


def library_scope_allows(mode: str, selected: bool) -> bool:
    """Evaluate one action's selected set, denying unknown modes defensively."""
    if mode == "include":
        return selected
    if mode == "exclude":
        return not selected
    return False


__all__ = [
    "LegacySectionPolicy",
    "LibraryAction",
    "LibraryGrantPolicy",
    "LibraryPolicy",
    "target_library_grants_digest",
    "resolve_library_policy",
]
