"""Attribute Page lifecycle restores to either a Caller or a master audit event.

Revision ID: 20260930_0019
Revises: 20260929_0018
Create Date: 2026-09-30

The rebuild keeps existing Caller history unchanged. Master-attributed history
cannot be represented by 0018, so its downgrade is intentionally refused.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from alembic import op

revision: str = "20260930_0019"
down_revision: str | None = "20260929_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENTS = "page_lifecycle_events"
_GUARDS = "page_lifecycle_guards"
_SHADOW = "__patchouli_0019_page_lifecycle_events"
_TRIGGERS = (
    "trg_page_lifecycle_guards_validate_insert",
    "trg_page_lifecycle_guards_no_update",
    "trg_page_lifecycle_guards_safe_delete",
    "trg_page_lifecycle_events_validate_insert",
    "trg_page_lifecycle_events_no_update",
    "trg_page_lifecycle_events_no_delete",
    "trg_pages_lifecycle_require_guard",
    "trg_pages_lifecycle_record",
)
_MASTER_TRIGGER = "trg_page_lifecycle_guards_master_audit"
_OLD_COLUMNS = (
    "library_id",
    "actor_home_library_id",
    "page_uid",
    "sequence",
    "action",
    "section_id",
    "old_deleted_at",
    "old_updated_at",
    "changed_at",
    "at_revision_number",
    "occurred_at_at_event",
    "actor_caller_id",
    "request_id",
)
_NEW_COLUMNS = _OLD_COLUMNS[:-1] + ("master_audit_event_id", "request_id")


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError("Unexpected Page lifecycle schema prevents a safe rebuild.")
    return source.replace(old, new, 1)


def _begin_rebuild() -> None:
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Master Page lifecycle migration requires SQLite.")
    # Earlier revisions can leave this Alembic connection inside a transaction
    # with foreign keys disabled. PRAGMA must be set before BEGIN IMMEDIATE.
    if driver.in_transaction:
        connection.commit()
    connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
        raise RuntimeError("Master Page lifecycle migration requires enabled foreign keys.")
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def _check_foreign_keys() -> None:
    if op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Page lifecycle relationships are inconsistent.")


def _preflight(*, upgrading: bool) -> None:
    connection = op.get_bind()
    expected = down_revision if upgrading else revision
    version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    if version != expected:
        raise RuntimeError("Unexpected schema revision for master Page lifecycle rebuild.")
    if connection.exec_driver_sql(f"SELECT 1 FROM {_GUARDS} LIMIT 1").first() is not None:
        raise RuntimeError("Cannot rebuild with a pending Page lifecycle guard.")
    _check_foreign_keys()
    if not upgrading:
        if (
            connection.exec_driver_sql(
                f"SELECT 1 FROM {_EVENTS} WHERE master_audit_event_id IS NOT NULL LIMIT 1"
            ).first()
            is not None
        ):
            raise RuntimeError("Cannot downgrade master-attributed Page lifecycle history.")
        if (
            connection.exec_driver_sql(
                f"SELECT 1 FROM {_EVENTS} WHERE actor_caller_id IS NULL "
                "OR actor_home_library_id IS NULL LIMIT 1"
            ).first()
            is not None
        ):
            raise RuntimeError("Cannot downgrade Page lifecycle history without a Caller.")


def _capture_table(name: str) -> tuple[str, tuple[str, ...], int]:
    connection = op.get_bind()
    sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (name,)
    ).scalar_one()
    if not isinstance(sql, str):
        raise RuntimeError("Missing Page lifecycle table SQL.")
    columns = tuple(row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({name})"))
    count = connection.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar_one()
    if type(count) is not int:
        raise RuntimeError("Invalid Page lifecycle table count.")
    return sql, columns, count


def _capture_triggers(*, upgrading: bool) -> dict[str, str]:
    names = (*_TRIGGERS, *(() if upgrading else (_MASTER_TRIGGER,)))
    rows = op.get_bind().exec_driver_sql(
        "SELECT name, sql FROM sqlite_schema WHERE type = 'trigger' "
        "AND (name LIKE 'trg_page_lifecycle_%' OR name LIKE 'trg_pages_lifecycle_%')"
    )
    captured = {name: sql for name, sql in rows if sql}
    # Two other Page triggers (initial-live and no-content-while-deleted) do
    # not refer to either rebuilt table and must remain untouched.
    untouched = {
        "trg_pages_lifecycle_initial_live",
        "trg_pages_lifecycle_no_content_while_deleted",
    }
    if set(captured) != set(names) | untouched:
        raise RuntimeError("Unexpected Page lifecycle trigger set prevents a safe rebuild.")
    return {name: captured[name] for name in names}


def _table_sql(name: str, source: str, *, upgrading: bool) -> str:
    prefix = f"CREATE TABLE {name} ("
    quoted_prefix = f'CREATE TABLE "{name}" ('
    if source.startswith(quoted_prefix):
        source = prefix + source[len(quoted_prefix) :]
    elif not source.startswith(prefix):
        raise RuntimeError("Unexpected Page lifecycle table declaration.")

    old_home = "\tactor_home_library_id VARCHAR(32) NOT NULL, \n"
    new_home = "\tactor_home_library_id VARCHAR(32), \n"
    old_actor = "\tactor_caller_id VARCHAR(32) NOT NULL, \n"
    new_actor = "\tactor_caller_id VARCHAR(32), \n\tmaster_audit_event_id VARCHAR(32), \n"
    constraints = (
        f", \n\tCONSTRAINT ck_{name}_actor_kind CHECK ("
        "(actor_caller_id IS NOT NULL AND actor_home_library_id IS NOT NULL "
        "AND master_audit_event_id IS NULL) OR "
        "(actor_caller_id IS NULL AND actor_home_library_id IS NULL "
        "AND master_audit_event_id IS NOT NULL)), \n"
        f"\tCONSTRAINT fk_{name}_master_audit "
        "FOREIGN KEY(master_audit_event_id) REFERENCES admin_master_audit_events (id) "
        "ON DELETE RESTRICT\n)"
    )
    if upgrading:
        source = _replace_once(source, old_home, new_home)
        source = _replace_once(source, old_actor, new_actor)
        return _replace_once(source, "\n)", constraints)
    source = _replace_once(source, new_home, old_home)
    source = _replace_once(source, new_actor, old_actor)
    return _replace_once(source, constraints, "\n)")


def _trigger_sql(name: str, source: str, *, upgrading: bool) -> str:
    changes: dict[str, tuple[tuple[str, str], ...]] = {
        "trg_page_lifecycle_guards_safe_delete": (
            (
                "AND e.actor_caller_id = OLD.actor_caller_id "
                "AND e.actor_home_library_id = OLD.actor_home_library_id ",
                "AND e.actor_caller_id IS OLD.actor_caller_id "
                "AND e.actor_home_library_id IS OLD.actor_home_library_id "
                "AND e.master_audit_event_id IS OLD.master_audit_event_id ",
            ),
        ),
        "trg_page_lifecycle_events_validate_insert": (
            (
                "AND g.actor_caller_id = NEW.actor_caller_id "
                "AND g.actor_home_library_id = NEW.actor_home_library_id ",
                "AND g.actor_caller_id IS NEW.actor_caller_id "
                "AND g.actor_home_library_id IS NEW.actor_home_library_id "
                "AND g.master_audit_event_id IS NEW.master_audit_event_id ",
            ),
        ),
        "trg_pages_lifecycle_record": (
            (
                "actor_caller_id, actor_home_library_id, request_id)",
                "actor_caller_id, actor_home_library_id, master_audit_event_id, request_id)",
            ),
            (
                "g.actor_caller_id, g.actor_home_library_id, g.request_id",
                "g.actor_caller_id, g.actor_home_library_id, g.master_audit_event_id, g.request_id",
            ),
        ),
    }
    for old, new in changes.get(name, ()):
        source = _replace_once(source, old, new) if upgrading else _replace_once(source, new, old)
    return source


def _create_master_guard_trigger() -> None:
    op.get_bind().exec_driver_sql(
        "CREATE TRIGGER trg_page_lifecycle_guards_master_audit "
        "BEFORE INSERT ON page_lifecycle_guards "
        "WHEN NEW.master_audit_event_id IS NOT NULL AND "
        "(NEW.action IS NOT 'restore' OR NOT EXISTS ("
        "SELECT 1 FROM admin_master_audit_events AS audit "
        "WHERE audit.id = NEW.master_audit_event_id "
        "AND audit.action = 'content.archive.restore' "
        "AND audit.target_type = 'page' "
        "AND audit.target_id = NEW.library_id || ':' || lower(hex(NEW.page_uid)) "
        "AND audit.occurred_at = NEW.changed_at)) BEGIN "
        "SELECT RAISE(ABORT, 'Master Page lifecycle audit is inconsistent.'); END"
    )


def _rebuild(*, upgrading: bool) -> None:
    _begin_rebuild()
    _preflight(upgrading=upgrading)
    events = _capture_table(_EVENTS)
    guards = _capture_table(_GUARDS)
    expected_columns = _OLD_COLUMNS if upgrading else _NEW_COLUMNS
    if events[1] != expected_columns or guards[1] != expected_columns:
        raise RuntimeError("Unexpected Page lifecycle columns prevent a safe rebuild.")
    if guards[2] != 0:
        raise RuntimeError("Cannot rebuild with a pending Page lifecycle guard.")
    triggers = _capture_triggers(upgrading=upgrading)
    connection = op.get_bind()

    connection.exec_driver_sql(f'CREATE TABLE "{_SHADOW}" AS SELECT * FROM {_EVENTS}')
    if connection.exec_driver_sql(f'SELECT count(*) FROM "{_SHADOW}"').scalar_one() != events[2]:
        raise RuntimeError("Page lifecycle history copy was incomplete.")
    for name in triggers:
        connection.exec_driver_sql(f"DROP TRIGGER {name}")
    connection.exec_driver_sql(f"DROP TABLE {_GUARDS}")
    connection.exec_driver_sql(f"DROP TABLE {_EVENTS}")
    connection.exec_driver_sql(_table_sql(_EVENTS, events[0], upgrading=upgrading))
    old_columns = ", ".join(events[1])
    if upgrading:
        connection.exec_driver_sql(
            f"INSERT INTO {_EVENTS} ({old_columns}, master_audit_event_id) "
            f'SELECT {old_columns}, NULL FROM "{_SHADOW}"'
        )
    else:
        target_columns = ", ".join(
            column for column in events[1] if column != "master_audit_event_id"
        )
        connection.exec_driver_sql(
            f'INSERT INTO {_EVENTS} ({target_columns}) SELECT {target_columns} FROM "{_SHADOW}"'
        )
    if connection.exec_driver_sql(f"SELECT count(*) FROM {_EVENTS}").scalar_one() != events[2]:
        raise RuntimeError("Page lifecycle history restore was incomplete.")
    connection.exec_driver_sql(_table_sql(_GUARDS, guards[0], upgrading=upgrading))
    connection.exec_driver_sql(f'DROP TABLE "{_SHADOW}"')
    for name, sql in triggers.items():
        if name != _MASTER_TRIGGER:
            connection.exec_driver_sql(_trigger_sql(name, sql, upgrading=upgrading))
    if upgrading:
        _create_master_guard_trigger()
    _check_foreign_keys()


def upgrade() -> None:
    _rebuild(upgrading=True)


def downgrade() -> None:
    _rebuild(upgrading=False)
