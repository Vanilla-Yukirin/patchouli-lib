from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from time import time
from uuid import uuid4

from sqlalchemy import Connection, Engine, insert, select

from patchouli_lib.admin.contracts import (
    BootstrapInput,
    PageTagFormInput,
    ProvisionAgentInput,
    RecoverOperatorInput,
    RestoreArchiveFormInput,
    RevokeAgentCredentialInput,
    TagFormInput,
)
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.auth.models import AdminStructureAuditEvent
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    MAX_RFC3339_TIMESTAMP_MICROSECONDS,
    CallerKind,
    LocalOperatorRecovery,
    OperatorBootstrap,
)
from patchouli_lib.auth.service import AuthenticationError, AuthenticationService, utc_microseconds
from patchouli_lib.content.models import Page
from patchouli_lib.content.schemas import ArchiveIdempotencyKey, PageLifecycleCommand
from patchouli_lib.content.service import ArchiveService
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import (
    OriginalResponse,
    ReplayResponse,
    digest_idempotency_key,
)
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
    LocalOperatorRecoveryService,
    OperatorBootstrapService,
    OperatorService,
    ResourceNotFoundError,
)
from patchouli_lib.tags.service import TagNotFoundError, TagService

Clock = Callable[[], int]
RequestIdFactory = Callable[[], str]
_MICROSECONDS_PER_SECOND = 1_000_000


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
            self._require_current_structure_session(connection, master_session, session_fingerprint)
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
            self._require_current_structure_session(connection, master_session, session_fingerprint)
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
            self._require_current_structure_session(connection, master_session, session_fingerprint)
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
    def _require_current_structure_session(
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

    def create_tag(self, library_id: str, request: TagFormInput) -> tuple[str, bool]:
        token = request.operator_token.get_secret_value()
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
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
            return ArchiveService(connection, clock=self._clock).restore_page_as_operator(
                token, command, idempotency
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
