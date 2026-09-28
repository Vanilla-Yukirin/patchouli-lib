"""Transaction-neutral persistence operations for Page and Revision content."""

from __future__ import annotations

from sqlalchemy import Connection, func, insert, select, update

from patchouli_lib.content.models import (
    Page,
    PageIdCollisionCounter,
    PageIdentifier,
    PageOccurrenceCorrection,
    PageOccurrenceCorrectionGuard,
    PageSource,
    Revision,
)
from patchouli_lib.content.schemas import (
    NewPage,
    NewPageIdCollisionCounter,
    NewPageIdentifier,
    NewPageSource,
    NewRevision,
    PageIdCollisionCounterRecord,
    PageIdentifierRecord,
    PageOccurrenceCorrectionCommand,
    PageOccurrenceCorrectionRecord,
    PageRecord,
    PageSourceRecord,
    RevisionRecord,
)
from patchouli_lib.identifiers import canonical_utc_wire, page_id_registry_digest
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

    def add_revision(self, revision: NewRevision) -> RevisionRecord:
        values = revision.model_dump()
        self._connection.execute(insert(Revision), values)
        return RevisionRecord.model_validate(values)

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
