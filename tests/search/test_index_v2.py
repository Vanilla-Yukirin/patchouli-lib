"""Candidate search index integration against synthetic, migrated SQLite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from sqlalchemy import Engine

from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.search.index_v2 import (
    INDEX_VERSION,
    SearchIndexProjectionError,
    rebuild_search_index,
    require_ready_index,
)
from patchouli_lib.search.literal_v2 import candidate_match_expression


@pytest.fixture
def migrated_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    database_url = f"sqlite:///{(tmp_path / 'index.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "head")
    engine = build_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


def test_empty_rebuild_enables_a_complete_generation(migrated_engine: Engine) -> None:
    generation = rebuild_search_index(migrated_engine, clock=lambda: 1_000_000)
    assert generation == 1
    with migrated_engine.connect() as connection, connection.begin():
        state = require_ready_index(connection)
        assert state.generation == generation
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM search_page_state").scalar_one() == 0
        )
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM search_dirty_pages").scalar_one() == 0
        )


def test_rebuild_indexes_exact_current_title_filename_and_text(migrated_engine: Engine) -> None:
    library_id, section_id, book_id = seed_library_structure(migrated_engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
        title="中文报告",
        content_md="# 技术资料\nSearchable text.\n".encode(),
    )
    with immediate_transaction(migrated_engine) as connection:
        insert_page_graph(connection, values)
    generation = rebuild_search_index(migrated_engine, clock=lambda: 2_000_000)
    with migrated_engine.connect() as connection, connection.begin():
        state = require_ready_index(connection)
        assert state.generation == generation
        assert (
            connection.exec_driver_sql(
                "SELECT document_count FROM search_page_state WHERE generation = ?",
                (generation,),
            ).scalar_one()
            == 3
        )
        rows = connection.exec_driver_sql(
            "SELECT source_kind, file_name, normalized_text FROM search_documents "
            "WHERE generation = ? ORDER BY source_kind",
            (generation,),
        ).all()
        assert [tuple(row) for row in rows] == [
            ("file_name", "content.md", "content.md"),
            ("file_text", "content.md", "# 技术资料\nsearchable text.\n"),
            ("title", None, "中文报告"),
        ]
        for keyword in ("技术", "中文报", "Searchable"):
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM search_terms WHERE search_terms MATCH ?",
                    (candidate_match_expression((keyword,)),),
                ).scalar_one()
                >= 1
            )
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM search_dirty_pages").scalar_one() == 0
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_immediate_transaction_indexes_new_page_before_commit(migrated_engine: Engine) -> None:
    generation = rebuild_search_index(migrated_engine, clock=lambda: 1_000_000)
    library_id, section_id, book_id = seed_library_structure(migrated_engine)
    values = page_graph_values(library_id=library_id, section_id=section_id, book_id=book_id)

    with immediate_transaction(migrated_engine) as connection:
        insert_page_graph(connection, values)
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM search_dirty_pages").scalar_one() == 1
        )

    with migrated_engine.connect() as connection, connection.begin():
        assert require_ready_index(connection).generation == generation
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM search_dirty_pages").scalar_one() == 0
        )
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM search_page_state").scalar_one() == 1
        )
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM search_documents").scalar_one() == 3


def test_projection_failure_rolls_back_authority_and_dirty_marker(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    rebuild_search_index(migrated_engine, clock=lambda: 1_000_000)
    library_id, section_id, book_id = seed_library_structure(migrated_engine)
    values = page_graph_values(library_id=library_id, section_id=section_id, book_id=book_id)

    def fail_projection(*_args: object) -> None:
        raise SearchIndexProjectionError("synthetic projection failure")

    monkeypatch.setattr("patchouli_lib.search.index_v2._project_page", fail_projection)
    with (
        pytest.raises(SearchIndexProjectionError, match="synthetic projection failure"),
        immediate_transaction(migrated_engine) as connection,
    ):
        insert_page_graph(connection, values)

    with migrated_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM pages").scalar_one() == 0
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM search_dirty_pages").scalar_one() == 0
        )
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM search_page_state").scalar_one() == 0
        )


def test_rebuild_failure_preserves_previous_ready_generation(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = rebuild_search_index(migrated_engine, clock=lambda: 1_000_000)

    def fail_projection(*_args: object) -> None:
        raise SearchIndexProjectionError("synthetic projection failure")

    monkeypatch.setattr("patchouli_lib.search.index_v2._project_page", fail_projection)
    library_id, section_id, book_id = seed_library_structure(migrated_engine)
    values = page_graph_values(library_id=library_id, section_id=section_id, book_id=book_id)
    # Bypass the application's transaction coordinator on purpose. The dirty
    # marker prevents an older generation from claiming complete coverage.
    with migrated_engine.begin() as connection:
        insert_page_graph(connection, values)
    with pytest.raises(SearchIndexProjectionError, match="synthetic projection failure"):
        rebuild_search_index(migrated_engine, clock=lambda: 2_000_000)
    with migrated_engine.connect() as connection, connection.begin():
        assert require_ready_index(connection).generation == original
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM search_generations").scalar_one() == 1
        )
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM search_dirty_pages").scalar_one() == 1
        )
        assert (
            connection.exec_driver_sql("SELECT index_version FROM search_meta").scalar_one()
            == INDEX_VERSION
        )
