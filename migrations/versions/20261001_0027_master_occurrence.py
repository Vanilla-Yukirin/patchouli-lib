"""Attribute Page occurrence corrections to a Caller or a master audit event.

Revision ID: 20261001_0027
Revises: 20261001_0026
Create Date: 2026-10-01

Existing Caller history is copied without changing its values. A master-attributed
correction cannot be represented by 0026 and blocks downgrade.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from alembic import op

revision: str = "20261001_0027"
down_revision: str | None = "20261001_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENTS = "page_occurrence_corrections"
_GUARDS = "page_occurrence_correction_guards"
_SHADOW = "__patchouli_0027_page_occurrence_corrections"
_TRIGGERS = (
    "trg_page_occurrence_guards_validate_insert",
    "trg_page_occurrence_guards_no_update",
    "trg_page_occurrence_guards_safe_delete",
    "trg_page_occurrence_corrections_validate_insert",
    "trg_page_occurrence_corrections_no_update",
    "trg_page_occurrence_corrections_no_delete",
    "trg_pages_occurrence_require_guard",
    "trg_pages_occurrence_record",
)
_MASTER_TRIGGER = "trg_page_occurrence_guards_master_audit"
_OLD_EVENTS = (
    "library_id",
    "actor_home_library_id",
    "page_uid",
    "sequence",
    "old_occurred_at",
    "new_occurred_at",
    "at_revision_number",
    "actor_caller_id",
    "corrected_at",
)
_OLD_GUARDS = (
    "library_id",
    "actor_home_library_id",
    "page_uid",
    "sequence",
    "old_occurred_at",
    "new_occurred_at",
    "actor_caller_id",
    "corrected_at",
)


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError("Unexpected Page occurrence schema prevents a safe rebuild.")
    return source.replace(old, new, 1)


def _begin_rebuild() -> None:
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Master Page occurrence migration requires SQLite.")
    # Prior Alembic steps may leave an implicit transaction with FK enforcement
    # off. Commit the prior version stamp before enabling FKs and taking a lock.
    if driver.in_transaction:
        connection.commit()
    connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
        raise RuntimeError("Master Page occurrence migration requires enabled foreign keys.")
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def _check_foreign_keys() -> None:
    if op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Page occurrence relationships are inconsistent.")


def _preflight(*, upgrading: bool) -> None:
    connection = op.get_bind()
    expected = down_revision if upgrading else revision
    if (
        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        != expected
    ):
        raise RuntimeError("Unexpected schema revision for master Page occurrence rebuild.")
    if connection.exec_driver_sql(f"SELECT 1 FROM {_GUARDS} LIMIT 1").first() is not None:
        raise RuntimeError("Cannot rebuild with a pending Page occurrence guard.")
    _check_foreign_keys()
    if not upgrading and (
        connection.exec_driver_sql(
            f"SELECT 1 FROM {_EVENTS} WHERE master_audit_event_id IS NOT NULL LIMIT 1"
        ).first()
        is not None
        or connection.exec_driver_sql(
            "SELECT 1 FROM admin_master_audit_events "
            "WHERE action = 'content.page.occurrence.correct' LIMIT 1"
        ).first()
        is not None
    ):
        raise RuntimeError("Cannot downgrade master-attributed Page occurrence history.")


def _capture_table(name: str) -> tuple[str, tuple[str, ...], int]:
    connection = op.get_bind()
    sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (name,)
    ).scalar_one()
    if not isinstance(sql, str):
        raise RuntimeError("Missing Page occurrence table SQL.")
    columns = tuple(row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({name})"))
    count = connection.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar_one()
    if type(count) is not int:
        raise RuntimeError("Invalid Page occurrence table count.")
    return sql, columns, count


def _capture_triggers(*, upgrading: bool) -> dict[str, str]:
    rows = op.get_bind().exec_driver_sql(
        "SELECT name, sql FROM sqlite_schema WHERE type = 'trigger' "
        "AND (name LIKE 'trg_page_occurrence_%' OR name LIKE 'trg_pages_occurrence_%')"
    )
    captured = {name: sql for name, sql in rows if sql}
    expected = set(_TRIGGERS) | (set() if upgrading else {_MASTER_TRIGGER})
    if set(captured) != expected:
        raise RuntimeError("Unexpected Page occurrence trigger set prevents a safe rebuild.")
    return captured


def _table_sql(name: str, source: str, *, upgrading: bool) -> str:
    prefix = f"CREATE TABLE {name} ("
    quoted_prefix = f'CREATE TABLE "{name}" ('
    if source.startswith(quoted_prefix):
        source = prefix + source[len(quoted_prefix) :]
    elif not source.startswith(prefix):
        raise RuntimeError("Unexpected Page occurrence table declaration.")

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
        + (
            f"\tCONSTRAINT uq_{name}_master_audit UNIQUE (master_audit_event_id), \n"
            if name == _EVENTS
            else ""
        )
        + f"\tCONSTRAINT fk_{name}_master_audit "
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
        "trg_page_occurrence_guards_safe_delete": (
            (
                "AND c.actor_caller_id = OLD.actor_caller_id "
                "AND c.actor_home_library_id = OLD.actor_home_library_id ",
                "AND c.actor_caller_id IS OLD.actor_caller_id "
                "AND c.actor_home_library_id IS OLD.actor_home_library_id "
                "AND c.master_audit_event_id IS OLD.master_audit_event_id ",
            ),
        ),
        "trg_page_occurrence_corrections_validate_insert": (
            (
                "AND g.actor_caller_id = NEW.actor_caller_id "
                "AND g.actor_home_library_id = NEW.actor_home_library_id ",
                "AND g.actor_caller_id IS NEW.actor_caller_id "
                "AND g.actor_home_library_id IS NEW.actor_home_library_id "
                "AND g.master_audit_event_id IS NEW.master_audit_event_id ",
            ),
        ),
        "trg_pages_occurrence_record": (
            (
                "at_revision_number, actor_caller_id, actor_home_library_id, corrected_at)",
                "at_revision_number, actor_caller_id, actor_home_library_id, "
                "master_audit_event_id, corrected_at)",
            ),
            (
                "g.actor_caller_id, g.actor_home_library_id, NEW.updated_at",
                "g.actor_caller_id, g.actor_home_library_id, "
                "g.master_audit_event_id, NEW.updated_at",
            ),
        ),
    }
    for old, new in changes.get(name, ()):
        source = _replace_once(source, old, new) if upgrading else _replace_once(source, new, old)
    return source


def _create_master_guard_trigger() -> None:
    op.get_bind().exec_driver_sql(
        "CREATE TRIGGER trg_page_occurrence_guards_master_audit "
        "BEFORE INSERT ON page_occurrence_correction_guards "
        "WHEN NEW.master_audit_event_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM admin_master_audit_events AS audit "
        "WHERE audit.id = NEW.master_audit_event_id "
        "AND audit.action = 'content.page.occurrence.correct' "
        "AND audit.target_type = 'page' "
        "AND audit.target_id = NEW.library_id || ':' || lower(hex(NEW.page_uid)) "
        "AND audit.occurred_at = NEW.corrected_at) BEGIN "
        "SELECT RAISE(ABORT, 'Master Page occurrence audit is inconsistent.'); END"
    )


def _rebuild(*, upgrading: bool) -> None:
    _begin_rebuild()
    _preflight(upgrading=upgrading)
    events = _capture_table(_EVENTS)
    guards = _capture_table(_GUARDS)
    expected_events = (
        _OLD_EVENTS if upgrading else (*_OLD_EVENTS[:-1], "master_audit_event_id", _OLD_EVENTS[-1])
    )
    expected_guards = (
        _OLD_GUARDS if upgrading else (*_OLD_GUARDS[:-1], "master_audit_event_id", _OLD_GUARDS[-1])
    )
    if events[1] != expected_events or guards[1] != expected_guards:
        raise RuntimeError("Unexpected Page occurrence columns prevent a safe rebuild.")
    if guards[2] != 0:
        raise RuntimeError("Cannot rebuild with a pending Page occurrence guard.")
    triggers = _capture_triggers(upgrading=upgrading)
    connection = op.get_bind()

    connection.exec_driver_sql(f'CREATE TABLE "{_SHADOW}" AS SELECT * FROM {_EVENTS}')
    if connection.exec_driver_sql(f'SELECT count(*) FROM "{_SHADOW}"').scalar_one() != events[2]:
        raise RuntimeError("Page occurrence history copy was incomplete.")
    for name in triggers:
        connection.exec_driver_sql(f"DROP TRIGGER {name}")
    connection.exec_driver_sql(f"DROP TABLE {_GUARDS}")
    connection.exec_driver_sql(f"DROP TABLE {_EVENTS}")
    connection.exec_driver_sql(_table_sql(_EVENTS, events[0], upgrading=upgrading))
    if upgrading:
        old_columns = ", ".join(events[1])
        connection.exec_driver_sql(
            f"INSERT INTO {_EVENTS} ({old_columns}, master_audit_event_id) "
            f'SELECT {old_columns}, NULL FROM "{_SHADOW}"'
        )
    else:
        old_columns = ", ".join(column for column in events[1] if column != "master_audit_event_id")
        connection.exec_driver_sql(
            f'INSERT INTO {_EVENTS} ({old_columns}) SELECT {old_columns} FROM "{_SHADOW}"'
        )
    if connection.exec_driver_sql(f"SELECT count(*) FROM {_EVENTS}").scalar_one() != events[2]:
        raise RuntimeError("Page occurrence history restore was incomplete.")
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
