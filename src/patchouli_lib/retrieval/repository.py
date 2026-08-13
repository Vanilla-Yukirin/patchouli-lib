"""Transaction-neutral persistence queries for non-search retrieval."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Connection, and_, select

from patchouli_lib.auth.models import Caller, Credential, SectionGrant
from patchouli_lib.auth.schemas import (
    CallerRecord,
    CredentialRecord,
    SectionAction,
    StoredCredential,
    credential_metadata,
)
from patchouli_lib.content.models import Page, PageIdentifier, Revision
from patchouli_lib.content.schemas import PageRecord, RevisionRecord
from patchouli_lib.identifiers import page_id_registry_digest
from patchouli_lib.library.models import Book, Section
from patchouli_lib.library.schemas import BookRecord, SectionRecord
from patchouli_lib.retrieval.schemas import KeysetPage, ReadWindow


@dataclass(frozen=True, slots=True)
class StoredDocument:
    page: PageRecord
    revision: RevisionRecord


class RetrievalRepository:
    """Read scoped state without beginning, committing, or rolling back work."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def get_caller(self, library_id: str, caller_id: str) -> CallerRecord | None:
        statement = select(Caller.__table__).where(
            Caller.library_id == library_id,
            Caller.id == caller_id,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else CallerRecord.model_validate(dict(row))

    def get_credential(
        self,
        library_id: str,
        caller_id: str,
        credential_id: str,
    ) -> CredentialRecord | None:
        statement = select(Credential.__table__).where(
            Credential.library_id == library_id,
            Credential.caller_id == caller_id,
            Credential.id == credential_id,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        if row is None:
            return None
        return credential_metadata(StoredCredential.model_validate(dict(row)))

    def section_actions(
        self,
        library_id: str,
        caller_id: str,
        section_id: str,
    ) -> tuple[SectionAction, ...]:
        statement = (
            select(SectionGrant.action)
            .where(
                SectionGrant.library_id == library_id,
                SectionGrant.caller_id == caller_id,
                SectionGrant.section_id == section_id,
            )
            .order_by(SectionGrant.action)
        )
        return tuple(
            SectionAction(value) for value in self._connection.execute(statement).scalars().all()
        )

    def list_queryable_sections(
        self,
        library_id: str,
        caller_id: str,
        window: ReadWindow,
    ) -> KeysetPage[SectionRecord]:
        statement = (
            select(Section.__table__)
            .join(
                SectionGrant,
                and_(
                    SectionGrant.library_id == Section.library_id,
                    SectionGrant.section_id == Section.id,
                ),
            )
            .where(
                Section.library_id == library_id,
                SectionGrant.caller_id == caller_id,
                SectionGrant.action == SectionAction.QUERY.value,
            )
            .order_by(Section.id)
        )
        if window.after_key is not None:
            statement = statement.where(Section.id > window.after_key)
        rows = self._connection.execute(statement.limit(window.limit + 1)).mappings().all()
        records = tuple(SectionRecord.model_validate(dict(row)) for row in rows)
        return self._page(records, window.limit, key=lambda item: item.id)

    def get_section(self, library_id: str, section_id: str) -> SectionRecord | None:
        statement = select(Section.__table__).where(
            Section.library_id == library_id,
            Section.id == section_id,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else SectionRecord.model_validate(dict(row))

    def list_books(
        self,
        library_id: str,
        section_id: str,
        window: ReadWindow,
    ) -> KeysetPage[BookRecord]:
        statement = (
            select(Book.__table__)
            .where(
                Book.library_id == library_id,
                Book.section_id == section_id,
            )
            .order_by(Book.id)
        )
        if window.after_key is not None:
            statement = statement.where(Book.id > window.after_key)
        rows = self._connection.execute(statement.limit(window.limit + 1)).mappings().all()
        records = tuple(BookRecord.model_validate(dict(row)) for row in rows)
        return self._page(records, window.limit, key=lambda item: item.id)

    def list_pages(
        self,
        library_id: str,
        section_id: str,
        window: ReadWindow,
    ) -> KeysetPage[PageRecord]:
        statement = (
            select(Page.__table__)
            .where(
                Page.library_id == library_id,
                Page.section_id == section_id,
                Page.deleted_at.is_(None),
            )
            .order_by(Page.page_id)
        )
        if window.after_key is not None:
            statement = statement.where(Page.page_id > window.after_key)
        rows = self._connection.execute(statement.limit(window.limit + 1)).mappings().all()
        records = tuple(PageRecord.model_validate(dict(row)) for row in rows)
        return self._page(records, window.limit, key=lambda item: item.page_id)

    def get_page(
        self,
        library_id: str,
        section_id: str,
        identifier_text: str,
    ) -> PageRecord | None:
        digest = page_id_registry_digest(identifier_text)
        statement = (
            select(Page.__table__)
            .join(
                PageIdentifier,
                and_(
                    PageIdentifier.library_id == Page.library_id,
                    PageIdentifier.page_uid == Page.page_uid,
                ),
            )
            .where(
                Page.library_id == library_id,
                Page.section_id == section_id,
                Page.deleted_at.is_(None),
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

    def get_current_document(
        self,
        library_id: str,
        section_id: str,
        identifier_text: str,
    ) -> StoredDocument | None:
        page = self.get_page(library_id, section_id, identifier_text)
        if page is None:
            return None
        revision = self.get_revision(
            library_id,
            page.page_uid,
            page.current_revision_number,
        )
        if revision is None or revision.revision_id != page.current_revision_id:
            raise RuntimeError("Current Revision pointer could not be resolved.")
        return StoredDocument(page=page, revision=revision)

    @staticmethod
    def _page[ItemT](
        records: tuple[ItemT, ...],
        limit: int,
        *,
        key: Callable[[ItemT], str],
    ) -> KeysetPage[ItemT]:
        visible = records[:limit]
        if len(records) <= limit:
            return KeysetPage(items=visible, next_key=None)
        return KeysetPage(items=visible, next_key=key(visible[-1]))


__all__ = ["RetrievalRepository", "StoredDocument"]
