from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from patchouli_lib.auth.models import Caller
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService


@pytest.fixture
def auth_engine(tmp_path: Path) -> Iterator[Engine]:
    database_path = (tmp_path / "auth.db").as_posix()
    engine = build_engine(f"sqlite:///{database_path}")
    Caller.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def scoped_library(auth_engine: Engine) -> tuple[str, str, str]:
    identifiers: Iterator[str] = iter(("1" * 32, "2" * 32, "3" * 32))
    with immediate_transaction(auth_engine) as connection:
        seeded = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: 1_000_000,
        ).seed(
            LibraryStructureSeed(
                library_name="Synthetic Auth Library",
                section_name="Synthetic Authorized Section",
                book_name="Synthetic Auth Book",
            )
        )
    return seeded.library.id, seeded.section.id, seeded.book.id
