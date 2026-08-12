from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from patchouli_lib.database import build_engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    return Config(str(REPOSITORY_ROOT / "alembic.ini"))


def configure_database(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    database_url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return database_url, alembic_config()


@pytest.fixture
def content_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    database_url, config = configure_database(tmp_path / "content.db", monkeypatch)
    command.upgrade(config, "head")
    engine = build_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()
