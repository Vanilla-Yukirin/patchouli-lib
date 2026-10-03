"""Internal, authorized creation of an Archive Page with an initial file set.

This is not a public route or wire contract. The caller owns a real SQLite
transaction, preferably ``BEGIN IMMEDIATE``; the service obtains a writer
reservation before sensitive reads. The caller must commit the Page, complete
first Revision, Source, audit event and idempotency response together. One
Markdown file follows the same path as any other complete flat file set.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, Literal
from uuid import uuid4

from pydantic import Field, field_validator
from sqlalchemy import Connection

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import AuditEventRecord, AuditOutcome, NewAuditEvent, SectionAction
from patchouli_lib.auth.service import AuthenticationService, AuthorizationError, utc_microseconds
from patchouli_lib.content.file_manifest import FileManifest, build_file_manifest
from patchouli_lib.content.file_set_create_core import (
    FileSetCreateIdentifierExhaustedError,
    FileSetCreateTransactionRequiredError,
    FileSetPageCreateCore,
)
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    ArchiveIdempotencyKey,
    ArchiveSourceInput,
    ContentSchema,
    OccurrenceMicros,
    OpaqueId,
    PageId,
    PageRecord,
    RequestId,
    RevisionId,
)
from patchouli_lib.content.service import (
    ArchiveReplayCorruptError,
    _require_replay_state,
    page_current_etag,
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
    canonical_utc_wire,
    generate_page_uid,
    generate_revision_id,
    parse_occurrence_time,
)

Clock = Callable[[], int]
IdFactory = Callable[[], str]
PageUidFactory = Callable[[], bytes]
RevisionIdFactory = Callable[[], str]
# Fixed internal idempotency namespace, exported for exact backup verification.
# It does not register or promise a public URL.
FILE_SET_CREATE_ROUTE_TEMPLATE: Final = (
    "/api/v1/sections/{section_id}/books/{book_id}/file-set-pages"
)


class FileSetCreateNotFoundError(RuntimeError):
    """The authorized Section does not contain the requested Book."""


class FileSetCreateReplayCorruptError(RuntimeError):
    """Stored replay data does not match the requested resource."""


class FileSetCreateCommand(ContentSchema):
    """Validated internal semantic request, excluding bearer and raw key."""

    library_id: OpaqueId
    section_id: OpaqueId
    book_id: OpaqueId
    title: str = Field(min_length=1)
    occurred_at: OccurrenceMicros | None = None
    files: tuple[tuple[str, bytes], ...] = Field(repr=False)
    source: ArchiveSourceInput
    request_id: RequestId

    @field_validator("title")
    @classmethod
    def require_safe_title(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Page title must not contain NUL characters.")
        value.encode("utf-8", errors="strict")
        return value

    @field_validator("files")
    @classmethod
    def require_complete_snapshot(
        cls, value: tuple[tuple[str, bytes], ...]
    ) -> tuple[tuple[str, bytes], ...]:
        manifest = build_file_manifest(value)
        return tuple((entry.name, entry.content) for entry in manifest.files)


class _FileSummary(ContentSchema):
    filename: str
    size_bytes: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _CreateBody(ContentSchema):
    section_id: OpaqueId
    book_id: OpaqueId
    page_id: PageId
    revision_id: RevisionId
    revision_number: Literal[1]
    occurred_at: str
    occurrence_defaulted: bool
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[_FileSummary, ...]


@dataclass(frozen=True, slots=True)
class FileSetCreateSuccess:
    page: PageRecord
    manifest: FileManifest = field(repr=False)
    response: OriginalResponse = field(repr=False)
    audit_event: AuditEventRecord


@dataclass(frozen=True, slots=True)
class FileSetCreateReplay:
    response: ReplayResponse = field(repr=False)


FileSetCreateResult = FileSetCreateSuccess | FileSetCreateReplay


class FileSetCreateService:
    """Create the Page graph and durable replay as one savepoint in an outer write."""

    def __init__(
        self,
        connection: Connection,
        *,
        clock: Clock = utc_microseconds,
        id_factory: IdFactory = lambda: uuid4().hex,
        page_uid_factory: PageUidFactory = generate_page_uid,
        revision_id_factory: RevisionIdFactory = generate_revision_id,
        collision_attempts: int = DEFAULT_COLLISION_ATTEMPTS,
    ) -> None:
        self._connection = connection
        self._content = ContentRepository(connection)
        self._auth_repository = AuthRepository(connection)
        self._idempotency = IdempotencyService(IdempotencyRepository(connection))
        self._clock = clock
        self._id_factory = id_factory
        self._create_core = FileSetPageCreateCore(
            connection,
            id_factory=id_factory,
            page_uid_factory=page_uid_factory,
            revision_id_factory=revision_id_factory,
            collision_attempts=collision_attempts,
        )

    def create_page(
        self,
        token_value: str,
        command: FileSetCreateCommand,
        idempotency: ArchiveIdempotencyKey,
    ) -> FileSetCreateResult:
        """Create directly from a complete snapshot, or replay an exact request.

        Authorization precedes Book lookup so an ungranted caller cannot use
        this operation as a Book-existence probe. The caller must commit the
        outer transaction before any result becomes durable.
        """

        self._create_core.require_transaction()
        manifest = build_file_manifest(command.files)
        with self._connection.begin_nested():
            # Obtain SQLite's writer reservation before reading auth, Book or
            # replay state. This preserves serialization even if a future
            # caller mistakenly starts a deferred transaction. The UPDATE
            # matches no row and fires no application mutation trigger.
            self._connection.exec_driver_sql("UPDATE libraries SET id = id WHERE 0")
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
                route_template=FILE_SET_CREATE_ROUTE_TEMPLATE,
                key_digest=idempotency.key_digest,
                request_fingerprint=self._fingerprint(command, manifest),
            )
            stored = IdempotencyRepository(self._connection).get(caller, request)
            if stored is not None:
                try:
                    body = _CreateBody.model_validate_json(stored.response_body)
                except ValueError:
                    raise AuthorizationError from None
                page = self._content.get_page(command.library_id, body.page_id)
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
                    raise FileSetCreateReplayCorruptError(
                        "Stored file-set response is unavailable."
                    )
                try:
                    state = _require_replay_state(
                        self._connection,
                        page,
                        replay,
                        section_id=command.section_id,
                        book_id=command.book_id,
                        revision_id=body.revision_id,
                        revision_number=1,
                        updated_at=parse_occurrence_time(
                            replay.original_request_timestamp
                        ).utc_microseconds,
                    )
                except (ArchiveReplayCorruptError, ValueError):
                    raise FileSetCreateReplayCorruptError(
                        "Stored file-set history is invalid."
                    ) from None
                if (
                    replay.response_status != 201
                    or replay.response_location is not None
                    or body.section_id != command.section_id
                    or body.book_id != command.book_id
                    or body.occurred_at != canonical_utc_wire(state.occurred_at)
                    or body.occurrence_defaulted != (command.occurred_at is None)
                    or state.title != command.title
                    or body.snapshot_sha256 != manifest.snapshot_sha256.hex()
                    or [(f.filename, f.size_bytes, f.content_sha256) for f in body.files]
                    != [
                        (f.name, f.content_size_bytes, f.content_sha256.hex())
                        for f in manifest.files
                    ]
                ):
                    raise FileSetCreateReplayCorruptError(
                        "Stored file-set response target changed."
                    )
                return FileSetCreateReplay(replay)

            authenticated = authentication.authorize_content(
                token_value,
                library_id=command.library_id,
                section_id=command.section_id,
                action=SectionAction.ARCHIVE_WRITE,
            )
            book = self._content.get_book(command.library_id, command.book_id)
            if book is None or not hmac.compare_digest(book.section_id, command.section_id):
                raise FileSetCreateNotFoundError("Book is not available.")
            if self._idempotency.lookup(caller, request) is not None:
                raise FileSetCreateReplayCorruptError("Stored file-set response is unexpected.")

            occurred_at = operation_at if command.occurred_at is None else command.occurred_at
            page = self._create_core.create_page(
                book=book,
                title=command.title,
                occurred_at=occurred_at,
                operation_at=operation_at,
                manifest=manifest,
                source=command.source,
            )
            audit = self._auth_repository.add_audit_event(
                NewAuditEvent(
                    id=self._id_factory(),
                    library_id=page.library_id,
                    actor_home_library_id=authenticated.caller.library_id,
                    actor_caller_id=authenticated.caller.id,
                    actor_credential_id=authenticated.credential.id,
                    action="content.page.file_set.create",
                    resource_type="page",
                    resource_id=page.page_id,
                    outcome=AuditOutcome.SUCCEEDED,
                    request_id=command.request_id,
                    occurred_at=operation_at,
                )
            )
            response = self._response(page, manifest, command, operation_at)
            self._idempotency.record_success(caller, request, response)
            return FileSetCreateSuccess(page, manifest, response, audit)

    @staticmethod
    def _fingerprint(command: FileSetCreateCommand, manifest: FileManifest) -> bytes:
        metadata = {
            "operation": "file-set-create-v1",
            "library_id": command.library_id,
            "section_id": command.section_id,
            "book_id": command.book_id,
            "title": command.title,
            "occurred_at": command.occurred_at,
            "source": {
                "kind": command.source.kind,
                "locator": command.source.locator,
                "captured_at": command.source.captured_at,
            },
        }
        parts = [
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            ),
            len(manifest.files).to_bytes(8, "big"),
        ]
        for entry in manifest.files:
            parts.extend((entry.name.encode("utf-8"), entry.content))
        return digest_request_fingerprint(*parts)

    @staticmethod
    def _response(
        page: PageRecord,
        manifest: FileManifest,
        command: FileSetCreateCommand,
        operation_at: int,
    ) -> OriginalResponse:
        body = _CreateBody(
            section_id=page.section_id,
            book_id=page.book_id,
            page_id=page.page_id,
            revision_id=page.current_revision_id,
            revision_number=1,
            occurred_at=canonical_utc_wire(page.occurred_at),
            occurrence_defaulted=command.occurred_at is None,
            snapshot_sha256=manifest.snapshot_sha256.hex(),
            files=tuple(
                _FileSummary(
                    filename=entry.name,
                    size_bytes=entry.content_size_bytes,
                    content_sha256=entry.content_sha256.hex(),
                )
                for entry in manifest.files
            ),
        )
        return OriginalResponse(
            response_status=201,
            response_body=body.model_dump_json().encode("utf-8"),
            response_etag=page_current_etag(
                page.page_uid,
                page.current_revision_id,
                1,
                page.occurred_at,
                page.updated_at,
            ),
            original_request_id=command.request_id,
            original_request_timestamp=canonical_utc_wire(operation_at),
        )

    def _operation_time(self) -> int:
        value = self._clock()
        if type(value) is not int or value < 0:
            raise ValueError("Invalid operation time.")
        canonical_utc_wire(value)
        return value


__all__ = [
    "FILE_SET_CREATE_ROUTE_TEMPLATE",
    "FileSetCreateCommand",
    "FileSetCreateIdentifierExhaustedError",
    "FileSetCreateNotFoundError",
    "FileSetCreateReplay",
    "FileSetCreateReplayCorruptError",
    "FileSetCreateResult",
    "FileSetCreateService",
    "FileSetCreateSuccess",
    "FileSetCreateTransactionRequiredError",
]
