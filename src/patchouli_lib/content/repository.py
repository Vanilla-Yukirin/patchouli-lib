"""Transaction-neutral persistence operations for Page and Revision content."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import Connection, and_, func, insert, or_, select, update

from patchouli_lib.content.file_manifest import (
    MAX_FILES_PER_PAGE,
    MAX_PAGE_BYTES,
    FileManifest,
    build_file_manifest,
)
from patchouli_lib.content.models import (
    Page,
    PageIdCollisionCounter,
    PageIdentifier,
    PageLifecycleEvent,
    PageLifecycleGuard,
    PageOccurrenceCorrection,
    PageOccurrenceCorrectionGuard,
    PageSource,
    Revision,
    RevisionFile,
    RevisionFileSeal,
    RevisionFileSealGuard,
    RevisionFileSet,
)
from patchouli_lib.content.schemas import (
    NewPage,
    NewPageIdCollisionCounter,
    NewPageIdentifier,
    NewPageSource,
    NewRevision,
    PageIdCollisionCounterRecord,
    PageIdentifierRecord,
    PageLifecycleEventRecord,
    PageOccurrenceCorrectionCommand,
    PageOccurrenceCorrectionRecord,
    PageRecord,
    PageSourceRecord,
    RevisionRecord,
)
from patchouli_lib.identifiers import canonical_utc_wire, page_id_registry_digest, validate_page_id
from patchouli_lib.library.models import Book
from patchouli_lib.library.schemas import BookRecord


class ContentRepository:
    """Read and persist content state without owning or committing a transaction."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def get_book(self, library_id: str, book_id: str) -> BookRecord | None:
        statement = select(Book.__table__).where(
            Book.library_id == library_id,
            Book.id == book_id,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else BookRecord.model_validate(dict(row))

    def page_uid_exists(self, library_id: str, page_uid: bytes) -> bool:
        statement = select(Page.page_uid).where(
            Page.library_id == library_id,
            Page.page_uid == page_uid,
        )
        return self._connection.execute(statement).scalar_one_or_none() is not None

    def revision_id_exists(self, library_id: str, revision_id: str) -> bool:
        statement = select(Revision.revision_id).where(
            Revision.library_id == library_id,
            Revision.revision_id == revision_id,
        )
        return self._connection.execute(statement).scalar_one_or_none() is not None

    def identifier_exists(self, library_id: str, identifier_text: str) -> bool:
        statement = select(PageIdentifier.identifier_digest).where(
            PageIdentifier.library_id == library_id,
            PageIdentifier.identifier_digest == page_id_registry_digest(identifier_text),
        )
        return self._connection.execute(statement).scalar_one_or_none() is not None

    def get_page(self, library_id: str, identifier_text: str) -> PageRecord | None:
        digest = page_id_registry_digest(identifier_text)
        statement = (
            select(Page.__table__)
            .join(
                PageIdentifier,
                (PageIdentifier.library_id == Page.library_id)
                & (PageIdentifier.page_uid == Page.page_uid),
            )
            .where(
                PageIdentifier.library_id == library_id,
                PageIdentifier.identifier_digest == digest,
                PageIdentifier.identifier_text == identifier_text,
            )
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else PageRecord.model_validate(dict(row))

    def list_deleted_pages(
        self,
        library_id: str,
        section_id: str,
        *,
        limit: int,
        before: tuple[int, str] | None = None,
    ) -> tuple[PageRecord, ...]:
        """Return only tombstones, newest first, with a stable keyset boundary."""

        if type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError("Page lifecycle list limit must be within 1..101.")
        statement = select(Page.__table__).where(
            Page.library_id == library_id,
            Page.section_id == section_id,
            Page.page_type == "archive",
            Page.deleted_at.is_not(None),
        )
        if before is not None:
            if (
                type(before) is not tuple
                or len(before) != 2
                or type(before[0]) is not int
                or before[0] < 0
                or type(before[1]) is not str
            ):
                raise ValueError("Invalid Page lifecycle keyset boundary.")
            validate_page_id(before[1])
            statement = statement.where(
                or_(
                    Page.deleted_at < before[0],
                    and_(Page.deleted_at == before[0], Page.page_id > before[1]),
                )
            )
        rows = self._connection.execute(
            statement.order_by(Page.deleted_at.desc(), Page.page_id).limit(limit)
        ).mappings()
        return tuple(PageRecord.model_validate(dict(row)) for row in rows)

    def get_revision(
        self,
        library_id: str,
        page_uid: bytes,
        revision_number: int,
    ) -> RevisionRecord | None:
        statement = select(Revision.__table__).where(
            Revision.library_id == library_id,
            Revision.page_uid == page_uid,
            Revision.revision_number == revision_number,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else RevisionRecord.model_validate(dict(row))

    def get_collision_counter(
        self,
        library_id: str,
        id_scheme: str,
        id_timestamp_micros: int,
        base_slug: str,
    ) -> PageIdCollisionCounterRecord | None:
        statement = select(PageIdCollisionCounter.__table__).where(
            PageIdCollisionCounter.library_id == library_id,
            PageIdCollisionCounter.id_scheme == id_scheme,
            PageIdCollisionCounter.id_timestamp_micros == id_timestamp_micros,
            PageIdCollisionCounter.base_slug == base_slug,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else PageIdCollisionCounterRecord.model_validate(dict(row))

    def add_collision_counter(
        self,
        counter: NewPageIdCollisionCounter,
    ) -> PageIdCollisionCounterRecord:
        values = counter.model_dump()
        self._connection.execute(insert(PageIdCollisionCounter), values)
        return PageIdCollisionCounterRecord.model_validate(values)

    def advance_collision_counter(
        self,
        counter: PageIdCollisionCounterRecord,
        *,
        next_ordinal: int,
    ) -> PageIdCollisionCounterRecord | None:
        statement = (
            update(PageIdCollisionCounter)
            .where(
                PageIdCollisionCounter.library_id == counter.library_id,
                PageIdCollisionCounter.id_scheme == counter.id_scheme,
                PageIdCollisionCounter.id_timestamp_micros == counter.id_timestamp_micros,
                PageIdCollisionCounter.base_slug == counter.base_slug,
                PageIdCollisionCounter.next_ordinal == counter.next_ordinal,
            )
            .values(next_ordinal=next_ordinal)
        )
        if self._connection.execute(statement).rowcount != 1:
            return None
        return counter.model_copy(update={"next_ordinal": next_ordinal})

    def add_page(self, page: NewPage) -> PageRecord:
        values = page.model_dump()
        self._connection.execute(insert(Page), values)
        return PageRecord.model_validate(values)

    def correct_occurrence(
        self,
        page: PageRecord,
        command: PageOccurrenceCorrectionCommand,
    ) -> tuple[PageRecord, PageOccurrenceCorrectionRecord]:
        """Atomically change declared time and append its audit row.

        The caller owns the immediate write transaction and MUST first
        authorize ``actor_caller_id``. The database consumes the short-lived
        guard and creates the immutable audit row in the Page UPDATE trigger.
        A stale Page or failed UPDATE aborts the surrounding transaction.
        """

        if not self._connection.in_transaction():
            raise RuntimeError("Page occurrence correction requires a write transaction.")
        if (
            page.library_id != command.library_id
            or page.page_uid != command.page_uid
            or page.occurred_at != command.old_occurred_at
        ):
            raise ValueError("Page occurrence correction target is inconsistent.")
        # The supplied wall clock can be the same microsecond as the previous
        # write (or lag behind it). Persist one strictly advancing logical
        # instant so current ETags cannot repeat after metadata mutations.
        corrected_at = max(command.corrected_at, page.updated_at + 1)
        canonical_utc_wire(corrected_at)
        normalized = command.model_copy(update={"corrected_at": corrected_at})
        sequence = self._connection.scalar(
            select(func.coalesce(func.max(PageOccurrenceCorrection.sequence), 0) + 1).where(
                PageOccurrenceCorrection.library_id == page.library_id,
                PageOccurrenceCorrection.page_uid == page.page_uid,
            )
        )
        if type(sequence) is not int or not 1 <= sequence <= (1 << 63) - 1:
            raise ValueError("Page occurrence correction sequence is exhausted.")
        self._connection.execute(
            insert(PageOccurrenceCorrectionGuard),
            normalized.model_dump() | {"sequence": sequence},
        )
        result = self._connection.execute(
            update(Page)
            .where(
                Page.library_id == page.library_id,
                Page.page_uid == page.page_uid,
                Page.occurred_at == page.occurred_at,
                Page.updated_at == page.updated_at,
                Page.current_revision_id == page.current_revision_id,
                Page.current_revision_number == page.current_revision_number,
            )
            .values(occurred_at=command.new_occurred_at, updated_at=corrected_at)
        )
        if result.rowcount != 1:
            raise RuntimeError("Page occurrence correction encountered stale content.")
        row = (
            self._connection.execute(
                select(PageOccurrenceCorrection.__table__).where(
                    PageOccurrenceCorrection.library_id == page.library_id,
                    PageOccurrenceCorrection.page_uid == page.page_uid,
                    PageOccurrenceCorrection.sequence == sequence,
                )
            )
            .mappings()
            .one()
        )
        return (
            page.model_copy(
                update={
                    "occurred_at": command.new_occurred_at,
                    "updated_at": corrected_at,
                }
            ),
            PageOccurrenceCorrectionRecord.model_validate(dict(row)),
        )

    def transition_page_lifecycle(
        self,
        page: PageRecord,
        *,
        action: Literal["delete", "restore"],
        actor_caller_id: str,
        request_id: str,
        changed_at: int,
    ) -> tuple[PageRecord, PageLifecycleEventRecord]:
        """Apply one guarded tombstone transition within the caller's write transaction."""

        if not self._connection.in_transaction():
            raise RuntimeError("Page lifecycle change requires a write transaction.")
        if action not in {"delete", "restore"} or page.page_type != "archive":
            raise ValueError("Unsupported Page lifecycle operation.")
        if (action == "delete") != (page.deleted_at is None):
            raise ValueError("Page lifecycle operation does not change the current state.")
        if type(changed_at) is not int or changed_at < 0:
            raise ValueError("Invalid Page lifecycle operation time.")
        transition_at = max(changed_at, page.updated_at + 1)
        canonical_utc_wire(transition_at)
        sequence = self._connection.scalar(
            select(func.coalesce(func.max(PageLifecycleEvent.sequence), 0) + 1).where(
                PageLifecycleEvent.library_id == page.library_id,
                PageLifecycleEvent.page_uid == page.page_uid,
            )
        )
        if type(sequence) is not int or not 1 <= sequence <= (1 << 63) - 1:
            raise ValueError("Page lifecycle sequence is exhausted.")
        expected_event = PageLifecycleEventRecord(
            library_id=page.library_id,
            page_uid=page.page_uid,
            sequence=sequence,
            action=action,
            section_id=page.section_id,
            old_deleted_at=page.deleted_at,
            old_updated_at=page.updated_at,
            changed_at=transition_at,
            at_revision_number=page.current_revision_number,
            occurred_at_at_event=page.occurred_at,
            actor_caller_id=actor_caller_id,
            request_id=request_id,
        )
        self._connection.execute(insert(PageLifecycleGuard), expected_event.model_dump())
        next_deleted_at = transition_at if action == "delete" else None
        statement = (
            update(Page)
            .where(
                Page.library_id == page.library_id,
                Page.page_uid == page.page_uid,
                Page.section_id == page.section_id,
                Page.page_type == "archive",
                Page.updated_at == page.updated_at,
                Page.deleted_at.is_(page.deleted_at),
                Page.current_revision_id == page.current_revision_id,
                Page.current_revision_number == page.current_revision_number,
                Page.occurred_at == page.occurred_at,
            )
            .values(deleted_at=next_deleted_at, updated_at=transition_at)
        )
        if self._connection.execute(statement).rowcount != 1:
            raise RuntimeError("Page lifecycle change encountered stale content.")
        stored = (
            self._connection.execute(
                select(PageLifecycleEvent.__table__).where(
                    PageLifecycleEvent.library_id == page.library_id,
                    PageLifecycleEvent.page_uid == page.page_uid,
                    PageLifecycleEvent.sequence == sequence,
                )
            )
            .mappings()
            .one()
        )
        return (
            page.model_copy(update={"deleted_at": next_deleted_at, "updated_at": transition_at}),
            PageLifecycleEventRecord.model_validate(dict(stored)),
        )

    def add_revision(self, revision: NewRevision) -> RevisionRecord:
        values = revision.model_dump()
        self._connection.execute(insert(Revision), values)
        return RevisionRecord.model_validate(values)

    def get_current_revision_storage_format(
        self, page: PageRecord
    ) -> Literal["legacy_markdown", "file_set_v1"]:
        """Read the exact current Revision format, failing closed on missing state."""

        row = self._connection.execute(
            select(RevisionFileSet.storage_format, Revision.content_md.is_not(None))
            .join(
                Revision,
                (Revision.library_id == RevisionFileSet.library_id)
                & (Revision.page_uid == RevisionFileSet.page_uid)
                & (Revision.revision_id == RevisionFileSet.revision_id)
                & (Revision.revision_number == RevisionFileSet.revision_number),
            )
            .where(
                RevisionFileSet.library_id == page.library_id,
                RevisionFileSet.page_uid == page.page_uid,
                RevisionFileSet.revision_id == page.current_revision_id,
                RevisionFileSet.revision_number == page.current_revision_number,
            )
        ).one_or_none()
        if row is None:
            raise RuntimeError("Current Revision file-set manifest is missing.")
        storage_format, content_md_present = row
        if storage_format == "legacy_markdown" and content_md_present:
            return "legacy_markdown"
        if storage_format == "file_set_v1" and not content_md_present:
            return "file_set_v1"
        raise RuntimeError("Current Revision file-set format is inconsistent.")

    def get_current_file_manifest(self, page: PageRecord) -> FileManifest:
        """Read and verify the exact sealed current Revision, including legacy data."""

        identity = (
            Revision.library_id == page.library_id,
            Revision.page_uid == page.page_uid,
            Revision.revision_id == page.current_revision_id,
            Revision.revision_number == page.current_revision_number,
        )
        row = self._connection.execute(
            select(
                Revision.content_md,
                Revision.content_size_bytes,
                Revision.content_sha256,
                RevisionFileSet.storage_format,
                RevisionFileSet.file_count,
                RevisionFileSet.total_size_bytes,
                RevisionFileSet.snapshot_sha256,
            )
            .join(
                RevisionFileSet,
                (RevisionFileSet.library_id == Revision.library_id)
                & (RevisionFileSet.page_uid == Revision.page_uid)
                & (RevisionFileSet.revision_id == Revision.revision_id)
                & (RevisionFileSet.revision_number == Revision.revision_number),
            )
            .where(*identity)
        ).one_or_none()
        if row is None:
            raise RuntimeError("Current Revision file manifest is missing.")
        seal = self._connection.execute(
            select(RevisionFileSeal.revision_id).where(
                RevisionFileSeal.library_id == page.library_id,
                RevisionFileSeal.page_uid == page.page_uid,
                RevisionFileSeal.revision_id == page.current_revision_id,
                RevisionFileSeal.revision_number == page.current_revision_number,
            )
        ).scalar_one_or_none()
        guard = self._connection.execute(
            select(RevisionFileSealGuard.revision_id).where(
                RevisionFileSealGuard.library_id == page.library_id,
                RevisionFileSealGuard.page_uid == page.page_uid,
                RevisionFileSealGuard.revision_id == page.current_revision_id,
                RevisionFileSealGuard.revision_number == page.current_revision_number,
            )
        ).scalar_one_or_none()
        if seal is None or guard is None:
            raise RuntimeError("Current Revision file seal is missing.")
        rows = self._connection.execute(
            select(
                RevisionFile.filename,
                RevisionFile.content_bytes,
                RevisionFile.size_bytes,
                RevisionFile.content_sha256,
            )
            .where(
                RevisionFile.library_id == page.library_id,
                RevisionFile.page_uid == page.page_uid,
                RevisionFile.revision_id == page.current_revision_id,
                RevisionFile.revision_number == page.current_revision_number,
            )
            .order_by(RevisionFile.filename)
            .limit(MAX_FILES_PER_PAGE + 1)
        )
        stored_files: list[tuple[str, bytes, int, bytes]] = []
        observed_size = 0
        for filename, content, size, digest in rows:
            if (
                len(stored_files) >= MAX_FILES_PER_PAGE
                or type(content) is not bytes
                or observed_size + len(content) > MAX_PAGE_BYTES
            ):
                raise RuntimeError("Current Revision file set exceeds its bounds.")
            observed_size += len(content)
            stored_files.append((filename, content, size, digest))
        try:
            manifest = build_file_manifest(
                (filename, content) for filename, content, _size, _digest in stored_files
            )
        except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
            raise RuntimeError("Current Revision file bytes are invalid.") from exc
        if (
            len(manifest.files) != row.file_count
            or manifest.total_size_bytes != row.total_size_bytes
            or any(
                (entry.name, entry.content_size_bytes, entry.content_sha256)
                != (filename, size, digest)
                for entry, (filename, _content, size, digest) in zip(
                    manifest.files, stored_files, strict=True
                )
            )
        ):
            raise RuntimeError("Current Revision file metadata is inconsistent.")
        if row.storage_format == "legacy_markdown":
            if (
                len(manifest.files) != 1
                or manifest.files[0].name != "content.md"
                or manifest.files[0].content != row.content_md
                or manifest.files[0].content_size_bytes != row.content_size_bytes
                or manifest.files[0].content_sha256 != row.content_sha256
                or row.snapshot_sha256 is not None
            ):
                raise RuntimeError("Current legacy Revision mirror is inconsistent.")
        elif row.storage_format == "file_set_v1":
            if (
                row.content_md is not None
                or row.content_size_bytes is not None
                or row.content_sha256 is not None
                or row.snapshot_sha256 != manifest.snapshot_sha256
            ):
                raise RuntimeError("Current Revision file snapshot is inconsistent.")
        else:
            raise RuntimeError("Current Revision file format is unsupported.")
        return manifest

    def add_file_set_revision(
        self,
        page: PageRecord,
        *,
        revision_id: str,
        created_at: int,
        manifest: FileManifest,
    ) -> None:
        """Append one new-format snapshot; caller must advance Page in the same transaction."""

        number = page.current_revision_number + 1
        key = {
            "library_id": page.library_id,
            "page_uid": page.page_uid,
            "revision_id": revision_id,
            "revision_number": number,
        }
        self._connection.execute(
            insert(Revision),
            key
            | {
                "content_md": None,
                "content_size_bytes": None,
                "content_sha256": None,
                "created_at": created_at,
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

    def advance_file_set_current_revision(
        self,
        page: PageRecord,
        *,
        revision_id: str,
        updated_at: int,
    ) -> PageRecord | None:
        """Advance an unchanged Page using its exact prior identity and clock."""

        statement = (
            update(Page)
            .where(
                Page.library_id == page.library_id,
                Page.page_uid == page.page_uid,
                Page.current_revision_id == page.current_revision_id,
                Page.current_revision_number == page.current_revision_number,
                Page.occurred_at == page.occurred_at,
                Page.updated_at == page.updated_at,
                Page.deleted_at.is_(None),
            )
            .values(
                current_revision_id=revision_id,
                current_revision_number=page.current_revision_number + 1,
                updated_at=updated_at,
            )
        )
        if self._connection.execute(statement).rowcount != 1:
            return None
        return page.model_copy(
            update={
                "current_revision_id": revision_id,
                "current_revision_number": page.current_revision_number + 1,
                "updated_at": updated_at,
            }
        )

    def add_identifier(self, identifier: NewPageIdentifier) -> PageIdentifierRecord:
        values = identifier.model_dump()
        self._connection.execute(insert(PageIdentifier), values)
        return PageIdentifierRecord.model_validate(values)

    def add_source(self, source: NewPageSource) -> PageSourceRecord:
        values = source.model_dump()
        self._connection.execute(insert(PageSource), values)
        return PageSourceRecord.model_validate(values)

    def advance_current_revision(
        self,
        page: PageRecord,
        revision: RevisionRecord,
        *,
        updated_at: int,
    ) -> PageRecord | None:
        if updated_at <= page.updated_at or updated_at != revision.created_at:
            raise ValueError("Revision timestamp must advance the Page clock exactly.")
        statement = (
            update(Page)
            .where(
                Page.library_id == page.library_id,
                Page.page_uid == page.page_uid,
                Page.current_revision_id == page.current_revision_id,
                Page.current_revision_number == page.current_revision_number,
                Page.occurred_at == page.occurred_at,
                Page.updated_at == page.updated_at,
                Page.deleted_at.is_(None),
            )
            .values(
                current_revision_id=revision.revision_id,
                current_revision_number=revision.revision_number,
                updated_at=updated_at,
            )
        )
        if self._connection.execute(statement).rowcount != 1:
            return None
        return page.model_copy(
            update={
                "current_revision_id": revision.revision_id,
                "current_revision_number": revision.revision_number,
                "updated_at": updated_at,
            }
        )


__all__ = ["ContentRepository"]
