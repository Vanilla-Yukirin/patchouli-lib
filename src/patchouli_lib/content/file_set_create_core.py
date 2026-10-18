"""Transaction-neutral first Page file-set creation after caller authorization.

The caller must resolve an authorized Book and own a concrete SQLite write
transaction. This core writes content and Source only; it never authenticates,
records audit or idempotency, commits, or defines an HTTP contract.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from uuid import uuid4

from sqlalchemy import Connection, insert

from patchouli_lib.content.file_manifest import FileManifest
from patchouli_lib.content.models import Revision, RevisionFile, RevisionFileSeal, RevisionFileSet
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    ArchiveSourceInput,
    NewPage,
    NewPageIdCollisionCounter,
    NewPageIdentifier,
    NewPageSource,
    PageRecord,
)
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
from patchouli_lib.library.schemas import BookRecord

IdFactory = Callable[[], str]
PageUidFactory = Callable[[], bytes]
RevisionIdFactory = Callable[[], str]


class FileSetCreateTransactionRequiredError(RuntimeError):
    """No caller-owned concrete SQLite transaction is active."""


class FileSetCreateIdentifierExhaustedError(RuntimeError):
    """A collision-free Page or Revision identity could not be allocated."""


class FileSetPageCreateCore:
    """Persist one complete initial snapshot in a caller-owned write transaction."""

    def __init__(
        self,
        connection: Connection,
        *,
        id_factory: IdFactory = lambda: uuid4().hex,
        page_uid_factory: PageUidFactory = generate_page_uid,
        revision_id_factory: RevisionIdFactory = generate_revision_id,
        collision_attempts: int = DEFAULT_COLLISION_ATTEMPTS,
    ) -> None:
        if type(collision_attempts) is not int or collision_attempts < 1:
            raise ValueError("Identifier collision attempt bound must be positive.")
        self._connection = connection
        self._content = ContentRepository(connection)
        self._id_factory = id_factory
        self._page_uid_factory = page_uid_factory
        self._revision_id_factory = revision_id_factory
        self._collision_attempts = collision_attempts

    def create_page(
        self,
        *,
        book: BookRecord,
        title: str,
        occurred_at: int,
        operation_at: int,
        manifest: FileManifest,
        source: ArchiveSourceInput,
    ) -> PageRecord:
        """Write Page, Revision 1, files, seal, identifier and Source atomically.

        The caller has already authorized this Book in the same outer write
        transaction and must add its own audit/replay records before commit.
        A failed insert or readback rolls back only this core's savepoint.
        """

        self.require_transaction()
        manifest.__post_init__()
        canonical_utc_wire(occurred_at)
        canonical_utc_wire(operation_at)
        with self._connection.begin_nested():
            generated = self._allocate_page_id(book.library_id, title, occurred_at)
            page_uid = self._allocate_page_uid(book.library_id)
            revision_id = self._allocate_revision_id(book.library_id)
            page = self._content.add_page(
                NewPage(
                    library_id=book.library_id,
                    page_uid=page_uid,
                    section_id=book.section_id,
                    book_id=book.id,
                    page_id=generated.value,
                    id_scheme=PAGE_ID_SCHEME,
                    id_timestamp_micros=(occurred_at // 1_000) * 1_000,
                    base_slug=generated.base_slug,
                    collision_ordinal=generated.collision_ordinal,
                    title=title,
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
                    kind=source.kind,
                    locator=source.locator,
                    captured_at=source.captured_at,
                    created_at=operation_at,
                )
            )
            if self._content.get_current_file_manifest(page) != manifest:
                raise RuntimeError("Persisted initial file snapshot differs from the request.")
            return page

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

    def require_transaction(self) -> None:
        """Reject SQLAlchemy autobegin without a concrete outer SQLite BEGIN."""
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
    "FileSetCreateIdentifierExhaustedError",
    "FileSetCreateTransactionRequiredError",
    "FileSetPageCreateCore",
]
