"""Synthetic-only 0027 master-attributed occurrence storage checks."""

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
from patchouli_lib.content.schemas import PageOccurrenceCorrectionCommand
from patchouli_lib.database import build_engine, immediate_transaction

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from content.helpers import (  # noqa: E402
    insert_page_graph,
    page_graph_values,
    seed_library_structure,
)

OLD_REVISION = "20261001_0026"
NEW_REVISION = "20261001_0027"
CALLER = "4" * 32
MASTER_AUDIT = "5" * 32


def _config(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return url, Config(str(ROOT / "alembic.ini"))


def _seed_page(engine: Engine) -> tuple[str, bytes, int]:
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
    return library_id, page.page_uid, page.occurred_at


def _guard(
    connection: Connection,
    *,
    library_id: str,
    page_uid: bytes,
    old: int,
    new: int,
    sequence: int,
    changed_at: int,
    actor: str | None,
    home: str | None,
    master: str | None = None,
    migrated: bool,
) -> None:
    master_column = ", master_audit_event_id" if migrated else ""
    master_value = ", :master" if migrated else ""
    connection.execute(
        text(
            "INSERT INTO page_occurrence_correction_guards "
            "(library_id, page_uid, sequence, old_occurred_at, new_occurred_at, "
            f"actor_caller_id, actor_home_library_id{master_column}, corrected_at) "
            "VALUES (:library, :uid, :sequence, :old, :new, "
            f":actor, :home{master_value}, :changed)"
        ),
        {
            "library": library_id,
            "uid": page_uid,
            "sequence": sequence,
            "old": old,
            "new": new,
            "actor": actor,
            "home": home,
            "master": master,
            "changed": changed_at,
        },
    )


def _correct(
    connection: Connection,
    *,
    library_id: str,
    page_uid: bytes,
    new: int,
    changed_at: int,
) -> None:
    connection.execute(
        text(
            "UPDATE pages SET occurred_at = :new, updated_at = :changed "
            "WHERE library_id = :library AND page_uid = :uid"
        ),
        {"library": library_id, "uid": page_uid, "new": new, "changed": changed_at},
    )


def _audit(
    connection: Connection,
    *,
    library_id: str,
    page_uid: bytes,
    changed_at: int,
    action: str = "content.page.occurrence.correct",
    target_type: str = "page",
    target_id: str | None = None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO admin_master_audit_events "
            "(id, identity_id, session_generation, session_fingerprint, action, "
            "target_type, target_id, occurred_at) "
            "VALUES (:id, :identity, 1, :fingerprint, :action, :type, :target, :at)"
        ),
        {
            "id": MASTER_AUDIT,
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


def test_empty_roundtrip_restores_exact_0026_schema(
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
            for table in ("page_occurrence_corrections", "page_occurrence_correction_guards"):
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
    finally:
        engine.dispose()


def test_agent_history_survives_roundtrip_and_master_history_blocks_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "history.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, OLD_REVISION)
    engine = build_engine(url)
    try:
        library, uid, original = _seed_page(engine)
        first = original + 1_000_000
        with immediate_transaction(engine) as connection:
            _guard(
                connection,
                library_id=library,
                page_uid=uid,
                old=original,
                new=first,
                sequence=1,
                changed_at=3_000_000,
                actor=CALLER,
                home=library,
                migrated=False,
            )
            _correct(connection, library_id=library, page_uid=uid, new=first, changed_at=3_000_000)
        with closing(sqlite3.connect(path)) as raw:
            old_columns = [
                row[1] for row in raw.execute("PRAGMA table_info(page_occurrence_corrections)")
            ]
            old_event = raw.execute("SELECT * FROM page_occurrence_corrections").fetchone()
        assert old_event is not None

        command.upgrade(config, NEW_REVISION)
        with closing(sqlite3.connect(path)) as raw:
            columns = [
                row[1] for row in raw.execute("PRAGMA table_info(page_occurrence_corrections)")
            ]
            event = raw.execute("SELECT * FROM page_occurrence_corrections").fetchone()
            assert event is not None
            assert tuple(event[columns.index(column)] for column in old_columns) == old_event
            assert event[columns.index("master_audit_event_id")] is None
        command.downgrade(config, OLD_REVISION)
        with closing(sqlite3.connect(path)) as raw:
            assert raw.execute("SELECT * FROM page_occurrence_corrections").fetchone() == old_event
            assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        command.upgrade(config, NEW_REVISION)

        second = first + 1_000_000
        with immediate_transaction(engine) as connection:
            _audit(connection, library_id=library, page_uid=uid, changed_at=4_000_000)
            _guard(
                connection,
                library_id=library,
                page_uid=uid,
                old=first,
                new=second,
                sequence=2,
                changed_at=4_000_000,
                actor=None,
                home=None,
                master=MASTER_AUDIT,
                migrated=True,
            )
            _correct(connection, library_id=library, page_uid=uid, new=second, changed_at=4_000_000)
            rows = connection.exec_driver_sql(
                "SELECT sequence, actor_caller_id, actor_home_library_id, "
                "master_audit_event_id FROM page_occurrence_corrections ORDER BY sequence"
            ).all()
            assert [tuple(row) for row in rows] == [
                (1, CALLER, library, None),
                (2, None, None, MASTER_AUDIT),
            ]
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM page_occurrence_correction_guards"
                ).scalar_one()
                == 0
            )
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.exec_driver_sql(
                    "UPDATE page_occurrence_corrections SET master_audit_event_id = NULL "
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


def test_master_guard_requires_exact_audit_and_strict_actor_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "guard.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, NEW_REVISION)
    engine = build_engine(url)
    try:
        library, uid, occurred = _seed_page(engine)
        with immediate_transaction(engine) as connection:
            _audit(connection, library_id=library, page_uid=uid, changed_at=3_000_000)
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
                        page_uid=uid,
                        old=occurred,
                        new=occurred + 1,
                        sequence=1,
                        changed_at=3_000_000,
                        actor=actor,
                        home=home,
                        master=master,
                        migrated=True,
                    )
            with pytest.raises(IntegrityError), connection.begin_nested():
                _guard(
                    connection,
                    library_id=library,
                    page_uid=uid,
                    old=occurred,
                    new=occurred + 1,
                    sequence=1,
                    changed_at=3_000_001,
                    actor=None,
                    home=None,
                    master=MASTER_AUDIT,
                    migrated=True,
                )
            with pytest.raises(IntegrityError), connection.begin_nested():
                _guard(
                    connection,
                    library_id=library,
                    page_uid=uid,
                    old=occurred,
                    new=occurred + 1,
                    sequence=1,
                    changed_at=3_000_000,
                    actor=None,
                    home=None,
                    master="c" * 32,
                    migrated=True,
                )
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM page_occurrence_correction_guards"
                ).scalar_one()
                == 0
            )
        for action, target_type, target_id in (
            ("content.archive.correct_occurrence", "page", None),
            ("content.page.occurrence.correct", "library", None),
            ("content.page.occurrence.correct", "page", "unrelated"),
        ):
            other_path = tmp_path / f"bad-{action}-{target_type}-{target_id}.sqlite"
            other_url, other_config = _config(other_path, monkeypatch)
            command.upgrade(other_config, NEW_REVISION)
            other_engine = build_engine(other_url)
            try:
                other_library, other_uid, other_occurred = _seed_page(other_engine)
                with immediate_transaction(other_engine) as connection:
                    _audit(
                        connection,
                        library_id=other_library,
                        page_uid=other_uid,
                        changed_at=3_000_000,
                        action=action,
                        target_type=target_type,
                        target_id=target_id,
                    )
                    with pytest.raises(IntegrityError), connection.begin_nested():
                        _guard(
                            connection,
                            library_id=other_library,
                            page_uid=other_uid,
                            old=other_occurred,
                            new=other_occurred + 1,
                            sequence=1,
                            changed_at=3_000_000,
                            actor=None,
                            home=None,
                            master=MASTER_AUDIT,
                            migrated=True,
                        )
            finally:
                other_engine.dispose()
    finally:
        engine.dispose()


