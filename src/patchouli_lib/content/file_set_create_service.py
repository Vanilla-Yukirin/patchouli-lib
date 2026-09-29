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
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, Literal
from uuid import uuid4

from pydantic import Field, field_validator
from sqlalchemy import Connection, insert

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import AuditEventRecord, AuditOutcome, NewAuditEvent, SectionAction
from patchouli_lib.auth.service import AuthenticationService, utc_microseconds
from patchouli_lib.content.file_manifest import FileManifest, build_file_manifest
from patchouli_lib.content.models import Revision, RevisionFile, RevisionFileSeal, RevisionFileSet
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    ArchiveIdempotencyKey,
    ArchiveSourceInput,
    ContentSchema,
    NewPage,
    NewPageIdCollisionCounter,
    NewPageIdentifier,
    NewPageSource,
    OccurrenceMicros,
    OpaqueId,
    PageId,
    PageRecord,
    RequestId,
    RevisionId,
)
from patchouli_lib.content.service import page_current_etag
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
    PAGE_ID_SCHEME,
    GeneratedPageId,
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
# Fixed internal idempotency namespace, exported for exact backup verification.
# It does not register or promise a public URL.
FILE_SET_CREATE_ROUTE_TEMPLATE: Final = (
    "/api/v1/sections/{section_id}/books/{book_id}/file-set-pages"
)


class FileSetCreateNotFoundError(RuntimeError):
    """The authorized Section does not contain the requested Book."""


class FileSetCreateTransactionRequiredError(RuntimeError):
    """No caller-owned concrete SQLite transaction is active."""


class FileSetCreateReplayCorruptError(RuntimeError):
    """Stored replay data does not match the requested resource."""


