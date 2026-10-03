"""Shared synthetic Page movement inputs."""

from sqlalchemy import Engine

from patchouli_lib.admin.page_move_service import MasterPageMoveCommand
from patchouli_lib.content.schemas import PageRecord
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import NewBook, NewSection

from .test_master_revision_restore_service import _etag


def _target(engine: Engine, page: PageRecord, *, cross_section: bool = True) -> tuple[str, str]:
    section = "5" * 32 if cross_section else page.section_id
    book = "6" * 32
    with immediate_transaction(engine) as connection:
        repository = LibraryRepository(connection)
        if cross_section:
            repository.add_section(
                NewSection(
                    id=section,
                    library_id=page.library_id,
                    name="Synthetic destination",
                    created_at=1_000_000,
                    updated_at=1_000_000,
                )
            )
        repository.add_book(
            NewBook(
                id=book,
                library_id=page.library_id,
                section_id=section,
                name="Synthetic destination book",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
    return section, book


def _command(page: PageRecord, target: tuple[str, str]) -> MasterPageMoveCommand:
    return MasterPageMoveCommand(
        library_id=page.library_id,
        page_id=page.page_id,
        source_section_id=page.section_id,
        source_book_id=page.book_id,
        target_section_id=target[0],
        target_book_id=target[1],
        expected_etag=_etag(page),
    )
