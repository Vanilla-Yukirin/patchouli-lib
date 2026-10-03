"""Synthetic-only 0025 migration and master Page deletion guard checks."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Literal, cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError

from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.database import build_engine, immediate_transaction

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from content.helpers import (  # noqa: E402
    insert_page_graph,
    page_graph_values,
    seed_library_structure,
)

OLD_REVISION = "20260930_0024"
NEW_REVISION = "20261001_0025"
TRIGGER = "trg_page_lifecycle_guards_master_audit"
CALLER = "4" * 32
MASTER_EVENT = "5" * 32


def _config(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return url, Config(str(ROOT / "alembic.ini"))


def _snapshot(path: Path) -> list[tuple[str, str, str]]:
    with closing(sqlite3.connect(path)) as database:
        return database.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' "
            "ORDER BY type, name"
        ).fetchall()


def _seed(engine: Engine) -> tuple[str, str, bytes]:
    library, section, book = seed_library_structure(engine)
    values = page_graph_values(library_id=library, section_id=section, book_id=book)
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, values)
        AuthRepository(connection).add_caller(
            NewCaller(
                id=CALLER,
                library_id=library,
                kind=CallerKind.AGENT,
                name="Synthetic Actor",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
    return library, values[0].page_id, values[0].page_uid


def _transition(
    engine: Engine,
    *,
    library: str,
    page_id: str,
    action: Literal["delete", "restore"],
    at: int,
    master: bool,
    audit_action: str | None = None,
    target_type: str = "page",
    target_id: str | None = None,
    audit_time: int | None = None,
) -> None:
    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        page = repository.get_page(library, page_id)
        assert page is not None
        if master:
            MasterAuditRepository(connection).add_success(
                identity_id="a" * 32,
                session_generation=1,
                session_fingerprint=b"f" * 32,
                action=audit_action or f"content.archive.{action}",
                target_type=target_type,
                target_id=target_id or f"{library}:{page.page_uid.hex()}",
                occurred_at=at if audit_time is None else audit_time,
                event_id=MASTER_EVENT,
            )
        repository.transition_page_lifecycle(
            page,
            action=action,
            actor_caller_id=None if master else CALLER,
            actor_home_library_id=None if master else library,
            master_audit_event_id=MASTER_EVENT if master else None,
            request_id="req_" + "6" * 32,
            changed_at=at,
        )


def _content_rows(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with closing(sqlite3.connect(path)) as database:
        return {
            table: database.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "revisions",
                "revision_files",
                "revision_file_sets",
                "revision_file_seals",
                "page_identifier_registry",
                "page_sources",
            )
        }


def test_0025_empty_round_trip_preserves_exact_0024_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty.sqlite"
    _, config = _config(path, monkeypatch)
    command.upgrade(config, OLD_REVISION)
    before = _snapshot(path)
    command.upgrade(config, NEW_REVISION)
    after = _snapshot(path)
    assert [row for row in after if row[1] != TRIGGER] == [
        row for row in before if row[1] != TRIGGER
    ]
    assert after != before
    command.downgrade(config, OLD_REVISION)
    assert _snapshot(path) == before
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("SELECT version_num FROM alembic_version").fetchone() == (
            OLD_REVISION,
        )
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0024_caller_delete_master_restore_survives_upgrade_and_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "history.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, OLD_REVISION)
    engine = build_engine(url)
    try:
        library, page_id, uid = _seed(engine)
        # A master delete is not permitted by 0024. Its audit must roll back too.
        with pytest.raises(IntegrityError):
            _transition(
                engine, library=library, page_id=page_id, action="delete", at=3_000_000, master=True
            )
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM admin_master_audit_events"
                ).scalar_one()
                == 0
            )
        _transition(
            engine, library=library, page_id=page_id, action="delete", at=3_000_000, master=False
        )
        _transition(
            engine, library=library, page_id=page_id, action="restore", at=4_000_000, master=True
        )
        before = _snapshot(path)
        content_before = _content_rows(path)
        with engine.connect() as connection:
            history_before = connection.exec_driver_sql(
                "SELECT * FROM page_lifecycle_events ORDER BY sequence"
            ).all()
        command.upgrade(config, NEW_REVISION)
        assert _content_rows(path) == content_before
        command.downgrade(config, OLD_REVISION)
        assert _snapshot(path) == before
        assert _content_rows(path) == content_before
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT * FROM page_lifecycle_events ORDER BY sequence"
                ).all()
                == history_before
            )
            assert connection.exec_driver_sql("SELECT page_uid, deleted_at FROM pages").one() == (
                uid,
                None,
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
    finally:
        engine.dispose()


def test_master_delete_preserves_content_and_blocks_lossy_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "master-delete.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, NEW_REVISION)
    engine = build_engine(url)
    try:
        library, page_id, uid = _seed(engine)
        content_before = _content_rows(path)
        _transition(
            engine, library=library, page_id=page_id, action="delete", at=3_000_000, master=True
        )
        assert _content_rows(path) == content_before
        before = _snapshot(path)
        with pytest.raises(RuntimeError, match="Cannot downgrade master-attributed Page deletion"):
            command.downgrade(config, OLD_REVISION)
        assert _snapshot(path) == before
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == NEW_REVISION
            )
            assert connection.exec_driver_sql("SELECT page_uid, deleted_at FROM pages").one() == (
                uid,
                3_000_000,
            )
            assert connection.exec_driver_sql(
                "SELECT action, actor_caller_id, actor_home_library_id, master_audit_event_id "
                "FROM page_lifecycle_events"
            ).one() == ("delete", None, None, MASTER_EVENT)
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
    finally:
        engine.dispose()


def test_orphan_master_delete_audit_blocks_downgrade_without_schema_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "orphan.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, NEW_REVISION)
    engine = build_engine(url)
    try:
        library, _page_id, uid = _seed(engine)
        with immediate_transaction(engine) as connection:
            MasterAuditRepository(connection).add_success(
                identity_id="a" * 32,
                session_generation=1,
                session_fingerprint=b"f" * 32,
                action="content.archive.delete",
                target_type="page",
                target_id=f"{library}:{uid.hex()}",
                occurred_at=3_000_000,
                event_id=MASTER_EVENT,
            )
    finally:
        engine.dispose()
    before = _snapshot(path)
    with pytest.raises(RuntimeError, match="Cannot downgrade master-attributed Page deletion"):
        command.downgrade(config, OLD_REVISION)
    assert _snapshot(path) == before
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("SELECT version_num FROM alembic_version").fetchone() == (
            NEW_REVISION,
        )
        assert database.execute("SELECT count(*) FROM page_lifecycle_events").fetchone() == (0,)
        assert database.execute("SELECT count(*) FROM admin_master_audit_events").fetchone() == (1,)


@pytest.mark.parametrize(
    "invalid_audit",
    [
        {"audit_action": "content.archive.restore"},
        {"target_type": "library"},
        {"target_id": "1" * 32 + ":" + "0" * 32},
        {"audit_time": 3_000_001},
    ],
)
def test_master_delete_requires_exact_audit_and_rolls_back_invalid_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_audit: dict[str, str | int]
) -> None:
    path = tmp_path / "audit-invalid.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, NEW_REVISION)
    engine = build_engine(url)
    try:
        library, page_id, _uid = _seed(engine)
        with pytest.raises(IntegrityError):
            _transition(
                engine,
                library=library,
                page_id=page_id,
                action="delete",
                at=3_000_000,
                master=True,
                **invalid_audit,  # type: ignore[arg-type]
            )
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT deleted_at FROM pages").scalar_one() is None
            for table in (
                "admin_master_audit_events",
                "page_lifecycle_events",
                "page_lifecycle_guards",
            ):
                assert connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize("starting_revision", [OLD_REVISION, NEW_REVISION])
def test_pending_guard_refuses_migration_in_both_directions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, starting_revision: str
) -> None:
    path = tmp_path / "pending.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, starting_revision)
    engine = build_engine(url)
    try:
        library, _page_id, uid = _seed(engine)
        with engine.connect() as connection:
            section, occurrence = connection.exec_driver_sql(
                "SELECT section_id, occurred_at FROM pages"
            ).one()
    finally:
        engine.dispose()
    # Commit an unfinished guard only into a synthetic corrupt DB, bypassing its
    # deferred completion FK. Normal application transactions cannot commit it.
    with closing(sqlite3.connect(path)) as database:
        database.execute("PRAGMA foreign_keys = OFF")
        database.execute(
            "INSERT INTO page_lifecycle_guards "
            "(library_id, page_uid, sequence, action, section_id, old_deleted_at, "
            "old_updated_at, changed_at, at_revision_number, occurred_at_at_event, "
            "actor_caller_id, actor_home_library_id, request_id) "
            "VALUES (?, ?, 1, 'delete', ?, NULL, 2000000, 3000000, 1, ?, ?, ?, ?)",
            (library, uid, section, occurrence, CALLER, library, "req_" + "6" * 32),
        )
        database.commit()
    before = _snapshot(path)
    with pytest.raises(RuntimeError, match="pending guard"):
        if starting_revision == OLD_REVISION:
            command.upgrade(config, NEW_REVISION)
        else:
            command.downgrade(config, OLD_REVISION)
    assert _snapshot(path) == before
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("SELECT version_num FROM alembic_version").fetchone() == (
            starting_revision,
        )
        assert database.execute("SELECT count(*) FROM page_lifecycle_guards").fetchone() == (1,)


@pytest.mark.parametrize("starting_revision", [OLD_REVISION, NEW_REVISION])
def test_interrupted_trigger_replacement_is_transactional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, starting_revision: str
) -> None:
    path = tmp_path / "interrupted.sqlite"
    _, config = _config(path, monkeypatch)
    command.upgrade(config, starting_revision)
    before = _snapshot(path)
    original = cast(Callable[..., object], Connection.exec_driver_sql)

    def interrupt_after_drop(
        connection: Connection, statement: str, *args: object, **kwargs: object
    ) -> object:
        if statement.startswith(f"CREATE TRIGGER {TRIGGER}"):
            raise RuntimeError("synthetic trigger interruption")
        return original(connection, statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "exec_driver_sql", interrupt_after_drop)
        with pytest.raises(RuntimeError, match="synthetic trigger interruption"):
            if starting_revision == OLD_REVISION:
                command.upgrade(config, NEW_REVISION)
            else:
                command.downgrade(config, OLD_REVISION)
    assert _snapshot(path) == before
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("SELECT version_num FROM alembic_version").fetchone() == (
            starting_revision,
        )
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unexpected_source_trigger_refuses_upgrade_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "unexpected-trigger.sqlite"
    _, config = _config(path, monkeypatch)
    command.upgrade(config, OLD_REVISION)
    with closing(sqlite3.connect(path)) as database:
        source = database.execute(
            "SELECT sql FROM sqlite_schema WHERE name = ?", (TRIGGER,)
        ).fetchone()[0]
        database.execute(f"DROP TRIGGER {TRIGGER}")
        database.execute(source.replace("NEW.action IS NOT 'restore'", "NEW.action != 'restore'"))
        database.commit()
    before = _snapshot(path)
    with pytest.raises(RuntimeError, match="Unexpected master Page lifecycle trigger"):
        command.upgrade(config, NEW_REVISION)
    assert _snapshot(path) == before
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("SELECT version_num FROM alembic_version").fetchone() == (
            OLD_REVISION,
        )
