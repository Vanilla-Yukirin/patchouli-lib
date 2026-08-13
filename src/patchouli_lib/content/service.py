"""Transaction-neutral Archive Page mutation orchestration."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from typing import Final
from uuid import uuid4

from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from patchouli_lib.api.contracts import build_api_v1_path
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import AuditOutcome, NewAuditEvent, SectionAction
from patchouli_lib.auth.service import AuthenticationService, utc_microseconds
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    AppendArchiveRevisionCommand,
    ArchiveCitation,
    ArchiveIdempotencyKey,
    ArchiveMutationReplay,
    ArchiveMutationResult,
    ArchiveMutationSuccess,
    ArchivePageView,
    ArchiveResponseBody,
    ArchiveRevisionView,
    ArchiveSourceInput,
    CreateArchiveCommand,
    MarkdownContent,
    NewPage,
    NewPageIdCollisionCounter,
    NewPageIdentifier,
    NewPageSource,
    NewRevision,
    PageRecord,
    PageSourceRecord,
    RevisionRecord,
)
from patchouli_lib.idempotency.repository import IdempotencyRepository
from patchouli_lib.idempotency.schemas import (
    IdempotencyRequest,
    OriginalResponse,
    ReplayResponse,
    TransactionValidatedCaller,
    digest_request_fingerprint,
)
from patchouli_lib.idempotency.service import IdempotencyService
from patchouli_lib.identifiers import (
    DEFAULT_COLLISION_ATTEMPTS,
    MAX_COLLISION_ORDINAL,
    MAX_REVISION_NUMBER,
    PAGE_ID_SCHEME,
    GeneratedPageId,
    IdentifierGenerationError,
    OccurrenceTime,
    canonical_utc_wire,
    generate_page_id,
    generate_page_uid,
    generate_revision_id,
    page_id_registry_digest,
    validate_page_uid,
    validate_revision_id,
)

Clock = Callable[[], int]
IdFactory = Callable[[], str]
PageUidFactory = Callable[[], bytes]
RevisionIdFactory = Callable[[], str]

CREATE_ROUTE_TEMPLATE: Final = "/api/v1/sections/{section_id}/books/{book_id}/pages"
REVISE_ROUTE_TEMPLATE: Final = "/api/v1/sections/{section_id}/pages/{page_id}/revisions"
PAGE_ETAG_DOMAIN: Final = b"patchouli-lib/page-current-etag/v1\x00"


class ArchiveTransactionRequiredError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("An active caller-owned write transaction is required.")


class ArchiveNotFoundError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("The requested archive parent was not found.")


class ArchivePreconditionRequiredError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("A current strong ETag is required.")


class ArchivePreconditionFailedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("The current archive revision does not match the precondition.")


class ArchivePersistenceError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Archive mutation could not be persisted.")


class ArchiveIdentifierExhaustedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Archive identifier allocation failed.")


class ArchiveReplayCorruptError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Stored archive replay data is invalid.")


def _new_opaque_id() -> str:
    return uuid4().hex


def page_current_etag(page_uid: bytes, revision_id: str, revision_number: int) -> str:
    """Return a strong Page-scoped validator for one exact current Revision."""

    validated_uid = validate_page_uid(page_uid)
    validated_revision = validate_revision_id(revision_id)
    if type(revision_number) is not int or not 1 <= revision_number <= MAX_REVISION_NUMBER:
        raise ValueError("Invalid current Revision number.")
    digest = hashlib.sha256(PAGE_ETAG_DOMAIN)
    digest.update(validated_uid)
    digest.update(validated_revision.encode("ascii"))
    digest.update(revision_number.to_bytes(8, "big"))
    return f'"page-v1-{digest.hexdigest()}"'


class ArchiveService:
    """Coordinate authorized Archive writes without starting or committing a transaction.

    The supplied connection must already be inside the caller's short
    ``BEGIN IMMEDIATE`` transaction. Authentication, current Section grant,
    route-to-resource relationship, content, audit, and replay state are all
    read or written through that same connection.
    """

    def __init__(
        self,
        connection: Connection,
        *,
        clock: Clock = utc_microseconds,
        id_factory: IdFactory = _new_opaque_id,
        page_uid_factory: PageUidFactory = generate_page_uid,
        revision_id_factory: RevisionIdFactory = generate_revision_id,
        collision_attempts: int = DEFAULT_COLLISION_ATTEMPTS,
    ) -> None:
        if type(collision_attempts) is not int or collision_attempts < 1:
            raise ValueError("Identifier collision attempt bound must be positive.")
        self._connection = connection
        self._content = ContentRepository(connection)
        self._auth_repository = AuthRepository(connection)
        self._idempotency = IdempotencyService(IdempotencyRepository(connection))
        self._clock = clock
        self._id_factory = id_factory
        self._page_uid_factory = page_uid_factory
        self._revision_id_factory = revision_id_factory
        self._collision_attempts = collision_attempts

    def create_archive(
        self,
        token_value: str,
        command: CreateArchiveCommand,
        idempotency: ArchiveIdempotencyKey,
    ) -> ArchiveMutationResult:
        """Create one Archive Page graph and its replay/audit state atomically."""

        self._require_transaction()
        book = self._content.get_book(command.library_id, command.book_id)
        if book is None:
            raise ArchiveNotFoundError
        operation_at = self._operation_time()
        authenticated = AuthenticationService(
            self._auth_repository,
            clock=lambda: operation_at,
        ).authorize_content(
            token_value,
            library_id=command.library_id,
            section_id=book.section_id,
            action=SectionAction.ARCHIVE_WRITE,
        )
        caller = TransactionValidatedCaller(
            library_id=command.library_id,
            caller_id=authenticated.caller.id,
        )
        request = IdempotencyRequest(
            method="POST",
            route_template=CREATE_ROUTE_TEMPLATE,
            key_digest=idempotency.key_digest,
            request_fingerprint=self._create_fingerprint(command),
        )
        replay = self._idempotency.lookup(caller, request)
        # A reused key must compare its route-bound fingerprint first; neither
        # an exact replay nor a fresh mutation may cross the relationship gate.
        self._require_route_section(book.section_id, command.section_id)
        if replay is not None:
            return self._replay_result(replay)

        try:
            generated = self._allocate_page_id(command)
            page_uid = self._allocate_page_uid(command.library_id)
            revision_id = self._allocate_revision_id(command.library_id)
            markdown = MarkdownContent.from_bytes(command.content_md)
            page = self._content.add_page(
                NewPage(
                    library_id=command.library_id,
                    page_uid=page_uid,
                    section_id=book.section_id,
                    book_id=book.id,
                    page_id=generated.value,
                    id_scheme=PAGE_ID_SCHEME,
                    id_timestamp_micros=(command.occurred_at // 1_000) * 1_000,
                    base_slug=generated.base_slug,
                    collision_ordinal=generated.collision_ordinal,
                    title=command.title,
                    page_type="archive",
                    occurred_at=command.occurred_at,
                    current_revision_id=revision_id,
                    current_revision_number=1,
                    created_at=operation_at,
                    updated_at=operation_at,
                )
            )
            revision = self._content.add_revision(
                NewRevision(
                    library_id=command.library_id,
                    revision_id=revision_id,
                    page_uid=page_uid,
                    revision_number=1,
                    created_at=operation_at,
                    **markdown.model_dump(),
                )
            )
            self._content.add_identifier(
                NewPageIdentifier(
                    library_id=command.library_id,
                    identifier_digest=page_id_registry_digest(page.page_id),
                    identifier_text=page.page_id,
                    id_scheme=PAGE_ID_SCHEME,
                    identifier_kind="canonical",
                    page_uid=page_uid,
                    created_at=operation_at,
                )
            )
            source = self._add_source(
                command.source,
                library_id=command.library_id,
                page_uid=page_uid,
                revision=revision,
                operation_at=operation_at,
            )
            response, citation = self._fresh_response(
                page,
                revision,
                command.request_id,
                revision_location=False,
            )
            audit = self._auth_repository.add_audit_event(
                NewAuditEvent(
                    id=self._id_factory(),
                    library_id=command.library_id,
                    actor_caller_id=authenticated.caller.id,
                    actor_credential_id=authenticated.credential.id,
                    action="content.archive.create",
                    resource_type="page",
                    resource_id=page.page_id,
                    outcome=AuditOutcome.SUCCEEDED,
                    request_id=command.request_id,
                    occurred_at=operation_at,
                )
            )
            self._idempotency.record_success(caller, request, response)
        except SQLAlchemyError:
            raise ArchivePersistenceError from None
        return ArchiveMutationSuccess(page, revision, source, citation, audit, response)

    def append_revision(
        self,
        token_value: str,
        command: AppendArchiveRevisionCommand,
        idempotency: ArchiveIdempotencyKey,
    ) -> ArchiveMutationResult:
        """Append exactly one immutable Revision and advance current atomically."""

        self._require_transaction()
        page = self._content.get_page(command.library_id, command.page_id)
        if page is None or page.page_type != "archive":
            raise ArchiveNotFoundError
        operation_at = self._operation_time()
        authenticated = AuthenticationService(
            self._auth_repository,
            clock=lambda: operation_at,
        ).authorize_content(
            token_value,
            library_id=command.library_id,
            section_id=page.section_id,
            action=SectionAction.ARCHIVE_WRITE,
        )
        caller = TransactionValidatedCaller(
            library_id=command.library_id,
            caller_id=authenticated.caller.id,
        )
        request = IdempotencyRequest(
            method="POST",
            route_template=REVISE_ROUTE_TEMPLATE,
            key_digest=idempotency.key_digest,
            request_fingerprint=self._revision_fingerprint(command),
        )
        replay = self._idempotency.lookup(caller, request)
        # The exact route is part of the request identity, while this gate keeps
        # both replay presentation and revision state scoped to the target Page.
        self._require_route_section(page.section_id, command.section_id)
        if replay is not None:
            return self._replay_result(replay)
        if command.expected_etag is None:
            raise ArchivePreconditionRequiredError
        current_etag = page_current_etag(
            page.page_uid,
            page.current_revision_id,
            page.current_revision_number,
        )
        if not hmac.compare_digest(command.expected_etag, current_etag):
            raise ArchivePreconditionFailedError
        if page.current_revision_number >= MAX_REVISION_NUMBER:
            raise ArchivePersistenceError

        try:
            revision_id = self._allocate_revision_id(command.library_id)
            markdown = MarkdownContent.from_bytes(command.content_md)
            revision = self._content.add_revision(
                NewRevision(
                    library_id=command.library_id,
                    revision_id=revision_id,
                    page_uid=page.page_uid,
                    revision_number=page.current_revision_number + 1,
                    created_at=operation_at,
                    **markdown.model_dump(),
                )
            )
            advanced = self._content.advance_current_revision(
                page,
                revision,
                updated_at=operation_at,
            )
            if advanced is None:
                raise ArchivePreconditionFailedError
            source = self._add_source(
                command.source,
                library_id=command.library_id,
                page_uid=page.page_uid,
                revision=revision,
                operation_at=operation_at,
            )
            response, citation = self._fresh_response(
                advanced,
                revision,
                command.request_id,
                revision_location=True,
            )
            audit = self._auth_repository.add_audit_event(
                NewAuditEvent(
                    id=self._id_factory(),
                    library_id=command.library_id,
                    actor_caller_id=authenticated.caller.id,
                    actor_credential_id=authenticated.credential.id,
                    action="content.archive.revise",
                    resource_type="revision",
                    resource_id=revision.revision_id,
                    outcome=AuditOutcome.SUCCEEDED,
                    request_id=command.request_id,
                    occurred_at=operation_at,
                )
            )
            self._idempotency.record_success(caller, request, response)
        except SQLAlchemyError:
            raise ArchivePersistenceError from None
        return ArchiveMutationSuccess(advanced, revision, source, citation, audit, response)

    def _require_transaction(self) -> None:
        if not self._connection.in_transaction():
            raise ArchiveTransactionRequiredError

    @staticmethod
    def _require_route_section(actual_section_id: str, route_section_id: str) -> None:
        if not hmac.compare_digest(actual_section_id, route_section_id):
            raise ArchiveNotFoundError

    def _operation_time(self) -> int:
        value = self._clock()
        if type(value) is not int or value < 0:
            raise ArchivePersistenceError
        try:
            canonical_utc_wire(value)
        except ValueError:
            raise ArchivePersistenceError from None
        return value

    def _allocate_page_uid(self, library_id: str) -> bytes:
        for _ in range(self._collision_attempts):
            try:
                candidate = validate_page_uid(self._page_uid_factory())
            except (TypeError, ValueError):
                continue
            if not self._content.page_uid_exists(library_id, candidate):
                return candidate
        raise IdentifierGenerationError

    def _allocate_revision_id(self, library_id: str) -> str:
        for _ in range(self._collision_attempts):
            try:
                candidate = validate_revision_id(self._revision_id_factory())
            except (TypeError, ValueError):
                continue
            if not self._content.revision_id_exists(library_id, candidate):
                return candidate
        raise IdentifierGenerationError

    def _allocate_page_id(self, command: CreateArchiveCommand) -> GeneratedPageId:
        occurrence = OccurrenceTime(
            utc_microseconds=command.occurred_at,
            canonical_utc=canonical_utc_wire(command.occurred_at),
        )
        base = generate_page_id(occurrence, command.title)
        counter = self._content.get_collision_counter(
            command.library_id,
            PAGE_ID_SCHEME,
            (command.occurred_at // 1_000) * 1_000,
            base.base_slug,
        )
        if counter is None:
            ordinal = 1
            for _ in range(self._collision_attempts):
                if ordinal > MAX_COLLISION_ORDINAL:
                    break
                candidate = generate_page_id(
                    occurrence,
                    command.title,
                    collision_ordinal=ordinal,
                )
                if not self._content.identifier_exists(command.library_id, candidate.value):
                    self._content.add_collision_counter(
                        NewPageIdCollisionCounter(
                            library_id=command.library_id,
                            id_scheme=PAGE_ID_SCHEME,
                            id_timestamp_micros=(command.occurred_at // 1_000) * 1_000,
                            base_slug=base.base_slug,
                            next_ordinal=ordinal + 1,
                        )
                    )
                    return candidate
                ordinal += 1
            raise ArchiveIdentifierExhaustedError

        for _ in range(self._collision_attempts):
            if counter.next_ordinal > MAX_COLLISION_ORDINAL:
                break
            ordinal = counter.next_ordinal
            advanced = self._content.advance_collision_counter(
                counter,
                next_ordinal=ordinal + 1,
            )
            if advanced is None:
                raise ArchivePersistenceError
            counter = advanced
            candidate = generate_page_id(
                occurrence,
                command.title,
                collision_ordinal=ordinal,
            )
            if not self._content.identifier_exists(command.library_id, candidate.value):
                return candidate
        raise ArchiveIdentifierExhaustedError

    def _add_source(
        self,
        source: ArchiveSourceInput,
        *,
        library_id: str,
        page_uid: bytes,
        revision: RevisionRecord,
        operation_at: int,
    ) -> PageSourceRecord:
        return self._content.add_source(
            NewPageSource(
                library_id=library_id,
                source_id=self._id_factory(),
                page_uid=page_uid,
                revision_id=revision.revision_id,
                revision_number=revision.revision_number,
                kind=source.kind,
                locator=source.locator,
                captured_at=source.captured_at,
                created_at=operation_at,
            )
        )

    @staticmethod
    def _fresh_response(
        page: PageRecord,
        revision: RevisionRecord,
        request_id: str,
        *,
        revision_location: bool,
    ) -> tuple[OriginalResponse, ArchiveCitation]:
        href = build_api_v1_path(
            "sections",
            page.section_id,
            "pages",
            page.page_id,
            "revisions",
            str(revision.revision_number),
        )
        citation = ArchiveCitation(
            section_id=page.section_id,
            page_id=page.page_id,
            revision_id=revision.revision_id,
            revision_number=revision.revision_number,
            href=href,
        )
        body = ArchiveResponseBody(
            page=ArchivePageView(
                section_id=page.section_id,
                book_id=page.book_id,
                page_id=page.page_id,
                title=page.title,
                type="archive",
                occurred_at=canonical_utc_wire(page.occurred_at),
                current_revision_id=revision.revision_id,
                current_revision_number=revision.revision_number,
            ),
            revision=ArchiveRevisionView(
                page_id=page.page_id,
                revision_id=revision.revision_id,
                revision_number=revision.revision_number,
                created_at=canonical_utc_wire(revision.created_at),
                content_sha256=revision.content_sha256.hex(),
                content=revision.content_md.decode("utf-8"),
            ),
            citation=citation,
        )
        page_location = build_api_v1_path(
            "sections",
            page.section_id,
            "pages",
            page.page_id,
        )
        response = OriginalResponse(
            response_status=201,
            response_body=body.model_dump_json().encode("utf-8"),
            response_location=citation.href if revision_location else page_location,
            response_etag=page_current_etag(
                page.page_uid,
                revision.revision_id,
                revision.revision_number,
            ),
            original_request_id=request_id,
            original_request_timestamp=canonical_utc_wire(revision.created_at),
        )
        return response, citation

    @staticmethod
    def _replay_result(replay: ReplayResponse) -> ArchiveMutationReplay:
        try:
            body = ArchiveResponseBody.model_validate_json(replay.response_body)
        except ValueError:
            raise ArchiveReplayCorruptError from None
        return ArchiveMutationReplay(body, replay)

    @staticmethod
    def _create_fingerprint(command: CreateArchiveCommand) -> bytes:
        source = {
            "captured_at": command.source.captured_at,
            "kind": command.source.kind,
            "locator": command.source.locator,
        }
        metadata = {
            "book_id": command.book_id,
            "library_id": command.library_id,
            "occurred_at": command.occurred_at,
            "operation": "archive-create-v1",
            "section_id": command.section_id,
            "source": source,
            "title": command.title,
        }
        return digest_request_fingerprint(
            json.dumps(
                metadata,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
            command.content_md,
        )

    @staticmethod
    def _revision_fingerprint(command: AppendArchiveRevisionCommand) -> bytes:
        metadata = {
            "expected_etag": command.expected_etag,
            "library_id": command.library_id,
            "operation": "archive-revise-v1",
            "page_id": command.page_id,
            "section_id": command.section_id,
            "source": {
                "captured_at": command.source.captured_at,
                "kind": command.source.kind,
                "locator": command.source.locator,
            },
        }
        return digest_request_fingerprint(
            json.dumps(
                metadata,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
            command.content_md,
        )


__all__ = [
    "CREATE_ROUTE_TEMPLATE",
    "PAGE_ETAG_DOMAIN",
    "REVISE_ROUTE_TEMPLATE",
    "ArchiveIdentifierExhaustedError",
    "ArchiveNotFoundError",
    "ArchivePersistenceError",
    "ArchivePreconditionFailedError",
    "ArchivePreconditionRequiredError",
    "ArchiveReplayCorruptError",
    "ArchiveService",
    "ArchiveTransactionRequiredError",
    "page_current_etag",
]
