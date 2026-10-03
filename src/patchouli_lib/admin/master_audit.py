"""Non-secret audit writes for the single-person master administration session."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection, func, insert, select

from patchouli_lib.auth.models import MasterAuditEvent


def grant_revisions_for_credentials(
    connection: Connection, credential_ids: Iterable[str]
) -> dict[tuple[str, str], int]:
    """Count committed grant-change events; counts remain monotonic across ABA edits."""

    ids = frozenset(credential_ids)
    if not ids:
        return {}
    rows = connection.scalars(
        select(MasterAuditEvent.target_id).where(
            MasterAuditEvent.action == "auth.agent_credential.grants_update",
            MasterAuditEvent.target_type == "credential_library_grant",
            func.substr(MasterAuditEvent.target_id, 1, 32).in_(ids),
        )
    )
    revisions: dict[tuple[str, str], int] = {}
    for target in rows:
        parts = target.split(":")
        if len(parts) != 4 or parts[0] not in ids:
            continue
        key = parts[0], parts[1]
        revisions[key] = revisions.get(key, 0) + 1
    return revisions


class MasterAuditRepository:
    """Append audit events in the caller's already-authorized transaction.

    The caller must hold a short ``BEGIN IMMEDIATE`` transaction and recheck
    the master identity and session generation inside it. An insert failure
    must abort that transaction; a sensitive result must not be returned.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add_success(
        self,
        *,
        identity_id: str,
        session_generation: int,
        session_fingerprint: bytes,
        action: str,
        target_type: str,
        target_id: str,
        occurred_at: int,
        event_id: str,
    ) -> None:
        if not self._connection.in_transaction():
            raise RuntimeError("Master audit requires an active transaction.")
        self._connection.execute(
            insert(MasterAuditEvent),
            {
                "id": event_id,
                "identity_id": identity_id,
                "session_generation": session_generation,
                "session_fingerprint": session_fingerprint,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "occurred_at": occurred_at,
            },
        )


__all__ = ["MasterAuditRepository", "grant_revisions_for_credentials"]
