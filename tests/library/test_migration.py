from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from patchouli_lib.database import build_engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    return Config(str(REPOSITORY_ROOT / "alembic.ini"))


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _configure_database(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    database_url = _database_url(path)
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return database_url, _alembic_config()


def _assert_upgraded_schema(database_url: str) -> None:
    engine = build_engine(database_url)
    try:
        inspector = inspect(engine)
        assert {"libraries", "sections", "books"}.issubset(inspector.get_table_names())

        section_foreign_keys = inspector.get_foreign_keys("sections")
        assert section_foreign_keys == [
            {
                "name": "fk_sections_library_id_libraries",
                "constrained_columns": ["library_id"],
                "referred_schema": None,
                "referred_table": "libraries",
                "referred_columns": ["id"],
                "options": {"ondelete": "RESTRICT"},
            }
        ]
        book_foreign_keys = inspector.get_foreign_keys("books")
        assert book_foreign_keys == [
            {
                "name": "fk_books_section_library_sections",
                "constrained_columns": ["section_id", "library_id"],
                "referred_schema": None,
                "referred_table": "sections",
                "referred_columns": ["id", "library_id"],
                "options": {"ondelete": "RESTRICT"},
            }
        ]

        with engine.connect() as first, engine.connect() as second:
            assert first.connection.driver_connection is not second.connection.driver_connection
            assert first.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert second.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert first.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert second.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            revision = first.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert revision == "20260812_0002"
    finally:
        engine.dispose()


def test_alembic_upgrade_downgrade_upgrade_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url, config = _configure_database(tmp_path / "migration.db", monkeypatch)

    command.upgrade(config, "head")
    _assert_upgraded_schema(database_url)

    command.downgrade(config, "base")
    engine = build_engine(database_url)
    try:
        table_names = inspect(engine).get_table_names()
        assert "libraries" not in table_names
        assert "sections" not in table_names
        assert "books" not in table_names
        assert "schema_metadata" not in table_names
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    _assert_upgraded_schema(database_url)


def test_upgraded_schema_has_no_alembic_metadata_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, config = _configure_database(tmp_path / "metadata-drift.db", monkeypatch)

    command.upgrade(config, "head")
    command.check(config)
