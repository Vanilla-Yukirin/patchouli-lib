from collections.abc import Callable, Iterator
from typing import Never

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError

from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.models import Book, Library, Section
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedConflictError, LibrarySeedService

SYNTHETIC_SEED = LibraryStructureSeed(
    library_name="Example Library",
    section_name="Example Archives",
    section_description="Synthetic conversation archives.",
    book_name="Example Agent Sessions",
    book_summary="Synthetic sessions for repository acceptance tests.",
)


def _id_factory(*identifiers: str) -> Callable[[], str]:
    values: Iterator[str] = iter(identifiers)
    return lambda: next(values)


def _unexpected_id() -> Never:
    raise AssertionError("An idempotent seed must not allocate another ID.")


def _unexpected_clock() -> Never:
    raise AssertionError("An idempotent seed must not allocate another timestamp.")


def test_default_seed_creates_valid_opaque_ids(library_engine: Engine) -> None:
    with immediate_transaction(library_engine) as connection:
        result = LibrarySeedService(LibraryRepository(connection)).seed(SYNTHETIC_SEED)

    assert result.created.library
    assert result.created.section
    assert result.created.book
    assert len(result.library.id) == 32
    assert len(result.section.id) == 32
    assert len(result.book.id) == 32
    assert result.library.created_at > 0
    assert result.library.created_at == result.section.created_at == result.book.created_at


def test_seed_replay_returns_existing_structure_without_duplicates(
    library_engine: Engine,
) -> None:
    identifiers = ("1" * 32, "2" * 32, "3" * 32)
    with immediate_transaction(library_engine) as connection:
        first = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=_id_factory(*identifiers),
            clock=lambda: 1_000_000,
        ).seed(SYNTHETIC_SEED)

    with immediate_transaction(library_engine) as connection:
        second = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=_unexpected_id,
            clock=_unexpected_clock,
        ).seed(SYNTHETIC_SEED)

    assert second.library == first.library
    assert second.section == first.section
    assert second.book == first.book
    assert not second.created.library
    assert not second.created.section
    assert not second.created.book

    with library_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Library)) == 1
        assert connection.scalar(select(func.count()).select_from(Section)) == 1
        assert connection.scalar(select(func.count()).select_from(Book)) == 1


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (
            SYNTHETIC_SEED.model_copy(update={"section_description": "A conflicting description."}),
            "Existing Section",
        ),
        (
            SYNTHETIC_SEED.model_copy(update={"book_summary": "A conflicting summary."}),
            "Existing Book",
        ),
    ],
)
def test_seed_rejects_conflicting_existing_metadata(
    library_engine: Engine,
    replacement: LibraryStructureSeed,
    message: str,
) -> None:
    with immediate_transaction(library_engine) as connection:
        LibrarySeedService(
            LibraryRepository(connection),
            id_factory=_id_factory("1" * 32, "2" * 32, "3" * 32),
            clock=lambda: 1_000_000,
        ).seed(SYNTHETIC_SEED)

    with (
        pytest.raises(LibrarySeedConflictError, match=message),
        immediate_transaction(library_engine) as connection,
    ):
        LibrarySeedService(LibraryRepository(connection)).seed(replacement)

    with library_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Library)) == 1
        assert connection.scalar(select(func.count()).select_from(Section)) == 1
        assert connection.scalar(select(func.count()).select_from(Book)) == 1


def test_failed_seed_rolls_back_all_new_structure(library_engine: Engine) -> None:
    with immediate_transaction(library_engine) as connection:
        LibrarySeedService(
            LibraryRepository(connection),
            id_factory=_id_factory("1" * 32, "2" * 32, "3" * 32),
            clock=lambda: 1_000_000,
        ).seed(SYNTHETIC_SEED)

    second_seed = LibraryStructureSeed(
        library_name="Second Example Library",
        section_name="Second Example Section",
        book_name="Second Example Book",
    )
    with (
        pytest.raises(IntegrityError),
        immediate_transaction(library_engine) as connection,
    ):
        LibrarySeedService(
            LibraryRepository(connection),
            id_factory=_id_factory("4" * 32, "5" * 32, "3" * 32),
            clock=lambda: 2_000_000,
        ).seed(second_seed)

    with library_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Library)) == 1
        assert connection.scalar(select(func.count()).select_from(Section)) == 1
        assert connection.scalar(select(func.count()).select_from(Book)) == 1


def test_seed_schema_rejects_blank_or_oversized_names() -> None:
    with pytest.raises(ValidationError):
        LibraryStructureSeed(
            library_name=" ",
            section_name="Example Section",
            book_name="Example Book",
        )

    with pytest.raises(ValidationError):
        LibraryStructureSeed(
            library_name="Example Library",
            section_name="s" * 201,
            book_name="Example Book",
        )
