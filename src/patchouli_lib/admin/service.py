from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from time import time
from uuid import uuid4

from sqlalchemy import Connection, Engine, delete, insert, select

from patchouli_lib.admin.contracts import (
    BootstrapInput,
    MasterPageTagFormInput,
    MasterProvisionAgentInput,
    MasterRestoreArchiveFormInput,
    MasterRotateAgentCredentialInput,
    MasterSetAgentLibraryGrantsInput,
    MasterTagFormInput,
    PageTagFormInput,
    ProvisionAgentInput,
    RecoverOperatorInput,
    RestoreArchiveFormInput,
    RevokeAgentCredentialInput,
    TagFormInput,
)
from patchouli_lib.admin.master_audit import (
    MasterAuditRepository,
    grant_revisions_for_credentials,
)
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.auth.library_policy import LibraryAction, target_library_grants_digest
from patchouli_lib.auth.models import (
    AdminStructureAuditEvent,
    CredentialLibraryGrant,
    CredentialLibraryPolicy,
)
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    MAX_RFC3339_TIMESTAMP_MICROSECONDS,
    CallerKind,
    LocalOperatorRecovery,
    NewCaller,
    OperatorBootstrap,
)
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthenticationService,
    CredentialIssuer,
    new_opaque_id,
    utc_microseconds,
)
from patchouli_lib.content.models import Page
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveIdempotencyKey, PageLifecycleCommand
from patchouli_lib.content.service import (
    ArchiveLifecycleUnchangedError,
    ArchiveNotFoundError,
    ArchivePersistenceError,
    ArchivePreconditionFailedError,
    ArchiveService,
    page_current_etag,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import (
    OriginalResponse,
    ReplayResponse,
    digest_idempotency_key,
)
from patchouli_lib.identifiers import canonical_utc_wire
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import (
    BookRecord,
    CreateBookInput,
    CreateLibraryInput,
    CreateSectionInput,
    LibraryRecord,
    LibraryStructureSeed,
    SectionRecord,
)
from patchouli_lib.library.service import LibrarySeedService, LibraryStructureService
from patchouli_lib.operator.service import (
    CredentialLifecycleError,
    LocalOperatorRecoveryService,
    OperatorBootstrapService,
    OperatorService,
    PolicyConflictError,
    ResourceNotFoundError,
)
from patchouli_lib.tags.repository import TagRepository, normalize_tag_name
from patchouli_lib.tags.service import TagNotFoundError, TagService, TagValidationError

Clock = Callable[[], int]
RequestIdFactory = Callable[[], str]
_MICROSECONDS_PER_SECOND = 1_000_000


class GrantVersionConflictError(ValueError):
    """The exact credential grants changed since the form was displayed."""


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class DeliveredCredential:
    value: str = field(repr=False)
    library_id: str
    caller_id: str
    credential_id: str

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(value=<redacted>, library_id={self.library_id!r}, "
            f"caller_id={self.caller_id!r}, credential_id={self.credential_id!r})"
        )


class AdminActionService:
    """Adapt the existing operator services to short web-owned transactions."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Clock = utc_microseconds,
        request_id_factory: RequestIdFactory | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._request_id_factory = request_id_factory or _request_id

    def create_library(
        self,
        request: CreateLibraryInput,
        *,
        session_fingerprint: bytes,
        master_session: MasterAdminSession | None = None,
    ) -> LibraryRecord:
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            self._require_current_admin_session(connection, master_session, session_fingerprint)
            result = LibraryStructureService(
                LibraryRepository(connection), clock=lambda: now
            ).create_library(request)
            self._record_structure_event(
                connection,
                session_fingerprint,
                "library.create",
                result.id,
                now,
                master_session=master_session,
                target_type="library",
                target_id=result.id,
            )
        return result

    def create_section(
        self,
        library_id: str,
        request: CreateSectionInput,
        *,
        session_fingerprint: bytes,
        master_session: MasterAdminSession | None = None,
    ) -> SectionRecord:
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            self._require_current_admin_session(connection, master_session, session_fingerprint)
            result = LibraryStructureService(
                LibraryRepository(connection), clock=lambda: now
            ).create_section(library_id, request)
            self._record_structure_event(
                connection,
                session_fingerprint,
                "section.create",
                library_id,
                now,
                section_id=result.id,
                master_session=master_session,
                target_type="section",
                target_id=result.id,
            )
        return result

    def create_book(
        self,
        library_id: str,
        section_id: str,
        request: CreateBookInput,
        *,
        session_fingerprint: bytes,
        master_session: MasterAdminSession | None = None,
    ) -> BookRecord:
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            self._require_current_admin_session(connection, master_session, session_fingerprint)
            result = LibraryStructureService(
                LibraryRepository(connection), clock=lambda: now
            ).create_book(library_id, section_id, request)
            self._record_structure_event(
                connection,
                session_fingerprint,
                "book.create",
                library_id,
                now,
                section_id=section_id,
                book_id=result.id,
                master_session=master_session,
                target_type="book",
                target_id=result.id,
            )
        return result

    @staticmethod
    def _require_current_admin_session(
        connection: Connection, session: MasterAdminSession | None, fingerprint: bytes
    ) -> None:
        repository = MasterTokenRepository(connection)
        if session is None:
            # A legacy cookie is only admitted before local master setup.
            # Recheck inside the write transaction so setup cannot race it.
            if repository.has_identity():
                raise AuthenticationError
            return
        if (
            fingerprint != session.audit_fingerprint()
            or session.expires_at <= int(time())
            or not repository.is_session_generation_current(
                session.identity_id, session.session_generation
            )
        ):
            raise AuthenticationError

    def _record_structure_event(
        self,
        connection: Connection,
        fingerprint: bytes,
        action: str,
        library_id: str,
        occurred_at: int,
        *,
        section_id: str | None = None,
        book_id: str | None = None,
        master_session: MasterAdminSession | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> None:
        if type(fingerprint) is not bytes or len(fingerprint) != 32:
            raise ValueError("Invalid administration session fingerprint.")
        if master_session is not None:
            if target_type is None or target_id is None:
                raise ValueError("A master structure write requires a target.")
            MasterAuditRepository(connection).add_success(
                identity_id=master_session.identity_id,
                session_generation=master_session.session_generation,
                session_fingerprint=fingerprint,
                action=action,
                target_type=target_type,
                target_id=target_id,
                occurred_at=occurred_at,
                event_id=uuid4().hex,
            )
            return
        connection.execute(
            insert(AdminStructureAuditEvent),
            {
                "id": uuid4().hex,
                "session_fingerprint": fingerprint,
                "action": action,
                "library_id": library_id,
                "section_id": section_id,
                "book_id": book_id,
                "request_id": self._request_id_factory(),
                "occurred_at": occurred_at,
            },
        )

    def bootstrap(self, request: BootstrapInput) -> DeliveredCredential:
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            structure = LibrarySeedService(
                LibraryRepository(connection),
                clock=lambda: now,
            ).seed(
                LibraryStructureSeed(
                    library_name=request.library_name,
                    section_name=request.section_name,
                    section_description=request.section_description,
                    book_name=request.book_name,
                    book_summary=request.book_summary,
                )
            )
            result = OperatorBootstrapService(
                AuthRepository(connection),
                clock=lambda: now,
            ).bootstrap(
                OperatorBootstrap(
                    library_id=structure.library.id,
                    operator_name=request.operator_name,
                    operator_description=request.operator_description,
                    credential_expires_at=_expires_at(
                        now,
                        request.credential_ttl_seconds,
                    ),
                    request_id=self._request_id_factory(),
                )
            )
        return DeliveredCredential(
            value=result.credential.value,
            library_id=result.caller.library_id,
            caller_id=result.caller.id,
            credential_id=result.credential.credential.id,
        )

    def recover_operator(self, request: RecoverOperatorInput) -> DeliveredCredential:
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            library_id = _require_library(
                LibraryRepository(connection),
                request.library_name,
            )
            result = LocalOperatorRecoveryService(
                AuthRepository(connection),
                clock=lambda: now,
            ).recover(
                LocalOperatorRecovery(
                    library_id=library_id,
                    credential_expires_at=_expires_at(
                        now,
                        request.credential_ttl_seconds,
                    ),
                    request_id=self._request_id_factory(),
                )
            )
        return DeliveredCredential(
            value=result.credential.value,
            library_id=result.caller.library_id,
            caller_id=result.caller.id,
            credential_id=result.credential.credential.id,
        )

    def provision_agent(self, request: ProvisionAgentInput) -> DeliveredCredential:
        actor_token = request.operator_token.get_secret_value()
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            # The legacy web form must not finish after local master setup wins a race.
            if MasterTokenRepository(connection).has_identity():
                raise AuthenticationError
            library_repository = LibraryRepository(connection)
            library_id = _require_library(library_repository, request.library_name)
            section = library_repository.find_section_by_name(
                library_id,
                request.section_name,
            )
            if section is None:
                raise ResourceNotFoundError
            service = OperatorService(
                AuthRepository(connection),
                clock=lambda: now,
            )
            caller = service.create_agent_caller(
                actor_token,
                library_id=library_id,
                name=request.agent_name,
                description=request.agent_description,
                request_id=self._request_id_factory(),
            )
            issued = service.create_credential(
                actor_token,
                library_id=library_id,
                caller_id=caller.id,
                expires_at=_expires_at(now, request.credential_ttl_seconds),
                request_id=self._request_id_factory(),
            )
            for action in request.grants:
                service.add_grant(
                    actor_token,
                    library_id=library_id,
                    caller_id=caller.id,
                    section_id=section.id,
                    action=action,
                    request_id=self._request_id_factory(),
                )
        return DeliveredCredential(
            value=issued.value,
            library_id=library_id,
            caller_id=caller.id,
            credential_id=issued.credential.id,
        )

    def provision_agent_as_master(
        self, request: MasterProvisionAgentInput, *, master_session: MasterAdminSession
    ) -> DeliveredCredential:
        """Issue one Agent credential with an explicit, default-deny Library policy."""
        with immediate_transaction(self._engine) as connection:
            now = self._clock()
            self._require_current_admin_session(
                connection, master_session, master_session.audit_fingerprint()
            )
            repository = AuthRepository(connection)
            if not repository.library_exists(request.home_library_id):
                raise ResourceNotFoundError
            if any(not repository.library_exists(grant.library_id) for grant in request.grants):
                raise ResourceNotFoundError
            caller = repository.add_caller(
                NewCaller(
                    id=new_opaque_id(),
                    library_id=request.home_library_id,
                    kind=CallerKind.AGENT,
                    name=request.agent_name,
                    description=request.agent_description,
                    created_at=now,
                    updated_at=now,
                )
            )
            issued = CredentialIssuer(repository, clock=lambda: now).issue(
                caller, expires_at=_expires_at(now, request.credential_ttl_seconds)
            )
            connection.execute(
                insert(CredentialLibraryPolicy),
                {
                    "credential_id": issued.credential.id,
                    "caller_id": caller.id,
                    "home_library_id": caller.library_id,
                    "mode": "library_grants",
                    "created_at": now,
                },
            )
            for grant in request.grants:
                connection.execute(
                    insert(CredentialLibraryGrant),
                    {
                        "credential_id": issued.credential.id,
                        "caller_id": caller.id,
                        "home_library_id": caller.library_id,
                        "target_library_id": grant.library_id,
                        "action": grant.action.value,
                        "created_at": now,
                    },
                )
            MasterAuditRepository(connection).add_success(
                identity_id=master_session.identity_id,
                session_generation=master_session.session_generation,
                session_fingerprint=master_session.audit_fingerprint(),
                action="auth.agent.provision",
                target_type="caller",
                target_id=caller.id,
                occurred_at=now,
                event_id=uuid4().hex,
            )
        return DeliveredCredential(
            value=issued.value,
            library_id=caller.library_id,
            caller_id=caller.id,
            credential_id=issued.credential.id,
        )

    def rotate_agent_credential_as_master(
        self,
        library_id: str,
        caller_id: str,
        credential_id: str,
        request: MasterRotateAgentCredentialInput,
        *,
        master_session: MasterAdminSession,
    ) -> DeliveredCredential:
        """Replace one active Library-scoped Agent Token without changing its grants."""
        with immediate_transaction(self._engine) as connection:
            now = self._clock()
            self._require_current_admin_session(
                connection, master_session, master_session.audit_fingerprint()
            )
            repository = AuthRepository(connection)
            caller = repository.get_caller(library_id, caller_id)
            current = repository.get_credential(library_id, caller_id, credential_id)
            if caller is None or caller.kind is not CallerKind.AGENT or current is None:
                raise ResourceNotFoundError
            if (
                caller.disabled_at is not None
                or current.created_at > now
                or current.expires_at <= now
                or current.revoked_at is not None
                or current.rotated_at is not None
            ):
                raise CredentialLifecycleError
            if not repository.has_library_grant_policy(library_id, caller_id, credential_id):
                # Legacy Section grants belong to the caller, not this credential.
                # Require explicit reprovision instead of silently changing modes.
                raise PolicyConflictError
            existing_grants = repository.list_credential_library_grants(
                home_library_id=library_id, caller_id=caller_id, credential_id=credential_id
            )
            replacement = CredentialIssuer(repository, clock=lambda: now).issue(
                caller, expires_at=_expires_at(now, request.credential_ttl_seconds)
            )
            connection.execute(
                insert(CredentialLibraryPolicy),
                {
                    "credential_id": replacement.credential.id,
                    "caller_id": caller_id,
                    "home_library_id": library_id,
                    "mode": "library_grants",
                    "created_at": now,
                },
            )
            for grant in existing_grants:
                for action in grant.actions:
                    connection.execute(
                        insert(CredentialLibraryGrant),
                        {
                            "credential_id": replacement.credential.id,
                            "caller_id": caller_id,
                            "home_library_id": library_id,
                            "target_library_id": grant.library_id,
                            "action": action.value,
                            "created_at": now,
                        },
                    )
            if (
                repository.mark_credential_rotated(
                    current, replacement.credential.id, rotated_at=now
                )
                is None
            ):
                raise CredentialLifecycleError
            MasterAuditRepository(connection).add_success(
                identity_id=master_session.identity_id,
                session_generation=master_session.session_generation,
                session_fingerprint=master_session.audit_fingerprint(),
                action="auth.agent_credential.rotate",
                target_type="credential",
                target_id=credential_id,
                occurred_at=now,
                event_id=uuid4().hex,
            )
        return DeliveredCredential(
            value=replacement.value,
            library_id=library_id,
            caller_id=caller_id,
            credential_id=replacement.credential.id,
        )

    def set_agent_grants_as_master(
        self,
        library_id: str,
        caller_id: str,
        credential_id: str,
        target_library_id: str,
        request: MasterSetAgentLibraryGrantsInput,
        *,
        master_session: MasterAdminSession,
    ) -> bool:
        """Replace grants on one active Library-mode Token, with stale-form protection."""
        with immediate_transaction(self._engine) as connection:
            now = self._clock()
            self._require_current_admin_session(
                connection, master_session, master_session.audit_fingerprint()
            )
            repository = AuthRepository(connection)
            caller = repository.get_caller(library_id, caller_id)
            current = repository.get_credential(library_id, caller_id, credential_id)
            if caller is None or caller.kind is not CallerKind.AGENT or current is None:
                raise ResourceNotFoundError
            if (
                caller.disabled_at is not None
                or current.created_at > now
                or current.expires_at <= now
                or current.revoked_at is not None
                or current.rotated_at is not None
            ):
                raise CredentialLifecycleError
            if not repository.has_library_grant_policy(library_id, caller_id, credential_id):
                raise PolicyConflictError
            if not repository.library_exists(target_library_id):
                raise ResourceNotFoundError
            existing = {
                action
                for grant in repository.list_credential_library_grants(
                    home_library_id=library_id, caller_id=caller_id, credential_id=credential_id
                )
                if grant.library_id == target_library_id
                for action in grant.actions
            }
            revision = grant_revisions_for_credentials(connection, (credential_id,)).get(
                (credential_id, target_library_id), 0
            )
            if (
                target_library_grants_digest(
                    library_id, caller_id, credential_id, target_library_id, existing, revision
                )
                != request.expected_digest
            ):
                raise GrantVersionConflictError
            desired = {
                action
                for action, enabled in (
                    (LibraryAction.READ, request.read),
                    (LibraryAction.WRITE, request.write),
                )
                if enabled
            }
            if desired == existing:
                return False
            old_bits = (
                f"{int(LibraryAction.READ in existing)}{int(LibraryAction.WRITE in existing)}"
            )
            new_bits = f"{int(request.read)}{int(request.write)}"
            for action in existing - desired:
                connection.execute(
                    delete(CredentialLibraryGrant).where(
                        CredentialLibraryGrant.credential_id == credential_id,
                        CredentialLibraryGrant.caller_id == caller_id,
                        CredentialLibraryGrant.home_library_id == library_id,
                        CredentialLibraryGrant.target_library_id == target_library_id,
                        CredentialLibraryGrant.action == action.value,
                    )
                )
            for action in desired - existing:
                connection.execute(
                    insert(CredentialLibraryGrant),
                    {
                        "credential_id": credential_id,
                        "caller_id": caller_id,
                        "home_library_id": library_id,
                        "target_library_id": target_library_id,
                        "action": action.value,
                        "created_at": now,
                    },
                )
            MasterAuditRepository(connection).add_success(
                identity_id=master_session.identity_id,
                session_generation=master_session.session_generation,
                session_fingerprint=master_session.audit_fingerprint(),
                action="auth.agent_credential.grants_update",
                target_type="credential_library_grant",
                target_id=f"{credential_id}:{target_library_id}:{old_bits}:{new_bits}",
                occurred_at=now,
                event_id=uuid4().hex,
            )
        return True

    def revoke_agent_credential(self, request: RevokeAgentCredentialInput) -> None:
        actor_token = request.operator_token.get_secret_value()
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            library_id = _require_library(
                LibraryRepository(connection),
                request.library_name,
            )
            repository = AuthRepository(connection)
            caller = repository.get_caller(library_id, request.caller_id)
            if caller is None or caller.kind is not CallerKind.AGENT:
                raise ResourceNotFoundError
            OperatorService(
                repository,
                clock=lambda: now,
            ).revoke_credential(
                actor_token,
                library_id=library_id,
                caller_id=request.caller_id,
                credential_id=request.credential_id,
                request_id=self._request_id_factory(),
            )

    def revoke_agent_credential_as_master(
        self,
        library_id: str,
        caller_id: str,
        credential_id: str,
        *,
        master_session: MasterAdminSession,
    ) -> bool:
        """Revoke one exact Agent credential without impersonating an operator."""
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            self._require_current_admin_session(
                connection, master_session, master_session.audit_fingerprint()
            )
            repository = AuthRepository(connection)
            caller = repository.get_caller(library_id, caller_id)
            credential = repository.get_credential(library_id, caller_id, credential_id)
            if caller is None or caller.kind is not CallerKind.AGENT or credential is None:
                raise ResourceNotFoundError
            if credential.revoked_at is not None or credential.rotated_at is not None:
                return False
            revoked = repository.revoke_credential(credential, revoked_at=now)
            if revoked is None or revoked.revoked_at != now:
                raise CredentialLifecycleError
            MasterAuditRepository(connection).add_success(
                identity_id=master_session.identity_id,
                session_generation=master_session.session_generation,
                session_fingerprint=master_session.audit_fingerprint(),
                action="auth.agent_credential.revoke",
                target_type="credential",
                target_id=credential_id,
                occurred_at=now,
                event_id=uuid4().hex,
            )
        return True

    def create_tag(self, library_id: str, request: TagFormInput) -> tuple[str, bool]:
        token = request.operator_token.get_secret_value()
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            if MasterTokenRepository(connection).has_identity():
                raise AuthenticationError
            AuthenticationService(AuthRepository(connection), clock=lambda: now).require_operator(
                token, library_id=library_id
            )
            tag, created = TagService(connection, clock=lambda: now).create_tag(
                token,
                library_id=library_id,
                name=request.name,
                request_id=self._request_id_factory(),
            )
        return tag.id, created

    def create_tag_as_master(
        self, library_id: str, request: MasterTagFormInput, *, master_session: MasterAdminSession
    ) -> tuple[str, bool]:
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            self._require_current_admin_session(
                connection, master_session, master_session.audit_fingerprint()
            )
            if LibraryRepository(connection).get_library(library_id) is None:
                raise TagNotFoundError
            try:
                normalize_tag_name(request.name)
            except ValueError:
                raise TagValidationError from None
            tags = TagRepository(connection)
            previous = tags.find_tag(library_id=library_id, name=request.name)
            if previous is not None:
                return previous.id, False
            tag = tags.add_tag(
                library_id=library_id, tag_id=uuid4().hex, name=request.name, created_at=now
            )
            MasterAuditRepository(connection).add_success(
                identity_id=master_session.identity_id,
                session_generation=master_session.session_generation,
                session_fingerprint=master_session.audit_fingerprint(),
                action="tag.create",
                target_type="tag",
                target_id=f"{library_id}:{tag.id}",
                occurred_at=now,
                event_id=uuid4().hex,
            )
            return tag.id, True

    def set_page_tag(
        self,
        library_id: str,
        section_id: str,
        book_id: str,
        page_id: str,
        request: PageTagFormInput,
    ) -> bool:
        token = request.operator_token.get_secret_value()
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            if MasterTokenRepository(connection).has_identity():
                raise AuthenticationError
            AuthenticationService(AuthRepository(connection), clock=lambda: now).require_operator(
                token, library_id=library_id
            )
            page_uid = connection.execute(
                select(Page.page_uid).where(
                    Page.library_id == library_id,
                    Page.section_id == section_id,
                    Page.book_id == book_id,
                    Page.page_id == page_id,
                    Page.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if page_uid is None:
                raise TagNotFoundError
            return TagService(connection, clock=lambda: now).set_page_tag(
                token,
                library_id=library_id,
                section_id=section_id,
                page_id=page_id,
                tag_id=request.tag_id,
                attach=request.operation == "attach",
                request_id=self._request_id_factory(),
            )

    def set_page_tag_as_master(
        self,
        library_id: str,
        section_id: str,
        book_id: str,
        page_id: str,
        request: MasterPageTagFormInput,
        *,
        master_session: MasterAdminSession,
    ) -> bool:
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            self._require_current_admin_session(
                connection, master_session, master_session.audit_fingerprint()
            )
            page_uid = connection.execute(
                select(Page.page_uid).where(
                    Page.library_id == library_id,
                    Page.section_id == section_id,
                    Page.book_id == book_id,
                    Page.page_id == page_id,
                    Page.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if page_uid is None:
                raise TagNotFoundError
            tags = TagRepository(connection)
            if tags.get_tag(library_id=library_id, tag_id=request.tag_id) is None:
                raise TagNotFoundError
            attached = tags.has_page_tag(
                library_id=library_id, page_uid=page_uid, tag_id=request.tag_id
            )
            attach = request.operation == "attach"
            if attached == attach:
                return False
            if attach:
                tags.attach_page(
                    library_id=library_id,
                    page_uid=page_uid,
                    tag_id=request.tag_id,
                    created_at=now,
                )
            elif not tags.detach_page(
                library_id=library_id, page_uid=page_uid, tag_id=request.tag_id
            ):
                raise RuntimeError("Expected Tag association disappeared inside transaction.")
            MasterAuditRepository(connection).add_success(
                identity_id=master_session.identity_id,
                session_generation=master_session.session_generation,
                session_fingerprint=master_session.audit_fingerprint(),
                action="tag.page.attach" if attach else "tag.page.detach",
                target_type="page_tag",
                target_id=f"{library_id}:{page_uid.hex()}:{request.tag_id}",
                occurred_at=now,
                event_id=uuid4().hex,
            )
            return True

    def restore_archive_page(
        self,
        library_id: str,
        section_id: str,
        page_id: str,
        request: RestoreArchiveFormInput,
    ) -> OriginalResponse | ReplayResponse:
        """Restore through the Archive domain service with a request-only Operator token."""

        command = PageLifecycleCommand(
            library_id=library_id,
            section_id=section_id,
            page_id=page_id,
            expected_etag=request.expected_etag,
            request_id=f"req_{uuid4().hex}",
        )
        idempotency = ArchiveIdempotencyKey(
            key_digest=digest_idempotency_key(request.idempotency_key)
        )
        token = request.operator_token.get_secret_value()
        with immediate_transaction(self._engine) as connection:
            if MasterTokenRepository(connection).has_identity():
                raise AuthenticationError
            return ArchiveService(connection, clock=self._clock).restore_page_as_operator(
                token, command, idempotency
            )

    def restore_archive_page_as_master(
        self,
        library_id: str,
        section_id: str,
        page_id: str,
        request: MasterRestoreArchiveFormInput,
        *,
        master_session: MasterAdminSession,
    ) -> None:
        """Restore a tombstoned Archive Page as the actual master identity."""

        with immediate_transaction(self._engine) as connection:
            self._require_current_admin_session(
                connection, master_session, master_session.audit_fingerprint()
            )
            content = ContentRepository(connection)
            page = content.get_page(library_id, page_id)
            if (
                page is None
                or page.page_type != "archive"
                or not hmac.compare_digest(page.section_id, section_id)
            ):
                raise ArchiveNotFoundError
            current_etag = page_current_etag(
                page.page_uid,
                page.current_revision_id,
                page.current_revision_number,
                page.occurred_at,
                page.updated_at,
            )
            if not hmac.compare_digest(request.expected_etag, current_etag):
                raise ArchivePreconditionFailedError
            if page.deleted_at is None:
                raise ArchiveLifecycleUnchangedError
            if page.updated_at >= (1 << 63) - 1:
                raise ArchivePersistenceError
            changed_at = max(self._clock(), page.updated_at + 1)
            try:
                canonical_utc_wire(changed_at)
            except ValueError:
                raise ArchivePersistenceError from None
            event_id = uuid4().hex
            MasterAuditRepository(connection).add_success(
                identity_id=master_session.identity_id,
                session_generation=master_session.session_generation,
                session_fingerprint=master_session.audit_fingerprint(),
                action="content.archive.restore",
                target_type="page",
                target_id=f"{library_id}:{page.page_uid.hex()}",
                occurred_at=changed_at,
                event_id=event_id,
            )
            content.transition_page_lifecycle(
                page,
                action="restore",
                actor_caller_id=None,
                actor_home_library_id=None,
                master_audit_event_id=event_id,
                request_id=f"req_{uuid4().hex}",
                changed_at=changed_at,
            )


def _expires_at(now: int, ttl_seconds: int) -> int:
    expires_at = now + ttl_seconds * _MICROSECONDS_PER_SECOND
    if expires_at > MAX_RFC3339_TIMESTAMP_MICROSECONDS:
        raise ValueError("Credential expiry exceeds the supported timestamp range.")
    return expires_at


def _require_library(repository: LibraryRepository, name: str) -> str:
    library = repository.find_library_by_name(name)
    if library is None:
        raise ResourceNotFoundError
    return library.id


def _request_id() -> str:
    return f"req_admin_{uuid4().hex}"


__all__ = ["AdminActionService", "DeliveredCredential"]
