from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, delete, insert, select, update

from patchouli_lib.auth.library_policy import LibraryAction, LibraryPolicy, resolve_library_policy
from patchouli_lib.auth.models import (
    AgentTokenValue,
    AuditEvent,
    BootstrapMarker,
    Caller,
    Credential,
    CredentialLibraryGrant,
    CredentialLibraryPolicy,
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
from patchouli_lib.auth.tokens import InvalidTokenError, parse_token, verify_token
from patchouli_lib.library.models import Library, Section


@dataclass(frozen=True, slots=True)
class CredentialLibraryGrantSummary:
    """Current rights for one target Library, never a derived Section grant."""

    library_id: str
    actions: tuple[LibraryAction, ...]


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
        self._connection.execute(
            delete(AgentTokenValue).where(
                AgentTokenValue.credential_id.in_(
                    select(Credential.id).where(
                        Credential.library_id == library_id,
                        Credential.caller_id == caller_id,
                    )
                )
            )
        )
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

    def has_library_grant_policy(
        self,
        library_id: str,
        caller_id: str,
        credential_id: str,
    ) -> bool:
        """Check explicit Library-grant mode for one exact credential identity."""
        statement = select(CredentialLibraryPolicy.credential_id).where(
            CredentialLibraryPolicy.home_library_id == library_id,
            CredentialLibraryPolicy.caller_id == caller_id,
            CredentialLibraryPolicy.credential_id == credential_id,
            CredentialLibraryPolicy.mode == "library_grants",
        )
        return self._connection.execute(statement).scalar_one_or_none() is not None

    def list_credential_library_grants(
        self,
        *,
        home_library_id: str,
        caller_id: str,
        credential_id: str,
    ) -> tuple[CredentialLibraryGrantSummary, ...]:
        """List grants for only this exact credential, in deterministic order."""

        statement = (
            select(CredentialLibraryGrant.target_library_id, CredentialLibraryGrant.action)
            .where(
                CredentialLibraryGrant.home_library_id == home_library_id,
                CredentialLibraryGrant.caller_id == caller_id,
                CredentialLibraryGrant.credential_id == credential_id,
            )
            .order_by(CredentialLibraryGrant.target_library_id, CredentialLibraryGrant.action)
        )
        grouped: dict[str, list[LibraryAction]] = {}
        for library_id, action in self._connection.execute(statement):
            grouped.setdefault(library_id, []).append(LibraryAction(action))
        return tuple(
            CredentialLibraryGrantSummary(library_id=library_id, actions=tuple(actions))
            for library_id, actions in grouped.items()
        )

    def get_library_policy(
        self,
        *,
        credential_id: str,
        caller_id: str,
        home_library_id: str,
        target_library_id: str,
        active_at: int,
    ) -> LibraryPolicy | None:
        """Resolve the current policy for one exact active Agent credential."""

        return resolve_library_policy(
            self._connection,
            credential_id=credential_id,
            caller_id=caller_id,
            home_library_id=home_library_id,
            target_library_id=target_library_id,
            active_at=active_at,
        )

    def find_credential_by_selector(self, selector: str) -> StoredCredential | None:
        statement = select(Credential.__table__).where(Credential.selector == selector)
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else StoredCredential.model_validate(row)

    def add_credential(self, credential: NewCredential) -> StoredCredential:
        values = credential.model_dump()
        self._connection.execute(insert(Credential), values)
        return StoredCredential.model_validate(values)

    def add_agent_credential_with_value(
        self, credential: NewCredential, *, token_value: str
    ) -> StoredCredential:
        """Persist verifier and revealable Agent value in one savepoint.

        A caller may catch issuance errors and commit its outer transaction.
        Rolling back this savepoint prevents a verifier-only half-issuance.
        """
        try:
            parsed = parse_token(token_value)
        except InvalidTokenError:
            raise ValueError("Agent token value does not match credential.") from None
        if (
            parsed.selector != credential.selector
            or parsed.version != credential.token_version
            or not verify_token(parsed, credential.verifier)
        ):
            raise ValueError("Agent token value does not match credential.")
        with self._connection.begin_nested():
            stored = self.add_credential(credential)
            self._connection.execute(
                insert(AgentTokenValue),
                {"credential_id": credential.id, "token_value": token_value},
            )
        return stored

    def get_active_agent_token_value(
        self,
        library_id: str,
        caller_id: str,
        credential_id: str,
        *,
        active_at: int,
    ) -> str | None:
        """Fetch a raw value only for a currently active Agent credential.

        The management caller must be authenticated separately before using
        this method. Legacy credentials intentionally return ``None``.
        """
        statement = (
            select(
                AgentTokenValue.token_value,
                Credential.selector,
                Credential.token_version,
                Credential.verifier,
            )
            .join(Credential, Credential.id == AgentTokenValue.credential_id)
            .join(
                Caller,
                (Caller.id == Credential.caller_id) & (Caller.library_id == Credential.library_id),
            )
            .where(
                Credential.id == credential_id,
                Credential.caller_id == caller_id,
                Credential.library_id == library_id,
                Credential.created_at <= active_at,
                Credential.expires_at > active_at,
                Credential.revoked_at.is_(None),
                Credential.rotated_at.is_(None),
                Caller.kind == "agent",
                Caller.disabled_at.is_(None),
            )
        )
        row = self._connection.execute(statement).one_or_none()
        if row is None:
            return None
        token_value, selector, token_version, verifier = row
        if (
            not isinstance(token_value, str)
            or not isinstance(selector, str)
            or type(token_version) is not int
            or not isinstance(verifier, bytes)
        ):
            return None
        try:
            parsed = parse_token(token_value)
        except InvalidTokenError:
            return None
        if (
            parsed.selector != selector
            or parsed.version != token_version
            or not verify_token(parsed, verifier)
        ):
            return None
        return token_value

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
        result = self._connection.execute(statement)
        if result.rowcount == 1:
            self._connection.execute(
                delete(AgentTokenValue).where(AgentTokenValue.credential_id == credential.id)
            )
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
        self._connection.execute(
            delete(AgentTokenValue).where(AgentTokenValue.credential_id == credential.id)
        )
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

    def list_grants(
        self,
        library_id: str,
        caller_id: str,
    ) -> tuple[SectionGrantRecord, ...]:
        statement = (
            select(SectionGrant.__table__)
            .where(
                SectionGrant.library_id == library_id,
                SectionGrant.caller_id == caller_id,
            )
            .order_by(SectionGrant.section_id, SectionGrant.action)
        )
        rows = self._connection.execute(statement).mappings().all()
        return tuple(SectionGrantRecord.model_validate(row) for row in rows)

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