def test_existing_agent_guard_still_records_an_immutable_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "agent-after-upgrade.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, NEW_REVISION)
    engine = build_engine(url)
    try:
        library, uid, occurred = _seed_page(engine)
        with immediate_transaction(engine) as connection:
            _guard(
                connection,
                library_id=library,
                page_uid=uid,
                old=occurred,
                new=occurred + 1,
                sequence=1,
                changed_at=3_000_000,
                actor=CALLER,
                home=library,
                migrated=True,
            )
            _correct(
                connection,
                library_id=library,
                page_uid=uid,
                new=occurred + 1,
                changed_at=3_000_000,
            )
            assert tuple(
                connection.exec_driver_sql(
                    "SELECT actor_caller_id, actor_home_library_id, master_audit_event_id "
                    "FROM page_occurrence_corrections"
                ).one()
            ) == (CALLER, library, None)
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.exec_driver_sql("DELETE FROM page_occurrence_corrections")
    finally:
        engine.dispose()


def test_schema_command_requires_one_actor_kind() -> None:
    base = {
        "library_id": "1" * 32,
        "page_uid": b"p" * 16,
        "old_occurred_at": 1,
        "new_occurred_at": 2,
        "corrected_at": 3,
    }
    assert (
        PageOccurrenceCorrectionCommand.model_validate(
            base | {"actor_caller_id": CALLER, "actor_home_library_id": "1" * 32}
        ).master_audit_event_id
        is None
    )
    assert (
        PageOccurrenceCorrectionCommand.model_validate(
            base | {"master_audit_event_id": MASTER_AUDIT}
        ).actor_caller_id
        is None
    )
    for fields in (
        {},
        {"actor_caller_id": CALLER},
        {
            "master_audit_event_id": MASTER_AUDIT,
            "actor_caller_id": CALLER,
            "actor_home_library_id": "1" * 32,
        },
    ):
        with pytest.raises(ValueError):
            PageOccurrenceCorrectionCommand.model_validate(base | fields)


