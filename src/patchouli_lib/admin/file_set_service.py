"""Master-session file-set writes and frozen success replay in one transaction."""

from __future__ import annotations

import hmac
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from time import time
from typing import Literal
from uuid import uuid4

from sqlalchemy import Engine, select

from patchouli_lib.admin.file_set_receipt_validation import validate_master_file_set_receipt
from patchouli_lib.admin.file_set_receipts import (
    MasterFileSetReceipt,
    MasterFileSetReceiptRepository,
)
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.auth.service import AuthenticationError, utc_microseconds
from patchouli_lib.content.file_manifest import FileManifest, build_file_manifest
from patchouli_lib.content.file_set_create_core import FileSetPageCreateCore
from patchouli_lib.content.file_set_create_service import FileSetCreateCommand
from patchouli_lib.content.file_set_service import FileSetRevisionService
from patchouli_lib.content.file_set_write_service import (
    FileSetAppendCommand,
    FileSetWritePreconditionRequiredError,
)
from patchouli_lib.content.models import PageSource
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveIdempotencyKey, NewPageSource, PageRecord
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import digest_request_fingerprint
from patchouli_lib.identifiers import canonical_utc_wire, generate_revision_id
from patchouli_lib.library.repository import LibraryRepository


class MasterFileSetConflictError(RuntimeError):
    """The same operation key was used for a different semantic request."""


class MasterFileSetNotFoundError(RuntimeError):
    """The target Book or active Page is absent from the requested path."""


@dataclass(frozen=True, slots=True)
class MasterFileSetResult:
    receipt: MasterFileSetReceipt
    replayed: bool
    manifest: FileManifest = field(repr=False)


