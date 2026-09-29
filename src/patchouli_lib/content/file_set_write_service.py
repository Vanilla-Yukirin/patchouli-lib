"""Internal authorized orchestration for a complete Page file-set append.

This is not an HTTP contract. The internal idempotency namespace is fixed;
the eventual adapter must hold a real SQLite ``BEGIN IMMEDIATE`` transaction.
Single Markdown and multiple files follow exactly the same path here.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final
from uuid import uuid4

from pydantic import Field, field_validator
from sqlalchemy import Connection

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    AuditEventRecord,
    AuditOutcome,
    AuthenticatedCaller,
    NewAuditEvent,
    SectionAction,
)
from patchouli_lib.auth.service import AuthenticationService, utc_microseconds
from patchouli_lib.content.file_manifest import FileManifest, build_file_manifest
from patchouli_lib.content.file_set_service import (
    FileSetAppendResult,
    FileSetPreconditionFailedError,
    FileSetRevisionService,
)
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    ArchiveIdempotencyKey,
    ArchiveSourceInput,
    ContentSchema,
    NewPageSource,
    OpaqueId,
    PageId,
    PageRecord,
    RequestId,
    RevisionId,
    StrongPageETag,
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
from patchouli_lib.identifiers import canonical_utc_wire, generate_revision_id, validate_page_id

Clock = Callable[[], int]
IdFactory = Callable[[], str]
RevisionIdFactory = Callable[[], str]
FILE_SET_APPEND_ROUTE_TEMPLATE: Final = (
    "/api/v1/sections/{section_id}/pages/{page_id}/file-revisions"
)


class FileSetWriteNotFoundError(RuntimeError):
    """Target Page or its requested Section is unavailable."""


class FileSetWritePreconditionRequiredError(RuntimeError):
    """An update has no strong current Page ETag."""


class FileSetWriteTransactionRequiredError(RuntimeError):
    """The caller has not started a concrete SQLite write transaction."""


class FileSetWritePersistenceError(RuntimeError):
    """An authorized file-set write could not be persisted safely."""


class FileSetWriteReplayCorruptError(RuntimeError):
    """A stored response does not match this internal file-set contract."""


class FileSetAppendCommand(ContentSchema):
    """Validated internal semantic request, excluding bearer and raw key."""

    library_id: OpaqueId
    section_id: OpaqueId
    page_id: PageId
    expected_etag: StrongPageETag | None = Field(default=None, repr=False)
    files: tuple[tuple[str, bytes], ...] = Field(repr=False)
    source: ArchiveSourceInput
    request_id: RequestId

    @field_validator("page_id")
    @classmethod
    def require_page_id(cls, value: str) -> str:
        return validate_page_id(value)

    @field_validator("files")
    @classmethod
    def require_complete_snapshot(
        cls, value: tuple[tuple[str, bytes], ...]
    ) -> tuple[tuple[str, bytes], ...]:
        manifest = build_file_manifest(value)
        return tuple((entry.name, entry.content) for entry in manifest.files)


class _FileSummary(ContentSchema):
    name: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _WriteBody(ContentSchema):
    changed: bool
    section_id: OpaqueId
    page_id: PageId
    revision_id: RevisionId
    revision_number: int = Field(ge=1)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[_FileSummary, ...]


@dataclass(frozen=True, slots=True)
class FileSetWriteSuccess:
    changed: bool
    page: PageRecord
    manifest: FileManifest = field(repr=False)
    response: OriginalResponse = field(repr=False)
    audit_event: AuditEventRecord | None = None


@dataclass(frozen=True, slots=True)
class FileSetWriteReplay:
    response: ReplayResponse = field(repr=False)


FileSetWriteResult = FileSetWriteSuccess | FileSetWriteReplay


class FileSetWriteService:
    """Authorize, replay, append, audit and record success in one outer transaction.

    This bounded first slice only accepts an existing Archive Page and its
    current Section grant. It does not broaden a legacy credential into the
    proposed cross-Library permission model. No route calls it yet.
    """

    def __init__(
        self,
        connection: Connection,
        *,
        clock: Clock = utc_microseconds,
        id_factory: IdFactory = lambda: uuid4().hex,
        revision_id_factory: RevisionIdFactory = generate_revision_id,
    ) -> None:
        self._connection = connection
        self._content = ContentRepository(connection)
        self._auth_repository = AuthRepository(connection)
        self._idempotency = IdempotencyService(IdempotencyRepository(connection))
        self._clock = clock
        self._id_factory = id_factory
        self._revision_id_factory = revision_id_factory

    def append_existing_page(
        self,
        token_value: str,
        command: FileSetAppendCommand,
        idempotency: ArchiveIdempotencyKey,
    ) -> FileSetWriteResult:
        """Append a changed full snapshot; exact identical content is a no-op.

        The savepoint covers even authentication's coalesced last-used update,
        Source, audit and replay state. On any exception it rolls back together.
        The caller still owns commit/rollback of its outer ``BEGIN IMMEDIATE``.
        """

        self._require_transaction()
        manifest = build_file_manifest(command.files)
        with self._connection.begin_nested():
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
            page = self._content.get_page(command.library_id, command.page_id)
            if page is None or page.page_type != "archive":
                raise FileSetWriteNotFoundError("Page is not available.")
            if not hmac.compare_digest(page.section_id, command.section_id):
                raise FileSetWriteNotFoundError("Page is not available.")
            caller = TransactionValidatedCaller(
                library_id=command.library_id,
                caller_id=authenticated.caller.id,
            )
            request = IdempotencyRequest(
                method="POST",
                route_template=FILE_SET_APPEND_ROUTE_TEMPLATE,
                key_digest=idempotency.key_digest,
                request_fingerprint=self._fingerprint(command, manifest),
            )
            replay = self._idempotency.lookup(caller, request)
            if replay is not None:
                try:
                    body = _WriteBody.model_validate_json(replay.response_body)
                except ValueError:
                    raise FileSetWriteReplayCorruptError(
                        "Stored file-set response is invalid."
                    ) from None
                if body.section_id != page.section_id or body.page_id != page.page_id:
                    raise FileSetWriteReplayCorruptError("Stored file-set response target changed.")
                return FileSetWriteReplay(replay)
            if page.deleted_at is not None:
                raise FileSetWriteNotFoundError("Page is not available.")
            if command.expected_etag is None:
                raise FileSetWritePreconditionRequiredError("A current strong ETag is required.")
            current_etag = page_current_etag(
                page.page_uid,
                page.current_revision_id,
                page.current_revision_number,
                page.occurred_at,
                page.updated_at,
            )
            if not hmac.compare_digest(command.expected_etag, current_etag):
                raise FileSetPreconditionFailedError("Page current ETag does not match.")
            current_manifest = self._content.get_current_file_manifest(page)
            if manifest == current_manifest:
                appended = FileSetAppendResult(False, page, current_etag, current_manifest)
            else:
                revision_id = self._revision_id_factory()
                revision_at = max(operation_at, page.updated_at + 1)
                try:
                    canonical_utc_wire(revision_at)
                    source = NewPageSource(
                        library_id=command.library_id,
                        source_id=self._id_factory(),
                        page_uid=page.page_uid,
                        revision_id=revision_id,
                        revision_number=page.current_revision_number + 1,
                        kind=command.source.kind,
                        locator=command.source.locator,
                        captured_at=command.source.captured_at,
                        created_at=operation_at,
                    )
                except ValueError as exc:
                    raise FileSetWritePersistenceError("Invalid revision metadata.") from exc
                appended = FileSetRevisionService(self._connection).append_existing_page(
                    library_id=command.library_id,
                    page_id=command.page_id,
                    expected_etag=command.expected_etag,
                    files=command.files,
                    revision_id=revision_id,
                    revision_at=revision_at,
                    source=source,
                )
            audit = self._audit_changed(command, authenticated, appended, operation_at)
            response = self._response(command, appended, operation_at)
            self._idempotency.record_success(caller, request, response)
            return FileSetWriteSuccess(
                changed=appended.changed,
                page=appended.page,
                manifest=appended.manifest,
                response=response,
                audit_event=audit,
            )

    def _audit_changed(
        self,
        command: FileSetAppendCommand,
        authenticated: AuthenticatedCaller,
        appended: FileSetAppendResult,
        operation_at: int,
    ) -> AuditEventRecord | None:
        if not appended.changed:
            return None
        return self._auth_repository.add_audit_event(
            NewAuditEvent(
                id=self._id_factory(),
                library_id=command.library_id,
                actor_caller_id=authenticated.caller.id,
                actor_credential_id=authenticated.credential.id,
                action="content.page.file_set.revise",
                resource_type="revision",
                resource_id=appended.page.current_revision_id,
                outcome=AuditOutcome.SUCCEEDED,
                request_id=command.request_id,
                occurred_at=operation_at,
            )
        )

    @staticmethod
    def _response(
        command: FileSetAppendCommand,
        appended: FileSetAppendResult,
        operation_at: int,
    ) -> OriginalResponse:
        body = _WriteBody(
            changed=appended.changed,
            section_id=appended.page.section_id,
            page_id=appended.page.page_id,
            revision_id=appended.page.current_revision_id,
            revision_number=appended.page.current_revision_number,
            snapshot_sha256=appended.manifest.snapshot_sha256.hex(),
            files=tuple(
                _FileSummary(
                    name=entry.name,
                    size_bytes=entry.content_size_bytes,
                    sha256=entry.content_sha256.hex(),
                )
                for entry in appended.manifest.files
            ),
        )
        return OriginalResponse(
            response_status=200,
            response_body=body.model_dump_json().encode("utf-8"),
            response_etag=appended.etag,
            original_request_id=command.request_id,
            original_request_timestamp=canonical_utc_wire(operation_at),
        )

    @staticmethod
    def _fingerprint(command: FileSetAppendCommand, manifest: FileManifest) -> bytes:
        metadata = {
            "operation": "file-set-append-v1",
            "library_id": command.library_id,
            "section_id": command.section_id,
            "page_id": command.page_id,
            "expected_etag": command.expected_etag,
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

    def _operation_time(self) -> int:
        value = self._clock()
        if type(value) is not int or value < 0:
            raise FileSetWritePersistenceError("Invalid operation time.")
        try:
            canonical_utc_wire(value)
        except ValueError as exc:
            raise FileSetWritePersistenceError("Invalid operation time.") from exc
        return value

    def _require_transaction(self) -> None:
        if not self._connection.in_transaction():
            raise FileSetWriteTransactionRequiredError(
                "A caller-owned SQLite BEGIN IMMEDIATE transaction is required."
            )
        raw = self._connection.connection.driver_connection
        if not isinstance(raw, sqlite3.Connection) or not raw.in_transaction:
            raise FileSetWriteTransactionRequiredError(
                "A caller-owned SQLite BEGIN IMMEDIATE transaction is required."
            )


__all__ = [
    "FILE_SET_APPEND_ROUTE_TEMPLATE",
    "FileSetAppendCommand",
    "FileSetWriteNotFoundError",
    "FileSetWritePersistenceError",
    "FileSetWritePreconditionRequiredError",
    "FileSetWriteReplay",
    "FileSetWriteReplayCorruptError",
    "FileSetWriteResult",
    "FileSetWriteService",
    "FileSetWriteSuccess",
    "FileSetWriteTransactionRequiredError",
]
