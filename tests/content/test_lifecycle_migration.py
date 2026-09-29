"""Page lifecycle migration refuses history loss and mirrors the declared schema."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, select, text, update

from patchouli_lib.content.models import Page, PageLifecycleEvent, PageLifecycleGuard
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.database import build_engine, immediate_transaction

from .conftest import OPERATION_TIME, ArchiveScope, alembic_config, configure_database
from .helpers import insert_page_graph, page_graph_values, seed_library_structure


def test_upgrade_and_empty_downgrade_match_model_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = configure_database(tmp_path / "lifecycle-empty.db", monkeypatch)
    command.upgrade(config, "20260929_0012")
    engine = build_engine(database_url)
    try:
        inspector = inspect(engine)
        for model in (PageLifecycleEvent, PageLifecycleGuard):
            actual = {column["name"] for column in inspector.get_columns(model.__tablename__)}
            assert actual == set(model.__table__.columns.keys())
        with engine.connect() as connection:
            trigger_names = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_schema WHERE type = 'trigger'")
                )
            }
            assert "trg_pages_lifecycle_require_guard" in trigger_names
            assert "trg_pages_lifecycle_record" in trigger_names
            assert "trg_page_lifecycle_events_no_update" in trigger_names
        command.downgrade(config, "20260929_0011")
        assert not inspect(engine).has_table("page_lifecycle_events")
        assert not inspect(engine).has_table("page_lifecycle_guards")
    finally:
        engine.dispose()


def test_upgrade_refuses_preexisting_tombstone_without_event_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, config = configure_database(tmp_path / "preexisting-tombstone.db", monkeypatch)
    command.upgrade(config, "20260929_0011")
    engine = build_engine(database_url)
    try:
        library_id, section_id, book_id = seed_library_structure(engine)
        values = page_graph_values(library_id=library_id, section_id=section_id, book_id=book_id)
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, values)
            connection.execute(
                update(Page)
                .where(Page.library_id == library_id, Page.page_uid == values[0].page_uid)
                .values(deleted_at=OPERATION_TIME, updated_at=OPERATION_TIME)
            )
        with pytest.raises(RuntimeError, match="Cannot invent missing history"):
            command.upgrade(config, "20260929_0012")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260929_0011"
            )
            assert connection.scalar(select(Page.deleted_at)) == OPERATION_TIME
        assert not inspect(engine).has_table("page_lifecycle_events")
    finally:
        engine.dispose()


def test_downgrade_refuses_to_discard_lifecycle_events(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    values = page_graph_values(
        library_id=archive_scope.library_id,
        section_id=archive_scope.section_id,
        book_id=archive_scope.book_id,
    )
    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)
        repository = ContentRepository(connection)
        page = repository.get_page(archive_scope.library_id, values[0].page_id)
        assert page is not None
        repository.transition_page_lifecycle(
            page,
            action="delete",
            actor_caller_id=archive_scope.caller_id,
            request_id="req_" + "a" * 32,
            changed_at=OPERATION_TIME,
        )
    with pytest.raises(RuntimeError, match="Cannot discard recorded"):
        command.downgrade(alembic_config(), "20260929_0011")
    with content_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "20260929_0012"
        )
        assert connection.scalar(select(PageLifecycleEvent.sequence)) == 1
