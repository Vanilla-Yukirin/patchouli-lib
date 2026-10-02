"""Transaction-neutral persistence queries for non-search retrieval."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Connection, and_, select

from patchouli_lib.auth.library_policy import LibraryPolicy, resolve_library_policy
from patchouli_lib.auth.models import Caller, Credential, SectionGrant
from patchouli_lib.auth.schemas import (
    CallerRecord,
    CredentialRecord,
    SectionAction,
    StoredCredential,
    credential_metadata,
)
from patchouli_lib.content.file_manifest import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_PAGE,
    MAX_PAGE_BYTES,
)
from patchouli_lib.content.models import (
    Page,
    PageIdentifier,
    Revision,
    RevisionFile,
    RevisionFileSeal,
    RevisionFileSealGuard,
    RevisionFileSet,
)
from patchouli_lib.content.schemas import PageRecord, RevisionRecord
from patchouli_lib.identifiers import page_id_registry_digest
from patchouli_lib.library.models import Book, Section
from patchouli_lib.library.schemas import BookRecord, SectionRecord
from patchouli_lib.retrieval.schemas import KeysetPage, ReadWindow


@dataclass(frozen=True, slots=True)
class StoredDocument:
    page: PageRecord
    revision: RevisionRecord


@dataclass(frozen=True, slots=True, repr=False)
class StoredRevisionFile:
    name: str
    content: bytes
    size_bytes: int
    content_sha256: bytes


class RetrievalUnsupportedFormatError(RuntimeError):
    """The legacy Markdown reader cannot represent this Revision format."""

    def __init__(self) -> None:
        super().__init__("The requested Revision format is not supported by this read route.")


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

    def get_library_policy(
        self,
        *,
        credential_id: str,
        caller_id: str,
        home_library_id: str,
        target_library_id: str,
        active_at: int,
    ) -> LibraryPolicy | None:
        return resolve_library_policy(
            self._connection,
            credential_id=credential_id,
            caller_id=caller_id,
            home_library_id=home_library_id,
            target_library_id=target_library_id,
            active_at=active_at,
        )

    def list_sections(
        self,
        library_id: str,
        window: ReadWindow,
    ) -> KeysetPage[SectionRecord]:
        """List one already-authorized Library without consulting legacy Section grants."""

        statement = (
            select(Section.__table__).where(Section.library_id == library_id).order_by(Section.id)
        )
        if window.after_key is not None:
            statement = statement.where(Section.id > window.after_key)
        rows = self._connection.execute(statement.limit(window.limit + 1)).mappings().all()
        records = tuple(SectionRecord.model_validate(dict(row)) for row in rows)
        return self._page(records, window.limit, key=lambda item: item.id)

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

    def get_page_by_id(self, library_id: str, identifier_text: str) -> PageRecord | None:
        """Resolve a scoped registry key without guessing its current Section."""
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
                Page.deleted_at.is_(None),
                PageIdentifier.library_id == library_id,
                PageIdentifier.identifier_digest == page_id_registry_digest(identifier_text),
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
        if row is None:
            return None
        # Check the stored format before validating the legacy Markdown model:
        # file_set_v1 may legitimately have no content_md mirror at all.
        format_statement = select(RevisionFileSet.storage_format).where(
            RevisionFileSet.library_id == library_id,
            RevisionFileSet.page_uid == page_uid,
            RevisionFileSet.revision_id == row["revision_id"],
            RevisionFileSet.revision_number == revision_number,
        )
        storage_format = self._connection.execute(format_statement).scalar_one_or_none()
        if storage_format == "file_set_v1":
            raise RetrievalUnsupportedFormatError
        if storage_format != "legacy_markdown":
            raise RuntimeError("Stored Revision file-set manifest is missing or invalid.")
        return RevisionRecord.model_validate(dict(row))

    def list_revision_files(
        self,
        library_id: str,
        page_uid: bytes,
        revision_id: str,
        revision_number: int,
    ) -> tuple[StoredRevisionFile, ...]:
        statement = (
            select(
                RevisionFile.filename,
                RevisionFile.content_bytes,
                RevisionFile.size_bytes,
                RevisionFile.content_sha256,
            )
            .where(
                RevisionFile.library_id == library_id,
                RevisionFile.page_uid == page_uid,
                RevisionFile.revision_id == revision_id,
                RevisionFile.revision_number == revision_number,
            )
            .order_by(RevisionFile.filename)
        )
        files: list[StoredRevisionFile] = []
        total_size = 0
        for row in self._connection.execute(statement):
            content = row[1]
            if (
                type(content) is not bytes
                or len(content) > MAX_FILE_BYTES
                or len(files) >= MAX_FILES_PER_PAGE
                or total_size + len(content) > MAX_PAGE_BYTES
            ):
                raise ValueError("Stored Revision file set exceeds its supported bounds.")
            files.append(StoredRevisionFile(*row))
            total_size += len(content)
        return tuple(files)

    def has_revision_file_seal(
        self,
        library_id: str,
        page_uid: bytes,
        revision_id: str,
        revision_number: int,
    ) -> bool:
        identity = (
            library_id,
            page_uid,
            revision_id,
            revision_number,
        )
        for model in (RevisionFileSeal, RevisionFileSealGuard):
            statement = select(model.revision_id).where(
                model.library_id == identity[0],
                model.page_uid == identity[1],
                model.revision_id == identity[2],
                model.revision_number == identity[3],
            )
            if self._connection.execute(statement).scalar_one_or_none() is None:
                return False
        return True

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


__all__ = [
    "RetrievalRepository",
    "RetrievalUnsupportedFormatError",
    "StoredDocument",
    "StoredRevisionFile",
]
