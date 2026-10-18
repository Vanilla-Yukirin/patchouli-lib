"""0030 changes behavior version only and refuses lossy downgrade."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from auth.test_optional_expiry_migration import _snapshot
from backup.conftest import _config
from content.test_agent_page_lifecycle import _orphan_audit, _run
from content.test_agent_page_move import _agent
from content.test_master_revision_restore_service import _etag, _page, _story
from sqlalchemy import Engine

from patchouli_lib.backup import validate_database
from patchouli_lib.backup.errors import BackupDatabaseError
from patchouli_lib.backup.manifest import (
    LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION,
)
from patchouli_lib.content.page_lifecycle_schemas import PageLifecycleCommand
from patchouli_lib.database import build_engine


@pytest.fixture(
    params=[LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION, OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION]
)
def lifecycle_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[Engine]:
    path = tmp_path / "lifecycle.sqlite"
    command.upgrade(_config(path, monkeypatch), request.param)
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    try:
        yield engine
    finally:
        engine.dispose()


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
        command.upgrade(config, LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION)
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION
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
    lifecycle_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_engine = lifecycle_engine
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
    path = Path(content_engine.url.database)
    before = _snapshot(path)
    with pytest.raises(RuntimeError, match="Cannot discard Library Page lifecycle"):
        command.downgrade(config, "20261002_0029")
    # A completed 0031 -> 0030 prefix is committed before 0030 checks whether
    # lifecycle history permits continuing to 0029. No content may be lost.
    with content_engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION
        )
    assert _snapshot(path) == before
    validate_database(path, schema_revision=LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION)


def test_downgrade_rejects_orphan_new_audit(
    lifecycle_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_engine = lifecycle_engine
    _orphan_audit(content_engine)
    assert content_engine.url.database is not None
    path = Path(content_engine.url.database)
    before = _snapshot(path)
    with pytest.raises(RuntimeError, match="Cannot discard Library Page lifecycle"):
        command.downgrade(_config(path, monkeypatch), "20261002_0029")
    assert _snapshot(path) == before
    with content_engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
    # This fixture deliberately has an audit without its lifecycle receipt;
    # refusing downgrade must preserve that evidence, not make it valid.
    with pytest.raises(BackupDatabaseError):
        validate_database(path, schema_revision=LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION)