@pytest.mark.parametrize("starting_revision", [OLD_REVISION, NEW_REVISION])
def test_pending_guard_refuses_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, starting_revision: str
) -> None:
    path = tmp_path / "pending.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, starting_revision)
    engine = build_engine(url)
    try:
        library, uid, occurred = _seed_page(engine)
    finally:
        engine.dispose()
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("PRAGMA foreign_keys = OFF")
        columns = ", master_audit_event_id" if starting_revision == NEW_REVISION else ""
        values = ", NULL" if starting_revision == NEW_REVISION else ""
        raw.execute(
            "INSERT INTO page_occurrence_correction_guards "
            "(library_id, page_uid, sequence, old_occurred_at, new_occurred_at, "
            f"actor_caller_id, actor_home_library_id{columns}, corrected_at) "
            f"VALUES (?, ?, 1, ?, ?, ?, ?{values}, 3000000)",
            (library, uid, occurred, occurred + 1, CALLER, library),
        )
        raw.commit()
    before = _snapshot(path)
    with pytest.raises(RuntimeError, match="pending Page occurrence guard"):
        if starting_revision == OLD_REVISION:
            command.upgrade(config, NEW_REVISION)
        else:
            command.downgrade(config, OLD_REVISION)
    assert _snapshot(path) == before


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
        if statement == "DROP TABLE page_occurrence_corrections":
            raise RuntimeError("synthetic occurrence rebuild interruption")
        return original(connection, statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "exec_driver_sql", interrupt_after_guard_drop)
        with pytest.raises(RuntimeError, match="synthetic occurrence rebuild interruption"):
            command.upgrade(config, NEW_REVISION)
    assert _snapshot(path) == before
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        assert raw.execute("SELECT version_num FROM alembic_version").fetchone() == (OLD_REVISION,)
