from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    AuditEventRecord,
    AuditOutcome,
    AuthenticatedCaller,
    BootstrappedOperator,
    CallerKind,
    CallerRecord,
    CredentialRecord,
    IssuedCredential,
    LocalOperatorRecovery,
    NewAuditEvent,
    NewBootstrapMarker,
    NewCaller,
    NewSectionGrant,
    OperatorBootstrap,
    RecoveredOperator,
    SectionAction,
    SectionGrantRecord,
    credential_metadata,
)
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthenticationService,
    Clock,
    CredentialExpiryError,
    CredentialIssuer,
    CredentialPersistenceError,
    IdFactory,
    new_opaque_id,
    utc_microseconds,
)


class BootstrapAlreadyCompletedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Local operator bootstrap has already completed.")


class ResourceNotFoundError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Resource was not found.")


class CredentialLifecycleError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Credential lifecycle operation is not available.")


class PolicyConflictError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Authorization policy changed during the operation.")


class OperatorRecoveryUnavailableError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Local operator recovery is not available.")


class OperatorBootstrapService:
    """Perform the one-time local bootstrap in a caller-owned write transaction."""

    def __init__(
        self,
        repository: AuthRepository,
        *,
        id_factory: IdFactory = new_opaque_id,
        clock: Clock = utc_microseconds,
    ) -> None:
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock

    def bootstrap(self, request: OperatorBootstrap) -> BootstrappedOperator:
        if self._repository.get_bootstrap_marker(request.library_id) is not None:
            raise BootstrapAlreadyCompletedError
        if not self._repository.library_exists(request.library_id):
            raise ResourceNotFoundError

        created_at = self._clock()
        if request.credential_expires_at <= created_at:
            raise CredentialExpiryError

        caller = self._repository.add_caller(
            NewCaller(
                id=self._id_factory(),
                library_id=request.library_id,
                kind=CallerKind.OPERATOR,
                name=request.operator_name,
                description=request.operator_description,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        credential = CredentialIssuer(
            self._repository,
            id_factory=self._id_factory,
            clock=lambda: created_at,
        ).issue(caller, expires_at=request.credential_expires_at)
        grants = tuple(
            self._repository.add_grant(
                NewSectionGrant(
                    library_id=request.library_id,
                    caller_id=caller.id,
                    section_id=grant.section_id,
                    action=grant.action,
                    created_at=created_at,
                )
            )
            for grant in request.initial_grants
        )
        marker = self._repository.add_bootstrap_marker(
            NewBootstrapMarker(
                library_id=request.library_id,
                operator_caller_id=caller.id,
                initial_credential_id=credential.credential.id,
                created_at=created_at,
            )
        )
        audit_event = self._repository.add_audit_event(
            NewAuditEvent(
                id=self._id_factory(),
                library_id=request.library_id,
                actor_caller_id=caller.id,
                actor_credential_id=credential.credential.id,
                action="operator.bootstrap",
                resource_type="library",
                resource_id=request.library_id,
                outcome=AuditOutcome.SUCCEEDED,
                request_id=request.request_id,
                occurred_at=created_at,
            )
        )
        return BootstrappedOperator(
            marker=marker,
            caller=caller,
            credential=credential,
            grants=grants,
            audit_event=audit_event,
        )


class LocalOperatorRecoveryService:
    """Recover the bootstrapped operator from an explicit local-only call boundary.

    Recovery accepts no bearer credential. In the caller-owned transaction it
    preserves the permanent marker and caller, retires every prior active
    operator credential, issues one fresh finite credential, and records one
    fixed audit event. A lost response is recovered by calling again, which
    retires the lost credential and returns a distinct secret.
    """

    def __init__(
        self,
        repository: AuthRepository,
        *,
        id_factory: IdFactory = new_opaque_id,
        clock: Clock = utc_microseconds,
    ) -> None:
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock

    def recover(self, request: LocalOperatorRecovery) -> RecoveredOperator:
        marker = self._repository.get_bootstrap_marker(request.library_id)
        if marker is None:
            raise OperatorRecoveryUnavailableError
        caller = self._repository.get_caller(request.library_id, marker.operator_caller_id)
        if (
            caller is None
            or caller.kind is not CallerKind.OPERATOR
            or caller.disabled_at is not None
            or self._repository.get_credential(
                request.library_id,
                marker.operator_caller_id,
                marker.initial_credential_id,
            )
            is None
        ):
            raise OperatorRecoveryUnavailableError

        recovered_at = self._clock()
        prior_active = self._repository.list_active_credentials(
            request.library_id,
            caller.id,
            active_at=recovered_at,
        )
        try:
            credential = CredentialIssuer(
                self._repository,
                id_factory=self._id_factory,
                clock=lambda: recovered_at,
            ).issue(caller, expires_at=request.credential_expires_at)
            retired_ids: list[str] = []
            for prior in prior_active:
                if self._repository.revoke_credential(prior, revoked_at=recovered_at) is None:
                    raise OperatorRecoveryUnavailableError
                retired_ids.append(prior.id)
            audit_event = self._repository.add_audit_event(
                NewAuditEvent(
                    id=self._id_factory(),
                    library_id=request.library_id,
                    actor_caller_id=caller.id,
                    actor_credential_id=credential.credential.id,
                    action="auth.operator.recovery",
                    resource_type="caller",
                    resource_id=caller.id,
                    outcome=AuditOutcome.SUCCEEDED,
                    request_id=request.request_id,
                    occurred_at=recovered_at,
                )
            )
        except (CredentialExpiryError, CredentialPersistenceError, IntegrityError):
            raise OperatorRecoveryUnavailableError from None

        return RecoveredOperator(
            marker=marker,
            caller=caller,
            credential=credential,
            retired_credential_ids=tuple(retired_ids),
            audit_event=audit_event,
        )


class OperatorService:
    """Operator-only lifecycle and exact-grant mutations without transaction ownership."""

    def __init__(
        self,
        repository: AuthRepository,
        *,
        id_factory: IdFactory = new_opaque_id,
        clock: Clock = utc_microseconds,
    ) -> None:
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock

    def create_agent_caller(
        self,
        actor_token: str,
        *,
        library_id: str,
        name: str,
        description: str = "",
        request_id: str,
    ) -> CallerRecord:
        actor = self._operator(actor_token, library_id)
        timestamp = self._clock()
        caller = self._repository.add_caller(
            NewCaller(
                id=self._id_factory(),
                library_id=library_id,
                kind=CallerKind.AGENT,
                name=name,
                description=description,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        self._audit(
            actor,
            action="auth.caller.create",
            resource_type="caller",
            resource_id=caller.id,
            request_id=request_id,
            occurred_at=timestamp,
        )
        return caller

    def create_credential(
        self,
        actor_token: str,
        *,
        library_id: str,
        caller_id: str,
        expires_at: int,
        request_id: str,
    ) -> IssuedCredential:
        actor = self._operator(actor_token, library_id)
        caller = self._require_caller(library_id, caller_id)
        issued = CredentialIssuer(
            self._repository,
            id_factory=self._id_factory,
            clock=self._clock,
        ).issue(caller, expires_at=expires_at)
        self._audit(
            actor,
            action="auth.credential.create",
            resource_type="credential",
            resource_id=issued.credential.id,
            request_id=request_id,
            occurred_at=issued.credential.created_at,
        )
        return issued

    def rotate_credential(
        self,
        actor_token: str,
        *,
        library_id: str,
        caller_id: str,
        credential_id: str,
        expires_at: int,
        request_id: str,
    ) -> IssuedCredential:
        actor = self._operator(actor_token, library_id)
        caller = self._require_caller(library_id, caller_id)
        current = self._repository.get_credential(library_id, caller_id, credential_id)
        if current is None or current.revoked_at is not None or current.rotated_at is not None:
            raise CredentialLifecycleError

        rotated_at = self._clock()
        replacement = CredentialIssuer(
            self._repository,
            id_factory=self._id_factory,
            clock=lambda: rotated_at,
        ).issue(caller, expires_at=expires_at)
        rotated = self._repository.mark_credential_rotated(
            current,
            replacement.credential.id,
            rotated_at=rotated_at,
        )
        if rotated is None:
            raise CredentialLifecycleError
        self._audit(
            actor,
            action="auth.credential.rotate",
            resource_type="credential",
            resource_id=current.id,
            request_id=request_id,
            occurred_at=rotated_at,
        )
        return replacement

    def revoke_credential(
        self,
        actor_token: str,
        *,
        library_id: str,
        caller_id: str,
        credential_id: str,
        request_id: str,
    ) -> CredentialRecord:
        actor = self._operator(actor_token, library_id)
        current = self._repository.get_credential(library_id, caller_id, credential_id)
        if current is None:
            raise CredentialLifecycleError
        if current.revoked_at is not None or current.rotated_at is not None:
            return credential_metadata(current)

        revoked_at = self._clock()
        revoked = self._repository.revoke_credential(current, revoked_at=revoked_at)
        if revoked is None:
            raise CredentialLifecycleError
        self._audit(
            actor,
            action="auth.credential.revoke",
            resource_type="credential",
            resource_id=current.id,
            request_id=request_id,
            occurred_at=revoked_at,
        )
        return credential_metadata(revoked)

    def disable_caller(
        self,
        actor_token: str,
        *,
        library_id: str,
        caller_id: str,
        request_id: str,
    ) -> CallerRecord:
        self._operator_without_usage_update(actor_token, library_id)
        current = self._repository.get_caller(library_id, caller_id)
        if current is None or current.kind is not CallerKind.AGENT:
            raise ResourceNotFoundError
        actor = self._operator(actor_token, library_id)
        if current.disabled_at is not None:
            return current
        disabled_at = self._clock()
        disabled = self._repository.disable_caller(
            library_id,
            caller_id,
            disabled_at=disabled_at,
        )
        if disabled is None:
            raise ResourceNotFoundError
        self._audit(
            actor,
            action="auth.caller.disable",
            resource_type="caller",
            resource_id=caller_id,
            request_id=request_id,
            occurred_at=disabled_at,
            policy_version_before=current.policy_version,
            policy_version_after=disabled.policy_version,
        )
        return disabled

    def add_grant(
        self,
        actor_token: str,
        *,
        library_id: str,
        caller_id: str,
        section_id: str,
        action: SectionAction,
        request_id: str,
    ) -> SectionGrantRecord:
        actor = self._operator(actor_token, library_id)
        caller = self._require_agent_caller(library_id, caller_id)
        existing = self._repository.get_grant(
            library_id,
            caller_id,
            section_id,
            action,
        )
        if existing is not None:
            return existing
        timestamp = self._clock()
        grant = self._repository.add_grant(
            NewSectionGrant(
                library_id=library_id,
                caller_id=caller_id,
                section_id=section_id,
                action=action,
                created_at=timestamp,
            )
        )
        updated = self._repository.increment_policy_version(
            library_id,
            caller_id,
            expected_version=caller.policy_version,
            updated_at=timestamp,
        )
        if updated is None:
            raise PolicyConflictError
        self._audit(
            actor,
            action="auth.grant.add",
            resource_type="section_grant",
            resource_id=section_id,
            request_id=request_id,
            occurred_at=timestamp,
            policy_version_before=caller.policy_version,
            policy_version_after=updated.policy_version,
            target_caller_id=caller_id,
            section_id=section_id,
            section_action=action,
        )
        return grant

    def remove_grant(
        self,
        actor_token: str,
        *,
        library_id: str,
        caller_id: str,
        section_id: str,
        action: SectionAction,
        request_id: str,
    ) -> bool:
        actor = self._operator(actor_token, library_id)
        caller = self._require_agent_caller(library_id, caller_id)
        removed = self._repository.remove_grant(
            library_id,
            caller_id,
            section_id,
            action,
        )
        if not removed:
            return False
        timestamp = self._clock()
        updated = self._repository.increment_policy_version(
            library_id,
            caller_id,
            expected_version=caller.policy_version,
            updated_at=timestamp,
        )
        if updated is None:
            raise PolicyConflictError
        self._audit(
            actor,
            action="auth.grant.remove",
            resource_type="section_grant",
            resource_id=section_id,
            request_id=request_id,
            occurred_at=timestamp,
            policy_version_before=caller.policy_version,
            policy_version_after=updated.policy_version,
            target_caller_id=caller_id,
            section_id=section_id,
            section_action=action,
        )
        return True

    def _operator(self, actor_token: str, library_id: str) -> AuthenticatedCaller:
        return AuthenticationService(
            self._repository,
            clock=self._clock,
        ).require_operator(actor_token, library_id=library_id)

    def _operator_without_usage_update(
        self,
        actor_token: str,
        library_id: str,
    ) -> AuthenticatedCaller:
        return AuthenticationService(
            self._repository,
            clock=self._clock,
            last_used_coalesce_microseconds=-1,
        ).require_operator(actor_token, library_id=library_id)

    def _require_caller(self, library_id: str, caller_id: str) -> CallerRecord:
        caller = self._repository.get_caller(library_id, caller_id)
        if caller is None:
            raise ResourceNotFoundError
        if caller.disabled_at is not None:
            raise AuthenticationError
        return caller

    def _require_agent_caller(self, library_id: str, caller_id: str) -> CallerRecord:
        caller = self._require_caller(library_id, caller_id)
        if caller.kind is not CallerKind.AGENT:
            raise ResourceNotFoundError
        return caller

    def _audit(
        self,
        actor: AuthenticatedCaller,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        request_id: str,
        occurred_at: int,
        policy_version_before: int | None = None,
        policy_version_after: int | None = None,
        target_caller_id: str | None = None,
        section_id: str | None = None,
        section_action: SectionAction | None = None,
    ) -> AuditEventRecord:
        return self._repository.add_audit_event(
            NewAuditEvent(
                id=self._id_factory(),
                library_id=actor.caller.library_id,
                actor_caller_id=actor.caller.id,
                actor_credential_id=actor.credential.id,
                target_caller_id=target_caller_id,
                section_id=section_id,
                section_action=section_action,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=AuditOutcome.SUCCEEDED,
                request_id=request_id,
                policy_version_before=policy_version_before,
                policy_version_after=policy_version_after,
                occurred_at=occurred_at,
            )
        )


__all__ = [
    "BootstrapAlreadyCompletedError",
    "CredentialLifecycleError",
    "OperatorBootstrapService",
    "LocalOperatorRecoveryService",
    "OperatorRecoveryUnavailableError",
    "OperatorService",
    "PolicyConflictError",
    "ResourceNotFoundError",
]
