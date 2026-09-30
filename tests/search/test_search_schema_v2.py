"""Synthetic migration coverage for the disabled search-v2 index skeleton."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from sqlalchemy import Connection, text

from patchouli_lib.database import build_engine, immediate_transaction

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OLD_SCHEMA = "20260930_0023"
NEW_SCHEMA = "20260930_0024"


def _database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[str, Config]:
    database_url = f"sqlite:///{(tmp_path / name).as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return database_url, Config(str(REPOSITORY_ROOT / "alembic.ini"))


def _meta(connection: Connection) -> tuple[int | None, int, int, str]:
    return cast(
        tuple[int | None, int, int, str],
        connection.execute(
            text("SELECT active_generation, ready, dirty_sequence, index_version FROM search_meta")
        )
        .one()
        ._tuple(),
    )


def test_empty_upgrade_and_downgrade_leave_authority_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch, "empty.db")
    command.upgrade(config, OLD_SCHEMA)
    command.upgrade(config, NEW_SCHEMA)
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert _meta(connection) == (None, 0, 0, "search-v2-candidate-schema-1")
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM search_dirty_pages").scalar_one()
                == 0
            )
            assert connection.exec_driver_sql("SELECT count(*) FROM search_terms").scalar_one() == 0
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
    command.downgrade(config, OLD_SCHEMA)
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == OLD_SCHEMA
            )
            assert connection.exec_driver_sql("SELECT count(*) FROM pages").scalar_one() == 0
            assert (
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_schema WHERE name LIKE 'search_%'"
                ).all()
                == []
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_existing_page_marked_dirty_and_authority_survives_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch, "existing.db")
    command.upgrade(config, OLD_SCHEMA)
    engine = build_engine(database_url)
    try:
        library_id, section_id, book_id = seed_library_structure(engine)
        values = page_graph_values(library_id=library_id, section_id=section_id, book_id=book_id)
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, values)
    finally:
        engine.dispose()

    command.upgrade(config, NEW_SCHEMA)
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert _meta(connection) == (None, 0, 1, "search-v2-candidate-schema-1")
            assert connection.exec_driver_sql(
                "SELECT library_id, page_uid, seq FROM search_dirty_pages"
            ).one()._tuple() == (library_id, values[0].page_uid, 1)
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM search_page_state").scalar_one()
                == 0
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()

    command.downgrade(config, OLD_SCHEMA)
    engine = build_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM pages").scalar_one() == 1
            assert connection.exec_driver_sql("SELECT count(*) FROM revisions").scalar_one() == 1
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM revision_files").scalar_one() == 1
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_trigger_sequence_and_two_isolated_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = _database(tmp_path, monkeypatch, "generations.db")
    command.upgrade(config, NEW_SCHEMA)
    engine = build_engine(database_url)
    try:
        library_id, section_id, book_id = seed_library_structure(engine)
        values = page_graph_values(library_id=library_id, section_id=section_id, book_id=book_id)
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, values)
            dirty = connection.exec_driver_sql(
                "SELECT seq FROM search_dirty_pages WHERE library_id = ? AND page_uid = ?",
                (library_id, values[0].page_uid),
            ).scalar_one()
            assert dirty == _meta(connection)[2]
            assert dirty >= 2  # Page, current Revision file, manifest and seal.
            connection.exec_driver_sql(
                "INSERT INTO tags (library_id, id, display_name, match_key, created_at) "
                "VALUES (?, ?, 'Search', 'search', 1000000)",
                (library_id, "a" * 32),
            )
            connection.exec_driver_sql(
                "INSERT INTO page_tags (library_id, page_uid, tag_id, created_at) "
                "VALUES (?, ?, ?, 1000000)",
                (library_id, values[0].page_uid, "a" * 32),
            )
            attached_seq = _meta(connection)[2]
            assert attached_seq > dirty
            connection.exec_driver_sql(
                "DELETE FROM page_tags WHERE library_id = ? AND page_uid = ? AND tag_id = ?",
                (library_id, values[0].page_uid, "a" * 32),
            )
            assert _meta(connection)[2] > attached_seq

            for generation in (1, 2):
                connection.exec_driver_sql(
                    "INSERT INTO search_generations "
                    "(generation, index_version, state, created_at, completed_at) "
                    "VALUES (?, 'search-v2-candidate-schema-1', 'building', 1000000, NULL)",
                    (generation,),
                )
                connection.exec_driver_sql(
                    "INSERT INTO search_page_state "
                    "(generation, library_id, page_uid, section_id, book_id, page_id, "
                    "revision_id, revision_number, occurred_at, snapshot_sha256, "
                    "source_sha256, document_count) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, 2)",
                    (
                        generation,
                        library_id,
                        values[0].page_uid,
                        section_id,
                        book_id,
                        values[0].page_id,
                        values[1].revision_id,
                        values[0].occurred_at,
                        b"s" * 32,
                    ),
                )
                for document_key, kind, name, normalized, gram in (
                    ("title", "title", None, "中文标题", "c1e4b8ad"),
                    ("file-name:content.md", "file_name", "content.md", "content.md", "c163"),
                ):
                    result = connection.exec_driver_sql(
                        "INSERT INTO search_documents "
                        "(generation, library_id, page_uid, revision_id, revision_number, "
                        "document_key, source_kind, file_name, normalized_text, source_sha256) "
                        "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                        (
                            generation,
                            library_id,
                            values[0].page_uid,
                            values[1].revision_id,
                            document_key,
                            kind,
                            name,
                            normalized,
                            b"d" * 32,
                        ),
                    )
                    connection.exec_driver_sql(
                        "INSERT INTO search_terms (rowid, grams) VALUES (?, ?)",
                        (result.lastrowid, gram),
                    )
            connection.exec_driver_sql(
                "UPDATE search_generations SET state = 'ready', completed_at = 1000001 "
                "WHERE generation = 1"
            )
            connection.exec_driver_sql(
                "UPDATE search_meta SET active_generation = 1, ready = 1 WHERE singleton = 1"
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM search_terms WHERE search_terms MATCH 'c1e4b8ad'"
                ).scalar_one()
                == 2
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM search_documents WHERE generation = 1"
                ).scalar_one()
                == 2
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM search_documents WHERE generation = 2"
                ).scalar_one()
                == 2
            )
            assert _meta(connection)[0:2] == (1, 1)
            connection.exec_driver_sql(
                "UPDATE search_generations SET state = 'ready', completed_at = 1000002 "
                "WHERE generation = 2"
            )
            connection.exec_driver_sql(
                "UPDATE search_meta SET active_generation = 2 WHERE singleton = 1"
            )
            assert _meta(connection)[0:2] == (2, 1)
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            connection.exec_driver_sql(
                "UPDATE search_meta SET ready = 0, active_generation = NULL WHERE singleton = 1"
            )
    finally:
        engine.dispose()
    command.downgrade(config, OLD_SCHEMA)
