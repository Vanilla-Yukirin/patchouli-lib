"""0030 changes behavior version only and refuses lossy downgrade."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from backup.conftest import _config
from content.conftest import content_engine as content_engine
from content.test_agent_page_lifecycle import _orphan_audit, _run
from content.test_agent_page_move import _agent
from content.test_master_revision_restore_service import _etag, _page, _story
from sqlalchemy import Engine

from patchouli_lib.content.page_lifecycle_schemas import PageLifecycleCommand
from patchouli_lib.database import CURRENT_SCHEMA_REVISION, build_engine


def test_behavior_upgrade_and_downgrade_preserve_exact_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "behavior.sqlite"
    config = _config(path, monkeypatch)
    command.upgrade(config, "20261002_0029")
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.connect() as connection:
            before = connection.exec_driver_sql(
                "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"
            ).all()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == CURRENT_SCHEMA_REVISION
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"
                ).all()
                == before
            )
        command.downgrade(config, "20261002_0029")
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"
                ).all()
                == before
            )
    finally:
        engine.dispose()


def test_downgrade_rejects_library_lifecycle_success(
    content_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    _run(
        content_engine,
        agent,
        PageLifecycleCommand(
            library_id=page.library_id,
            page_id=page.page_id,
            action="delete",
            expected_etag=_etag(page),
            request_id="req_" + "d" * 32,
        ),
        "delete",
    )
    assert content_engine.url.database is not None
    config = _config(Path(content_engine.url.database), monkeypatch)
    with pytest.raises(RuntimeError, match="Cannot discard Library Page lifecycle"):
        command.downgrade(config, "20261002_0029")
    with content_engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == CURRENT_SCHEMA_REVISION
        )


def test_downgrade_rejects_orphan_new_audit(
    content_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _orphan_audit(content_engine)
    assert content_engine.url.database is not None
    with pytest.raises(RuntimeError, match="Cannot discard Library Page lifecycle"):
        command.downgrade(_config(Path(content_engine.url.database), monkeypatch), "20261002_0029")
