from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from patchouli_lib.database import build_engine, immediate_transaction

from .helpers import insert_page_graph, page_graph_values, seed_library_structure

CONTENT_TABLES = {
    "pages",
    "revisions",
    "page_identifier_registry",
    "page_id_collision_counters",
    "page_revision_append_guards",
    "page_sources",
}
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def configure_database(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    database_url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return database_url, Config(str(REPOSITORY_ROOT / "alembic.ini"))


def test_page_content_migration_upgrade_check_downgrade_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url, config = configure_database(tmp_path / "migration.db", monkeypatch)

    command.upgrade(config, "head")
    engine = build_engine(database_url)
    try:
        inspector = inspect(engine)
        assert CONTENT_TABLES.issubset(inspector.get_table_names())

        page_foreign_keys = {item["name"]: item for item in inspector.get_foreign_keys("pages")}
        assert page_foreign_keys["fk_pages_book_section_library_books"]["constrained_columns"] == [
            "book_id",
            "section_id",
            "library_id",
        ]
        current_revision = page_foreign_keys["fk_pages_current_revision_page_library_revisions"]
        assert current_revision["constrained_columns"] == [
            "library_id",
            "page_uid",
            "current_revision_id",
            "current_revision_number",
        ]
        assert current_revision["referred_columns"] == [
            "library_id",
            "page_uid",
            "revision_id",
            "revision_number",
        ]
        assert current_revision["options"] == {
            "deferrable": True,
            "initially": "DEFERRED",
        }
        canonical_identifier = page_foreign_keys["fk_pages_canonical_identifier_registry"]
        assert canonical_identifier["constrained_columns"] == [
            "library_id",
            "page_id",
            "page_uid",
        ]
        assert canonical_identifier["referred_columns"] == [
            "library_id",
            "identifier_text",
            "page_uid",
        ]
        assert canonical_identifier["options"] == {
            "deferrable": True,
            "initially": "DEFERRED",
        }

        revision_foreign_keys = {
            item["name"]: item for item in inspector.get_foreign_keys("revisions")
        }
        revision_page = revision_foreign_keys["fk_revisions_library_page_pages"]
        assert revision_page["constrained_columns"] == ["library_id", "page_uid"]
        assert revision_page["options"] == {
            "deferrable": True,
            "initially": "DEFERRED",
        }

        guard_foreign_keys = {
            item["name"]: item for item in inspector.get_foreign_keys("page_revision_append_guards")
        }
        guard_current = guard_foreign_keys["fk_page_revision_append_guards_current_page"]
        assert guard_current["constrained_columns"] == [
            "library_id",
            "page_uid",
            "revision_id",
            "revision_number",
        ]
        assert guard_current["referred_columns"] == [
            "library_id",
            "page_uid",
            "current_revision_id",
            "current_revision_number",
        ]
        assert guard_current["options"] == {
            "deferrable": True,
            "initially": "DEFERRED",
        }

        indexes = {item["name"]: item for item in inspector.get_indexes("page_identifier_registry")}
        canonical = indexes["uq_page_identifier_registry_library_page_canonical"]
        assert canonical["unique"] == 1
        assert canonical["column_names"] == ["library_id", "page_uid"]
        assert str(canonical["dialect_options"]["sqlite_where"]) == "identifier_kind = 'canonical'"

        with engine.connect() as first, engine.connect() as second:
            assert first.connection.driver_connection is not second.connection.driver_connection
            for connection in (first, second):
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
                assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            trigger_names = set(
                first.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' AND tbl_name = 'revisions'"
                    )
                ).scalars()
            )
            assert trigger_names == {
                "trg_revisions_create_append_guard",
                "trg_revisions_immutable_delete",
                "trg_revisions_immutable_update",
                "trg_revisions_sequential_insert",
            }
            content_triggers = set(
                first.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                        "AND name LIKE 'trg_page%'"
                    )
                ).scalars()
            )
            assert content_triggers == {
                "trg_page_id_collision_counters_monotonic",
                "trg_page_id_collision_counters_no_delete",
                "trg_page_identifier_registry_no_delete",
                "trg_page_identifier_registry_kind_on_insert",
                "trg_page_identifier_registry_stable",
                "trg_page_revision_append_guards_no_update",
                "trg_page_revision_append_guards_safe_delete",
                "trg_pages_canonical_identifier_on_insert",
                "trg_pages_clear_append_guard",
                "trg_pages_current_revision_advance",
                "trg_pages_initial_revision_number",
                "trg_pages_stable_identity",
            }
            assert first.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "20260813_0005"
            )
    finally:
        engine.dispose()

    command.check(config)
    command.downgrade(config, "20260813_0003")
    engine = build_engine(database_url)
    try:
        table_names = set(inspect(engine).get_table_names())
        assert CONTENT_TABLES.isdisjoint(table_names)
        assert {"libraries", "auth_callers", "auth_audit_events"}.issubset(table_names)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("20260813_0003")
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_populated_downgrade_removes_only_content_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url, config = configure_database(tmp_path / "populated.db", monkeypatch)
    command.upgrade(config, "head")
    engine = build_engine(database_url)
    library_id, section_id, book_id = seed_library_structure(engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, values)
    engine.dispose()

    command.downgrade(config, "20260813_0003")
    engine = build_engine(database_url)
    try:
        inspector = inspect(engine)
        assert CONTENT_TABLES.isdisjoint(inspector.get_table_names())
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM libraries")).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM sections")).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM books")).scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    command.check(config)
