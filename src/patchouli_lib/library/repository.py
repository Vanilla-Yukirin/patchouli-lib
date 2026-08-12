from sqlalchemy import Connection, insert, select

from patchouli_lib.library.models import Book, Library, Section
from patchouli_lib.library.schemas import (
    BookRecord,
    LibraryRecord,
    NewBook,
    NewLibrary,
    NewSection,
    SectionRecord,
)


class LibraryRepository:
    """Persist scoped library structure without owning the transaction."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def get_library(self, library_id: str) -> LibraryRecord | None:
        statement = select(Library.__table__).where(Library.id == library_id)
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else LibraryRecord.model_validate(row)

    def find_library_by_name(self, name: str) -> LibraryRecord | None:
        statement = select(Library.__table__).where(Library.name == name)
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else LibraryRecord.model_validate(row)

    def add_library(self, library: NewLibrary) -> LibraryRecord:
        values = library.model_dump()
        self._connection.execute(insert(Library), values)
        return LibraryRecord.model_validate(values)

    def get_section(self, library_id: str, section_id: str) -> SectionRecord | None:
        statement = select(Section.__table__).where(
            Section.library_id == library_id,
            Section.id == section_id,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else SectionRecord.model_validate(row)

    def find_section_by_name(self, library_id: str, name: str) -> SectionRecord | None:
        statement = select(Section.__table__).where(
            Section.library_id == library_id,
            Section.name == name,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else SectionRecord.model_validate(row)

    def add_section(self, section: NewSection) -> SectionRecord:
        values = section.model_dump()
        self._connection.execute(insert(Section), values)
        return SectionRecord.model_validate(values)

    def get_book(self, library_id: str, section_id: str, book_id: str) -> BookRecord | None:
        statement = select(Book.__table__).where(
            Book.library_id == library_id,
            Book.section_id == section_id,
            Book.id == book_id,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else BookRecord.model_validate(row)

    def find_book_by_name(
        self,
        library_id: str,
        section_id: str,
        name: str,
    ) -> BookRecord | None:
        statement = select(Book.__table__).where(
            Book.library_id == library_id,
            Book.section_id == section_id,
            Book.name == name,
        )
        row = self._connection.execute(statement).mappings().one_or_none()
        return None if row is None else BookRecord.model_validate(row)

    def add_book(self, book: NewBook) -> BookRecord:
        values = book.model_dump()
        self._connection.execute(insert(Book), values)
        return BookRecord.model_validate(values)
