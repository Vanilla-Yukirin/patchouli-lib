"""Non-secret audit writes for the single-person master administration session."""

from __future__ import annotations

from sqlalchemy import Connection, insert

from patchouli_lib.auth.models import MasterAuditEvent


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


__all__ = ["MasterAuditRepository"]
