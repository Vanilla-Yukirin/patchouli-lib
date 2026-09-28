"""Read-only, scoped projections for the administration browser."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, and_, func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.sql import Select

from patchouli_lib.auth.models import AuditEvent, Caller
from patchouli_lib.content.models import Page, Revision, RevisionFile
from patchouli_lib.library.models import Book, Library, Section


@dataclass(frozen=True)
class LibraryItem:
    id: str
    name: str
    created_at: int
    page_count: int


@dataclass(frozen=True)
class SectionItem:
    id: str
    name: str
    description: str


@dataclass(frozen=True)
class BookItem:
    id: str
    name: str
    summary: str


@dataclass(frozen=True)
class PageItem:
    id: str
    title: str
    page_type: str
    occurred_at: int
    revision_number: int


@dataclass(frozen=True)
class LibraryView:
    library: LibraryItem
    sections: tuple[SectionItem, ...]


@dataclass(frozen=True)
class SectionView:
    library: LibraryItem
    section: SectionItem
    books: tuple[BookItem, ...]


@dataclass(frozen=True)
class BookView:
    library: LibraryItem
    section: SectionItem
    book: BookItem
    pages: tuple[PageItem, ...]


@dataclass(frozen=True)
class PageView:
    library: LibraryItem
    section: SectionItem
    book: BookItem
    page: PageItem
    markdown: str
    selected_revision_number: int
    selected_revision_created_at: int
    files: tuple[RevisionFileItem, ...]
    revisions: tuple[RevisionItem, ...]


@dataclass(frozen=True)
class RevisionFileItem:
    name: str
    size_bytes: int
    sha256_hex: str


@dataclass(frozen=True)
class RevisionItem:
    number: int
    created_at: int


@dataclass(frozen=True)
class ContentActivityItem:
    library_id: str
    actor_id: str
    actor_name: str
    action: str
    occurred_at: int
    page_title: str | None
    section_id: str | None
    book_id: str | None
    page_id: str | None
    revision_number: int | None


@dataclass(frozen=True)
class CallerView:
    library_id: str
    id: str
    name: str
    description: str
    kind: str
    disabled_at: int | None


@dataclass(frozen=True)
class CallerItem:
    library_id: str
    library_name: str
    id: str
    name: str
    kind: str
    created_at: int
    disabled_at: int | None


class AdminReadModel:
    """Never issues DML and does not authenticate callers; the router does that first."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list_libraries(self) -> tuple[LibraryItem, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(_library_summary_query()).mappings().all()
            return tuple(_library_item(row) for row in rows)

    def list_callers(self) -> tuple[CallerItem, ...]:
        """List identity metadata, never credential verifiers or raw tokens."""

        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        Caller.library_id,
                        Library.name.label("library_name"),
                        Caller.id,
                        Caller.name,
                        Caller.kind,
                        Caller.created_at,
                        Caller.disabled_at,
                    )
                    .join(Library, Library.id == Caller.library_id)
                    .order_by(Caller.created_at.desc(), Caller.id.desc())
                )
                .mappings()
                .all()
            )
            return tuple(CallerItem(**row) for row in rows)

    def recent_content_activity(self, *, limit: int = 50) -> tuple[ContentActivityItem, ...]:
        """Show successful content writes only; this is not an API request log."""

        if not 1 <= limit <= 50:
            raise ValueError("Activity limit must be between 1 and 50.")
        with self._engine.connect() as connection:
            events = (
                connection.execute(
                    select(
                        AuditEvent.library_id,
                        AuditEvent.actor_caller_id,
                        Caller.name.label("actor_name"),
                        AuditEvent.action,
                        AuditEvent.resource_id,
                        AuditEvent.occurred_at,
                    )
                    .join(
                        Caller,
                        and_(
                            Caller.library_id == AuditEvent.library_id,
                            Caller.id == AuditEvent.actor_caller_id,
                        ),
                    )
                    .where(
                        AuditEvent.outcome == "succeeded",
                        AuditEvent.action.in_(("content.archive.create", "content.archive.revise")),
                    )
                    .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            items: list[ContentActivityItem] = []
            for event in events:
                if event["action"] == "content.archive.create":
                    page = (
                        connection.execute(
                            select(
                                Page.section_id,
                                Page.book_id,
                                Page.page_id,
                                Page.title,
                                Page.deleted_at,
                            ).where(
                                Page.library_id == event["library_id"],
                                Page.page_id == event["resource_id"],
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    revision_number = None if page is None else 1
                else:
                    page = (
                        connection.execute(
                            select(
                                Page.section_id,
                                Page.book_id,
                                Page.page_id,
                                Page.title,
                                Page.deleted_at,
                                Revision.revision_number,
                            )
                            .join(
                                Revision,
                                and_(
                                    Revision.library_id == Page.library_id,
                                    Revision.page_uid == Page.page_uid,
                                ),
                            )
                            .where(
                                Revision.library_id == event["library_id"],
                                Revision.revision_id == event["resource_id"],
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    revision_number = None if page is None else page["revision_number"]
                if page is None or page["deleted_at"] is not None:
                    section_id = book_id = page_id = None
                    visible_revision_number = None
                else:
                    section_id = page["section_id"]
                    book_id = page["book_id"]
                    page_id = page["page_id"]
                    visible_revision_number = revision_number
                items.append(
                    ContentActivityItem(
                        library_id=event["library_id"],
                        actor_id=event["actor_caller_id"],
                        actor_name=event["actor_name"],
                        action=event["action"],
                        occurred_at=event["occurred_at"],
                        page_title=None if page is None else page["title"],
                        section_id=section_id,
                        book_id=book_id,
                        page_id=page_id,
                        revision_number=visible_revision_number,
                    )
                )
            return tuple(items)

    def get_caller(self, library_id: str, caller_id: str) -> CallerView | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        Caller.id,
                        Caller.library_id,
                        Caller.name,
                        Caller.description,
                        Caller.kind,
                        Caller.disabled_at,
                    ).where(Caller.library_id == library_id, Caller.id == caller_id)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            return CallerView(
                row["library_id"],
                row["id"],
                row["name"],
                row["description"],
                row["kind"],
                row["disabled_at"],
            )

    def get_library(self, library_id: str) -> LibraryView | None:
        with self._engine.connect() as connection:
            library = _get_library(connection, library_id)
            if library is None:
                return None
            rows = connection.execute(
                select(Section.id, Section.name, Section.description)
                .where(Section.library_id == library_id)
                .order_by(Section.name, Section.id)
            ).mappings()
            sections = tuple(_section_item(row) for row in rows)
            return LibraryView(library, sections)

    def get_section(self, library_id: str, section_id: str) -> SectionView | None:
        with self._engine.connect() as connection:
            library = _get_library(connection, library_id)
            section = _get_section(connection, library_id, section_id)
            if library is None or section is None:
                return None
            rows = connection.execute(
                select(Book.id, Book.name, Book.summary)
                .where(Book.library_id == library_id, Book.section_id == section_id)
                .order_by(Book.name, Book.id)
            ).mappings()
            books = tuple(_book_item(row) for row in rows)
            return SectionView(library, section, books)

    def get_book(self, library_id: str, section_id: str, book_id: str) -> BookView | None:
        with self._engine.connect() as connection:
            library = _get_library(connection, library_id)
            section = _get_section(connection, library_id, section_id)
            book = _get_book(connection, library_id, section_id, book_id)
            if library is None or section is None or book is None:
                return None
            rows = connection.execute(
                select(
                    Page.page_id,
                    Page.title,
                    Page.page_type,
                    Page.occurred_at,
                    Page.current_revision_number,
                )
                .where(
                    Page.library_id == library_id,
                    Page.section_id == section_id,
                    Page.book_id == book_id,
                    Page.deleted_at.is_(None),
                )
                .order_by(Page.occurred_at.desc(), Page.page_id)
            ).mappings()
            pages = tuple(_page_item(row) for row in rows)
            return BookView(library, section, book, pages)

    def get_page(
        self,
        library_id: str,
        section_id: str,
        book_id: str,
        page_id: str,
        revision_number: int | None = None,
    ) -> PageView | None:
        if revision_number is not None and not 1 <= revision_number <= (1 << 63) - 1:
            return None
        with self._engine.connect() as connection:
            library = _get_library(connection, library_id)
            section = _get_section(connection, library_id, section_id)
            book = _get_book(connection, library_id, section_id, book_id)
            if library is None or section is None or book is None:
                return None
            revision_match = (
                and_(
                    Revision.revision_number == Page.current_revision_number,
                    Revision.revision_id == Page.current_revision_id,
                )
                if revision_number is None
                else Revision.revision_number == revision_number
            )
            row = (
                connection.execute(
                    select(
                        Page.page_id,
                        Page.page_uid,
                        Page.title,
                        Page.page_type,
                        Page.occurred_at,
                        Page.current_revision_number,
                        Revision.content_md,
                        Revision.revision_id.label("selected_revision_id"),
                        Revision.revision_number.label("selected_revision_number"),
                        Revision.created_at.label("selected_revision_created_at"),
                    )
                    .join(
                        Revision,
                        and_(
                            Revision.library_id == Page.library_id,
                            Revision.page_uid == Page.page_uid,
                            revision_match,
                        ),
                    )
                    .where(
                        Page.library_id == library_id,
                        Page.section_id == section_id,
                        Page.book_id == book_id,
                        Page.page_id == page_id,
                        Page.deleted_at.is_(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            files = tuple(
                RevisionFileItem(
                    item["filename"], item["size_bytes"], bytes(item["content_sha256"]).hex()
                )
                for item in connection.execute(
                    select(
                        RevisionFile.filename,
                        RevisionFile.size_bytes,
                        RevisionFile.content_sha256,
                    )
                    .where(
                        RevisionFile.library_id == library_id,
                        RevisionFile.page_uid == row["page_uid"],
                        RevisionFile.revision_id == row["selected_revision_id"],
                        RevisionFile.revision_number == row["selected_revision_number"],
                    )
                    .order_by(RevisionFile.filename)
                ).mappings()
            )
            revisions = tuple(
                RevisionItem(item["revision_number"], item["created_at"])
                for item in connection.execute(
                    select(Revision.revision_number, Revision.created_at)
                    .where(
                        Revision.library_id == library_id,
                        Revision.page_uid == row["page_uid"],
                    )
                    .order_by(Revision.revision_number.desc())
                ).mappings()
            )
            return PageView(
                library,
                section,
                book,
                _page_item(row),
                bytes(row["content_md"]).decode("utf-8"),
                row["selected_revision_number"],
                row["selected_revision_created_at"],
                files,
                revisions,
            )


def _library_summary_query() -> Select[Any]:
    return (
        select(
            Library.id,
            Library.name,
            Library.created_at,
            func.count(Page.page_uid).label("page_count"),
        )
        .outerjoin(
            Page,
            and_(Page.library_id == Library.id, Page.deleted_at.is_(None)),
        )
        .group_by(Library.id, Library.name, Library.created_at)
        .order_by(Library.name, Library.id)
    )


def _get_library(connection: Connection, library_id: str) -> LibraryItem | None:
    row = (
        connection.execute(_library_summary_query().where(Library.id == library_id))
        .mappings()
        .one_or_none()
    )
    return None if row is None else _library_item(row)


def _get_section(connection: Connection, library_id: str, section_id: str) -> SectionItem | None:
    row = (
        connection.execute(
            select(Section.id, Section.name, Section.description).where(
                Section.library_id == library_id,
                Section.id == section_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _section_item(row)


def _get_book(
    connection: Connection, library_id: str, section_id: str, book_id: str
) -> BookItem | None:
    row = (
        connection.execute(
            select(Book.id, Book.name, Book.summary).where(
                Book.library_id == library_id,
                Book.section_id == section_id,
                Book.id == book_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _book_item(row)


def _library_item(row: RowMapping) -> LibraryItem:
    return LibraryItem(row["id"], row["name"], row["created_at"], row["page_count"])


def _section_item(row: RowMapping) -> SectionItem:
    return SectionItem(row["id"], row["name"], row["description"])


def _book_item(row: RowMapping) -> BookItem:
    return BookItem(row["id"], row["name"], row["summary"])


def _page_item(row: RowMapping) -> PageItem:
    return PageItem(
        row["page_id"],
        row["title"],
        row["page_type"],
        row["occurred_at"],
        row["current_revision_number"],
    )
