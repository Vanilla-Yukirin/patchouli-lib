"""Synthetic-only 0026 schema rollback and retained master history checks."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, event

from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.database import build_engine, immediate_transaction

ROOT = Path(__file__).resolve().parents[2]
OLD_REVISION = "20261001_0025"
NEW_REVISION = "20261001_0026"
TABLE = "admin_master_file_set_receipts"
UPDATE_TRIGGER = "trg_master_file_set_receipts_no_update"
DELETE_TRIGGER = "trg_master_file_set_receipts_no_delete"
RECEIPT_OBJECTS = frozenset({TABLE, UPDATE_TRIGGER, DELETE_TRIGGER})


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


def _assert_revision_and_foreign_keys(path: Path, revision: str) -> None:
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (revision,)
        ]
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []


def test_empty_0026_round_trip_restores_exact_0025_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty.sqlite"
    _, config = _config(path, monkeypatch)
    command.upgrade(config, OLD_REVISION)
    before = _snapshot(path)

    command.upgrade(config, NEW_REVISION)
    after = _snapshot(path)
    assert {name for _type, name, _sql in after} - {
        name for _type, name, _sql in before
    } == RECEIPT_OBJECTS
    assert [row for row in after if row[1] not in RECEIPT_OBJECTS] == before
    _assert_revision_and_foreign_keys(path, NEW_REVISION)
    with closing(sqlite3.connect(path)) as database:
        assert database.execute(f"SELECT count(*) FROM {TABLE}").fetchone() == (0,)

    command.downgrade(config, OLD_REVISION)
    assert _snapshot(path) == before
    _assert_revision_and_foreign_keys(path, OLD_REVISION)


@pytest.mark.parametrize(
    "starting_revision,interrupted_statement,remaining_triggers",
    [
        (OLD_REVISION, f"CREATE TRIGGER {UPDATE_TRIGGER}", frozenset({UPDATE_TRIGGER})),
        (
            OLD_REVISION,
            f"CREATE TRIGGER {DELETE_TRIGGER}",
            frozenset({UPDATE_TRIGGER, DELETE_TRIGGER}),
        ),
        (NEW_REVISION, f"DROP TRIGGER {DELETE_TRIGGER}", frozenset({UPDATE_TRIGGER})),
        (NEW_REVISION, f"DROP TRIGGER {UPDATE_TRIGGER}", frozenset()),
    ],
)
def test_trigger_ddl_interruption_rolls_back_actual_schema_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    starting_revision: str,
    interrupted_statement: str,
    remaining_triggers: frozenset[str],
) -> None:
    path = tmp_path / "interrupted.sqlite"
    _, config = _config(path, monkeypatch)
    command.upgrade(config, starting_revision)
    before = _snapshot(path)
    observed: list[frozenset[str]] = []

    def interrupt_after_ddl(
        connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if not statement.lstrip().startswith(interrupted_statement):
            return
        # Observe the executed DDL on the same real migration transaction,
        # rather than raising before any schema change has occurred.
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_schema WHERE type = 'table' AND name = ?", (TABLE,)
            ).scalar_one()
            == 1
        )
        observed.append(
            frozenset(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_schema WHERE type = 'trigger' AND name IN (?, ?)",
                    (UPDATE_TRIGGER, DELETE_TRIGGER),
                ).scalars()
            )
        )
        raise RuntimeError("synthetic receipt DDL interruption")

    event.listen(Engine, "after_cursor_execute", interrupt_after_ddl)
    try:
        with pytest.raises(RuntimeError, match="synthetic receipt DDL interruption"):
            if starting_revision == OLD_REVISION:
                command.upgrade(config, NEW_REVISION)
            else:
                command.downgrade(config, OLD_REVISION)
    finally:
        event.remove(Engine, "after_cursor_execute", interrupt_after_ddl)

    assert observed == [remaining_triggers]
    assert _snapshot(path) == before
    _assert_revision_and_foreign_keys(path, starting_revision)


@pytest.mark.parametrize("operation", ["create", "revise"])
def test_orphan_master_file_set_audit_blocks_downgrade_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    path = tmp_path / "orphan-audit.sqlite"
    url, config = _config(path, monkeypatch)
    command.upgrade(config, NEW_REVISION)
    engine = build_engine(url)
    try:
        # Deliberately incomplete synthetic history: no content or receipt.
        # Even this audit must not be discarded by withdrawing 0026.
        with immediate_transaction(engine) as connection:
            MasterAuditRepository(connection).add_success(
                identity_id="a" * 32,
                session_generation=1,
                session_fingerprint=b"f" * 32,
                action=f"content.page.file_set.{operation}",
                target_type="page",
                target_id="b" * 32 + ":" + "c" * 32,
                occurred_at=1_000_000,
                event_id="d" * 32,
            )
    finally:
        engine.dispose()
    before = _snapshot(path)
    with closing(sqlite3.connect(path)) as database:
        assert database.execute(f"SELECT count(*) FROM {TABLE}").fetchone() == (0,)
        audits_before = database.execute("SELECT * FROM admin_master_audit_events").fetchall()
        assert len(audits_before) == 1

    with pytest.raises(RuntimeError, match="Cannot discard master file-set write history"):
        command.downgrade(config, OLD_REVISION)
    assert _snapshot(path) == before
    _assert_revision_and_foreign_keys(path, NEW_REVISION)
    with closing(sqlite3.connect(path)) as database:
        assert database.execute(f"SELECT count(*) FROM {TABLE}").fetchone() == (0,)
        assert (
            database.execute("SELECT * FROM admin_master_audit_events").fetchall() == audits_before
        )