class MasterFileSetService:
    """Own commit; never return success before content, audit and receipt persist."""

    def __init__(self, engine: Engine, *, clock: Callable[[], int] = utc_microseconds) -> None:
        self._engine = engine
        self._clock = clock

    def create_page(
        self,
        command: FileSetCreateCommand,
        idempotency: ArchiveIdempotencyKey,
        *,
        master_session: MasterAdminSession,
    ) -> MasterFileSetResult:
        return self._write(command, command.book_id, idempotency, master_session, "create")

    def revise_page(
        self,
        command: FileSetAppendCommand,
        book_id: str,
        idempotency: ArchiveIdempotencyKey,
        *,
        master_session: MasterAdminSession,
    ) -> MasterFileSetResult:
        return self._write(command, book_id, idempotency, master_session, "revise")

    def _write(
        self,
        command: FileSetCreateCommand | FileSetAppendCommand,
        book_id: str,
        idempotency: ArchiveIdempotencyKey,
        session: MasterAdminSession,
        operation: Literal["create", "revise"],
    ) -> MasterFileSetResult:
        manifest = build_file_manifest(command.files)
        fingerprint = self._fingerprint(command, book_id, manifest, operation)
        with immediate_transaction(self._engine) as connection:
            # Recheck after acquiring the write lock, before any receipt lookup.
            if (
                type(session) is not MasterAdminSession
                or session.expires_at <= int(time())
                or not MasterTokenRepository(connection).is_session_generation_current(
                    session.identity_id, session.session_generation
                )
            ):
                raise AuthenticationError
            receipts = MasterFileSetReceiptRepository(connection)
            previous = receipts.find(session.identity_id, operation, idempotency.key_digest)
            raw = connection.connection.driver_connection
            if not isinstance(raw, sqlite3.Connection):
                raise RuntimeError("Master file-set writes require SQLite.")
            if previous is not None:
                if not hmac.compare_digest(previous.request_fingerprint, fingerprint):
                    raise MasterFileSetConflictError("Operation key conflicts with the request.")
                result = MasterFileSetResult(
                    previous, True, validate_master_file_set_receipt(raw, previous)
                )
            else:
                now = self._clock()
                canonical_utc_wire(now)
                if type(now) is not int or now < 0:
                    raise ValueError("Invalid file-set operation time.")
                book = LibraryRepository(connection).get_book(
                    command.library_id, command.section_id, book_id
                )
                if book is None:
                    raise MasterFileSetNotFoundError("Book is not available.")
                content = ContentRepository(connection)
                changed = True
                if isinstance(command, FileSetCreateCommand):
                    page = FileSetPageCreateCore(connection).create_page(
                        book=book,
                        title=command.title,
                        occurred_at=now if command.occurred_at is None else command.occurred_at,
                        operation_at=now,
                        manifest=manifest,
                        source=command.source,
                    )
                else:
                    target = content.get_page(command.library_id, command.page_id)
                    if (
                        target is None
                        or target.deleted_at is not None
                        or target.section_id != command.section_id
                        or target.book_id != book_id
                    ):
                        raise MasterFileSetNotFoundError("Page is not available.")
                    page = target
                    if command.expected_etag is None:
                        raise FileSetWritePreconditionRequiredError(
                            "A current strong ETag is required."
                        )
                    revision_id = generate_revision_id()
                    appended = FileSetRevisionService(connection).append_existing_page(
                        library_id=page.library_id,
                        page_id=page.page_id,
                        expected_etag=command.expected_etag,
                        files=command.files,
                        revision_id=revision_id,
                        revision_at=max(now, page.updated_at + 1),
                        source=NewPageSource(
                            library_id=page.library_id,
                            source_id=uuid4().hex,
                            page_uid=page.page_uid,
                            revision_id=revision_id,
                            revision_number=page.current_revision_number + 1,
                            kind=command.source.kind,
                            locator=command.source.locator,
                            captured_at=command.source.captured_at,
                            created_at=now,
                        ),
                    )
                    page, manifest, changed = appended.page, appended.manifest, appended.changed
                source_id = None
                audit_id = None
                if changed:
                    # Both shared content cores already created Source. Associate,
                    # do not duplicate it or silently choose one ambiguous row.
                    sources = connection.scalars(
                        select(PageSource.source_id).where(
                            PageSource.library_id == page.library_id,
                            PageSource.page_uid == page.page_uid,
                            PageSource.revision_id == page.current_revision_id,
                            PageSource.revision_number == page.current_revision_number,
                            PageSource.created_at == now,
                        )
                    ).all()
                    if len(sources) != 1:
                        raise RuntimeError("Master file-set Source association is ambiguous.")
                    source_id = sources[0]
                    audit_id = uuid4().hex
                    MasterAuditRepository(connection).add_success(
                        identity_id=session.identity_id,
                        session_generation=session.session_generation,
                        session_fingerprint=session.audit_fingerprint(),
                        action=f"content.page.file_set.{operation}",
                        target_type="page",
                        target_id=f"{page.library_id}:{page.page_uid.hex()}",
                        occurred_at=now,
                        event_id=audit_id,
                    )
                receipt = self._receipt(
                    page,
                    session.identity_id,
                    operation,
                    idempotency.key_digest,
                    fingerprint,
                    manifest,
                    changed,
                    now,
                    source_id,
                    audit_id,
                )
                receipts.add(receipt)
                result = MasterFileSetResult(
                    receipt, False, validate_master_file_set_receipt(raw, receipt)
                )
        return result

    @staticmethod
    def _receipt(
        page: PageRecord,
        identity_id: str,
        operation: Literal["create", "revise"],
        key_digest: bytes,
        fingerprint: bytes,
        manifest: FileManifest,
        changed: bool,
        operation_at: int,
        source_id: str | None,
        audit_id: str | None,
    ) -> MasterFileSetReceipt:
        return MasterFileSetReceipt(
            identity_id=identity_id,
            operation=operation,
            key_digest=key_digest,
            request_fingerprint=fingerprint,
            library_id=page.library_id,
            section_id=page.section_id,
            book_id=page.book_id,
            page_id=page.page_id,
            page_uid=page.page_uid,
            revision_id=page.current_revision_id,
            revision_number=page.current_revision_number,
            snapshot_sha256=manifest.snapshot_sha256,
            changed=1 if changed else 0,
            original_occurred_at=page.occurred_at,
            original_page_updated_at=page.updated_at,
            response_etag=page_current_etag(
                page.page_uid,
                page.current_revision_id,
                page.current_revision_number,
                page.occurred_at,
                page.updated_at,
            ),
            operation_at=operation_at,
            source_id=source_id,
            master_audit_event_id=audit_id,
        )

    @staticmethod
    def _fingerprint(
        command: FileSetCreateCommand | FileSetAppendCommand,
        book_id: str,
        manifest: FileManifest,
        operation: str,
    ) -> bytes:
        metadata: dict[str, object] = {
            "version": "master-file-set-v1",
            "operation": operation,
            "library_id": command.library_id,
            "section_id": command.section_id,
            "book_id": book_id,
            "source": command.source.model_dump(),
        }
        if isinstance(command, FileSetCreateCommand):
            metadata.update(title=command.title, occurred_at=command.occurred_at)
        else:
            metadata.update(page_id=command.page_id, expected_etag=command.expected_etag)
        parts = [
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            ),
            len(manifest.files).to_bytes(8, "big"),
        ]
        for entry in manifest.files:
            parts.extend((entry.name.encode("utf-8"), entry.content))
        return digest_request_fingerprint(*parts)
