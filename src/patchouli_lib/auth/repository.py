from __future__ import annotations

from sqlalchemy import Connection, delete, insert, select, update

from patchouli_lib.auth.models import (
    AuditEvent,
    BootstrapMarker,
    Caller,
    Credential,
    SectionGrant,
)
from patchouli_lib.auth.schemas import (
    AuditEventRecord,
    BootstrapMarkerRecord,
    CallerRecord,
    NewAuditEvent,
    NewBootstrapMarker,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
    SectionGrantRecord,
    StoredCredential,
)
from patchouli_lib.library.models import Library, Section


class AuthRepository:
    """Persist authentication state without owning or committing a transaction."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def library_exists(self, library_id: str) -> bool:
        statement = select(Library.id).where(Library.id == library_id)
        return self._connection.execute(statement).scalar_one_or_none() is not None

    def section_exists(self, library_id: str, section_id: str) -> bool:
        statement = select(Section.id).where(
            Section.library_id == library_id,
            Section.id == section_id,
        )
        return self._connection.execute(statement).scalar_one_or_none() is not None

    def get_caller(self, library_id: str, caller_id: str) -> CallerRecord | None:
        statement = select(Caller.__table__).where(
            Caller.library_id == library_id,
            Caller.id == caller_id,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else CallerRecord.model_validate(row)

    def find_caller_by_name(self, library_id: str, name: str) -> CallerRecord | None:
        statement = select(Caller.__table__).where(
            Caller.library_id == library_id,
            Caller.name == name,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else CallerRecord.model_validate(row)

    def add_caller(self, caller: NewCaller) -> CallerRecord:
        values = caller.model_dump()
        self._connection.execute(insert(Caller), values)
        return CallerRecord.model_validate(values)

    def disable_caller(
        self,
        library_id: str,
        caller_id: str,
        *,
        disabled_at: int,
    ) -> CallerRecord | None:
        statement = (
            update(Caller)
            .where(
                Caller.library_id == library_id,
                Caller.id == caller_id,
                Caller.disabled_at.is_(None),
            )
            .values(
                disabled_at=disabled_at,
                updated_at=disabled_at,
                policy_version=Caller.policy_version + 1,
            )
        )
        result = self._connection.execute(statement)
        if result.rowcount != 1:
            return self.get_caller(library_id, caller_id)
        return self.get_caller(library_id, caller_id)

    def increment_policy_version(
        self,
        library_id: str,
        caller_id: str,
        *,
        expected_version: int,
        updated_at: int,
    ) -> CallerRecord | None:
        statement = (
            update(Caller)
            .where(
                Caller.library_id == library_id,
                Caller.id == caller_id,
                Caller.policy_version == expected_version,
            )
            .values(
                policy_version=Caller.policy_version + 1,
                updated_at=updated_at,
            )
        )
        result = self._connection.execute(statement)
        if result.rowcount != 1:
            return None
        return self.get_caller(library_id, caller_id)

    def get_credential(
        self,
        library_id: str,
        caller_id: str,
        credential_id: str,
    ) -> StoredCredential | None:
        statement = select(Credential.__table__).where(
            Credential.library_id == library_id,
            Credential.caller_id == caller_id,
            Credential.id == credential_id,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else StoredCredential.model_validate(row)

    def find_credential_by_selector(self, selector: str) -> StoredCredential | None:
        statement = select(Credential.__table__).where(Credential.selector == selector)
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else StoredCredential.model_validate(row)

    def add_credential(self, credential: NewCredential) -> StoredCredential:
        values = credential.model_dump()
        self._connection.execute(insert(Credential), values)
        return StoredCredential.model_validate(values)

    def list_active_credentials(
        self,
        library_id: str,
        caller_id: str,
        *,
        active_at: int,
    ) -> tuple[StoredCredential, ...]:
        statement = select(Credential.__table__).where(
            Credential.library_id == library_id,
            Credential.caller_id == caller_id,
            Credential.created_at <= active_at,
            Credential.expires_at > active_at,
            Credential.revoked_at.is_(None),
            Credential.rotated_at.is_(None),
        )
        rows = self._connection.execute(statement).mappings().all()
        return tuple(StoredCredential.model_validate(row) for row in rows)

    def touch_credential_last_used(
        self,
        credential: StoredCredential,
        *,
        used_at: int,
    ) -> StoredCredential:
        statement = (
            update(Credential)
            .where(
                Credential.id == credential.id,
                Credential.caller_id == credential.caller_id,
                Credential.library_id == credential.library_id,
                Credential.revoked_at.is_(None),
                Credential.rotated_at.is_(None),
            )
            .values(last_used_at=used_at, updated_at=used_at)
        )
        self._connection.execute(statement)
        refreshed = self.get_credential(
            credential.library_id,
            credential.caller_id,
            credential.id,
        )
        return credential if refreshed is None else refreshed

    def revoke_credential(
        self,
        credential: StoredCredential,
        *,
        revoked_at: int,
    ) -> StoredCredential | None:
        statement = (
            update(Credential)
            .where(
                Credential.id == credential.id,
                Credential.caller_id == credential.caller_id,
                Credential.library_id == credential.library_id,
                Credential.revoked_at.is_(None),
                Credential.rotated_at.is_(None),
            )
            .values(revoked_at=revoked_at, updated_at=revoked_at)
        )
        self._connection.execute(statement)
        return self.get_credential(
            credential.library_id,
            credential.caller_id,
            credential.id,
        )

    def mark_credential_rotated(
        self,
        credential: StoredCredential,
        replacement_id: str,
        *,
        rotated_at: int,
    ) -> StoredCredential | None:
        statement = (
            update(Credential)
            .where(
                Credential.id == credential.id,
                Credential.caller_id == credential.caller_id,
                Credential.library_id == credential.library_id,
                Credential.revoked_at.is_(None),
                Credential.rotated_at.is_(None),
            )
            .values(
                revoked_at=rotated_at,
                rotated_at=rotated_at,
                rotated_to_credential_id=replacement_id,
                updated_at=rotated_at,
            )
        )
        result = self._connection.execute(statement)
        if result.rowcount != 1:
            return None
        return self.get_credential(
            credential.library_id,
            credential.caller_id,
            credential.id,
        )

    def get_grant(
        self,
        library_id: str,
        caller_id: str,
        section_id: str,
        action: SectionAction,
    ) -> SectionGrantRecord | None:
        statement = select(SectionGrant.__table__).where(
            SectionGrant.library_id == library_id,
            SectionGrant.caller_id == caller_id,
            SectionGrant.section_id == section_id,
            SectionGrant.action == action.value,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else SectionGrantRecord.model_validate(row)

    def add_grant(self, grant: NewSectionGrant) -> SectionGrantRecord:
        values = grant.model_dump()
        self._connection.execute(insert(SectionGrant), values)
        return SectionGrantRecord.model_validate(values)

    def remove_grant(
        self,
        library_id: str,
        caller_id: str,
        section_id: str,
        action: SectionAction,
    ) -> bool:
        statement = delete(SectionGrant).where(
            SectionGrant.library_id == library_id,
            SectionGrant.caller_id == caller_id,
            SectionGrant.section_id == section_id,
            SectionGrant.action == action.value,
        )
        return self._connection.execute(statement).rowcount == 1

    def get_bootstrap_marker(self, library_id: str) -> BootstrapMarkerRecord | None:
        statement = select(BootstrapMarker.__table__).where(
            BootstrapMarker.library_id == library_id
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else BootstrapMarkerRecord.model_validate(row)

    def add_bootstrap_marker(self, marker: NewBootstrapMarker) -> BootstrapMarkerRecord:
        values = marker.model_dump()
        self._connection.execute(insert(BootstrapMarker), values)
        return BootstrapMarkerRecord.model_validate(values)

    def add_audit_event(self, event: NewAuditEvent) -> AuditEventRecord:
        values = event.model_dump()
        self._connection.execute(insert(AuditEvent), values)
        return AuditEventRecord.model_validate(values)


__all__ = ["AuthRepository"]
