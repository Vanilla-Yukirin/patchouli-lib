"""Transaction-neutral Archive Page mutation orchestration."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, Literal
from uuid import uuid4

from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from patchouli_lib.api.contracts import build_api_v1_path
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    AuditOutcome,
    AuthenticatedCaller,
    NewAuditEvent,
    SectionAction,
)
from patchouli_lib.auth.service import AuthenticationService, AuthorizationError, utc_microseconds
from patchouli_lib.content.models import MAX_OCCURRENCE_MICROSECONDS, MIN_OCCURRENCE_MICROSECONDS
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    AppendArchiveRevisionCommand,
    ArchiveCitation,
    ArchiveIdempotencyKey,
    ArchiveMutationReplay,
    ArchiveMutationResult,
    ArchiveMutationSuccess,
    ArchiveOccurrenceNotice,
    ArchivePageView,
    ArchiveResponseBody,
    ArchiveRevisionView,
    ArchiveSourceInput,
    CorrectArchiveOccurrenceCommand,
    CreateArchiveCommand,
    MarkdownContent,
    NewPage,
    NewPageIdCollisionCounter,
    NewPageIdentifier,
    NewPageSource,
    NewRevision,
    OccurrenceCorrectionResponseBody,
    PageLifecycleCommand,
    PageLifecycleResponseBody,
    PageOccurrenceCorrectionCommand,
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
    parse_occurrence_time,
    validate_page_uid,
    validate_revision_id,
)

if TYPE_CHECKING:
    from patchouli_lib.content.page_membership_history import PageState, PageStateTimeline

Clock = Callable[[], int]
IdFactory = Callable[[], str]
PageUidFactory = Callable[[], bytes]
RevisionIdFactory = Callable[[], str]

CREATE_ROUTE_TEMPLATE: Final = "/api/v1/sections/{section_id}/books/{book_id}/pages"
REVISE_ROUTE_TEMPLATE: Final = "/api/v1/sections/{section_id}/pages/{page_id}/revisions"
CORRECT_OCCURRENCE_ROUTE_TEMPLATE: Final = (
    "/api/v1/sections/{section_id}/pages/{page_id}/occurrence"
)
DELETE_PAGE_ROUTE_TEMPLATE: Final = "/api/v1/sections/{section_id}/pages/{page_id}"
RESTORE_PAGE_ROUTE_TEMPLATE: Final = "/api/v1/sections/{section_id}/pages/{page_id}/restore"
LifecycleAction = Literal["delete", "restore"]
PAGE_ETAG_DOMAIN: Final = b"patchouli-lib/page-current-etag/v2\x00"
_LEGACY_PAGE_ETAG_DOMAIN: Final = b"patchouli-lib/page-current-etag/v1\x00"


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


class ArchiveUnsupportedRevisionFormatError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("The current Revision format is not supported by this route.")


class ArchiveOccurrenceUnchangedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("The Page already has the requested declared time.")


class ArchiveLifecycleUnchangedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("The Page already has the requested lifecycle state.")


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


def _validated_etag_identity(
    page_uid: bytes, revision_id: str, revision_number: int
) -> tuple[bytes, str]:
    """Validate the shared Page and Revision identity without changing v1 history."""

    validated_uid = validate_page_uid(page_uid)
    validated_revision = validate_revision_id(revision_id)
    if type(revision_number) is not int or not 1 <= revision_number <= MAX_REVISION_NUMBER:
        raise ValueError("Invalid current Revision number.")
    return validated_uid, validated_revision


def legacy_page_current_etag(page_uid: bytes, revision_id: str, revision_number: int) -> str:
    """Reconstruct immutable v1 response headers when validating historical backups."""

    validated_uid, validated_revision = _validated_etag_identity(
        page_uid, revision_id, revision_number
    )
    digest = hashlib.sha256(_LEGACY_PAGE_ETAG_DOMAIN)
    digest.update(validated_uid)
    digest.update(validated_revision.encode("ascii"))
    digest.update(revision_number.to_bytes(8, "big"))
    return f'"page-v1-{digest.hexdigest()}"'


def page_current_etag(
    page_uid: bytes,
    revision_id: str,
    revision_number: int,
    occurred_at: int,
    updated_at: int,
) -> str:
    """Validate current content plus metadata and the strictly advancing Page clock."""

    validated_uid, validated_revision = _validated_etag_identity(
        page_uid, revision_id, revision_number
    )
    if (
        type(occurred_at) is not int
        or not MIN_OCCURRENCE_MICROSECONDS <= occurred_at <= MAX_OCCURRENCE_MICROSECONDS
        or type(updated_at) is not int
        or not 0 <= updated_at <= (1 << 63) - 1
    ):
        raise ValueError("Invalid Page time for the current ETag.")
    digest = hashlib.sha256(PAGE_ETAG_DOMAIN)
    digest.update(validated_uid)
    digest.update(validated_revision.encode("ascii"))
    digest.update(revision_number.to_bytes(8, "big"))
    digest.update(occurred_at.to_bytes(8, "big", signed=True))
    digest.update(updated_at.to_bytes(8, "big"))
    return f'"page-v2-{digest.hexdigest()}"'


def _replay_timeline(connection: Connection, page: PageRecord) -> PageStateTimeline:
    # History imports the ETag function above. Load it only after service import
    # completes, and use the caller's existing transaction rather than a new read.
    from patchouli_lib.content.page_membership_history import (
        PageMembershipHistoryError,
        load_page_state_timeline,
    )

    raw = connection.connection.driver_connection
    if not isinstance(raw, sqlite3.Connection):
        raise ArchiveReplayCorruptError
    try:
        revision = raw.execute("SELECT version_num FROM alembic_version").fetchone()
        if revision is None:
            raise ArchiveReplayCorruptError
        return load_page_state_timeline(
            raw, schema_revision=revision[0], library_id=page.library_id, page_uid=page.page_uid
        )
    except (PageMembershipHistoryError, sqlite3.Error, ValueError):
        raise ArchiveReplayCorruptError from None


def _require_replay_state(
    connection: Connection,
    page: PageRecord,
    replay: ReplayResponse,
    *,
    section_id: str,
    revision_id: str,
    revision_number: int,
    updated_at: int | None = None,
    book_id: str | None = None,
    active: bool = True,
    allow_legacy_etag: bool = False,
) -> PageState:
    """Prove one complete historical state, not a path that merely once existed."""
    from patchouli_lib.content.page_membership_history import PageMembershipHistoryError

    timeline = _replay_timeline(connection, page)
    try:
        if updated_at is None:
            return timeline.match_active_etag(
                revision_id=revision_id,
                revision_number=revision_number,
                etag=replay.response_etag,
                section_id=section_id,
                book_id=book_id,
            )
        state = timeline.exact_state(updated_at)
    except PageMembershipHistoryError:
        raise ArchiveReplayCorruptError from None
    etags = {
        page_current_etag(
            page.page_uid,
            state.revision_id,
            state.revision_number,
            state.occurred_at,
            state.updated_at,
        )
    }
    if allow_legacy_etag:
        etags.add(legacy_page_current_etag(page.page_uid, state.revision_id, state.revision_number))
    if (
        state.section_id != section_id
        or (book_id is not None and state.book_id != book_id)
        or state.revision_id != revision_id
        or state.revision_number != revision_number
        or (state.deleted_at is None) != active
        or replay.response_etag not in etags
    ):
        raise ArchiveReplayCorruptError
    return state


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
        operation_at = self._operation_time()
        authentication = AuthenticationService(
            self._auth_repository,
            clock=lambda: operation_at,
        )
        authenticated = authentication.authenticate(token_value)
        caller = TransactionValidatedCaller(
            library_id=command.library_id,
            actor_home_library_id=authenticated.caller.library_id,
            caller_id=authenticated.caller.id,
        )
        request = IdempotencyRequest(
            method="POST",
            route_template=CREATE_ROUTE_TEMPLATE,
            key_digest=idempotency.key_digest,
            request_fingerprint=self._create_fingerprint(command),
        )
        stored = IdempotencyRepository(self._connection).get(caller, request)
        if stored is not None:
            # This body is internal lookup material only. The caller has not
            # yet been authorized to learn even whether its fingerprint matches.
            try:
                stored_body = ArchiveResponseBody.model_validate_json(stored.response_body)
            except ValueError:
                raise AuthorizationError from None
            page = self._content.get_page(command.library_id, stored_body.page.page_id)
            if page is None or page.page_type != "archive":
                raise AuthorizationError
            authentication.authorize_content(
                token_value,
                library_id=command.library_id,
                section_id=page.section_id,
                action=SectionAction.ARCHIVE_WRITE,
            )
            replay = self._idempotency.lookup(caller, request)
            if replay is None:
                raise ArchiveReplayCorruptError
            result = self._replay_result(replay)
            self._validate_archive_replay(page, result, command.section_id, revision_location=False)
            if result.body.page.book_id != command.book_id:
                raise ArchiveReplayCorruptError
            return result
        book = self._content.get_book(command.library_id, command.book_id)
        if book is None:
            raise ArchiveNotFoundError
        authenticated = authentication.authorize_content(
            token_value,
            library_id=command.library_id,
            section_id=book.section_id,
            action=SectionAction.ARCHIVE_WRITE,
        )
        self._require_route_section(book.section_id, command.section_id)
        if self._idempotency.lookup(caller, request) is not None:
            raise ArchiveReplayCorruptError

        occurred_at = operation_at if command.occurred_at is None else command.occurred_at
        try:
            generated = self._allocate_page_id(command.library_id, command.title, occurred_at)
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
                    id_timestamp_micros=(occurred_at // 1_000) * 1_000,
                    base_slug=generated.base_slug,
                    collision_ordinal=generated.collision_ordinal,
                    title=command.title,
                    page_type="archive",
                    occurred_at=occurred_at,
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
                occurrence_defaulted=command.occurred_at is None,
            )
            audit = self._auth_repository.add_audit_event(
                NewAuditEvent(
                    id=self._id_factory(),
                    library_id=command.library_id,
                    actor_home_library_id=authenticated.caller.library_id,
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
            actor_home_library_id=authenticated.caller.library_id,
            caller_id=authenticated.caller.id,
        )
        request = IdempotencyRequest(
            method="POST",
            route_template=REVISE_ROUTE_TEMPLATE,
            key_digest=idempotency.key_digest,
            request_fingerprint=self._revision_fingerprint(command),
        )
        replay = self._idempotency.lookup(caller, request)
        if replay is not None:
            result = self._replay_result(replay)
            self._validate_archive_replay(page, result, command.section_id, revision_location=True)
            return result
        self._require_route_section(page.section_id, command.section_id)
        if page.deleted_at is not None:
            raise ArchiveNotFoundError
        if command.expected_etag is None:
            raise ArchivePreconditionRequiredError
        current_etag = page_current_etag(
            page.page_uid,
            page.current_revision_id,
            page.current_revision_number,
            page.occurred_at,
            page.updated_at,
        )
        if not hmac.compare_digest(command.expected_etag, current_etag):
            raise ArchivePreconditionFailedError
        if self._content.get_current_revision_storage_format(page) != "legacy_markdown":
            raise ArchiveUnsupportedRevisionFormatError
        if page.current_revision_number >= MAX_REVISION_NUMBER:
            raise ArchivePersistenceError
        if page.updated_at >= (1 << 63) - 1:
            raise ArchivePersistenceError
        revision_at = max(operation_at, page.updated_at + 1)
        try:
            canonical_utc_wire(revision_at)
        except ValueError:
            raise ArchivePersistenceError from None

        try:
            revision_id = self._allocate_revision_id(command.library_id)
            markdown = MarkdownContent.from_bytes(command.content_md)
            revision = self._content.add_revision(
                NewRevision(
                    library_id=command.library_id,
                    revision_id=revision_id,
                    page_uid=page.page_uid,
                    revision_number=page.current_revision_number + 1,
                    created_at=revision_at,
                    **markdown.model_dump(),
                )
            )
            advanced = self._content.advance_current_revision(
                page,
                revision,
                updated_at=revision_at,
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
                    actor_home_library_id=authenticated.caller.library_id,
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

    def correct_occurrence(
        self,
        token_value: str,
        command: CorrectArchiveOccurrenceCommand,
        idempotency: ArchiveIdempotencyKey,
    ) -> OriginalResponse | ReplayResponse:
        """Correct one live Archive Page's declared time without a new Revision."""

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
            actor_home_library_id=authenticated.caller.library_id,
            caller_id=authenticated.caller.id,
        )
        request = IdempotencyRequest(
            method="PATCH",
            route_template=CORRECT_OCCURRENCE_ROUTE_TEMPLATE,
            key_digest=idempotency.key_digest,
            request_fingerprint=self._occurrence_fingerprint(command),
        )
        replay = self._idempotency.lookup(caller, request)
        if replay is not None:
            try:
                body = OccurrenceCorrectionResponseBody.model_validate_json(replay.response_body)
                corrected_at = parse_occurrence_time(
                    replay.original_request_timestamp
                ).utc_microseconds
            except ValueError:
                raise ArchiveReplayCorruptError from None
            state = _require_replay_state(
                self._connection,
                page,
                replay,
                section_id=command.section_id,
                revision_id=body.current_revision_id,
                revision_number=body.current_revision_number,
                updated_at=corrected_at,
            )
            correction_row = self._connection.exec_driver_sql(
                "SELECT old_occurred_at, new_occurred_at FROM page_occurrence_corrections "
                "WHERE library_id = ? AND page_uid = ? AND corrected_at = ?",
                (page.library_id, page.page_uid, corrected_at),
            ).one_or_none()
            if (
                correction_row is None
                or body.page_id != page.page_id
                or body.section_id != command.section_id
                or body.previous_occurred_at != canonical_utc_wire(correction_row[0])
                or body.occurred_at != canonical_utc_wire(state.occurred_at)
                or correction_row[1] != command.occurred_at
            ):
                raise ArchiveReplayCorruptError
            self._validate_metadata_replay(page, replay, body.citation, command.section_id)
            return replay
        self._require_route_section(page.section_id, command.section_id)
        if page.deleted_at is not None:
            raise ArchiveNotFoundError

        current_etag = page_current_etag(
            page.page_uid,
            page.current_revision_id,
            page.current_revision_number,
            page.occurred_at,
            page.updated_at,
        )
        if not hmac.compare_digest(command.expected_etag, current_etag):
            raise ArchivePreconditionFailedError
        if command.occurred_at == page.occurred_at:
            raise ArchiveOccurrenceUnchangedError
        if page.updated_at >= (1 << 63) - 1:
            raise ArchivePersistenceError

        try:
            updated, correction = self._content.correct_occurrence(
                page,
                PageOccurrenceCorrectionCommand(
                    library_id=page.library_id,
                    page_uid=page.page_uid,
                    old_occurred_at=page.occurred_at,
                    new_occurred_at=command.occurred_at,
                    actor_caller_id=authenticated.caller.id,
                    actor_home_library_id=authenticated.caller.library_id,
                    corrected_at=operation_at,
                ),
            )
            href = build_api_v1_path(
                "sections",
                page.section_id,
                "pages",
                page.page_id,
                "revisions",
                str(page.current_revision_number),
            )
            body = OccurrenceCorrectionResponseBody(
                section_id=page.section_id,
                page_id=page.page_id,
                previous_occurred_at=canonical_utc_wire(page.occurred_at),
                occurred_at=canonical_utc_wire(updated.occurred_at),
                current_revision_id=page.current_revision_id,
                current_revision_number=page.current_revision_number,
                citation=ArchiveCitation(
                    section_id=page.section_id,
                    page_id=page.page_id,
                    revision_id=page.current_revision_id,
                    revision_number=page.current_revision_number,
                    href=href,
                ),
            )
            response = OriginalResponse(
                response_status=200,
                response_body=body.model_dump_json().encode("utf-8"),
                response_location=build_api_v1_path(
                    "sections", page.section_id, "pages", page.page_id
                ),
                response_etag=page_current_etag(
                    updated.page_uid,
                    updated.current_revision_id,
                    updated.current_revision_number,
                    updated.occurred_at,
                    updated.updated_at,
                ),
                original_request_id=command.request_id,
                original_request_timestamp=canonical_utc_wire(correction.corrected_at),
            )
            self._auth_repository.add_audit_event(
                NewAuditEvent(
                    id=self._id_factory(),
                    library_id=page.library_id,
                    actor_home_library_id=authenticated.caller.library_id,
                    actor_caller_id=authenticated.caller.id,
                    actor_credential_id=authenticated.credential.id,
                    action="content.archive.correct_occurrence",
                    resource_type="page",
                    resource_id=page.page_id,
                    outcome=AuditOutcome.SUCCEEDED,
                    request_id=command.request_id,
                    occurred_at=correction.corrected_at,
                )
            )
            self._idempotency.record_success(caller, request, response)
        except SQLAlchemyError:
            raise ArchivePersistenceError from None
        return response

    def transition_page_lifecycle(
        self,
        token_value: str,
        command: PageLifecycleCommand,
        idempotency: ArchiveIdempotencyKey,
        *,
        action: LifecycleAction,
    ) -> OriginalResponse | ReplayResponse:
        """Soft-delete or restore one Archive Page without changing its Revisions."""

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
        return self._transition_page_lifecycle_authorized(
            page, authenticated, command, idempotency, action=action, operation_at=operation_at
        )

    def restore_page_as_operator(
        self,
        token_value: str,
        command: PageLifecycleCommand,
        idempotency: ArchiveIdempotencyKey,
    ) -> OriginalResponse | ReplayResponse:
        """Restore an Archive Page for the admin UI without broadening Agent API access."""

        self._require_transaction()
        operation_at = self._operation_time()
        authenticated = AuthenticationService(
            self._auth_repository,
            clock=lambda: operation_at,
        ).require_operator(token_value, library_id=command.library_id)
        page = self._content.get_page(command.library_id, command.page_id)
        if page is None or page.page_type != "archive":
            raise ArchiveNotFoundError
        return self._transition_page_lifecycle_authorized(
            page, authenticated, command, idempotency, action="restore", operation_at=operation_at
        )

    def _transition_page_lifecycle_authorized(
        self,
        page: PageRecord,
        authenticated: AuthenticatedCaller,
        command: PageLifecycleCommand,
        idempotency: ArchiveIdempotencyKey,
        *,
        action: LifecycleAction,
        operation_at: int,
    ) -> OriginalResponse | ReplayResponse:
        caller = TransactionValidatedCaller(
            library_id=command.library_id,
            actor_home_library_id=authenticated.caller.library_id,
            caller_id=authenticated.caller.id,
        )
        route = DELETE_PAGE_ROUTE_TEMPLATE if action == "delete" else RESTORE_PAGE_ROUTE_TEMPLATE
        request = IdempotencyRequest(
            method="DELETE" if action == "delete" else "POST",
            route_template=route,
            key_digest=idempotency.key_digest,
            request_fingerprint=self._lifecycle_fingerprint(command, action),
        )
        replay = self._idempotency.lookup(caller, request)
        if replay is not None:
            try:
                body = PageLifecycleResponseBody.model_validate_json(replay.response_body)
                changed_at = parse_occurrence_time(body.updated_at).utc_microseconds
            except ValueError:
                raise ArchiveReplayCorruptError from None
            state = _require_replay_state(
                self._connection,
                page,
                replay,
                section_id=command.section_id,
                revision_id=body.current_revision_id,
                revision_number=body.current_revision_number,
                updated_at=changed_at,
                active=action == "restore",
            )
            event_row = self._connection.exec_driver_sql(
                "SELECT action FROM page_lifecycle_events "
                "WHERE library_id = ? AND page_uid = ? AND changed_at = ?",
                (page.library_id, page.page_uid, changed_at),
            ).one_or_none()
            if (
                event_row is None
                or event_row[0] != action
                or body.page_id != page.page_id
                or body.section_id != command.section_id
                or body.state != ("trashed" if action == "delete" else "active")
                or body.deleted_at
                != (canonical_utc_wire(state.deleted_at) if state.deleted_at is not None else None)
                or replay.original_request_timestamp != body.updated_at
            ):
                raise ArchiveReplayCorruptError
            self._validate_metadata_replay(page, replay, body.citation, command.section_id)
            return replay
        self._require_route_section(page.section_id, command.section_id)

        current_etag = page_current_etag(
            page.page_uid,
            page.current_revision_id,
            page.current_revision_number,
            page.occurred_at,
            page.updated_at,
        )
        if not hmac.compare_digest(command.expected_etag, current_etag):
            raise ArchivePreconditionFailedError
        if (action == "delete") == (page.deleted_at is not None):
            raise ArchiveLifecycleUnchangedError
        if page.updated_at >= (1 << 63) - 1:
            raise ArchivePersistenceError

        try:
            updated, event = self._content.transition_page_lifecycle(
                page,
                action=action,
                actor_caller_id=authenticated.caller.id,
                actor_home_library_id=authenticated.caller.library_id,
                request_id=command.request_id,
                changed_at=operation_at,
            )
            href = build_api_v1_path(
                "sections",
                page.section_id,
                "pages",
                page.page_id,
                "revisions",
                str(page.current_revision_number),
            )
            body = PageLifecycleResponseBody(
                section_id=page.section_id,
                page_id=page.page_id,
                state="trashed" if action == "delete" else "active",
                deleted_at=(
                    canonical_utc_wire(updated.deleted_at)
                    if updated.deleted_at is not None
                    else None
                ),
                updated_at=canonical_utc_wire(updated.updated_at),
                current_revision_id=page.current_revision_id,
                current_revision_number=page.current_revision_number,
                citation=ArchiveCitation(
                    section_id=page.section_id,
                    page_id=page.page_id,
                    revision_id=page.current_revision_id,
                    revision_number=page.current_revision_number,
                    href=href,
                ),
            )
            response = OriginalResponse(
                response_status=200,
                response_body=body.model_dump_json().encode("utf-8"),
                response_location=build_api_v1_path(
                    "sections", page.section_id, "pages", page.page_id
                ),
                response_etag=page_current_etag(
                    updated.page_uid,
                    updated.current_revision_id,
                    updated.current_revision_number,
                    updated.occurred_at,
                    updated.updated_at,
                ),
                original_request_id=command.request_id,
                original_request_timestamp=canonical_utc_wire(event.changed_at),
            )
            self._auth_repository.add_audit_event(
                NewAuditEvent(
                    id=self._id_factory(),
                    library_id=page.library_id,
                    actor_home_library_id=authenticated.caller.library_id,
                    actor_caller_id=authenticated.caller.id,
                    actor_credential_id=authenticated.credential.id,
                    action=f"content.archive.{action}",
                    resource_type="page",
                    resource_id=page.page_id,
                    outcome=AuditOutcome.SUCCEEDED,
                    request_id=command.request_id,
                    occurred_at=event.changed_at,
                )
            )
            self._idempotency.record_success(caller, request, response)
        except SQLAlchemyError:
            raise ArchivePersistenceError from None
        return response

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

    def _allocate_page_id(self, library_id: str, title: str, occurred_at: int) -> GeneratedPageId:
        occurrence = OccurrenceTime(
            utc_microseconds=occurred_at,
            canonical_utc=canonical_utc_wire(occurred_at),
        )
        base = generate_page_id(occurrence, title)
        counter = self._content.get_collision_counter(
            library_id,
            PAGE_ID_SCHEME,
            (occurred_at // 1_000) * 1_000,
            base.base_slug,
        )
        if counter is None:
            ordinal = 1
            for _ in range(self._collision_attempts):
                if ordinal > MAX_COLLISION_ORDINAL:
                    break
                candidate = generate_page_id(
                    occurrence,
                    title,
                    collision_ordinal=ordinal,
                )
                if not self._content.identifier_exists(library_id, candidate.value):
                    self._content.add_collision_counter(
                        NewPageIdCollisionCounter(
                            library_id=library_id,
                            id_scheme=PAGE_ID_SCHEME,
                            id_timestamp_micros=(occurred_at // 1_000) * 1_000,
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
                title,
                collision_ordinal=ordinal,
            )
            if not self._content.identifier_exists(library_id, candidate.value):
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
        occurrence_defaulted: bool = False,
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
            occurrence_notice=ArchiveOccurrenceNotice() if occurrence_defaulted else None,
        )
        page_location = build_api_v1_path(
            "sections",
            page.section_id,
            "pages",
            page.page_id,
        )
        response = OriginalResponse(
            response_status=201,
            response_body=body.model_dump_json(
                exclude=None if occurrence_defaulted else {"occurrence_notice"}
            ).encode("utf-8"),
            response_location=citation.href if revision_location else page_location,
            response_etag=page_current_etag(
                page.page_uid,
                revision.revision_id,
                revision.revision_number,
                page.occurred_at,
                page.updated_at,
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

    def _validate_archive_replay(
        self,
        page: PageRecord,
        result: ArchiveMutationReplay,
        section_id: str,
        *,
        revision_location: bool,
    ) -> None:
        body, replay = result.body, result.response
        state = _require_replay_state(
            self._connection,
            page,
            replay,
            section_id=section_id,
            book_id=body.page.book_id,
            revision_id=body.revision.revision_id,
            revision_number=body.revision.revision_number,
            updated_at=parse_occurrence_time(body.revision.created_at).utc_microseconds,
            allow_legacy_etag=True,
        )
        revision = self._content.get_revision(
            page.library_id, page.page_uid, body.revision.revision_number
        )
        href = build_api_v1_path(
            "sections",
            section_id,
            "pages",
            page.page_id,
            "revisions",
            str(body.revision.revision_number),
        )
        if (
            body.page.page_id != page.page_id
            or body.page.section_id != section_id
            or body.page.title != state.title
            or body.page.occurred_at != canonical_utc_wire(state.occurred_at)
            or revision is None
            or body.revision.revision_id != revision.revision_id
            or body.revision.content.encode("utf-8") != revision.content_md
            or body.revision.content_sha256 != revision.content_sha256.hex()
            or replay.response_status != 201
            or replay.original_request_timestamp != body.revision.created_at
            or body.citation.href != href
            or replay.response_location
            != (
                href
                if revision_location
                else build_api_v1_path("sections", section_id, "pages", page.page_id)
            )
        ):
            raise ArchiveReplayCorruptError

    @staticmethod
    def _validate_metadata_replay(
        page: PageRecord, replay: ReplayResponse, citation: ArchiveCitation, section_id: str
    ) -> None:
        if (
            replay.response_status != 200
            or replay.response_location
            != build_api_v1_path("sections", section_id, "pages", page.page_id)
            or citation.href
            != build_api_v1_path(
                "sections",
                section_id,
                "pages",
                page.page_id,
                "revisions",
                str(citation.revision_number),
            )
        ):
            raise ArchiveReplayCorruptError

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

    @staticmethod
    def _occurrence_fingerprint(command: CorrectArchiveOccurrenceCommand) -> bytes:
        return digest_request_fingerprint(
            json.dumps(
                {
                    "expected_etag": command.expected_etag,
                    "library_id": command.library_id,
                    "operation": "archive-correct-occurrence-v1",
                    "page_id": command.page_id,
                    "section_id": command.section_id,
                    "occurred_at": command.occurred_at,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )

    @staticmethod
    def _lifecycle_fingerprint(command: PageLifecycleCommand, action: LifecycleAction) -> bytes:
        return digest_request_fingerprint(
            json.dumps(
                {
                    "expected_etag": command.expected_etag,
                    "library_id": command.library_id,
                    "operation": f"archive-{action}-v1",
                    "page_id": command.page_id,
                    "section_id": command.section_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )


__all__ = [
    "CORRECT_OCCURRENCE_ROUTE_TEMPLATE",
    "CREATE_ROUTE_TEMPLATE",
    "DELETE_PAGE_ROUTE_TEMPLATE",
    "PAGE_ETAG_DOMAIN",
    "REVISE_ROUTE_TEMPLATE",
    "RESTORE_PAGE_ROUTE_TEMPLATE",
    "ArchiveIdentifierExhaustedError",
    "ArchiveNotFoundError",
    "ArchiveOccurrenceUnchangedError",
    "ArchiveLifecycleUnchangedError",
    "ArchivePersistenceError",
    "ArchivePreconditionFailedError",
    "ArchivePreconditionRequiredError",
    "ArchiveReplayCorruptError",
    "ArchiveService",
    "ArchiveTransactionRequiredError",
    "legacy_page_current_etag",
    "page_current_etag",
]
