import pytest
from sqlalchemy import Engine, delete, insert
from sqlalchemy.exc import IntegrityError

from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.models import Book, Library, Section
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed, NewBook, NewSection
from patchouli_lib.library.service import LibrarySeedService


def _seed(
    engine: Engine,
    *,
    prefix: str,
    library_name: str,
    section_name: str,
    book_name: str,
) -> tuple[str, str, str]:
    identifiers = (prefix * 32, chr(ord(prefix) + 1) * 32, chr(ord(prefix) + 2) * 32)
    values = iter(identifiers)
    with immediate_transaction(engine) as connection:
        result = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=lambda: next(values),
            clock=lambda: 1_000_000,
        ).seed(
            LibraryStructureSeed(
                library_name=library_name,
                section_name=section_name,
                book_name=book_name,
            )
        )
    return result.library.id, result.section.id, result.book.id


def test_repository_lookups_conceal_wrong_library_and_section(
    library_engine: Engine,
) -> None:
    first_library, first_section, first_book = _seed(
        library_engine,
        prefix="1",
        library_name="First Example Library",
        section_name="First Example Section",
        book_name="First Example Book",
    )
    second_library, second_section, _ = _seed(
        library_engine,
        prefix="4",
        library_name="Second Example Library",
        section_name="Second Example Section",
        book_name="Second Example Book",
    )

    with library_engine.connect() as connection:
        repository = LibraryRepository(connection)
        assert repository.get_library(first_library) is not None
        assert repository.get_library("f" * 32) is None
        assert repository.get_section(first_library, first_section) is not None
        assert repository.get_section(second_library, first_section) is None
        assert repository.get_book(first_library, first_section, first_book) is not None
        assert repository.get_book(second_library, first_section, first_book) is None
        assert repository.get_book(first_library, second_section, first_book) is None
        assert repository.get_book(first_library, first_section, "f" * 32) is None


def test_two_independent_connections_enforce_orphan_book_foreign_key(
    library_engine: Engine,
) -> None:
    with library_engine.connect() as first, library_engine.connect() as second:
        assert first.connection.driver_connection is not second.connection.driver_connection
        for connection in (first, second):
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            connection.rollback()
            with pytest.raises(IntegrityError):
                connection.execute(
                    insert(Book),
                    {
                        "id": "1" * 32 if connection is first else "2" * 32,
                        "library_id": "3" * 32,
                        "section_id": "4" * 32,
                        "name": "Orphan Example Book",
                        "summary": "",
                        "created_at": 1_000_000,
                        "updated_at": 1_000_000,
                    },
                )
            connection.rollback()


def test_book_cannot_cross_library_scope(library_engine: Engine) -> None:
    first_library, first_section, _ = _seed(
        library_engine,
        prefix="1",
        library_name="First Example Library",
        section_name="First Example Section",
        book_name="First Example Book",
    )
    second_library, _, _ = _seed(
        library_engine,
        prefix="4",
        library_name="Second Example Library",
        section_name="Second Example Section",
        book_name="Second Example Book",
    )

    with (
        pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
        immediate_transaction(library_engine) as connection,
    ):
        LibraryRepository(connection).add_book(
            NewBook(
                id="7" * 32,
                library_id=second_library,
                section_id=first_section,
                name="Cross Scope Example Book",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )

    with library_engine.connect() as connection:
        assert (
            LibraryRepository(connection).find_book_by_name(
                first_library,
                first_section,
                "Cross Scope Example Book",
            )
            is None
        )


def test_names_are_unique_only_within_their_parent_scope(library_engine: Engine) -> None:
    first_library, first_section, _ = _seed(
        library_engine,
        prefix="1",
        library_name="First Example Library",
        section_name="Shared Example Section",
        book_name="Shared Example Book",
    )
    _seed(
        library_engine,
        prefix="4",
        library_name="Second Example Library",
        section_name="Shared Example Section",
        book_name="Shared Example Book",
    )

    with (
        pytest.raises(IntegrityError, match="UNIQUE constraint failed"),
        immediate_transaction(library_engine) as connection,
    ):
        LibraryRepository(connection).add_section(
            NewSection(
                id="7" * 32,
                library_id=first_library,
                name="Shared Example Section",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )

    with (
        pytest.raises(IntegrityError, match="UNIQUE constraint failed"),
        immediate_transaction(library_engine) as connection,
    ):
        LibraryRepository(connection).add_book(
            NewBook(
                id="8" * 32,
                library_id=first_library,
                section_id=first_section,
                name="Shared Example Book",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )


def test_parent_deletes_are_restricted(library_engine: Engine) -> None:
    library_id, section_id, _ = _seed(
        library_engine,
        prefix="1",
        library_name="Example Library",
        section_name="Example Section",
        book_name="Example Book",
    )

    with (
        pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
        immediate_transaction(library_engine) as connection,
    ):
        connection.execute(delete(Section).where(Section.id == section_id))

    with (
        pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
        immediate_transaction(library_engine) as connection,
    ):
        connection.execute(delete(Library).where(Library.id == library_id))

    with library_engine.connect() as connection:
        repository = LibraryRepository(connection)
        assert repository.get_library(library_id) is not None
        assert repository.get_section(library_id, section_id) is not None