class FileSetCreateIdentifierExhaustedError(RuntimeError):
    """A collision-free Page or Revision identity could not be allocated."""


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

        self._require_transaction()
        manifest = build_file_manifest(command.files)
        with self._connection.begin_nested():
            # Obtain SQLite's writer reservation before reading auth, Book or
            # replay state. This preserves serialization even if a future
            # caller mistakenly starts a deferred transaction. The UPDATE
            # matches no row and fires no application mutation trigger.
            self._connection.exec_driver_sql("UPDATE libraries SET id = id WHERE 0")
            operation_at = self._operation_time()
            authenticated = AuthenticationService(
                self._auth_repository,
                clock=lambda: operation_at,
            ).authorize_content(
                token_value,
                library_id=command.library_id,
                section_id=command.section_id,
                action=SectionAction.ARCHIVE_WRITE,
            )
            book = self._content.get_book(command.library_id, command.book_id)
            if book is None or not hmac.compare_digest(book.section_id, command.section_id):
                raise FileSetCreateNotFoundError("Book is not available.")
            caller = TransactionValidatedCaller(
                library_id=command.library_id,
                caller_id=authenticated.caller.id,
            )
            request = IdempotencyRequest(
                method="POST",
                route_template=FILE_SET_CREATE_ROUTE_TEMPLATE,
                key_digest=idempotency.key_digest,
                request_fingerprint=self._fingerprint(command, manifest),
            )
            replay = self._idempotency.lookup(caller, request)
            if replay is not None:
                try:
                    body = _CreateBody.model_validate_json(replay.response_body)
                except ValueError:
                    raise FileSetCreateReplayCorruptError(
                        "Stored file-set response is invalid."
                    ) from None
                if (
                    replay.response_status != 201
                    or replay.response_location is not None
                    or body.section_id != book.section_id
                    or body.book_id != book.id
                ):
                    raise FileSetCreateReplayCorruptError(
                        "Stored file-set response target changed."
                    )
                return FileSetCreateReplay(replay)

            occurred_at = operation_at if command.occurred_at is None else command.occurred_at
            generated = self._allocate_page_id(command.library_id, command.title, occurred_at)
            page_uid = self._allocate_page_uid(command.library_id)
            revision_id = self._allocate_revision_id(command.library_id)
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
            self._insert_initial_revision(page, manifest)
            self._content.add_identifier(
                NewPageIdentifier(
                    library_id=page.library_id,
                    identifier_digest=page_id_registry_digest(page.page_id),
                    identifier_text=page.page_id,
                    id_scheme=PAGE_ID_SCHEME,
                    identifier_kind="canonical",
                    page_uid=page.page_uid,
                    created_at=operation_at,
                )
            )
            self._content.add_source(
                NewPageSource(
                    library_id=page.library_id,
                    source_id=self._id_factory(),
                    page_uid=page.page_uid,
                    revision_id=revision_id,
                    revision_number=1,
                    kind=command.source.kind,
                    locator=command.source.locator,
                    captured_at=command.source.captured_at,
                    created_at=operation_at,
                )
            )
            audit = self._auth_repository.add_audit_event(
                NewAuditEvent(
                    id=self._id_factory(),
                    library_id=page.library_id,
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
            if self._content.get_current_file_manifest(page) != manifest:
                raise RuntimeError("Persisted initial file snapshot differs from the request.")
            response = self._response(page, manifest, command, operation_at)
            self._idempotency.record_success(caller, request, response)
            return FileSetCreateSuccess(page, manifest, response, audit)

    def _insert_initial_revision(self, page: PageRecord, manifest: FileManifest) -> None:
        """Insert Revision 1 without ever constructing a legacy Markdown mirror."""

        key = {
            "library_id": page.library_id,
            "page_uid": page.page_uid,
            "revision_id": page.current_revision_id,
            "revision_number": 1,
        }
        self._connection.execute(
            insert(Revision),
            key
            | {
                "content_md": None,
                "content_size_bytes": None,
                "content_sha256": None,
                "created_at": page.created_at,
            },
        )
        self._connection.execute(
            insert(RevisionFileSet),
            key
            | {
                "storage_format": "file_set_v1",
                "file_count": len(manifest.files),
                "total_size_bytes": manifest.total_size_bytes,
                "snapshot_sha256": manifest.snapshot_sha256,
            },
        )
        self._connection.execute(
            insert(RevisionFile),
            [
                key
                | {
                    "filename": entry.name,
                    "content_bytes": entry.content,
                    "size_bytes": entry.content_size_bytes,
                    "content_sha256": entry.content_sha256,
                }
                for entry in manifest.files
            ],
        )
        self._connection.execute(insert(RevisionFileSeal), key)

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

    def _allocate_page_id(self, library_id: str, title: str, occurred_at: int) -> GeneratedPageId:
        occurrence = OccurrenceTime(
            utc_microseconds=occurred_at,
            canonical_utc=canonical_utc_wire(occurred_at),
        )
        base = generate_page_id(occurrence, title)
        counter = self._content.get_collision_counter(
            library_id, PAGE_ID_SCHEME, (occurred_at // 1_000) * 1_000, base.base_slug
        )
        if counter is None:
            for ordinal in range(1, self._collision_attempts + 1):
                if ordinal > MAX_COLLISION_ORDINAL:
                    break
                candidate = generate_page_id(occurrence, title, collision_ordinal=ordinal)
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
            raise FileSetCreateIdentifierExhaustedError("Page identifier allocation failed.")

        for _ in range(self._collision_attempts):
            if counter.next_ordinal > MAX_COLLISION_ORDINAL:
                break
            ordinal = counter.next_ordinal
            advanced = self._content.advance_collision_counter(counter, next_ordinal=ordinal + 1)
            if advanced is None:
                raise FileSetCreateIdentifierExhaustedError("Page identifier allocation failed.")
            counter = advanced
            candidate = generate_page_id(occurrence, title, collision_ordinal=ordinal)
            if not self._content.identifier_exists(library_id, candidate.value):
                return candidate
        raise FileSetCreateIdentifierExhaustedError("Page identifier allocation failed.")

    def _allocate_page_uid(self, library_id: str) -> bytes:
        for _ in range(self._collision_attempts):
            try:
                candidate = validate_page_uid(self._page_uid_factory())
            except (TypeError, ValueError):
                continue
            if not self._content.page_uid_exists(library_id, candidate):
                return candidate
        raise FileSetCreateIdentifierExhaustedError("Page identity allocation failed.")

    def _allocate_revision_id(self, library_id: str) -> str:
        for _ in range(self._collision_attempts):
            try:
                candidate = validate_revision_id(self._revision_id_factory())
            except (TypeError, ValueError):
                continue
            if not self._content.revision_id_exists(library_id, candidate):
                return candidate
        raise FileSetCreateIdentifierExhaustedError("Revision identity allocation failed.")

    def _operation_time(self) -> int:
        value = self._clock()
        if type(value) is not int or value < 0:
            raise ValueError("Invalid operation time.")
        canonical_utc_wire(value)
        return value

    def _require_transaction(self) -> None:
        if not self._connection.in_transaction():
            raise FileSetCreateTransactionRequiredError(
                "A caller-owned concrete SQLite transaction is required."
            )
        raw = self._connection.connection.driver_connection
        if not isinstance(raw, sqlite3.Connection) or not raw.in_transaction:
            raise FileSetCreateTransactionRequiredError(
                "A caller-owned concrete SQLite transaction is required."
            )


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
