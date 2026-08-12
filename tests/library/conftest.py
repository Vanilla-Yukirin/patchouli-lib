from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from patchouli_lib.database import build_engine
from patchouli_lib.library.models import Library


@pytest.fixture
def library_engine(tmp_path: Path) -> Iterator[Engine]:
    database_path = (tmp_path / "library.db").as_posix()
    engine = build_engine(f"sqlite:///{database_path}")
    Library.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
