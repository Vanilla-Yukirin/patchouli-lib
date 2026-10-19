"""Transaction-owned hierarchy README reads and compare-and-set updates."""

from __future__ import annotations

import hashlib

from sqlalchemy import Connection, update

from patchouli_lib.library.models import Book, Library, Section
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import BookRecord, LibraryRecord, SectionRecord
from patchouli_lib.library.service import (
    LibraryStructureNotFoundError,
    LibraryStructureVersionConflictError,
)

GuideRecord = LibraryRecord | SectionRecord | BookRecord


def get_guide(
    connection: Connection,
    *,
    library_id: str,
    section_id: str | None = None,
    book_id: str | None = None,
) -> GuideRecord:
    repository = LibraryRepository(connection)
    if book_id is not None:
        record: GuideRecord | None = (
            None if section_id is None else repository.get_book(library_id, section_id, book_id)
        )
    elif section_id is not None:
        record = repository.get_section(library_id, section_id)
    else:
        record = repository.get_library(library_id)
    if record is None:
        raise LibraryStructureNotFoundError("Hierarchy item was not found.")
    return record


def guide_etag(record: GuideRecord) -> str:
    """Hash every exposed metadata field, including identity and README bytes."""
    digest = hashlib.sha256(record.model_dump_json().encode("utf-8")).hexdigest()
    return f'"{digest}"'


def update_readme(
    connection: Connection,
    *,
    library_id: str,
    section_id: str | None = None,
    book_id: str | None = None,
    readme: str,
    expected_updated_at: int,
    now: int,
) -> GuideRecord:
    """Update only Markdown in the caller's transaction; never alter ancestry."""
    current = get_guide(connection, library_id=library_id, section_id=section_id, book_id=book_id)
    if current.updated_at != expected_updated_at:
        raise LibraryStructureVersionConflictError("README changed since it was read.")
    if current.readme == readme:
        return current
    model = Book if book_id is not None else Section if section_id is not None else Library
    statement = update(model).where(model.id == current.id, model.updated_at == expected_updated_at)
    if book_id is not None:
        statement = statement.where(Book.library_id == library_id)
    elif section_id is not None:
        statement = statement.where(Section.library_id == library_id)
    if model is Book:
        statement = statement.where(Book.section_id == section_id)
    timestamp = max(now, current.updated_at + 1)
    if connection.execute(statement.values(readme=readme, updated_at=timestamp)).rowcount != 1:
        raise LibraryStructureVersionConflictError("README changed since it was read.")
    return current.model_copy(update={"readme": readme, "updated_at": timestamp})
