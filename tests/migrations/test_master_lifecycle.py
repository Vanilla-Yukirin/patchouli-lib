"""Synthetic-only checks for master-attributed Page lifecycle storage."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller
from patchouli_lib.database import build_engine, immediate_transaction

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from content.helpers import (  # noqa: E402
    insert_page_graph,
    page_graph_values,
    seed_library_structure,
)

OLD_REVISION = "20260929_0018"
NEW_REVISION = "20260930_0019"
CALLER = "4" * 32
MASTER_AUDIT = "5" * 32
REQUEST = "req_" + "6" * 32


def _config(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return url, Config(str(ROOT / "alembic.ini"))


def _seed_page(engine: Engine) -> tuple[str, str, bytes, int]:
    library_id, section_id, book_id = seed_library_structure(engine)
    values = page_graph_values(library_id=library_id, section_id=section_id, book_id=book_id)
    page = values[0]
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, values)
        AuthRepository(connection).add_caller(
            NewCaller(
                id=CALLER,
                library_id=library_id,
                kind=CallerKind.AGENT,
                name="Synthetic Actor",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
    return library_id, section_id, page.page_uid, page.occurred_at


def _guard(
    connection: object,
    *,
    library_id: str,
    section_id: str,
    page_uid: bytes,
    occurred_at: int,
    sequence: int,
    action: str,
    old_deleted_at: int | None,
    old_updated_at: int,
    changed_at: int,
    actor: str | None,
    home: str | None,
    master: str | None = None,
    new: bool,
) -> None:
    from sqlalchemy import Connection

    assert isinstance(connection, Connection)
    master_column = ", master_audit_event_id" if new else ""
    master_value = ", :master" if new else ""
    connection.execute(
        text(
            "INSERT INTO page_lifecycle_guards "
            "(library_id, page_uid, sequence, action, section_id, old_deleted_at, "
            "old_updated_at, changed_at, at_revision_number, occurred_at_at_event, "
            f"actor_caller_id, actor_home_library_id{master_column}, request_id) "
            "VALUES (:library, :uid, :sequence, :action, :section, :old_deleted, "
            ":old_updated, :changed, 1, :occurred, "
            f":actor, :home{master_value}, :request)"
        ),
        {
            "library": library_id,
            "uid": page_uid,
            "sequence": sequence,
            "action": action,
            "section": section_id,
            "old_deleted": old_deleted_at,
            "old_updated": old_updated_at,
            "changed": changed_at,
            "occurred": occurred_at,
            "actor": actor,
            "home": home,
            "master": master,
            "request": REQUEST,
        },
    )


def _page_transition(
    connection: object,
    *,
    library_id: str,
    page_uid: bytes,
    changed_at: int,
    deleted_at: int | None,
) -> None:
    from sqlalchemy import Connection

    assert isinstance(connection, Connection)
    connection.execute(
        text(
            "UPDATE pages SET updated_at = :changed, deleted_at = :deleted "
            "WHERE library_id = :library AND page_uid = :uid"
        ),
        {
            "changed": changed_at,
            "deleted": deleted_at,
            "library": library_id,
            "uid": page_uid,
        },
    )


def _audit(
    connection: object,
    *,
    event_id: str,
    library_id: str,
    page_uid: bytes,
    changed_at: int,
    action: str = "content.archive.restore",
    target_type: str = "page",
    target_id: str | None = None,
) -> None:
    from sqlalchemy import Connection

    assert isinstance(connection, Connection)
    connection.execute(
        text(
            "INSERT INTO admin_master_audit_events "
            "(id, identity_id, session_generation, session_fingerprint, action, "
            "target_type, target_id, occurred_at) "
            "VALUES (:id, :identity, 1, :fingerprint, :action, :type, :target, :at)"
        ),
        {
            "id": event_id,
            "identity": "a" * 32,
            "fingerprint": b"f" * 32,
            "action": action,
            "type": target_type,
            "target": target_id or f"{library_id}:{page_uid.hex()}",
            "at": changed_at,
        },
    )


def _snapshot(path: Path) -> list[tuple[str, str, str]]:
    with closing(sqlite3.connect(path)) as raw:
        return raw.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' "
            "ORDER BY type, name"
        ).fetchall()


def test_0019_empty_roundtrip_restores_exact_0018_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, OLD_REVISION)
    old_schema = _snapshot(path)
    command.upgrade(config, NEW_REVISION)
    engine = build_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
            for table in ("page_lifecycle_events", "page_lifecycle_guards"):
                columns = {
                    row[1]: row[3]
                    for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
                }
                assert columns["actor_caller_id"] == 0
                assert columns["actor_home_library_id"] == 0
                assert columns["master_audit_event_id"] == 0
                assert any(
                    row[2] == "admin_master_audit_events"
                    for row in connection.exec_driver_sql(f"PRAGMA foreign_key_list({table})")
                )
        command.downgrade(config, OLD_REVISION)
        assert _snapshot(path) == old_schema
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == OLD_REVISION
            )
    finally:
        engine.dispose()


def test_old_history_is_exact_and_master_restore_is_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "history.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, OLD_REVISION)
    engine = build_engine(url)
    try:
        library, section, uid, occurred = _seed_page(engine)
        with immediate_transaction(engine) as connection:
            _guard(
                connection,
                library_id=library,
                section_id=section,
                page_uid=uid,
                occurred_at=occurred,
                sequence=1,
                action="delete",
                old_deleted_at=None,
                old_updated_at=2_000_000,
                changed_at=3_000_000,
                actor=CALLER,
                home=library,
                new=False,
            )
            _page_transition(
                connection,
                library_id=library,
                page_uid=uid,
                changed_at=3_000_000,
                deleted_at=3_000_000,
            )
        with closing(sqlite3.connect(path)) as raw:
            old_columns = [
                row[1] for row in raw.execute("PRAGMA table_info(page_lifecycle_events)")
            ]
            old_event = raw.execute("SELECT * FROM page_lifecycle_events").fetchone()
        assert old_event is not None

        command.upgrade(config, NEW_REVISION)
        with closing(sqlite3.connect(path)) as raw:
            columns = [row[1] for row in raw.execute("PRAGMA table_info(page_lifecycle_events)")]
            event = raw.execute("SELECT * FROM page_lifecycle_events").fetchone()
            assert event is not None
            assert tuple(event[columns.index(column)] for column in old_columns) == old_event
            assert event[columns.index("master_audit_event_id")] is None

        command.downgrade(config, OLD_REVISION)
        with closing(sqlite3.connect(path)) as raw:
            assert raw.execute("SELECT * FROM page_lifecycle_events").fetchone() == old_event
            assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        command.upgrade(config, NEW_REVISION)

        with immediate_transaction(engine) as connection:
            _audit(
                connection,
                event_id=MASTER_AUDIT,
                library_id=library,
                page_uid=uid,
                changed_at=4_000_000,
            )
            _guard(
                connection,
                library_id=library,
                section_id=section,
                page_uid=uid,
                occurred_at=occurred,
                sequence=2,
                action="restore",
                old_deleted_at=3_000_000,
                old_updated_at=3_000_000,
                changed_at=4_000_000,
                actor=None,
                home=None,
                master=MASTER_AUDIT,
                new=True,
            )
            _page_transition(
                connection,
                library_id=library,
                page_uid=uid,
                changed_at=4_000_000,
                deleted_at=None,
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM page_lifecycle_guards"
                ).scalar_one()
                == 0
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
            rows = connection.exec_driver_sql(
                "SELECT sequence, actor_caller_id, actor_home_library_id, "
                "master_audit_event_id FROM page_lifecycle_events ORDER BY sequence"
            ).all()
            assert [tuple(row) for row in rows] == [
                (1, CALLER, library, None),
                (2, None, None, MASTER_AUDIT),
            ]
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.exec_driver_sql(
                    "UPDATE page_lifecycle_events SET master_audit_event_id = NULL "
                    "WHERE sequence = 2"
                )
        with pytest.raises(RuntimeError, match="Cannot downgrade master-attributed"):
            command.downgrade(config, OLD_REVISION)
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == NEW_REVISION
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
    finally:
        engine.dispose()


def test_actor_source_and_master_audit_contract_reject_invalid_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "invalid.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, NEW_REVISION)
    engine = build_engine(url)
    try:
        library, section, uid, occurred = _seed_page(engine)
        with immediate_transaction(engine) as connection:
            _guard(
                connection,
                library_id=library,
                section_id=section,
                page_uid=uid,
                occurred_at=occurred,
                sequence=1,
                action="delete",
                old_deleted_at=None,
                old_updated_at=2_000_000,
                changed_at=3_000_000,
                actor=CALLER,
                home=library,
                new=True,
            )
            _page_transition(
                connection,
                library_id=library,
                page_uid=uid,
                changed_at=3_000_000,
                deleted_at=3_000_000,
            )
            _audit(
                connection,
                event_id=MASTER_AUDIT,
                library_id=library,
                page_uid=uid,
                changed_at=4_000_000,
            )
            for actor, home, master in (
                (None, None, None),
                (CALLER, None, None),
                (None, library, None),
                (CALLER, library, MASTER_AUDIT),
            ):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    _guard(
                        connection,
                        library_id=library,
                        section_id=section,
                        page_uid=uid,
                        occurred_at=occurred,
                        sequence=2,
                        action="restore",
                        old_deleted_at=3_000_000,
                        old_updated_at=3_000_000,
                        changed_at=4_000_000,
                        actor=actor,
                        home=home,
                        master=master,
                        new=True,
                    )
            bad_audits: tuple[tuple[str, str, str, str | None, int], ...] = (
                ("7" * 32, "content.archive.delete", "page", None, 4_000_000),
                ("8" * 32, "content.archive.restore", "library", None, 4_000_000),
                ("9" * 32, "content.archive.restore", "page", "unrelated", 4_000_000),
                ("b" * 32, "content.archive.restore", "page", None, 4_000_001),
            )
            for audit_id, action, target_type, target_id, changed_at in bad_audits:
                _audit(
                    connection,
                    event_id=audit_id,
                    library_id=library,
                    page_uid=uid,
                    changed_at=changed_at,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                )
                with pytest.raises(IntegrityError), connection.begin_nested():
                    _guard(
                        connection,
                        library_id=library,
                        section_id=section,
                        page_uid=uid,
                        occurred_at=occurred,
                        sequence=2,
                        action="restore",
                        old_deleted_at=3_000_000,
                        old_updated_at=3_000_000,
                        changed_at=4_000_000,
                        actor=None,
                        home=None,
                        master=audit_id,
                        new=True,
                    )
            with pytest.raises(IntegrityError), connection.begin_nested():
                _guard(
                    connection,
                    library_id=library,
                    section_id=section,
                    page_uid=uid,
                    occurred_at=occurred,
                    sequence=2,
                    action="restore",
                    old_deleted_at=3_000_000,
                    old_updated_at=3_000_000,
                    changed_at=4_000_000,
                    actor=None,
                    home=None,
                    master="c" * 32,
                    new=True,
                )
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM page_lifecycle_guards"
                ).scalar_one()
                == 0
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("starting_revision", [OLD_REVISION, NEW_REVISION])
def test_pending_guard_refuses_rebuild_without_schema_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, starting_revision: str
) -> None:
    path = tmp_path / "pending.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, starting_revision)
    engine = build_engine(url)
    try:
        library, section, uid, occurred = _seed_page(engine)
    finally:
        engine.dispose()
    # A committed pending guard cannot arise with FK enforcement because its
    # deferred completion FK needs the event. Inject only into a synthetic DB.
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute(
            "INSERT INTO page_lifecycle_guards "
            "(library_id, page_uid, sequence, action, section_id, old_deleted_at, "
            "old_updated_at, changed_at, at_revision_number, occurred_at_at_event, "
            "actor_caller_id, actor_home_library_id, request_id) "
            "VALUES (?, ?, 1, 'delete', ?, NULL, 2000000, 3000000, 1, ?, ?, ?, ?)",
            (library, uid, section, occurred, CALLER, library, REQUEST),
        )
        raw.commit()
    before = _snapshot(path)
    target_revision = NEW_REVISION if starting_revision == OLD_REVISION else OLD_REVISION
    with pytest.raises(RuntimeError, match="pending Page lifecycle guard"):
        if starting_revision == OLD_REVISION:
            command.upgrade(config, target_revision)
        else:
            command.downgrade(config, target_revision)
    assert _snapshot(path) == before
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("SELECT version_num FROM alembic_version").fetchone() == (
            starting_revision,
        )


def test_interrupted_rebuild_rolls_back_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "interrupted.sqlite"
    _, config = _config(path, monkeypatch)
    command.upgrade(config, OLD_REVISION)
    before = _snapshot(path)
    original = cast(Callable[..., object], Connection.exec_driver_sql)

    def interrupt_after_guard_drop(
        connection: Connection, statement: str, *args: object, **kwargs: object
    ) -> object:
        if statement == "DROP TABLE page_lifecycle_events":
            raise RuntimeError("synthetic rebuild interruption")
        return original(connection, statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "exec_driver_sql", interrupt_after_guard_drop)
        with pytest.raises(RuntimeError, match="synthetic rebuild interruption"):
            command.upgrade(config, NEW_REVISION)
    assert _snapshot(path) == before
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        assert raw.execute("SELECT version_num FROM alembic_version").fetchone() == (OLD_REVISION,)
