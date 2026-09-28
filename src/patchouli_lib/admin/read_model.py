"""Read-only, scoped projections for the administration browser."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, and_, func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.sql import Select

from patchouli_lib.content.models import Page, Revision
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


class AdminReadModel:
    """Never issues DML and does not authenticate callers; the router does that first."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list_libraries(self) -> tuple[LibraryItem, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(_library_summary_query()).mappings().all()
            return tuple(_library_item(row) for row in rows)

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
    ) -> PageView | None:
        with self._engine.connect() as connection:
            library = _get_library(connection, library_id)
            section = _get_section(connection, library_id, section_id)
            book = _get_book(connection, library_id, section_id, book_id)
            if library is None or section is None or book is None:
                return None
            row = (
                connection.execute(
                    select(
                        Page.page_id,
                        Page.title,
                        Page.page_type,
                        Page.occurred_at,
                        Page.current_revision_number,
                        Revision.content_md,
                    )
                    .join(
                        Revision,
                        and_(
                            Revision.library_id == Page.library_id,
                            Revision.page_uid == Page.page_uid,
                            Revision.revision_id == Page.current_revision_id,
                            Revision.revision_number == Page.current_revision_number,
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
            return PageView(
                library,
                section,
                book,
                _page_item(row),
                bytes(row["content_md"]).decode("utf-8"),
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
