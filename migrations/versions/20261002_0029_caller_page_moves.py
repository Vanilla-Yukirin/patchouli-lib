"""Add real Caller attribution to the same immutable Page membership chain.

Revision ID: 20261002_0029
Revises: 20261001_0028

Old master events and receipts are copied verbatim; downgrade reconstructs the
exact original SQL only when no Caller movement success or audit would be lost.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from alembic import op

revision: str = "20261002_0029"
down_revision: str | None = "20261001_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENTS = "page_move_events"
_GUARDS = "page_move_guards"
_RECEIPTS = "admin_master_move_receipts"
_TRIGGERS = (
    "trg_page_move_guards_validate_insert",
    "trg_page_move_guards_no_update",
    "trg_page_move_guards_safe_delete",
    "trg_page_move_events_validate_insert",
    "trg_page_move_events_no_update",
    "trg_page_move_events_no_delete",
    "trg_pages_move_require_guard",
    "trg_pages_move_record",
    "trg_master_move_receipts_validate_insert",
    "trg_master_move_receipts_no_update",
    "trg_master_move_receipts_no_delete",
)
_ROUTE = "/api/v1/libraries/{library_id}/pages/{page_id}/move"
_ACTOR_CONSTRAINTS = (
    ", \n\tFOREIGN KEY(caller_audit_event_id) REFERENCES auth_audit_events (id) "
    "ON DELETE RESTRICT, "
    "\n\tUNIQUE (caller_audit_event_id), "
    "\n\tCHECK ((master_audit_event_id IS NOT NULL AND caller_audit_event_id IS NULL) OR "
    "(master_audit_event_id IS NULL AND caller_audit_event_id IS NOT NULL))\n)"
)


def _replace(source: str, old: str, new: str, count: int = 1) -> str:
    if source.count(old) != count:
        raise RuntimeError("Unexpected Page movement SQL prevents safe migration.")
    return source.replace(old, new)


def _table_sql(source: str, *, upgrading: bool) -> str:
    old = "\tmaster_audit_event_id VARCHAR(32) NOT NULL, \n"
    new = "\tmaster_audit_event_id VARCHAR(32), \n\tcaller_audit_event_id VARCHAR(32), \n"
    if upgrading:
        return _replace(_replace(source, old, new), "\n)", _ACTOR_CONSTRAINTS)
    return _replace(_replace(source, new, old), _ACTOR_CONSTRAINTS, "\n)")


def _trigger_sql(name: str, source: str, *, upgrading: bool) -> str:
    pairs: list[tuple[str, str, int]] = []
    if name == "trg_page_move_guards_validate_insert":
        pairs.extend(
            (
                (
                    "JOIN admin_master_audit_events a ON a.id = NEW.master_audit_event_id ",
                    "LEFT JOIN admin_master_audit_events a ON a.id = NEW.master_audit_event_id "
                    "LEFT JOIN auth_audit_events ca ON ca.id = NEW.caller_audit_event_id ",
                    1,
                ),
                (
                    "AND a.action = 'content.page.move' AND a.target_type = 'page' "
                    "AND a.target_id = NEW.library_id || ':' || lower(hex(NEW.page_uid)) "
                    "AND a.occurred_at = NEW.changed_at AND NOT EXISTS",
                    "AND ((NEW.master_audit_event_id IS NOT NULL "
                    "AND a.action = 'content.page.move' "
                    "AND a.target_type = 'page' AND a.target_id = NEW.library_id || ':' || "
                    "lower(hex(NEW.page_uid)) AND a.occurred_at = NEW.changed_at) OR "
                    "(NEW.caller_audit_event_id IS NOT NULL AND ca.library_id = NEW.library_id "
                    "AND ca.action = 'content.page.move' AND ca.resource_type = 'page' "
                    "AND ca.resource_id = p.page_id AND ca.outcome = 'succeeded' "
                    "AND ca.occurred_at = NEW.changed_at)) AND NOT EXISTS",
                    1,
                ),
                (
                    "WHERE e.master_audit_event_id = NEW.master_audit_event_id))",
                    "WHERE e.master_audit_event_id = NEW.master_audit_event_id "
                    "OR e.caller_audit_event_id = NEW.caller_audit_event_id))",
                    1,
                ),
            )
        )
    elif name == "trg_page_move_events_validate_insert":
        pairs.extend(
            (
                (
                    "OR e.master_audit_event_id = NEW.master_audit_event_id)",
                    "OR e.master_audit_event_id = NEW.master_audit_event_id "
                    "OR e.caller_audit_event_id = NEW.caller_audit_event_id)",
                    1,
                ),
                (
                    "g.master_audit_event_id = NEW.master_audit_event_id",
                    "g.master_audit_event_id IS NEW.master_audit_event_id "
                    "AND g.caller_audit_event_id IS NEW.caller_audit_event_id",
                    1,
                ),
            )
        )
    elif name == "trg_page_move_guards_safe_delete":
        pairs.append(
            (
                "e.master_audit_event_id = OLD.master_audit_event_id",
                "e.master_audit_event_id IS OLD.master_audit_event_id "
                "AND e.caller_audit_event_id IS OLD.caller_audit_event_id",
                1,
            )
        )
    elif name == "trg_pages_move_record":
        pairs.append(
            (
                "occurred_at_at_event, master_audit_event_id",
                "occurred_at_at_event, master_audit_event_id, caller_audit_event_id",
                2,
            )
        )
    for old, new, count in pairs:
        source = (
            _replace(source, old, new, count) if upgrading else _replace(source, new, old, count)
        )
    return source


def _rebuild(*, upgrading: bool) -> None:
    connection = op.get_bind()
    raw = connection.connection.driver_connection
    if not isinstance(raw, sqlite3.Connection):
        raise RuntimeError("Caller Page movement requires SQLite.")
    if raw.in_transaction:
        connection.commit()
    connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    expected = down_revision if upgrading else revision
    assert expected is not None
    if (
        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        != expected
    ):
        raise RuntimeError("Unexpected Page movement schema revision.")
    from patchouli_lib.backup.validation import (
        _require_idempotency_graph,
        _require_lifecycle_graph,
        _require_master_audit,
        _require_page_moves,
        _require_schema,
    )

    _require_schema(raw, expected)
    if (
        not upgrading
        and connection.exec_driver_sql(
            "SELECT 1 FROM admin_master_audit_events a WHERE a.action='content.page.move' "
            "AND NOT EXISTS (SELECT 1 FROM page_move_events e WHERE e.master_audit_event_id=a.id) "
            "LIMIT 1"
        ).first()
    ):
        # Preserve the old downgrade's explicit refusal, before the broader
        # historical graph check rejects this same inconsistent audit.
        raise RuntimeError("Cannot discard Page move audit history.")
    _require_lifecycle_graph(raw, expected)
    _require_idempotency_graph(raw, expected)
    _require_master_audit(raw, expected)
    _require_page_moves(raw, schema_revision=expected)
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Page movement relationships are inconsistent.")
    if not upgrading and (
        connection.exec_driver_sql(
            "SELECT 1 FROM page_move_events WHERE caller_audit_event_id IS NOT NULL LIMIT 1"
        ).first()
        is not None
        or connection.exec_driver_sql(
            "SELECT 1 FROM idempotency_records WHERE route_template = ? LIMIT 1", (_ROUTE,)
        ).first()
        is not None
        or connection.exec_driver_sql(
            "SELECT 1 FROM auth_audit_events WHERE action = 'content.page.move' LIMIT 1"
        ).first()
        is not None
    ):
        raise RuntimeError("Cannot discard Caller Page movement history or successes.")
    triggers = {
        name: sql
        for name, sql in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_schema WHERE type = 'trigger' AND "
            "(name LIKE 'trg_page_move_%' OR name LIKE 'trg_pages_move_%' "
            "OR name LIKE 'trg_master_move_%')"
        ).all()
    }
    if set(triggers) != set(_TRIGGERS):
        raise RuntimeError("Unexpected Page movement trigger inventory.")
    captured: dict[str, tuple[str, tuple[str, ...], int]] = {}
    for name in (_EVENTS, _GUARDS, _RECEIPTS):
        sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (name,)
        ).scalar_one()
        columns = tuple(row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({name})"))
        count = connection.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar_one()
        captured[name] = (sql, columns, count)
        connection.exec_driver_sql(f"CREATE TABLE __patchouli_0029_{name} AS SELECT * FROM {name}")
    for name in triggers:
        connection.exec_driver_sql(f"DROP TRIGGER {name}")
    # The receipt FK restricts dropping its event parent: remove and restore the
    # unchanged receipt table within this same locked transaction, with FKs ON.
    for name in (_GUARDS, _RECEIPTS, _EVENTS):
        connection.exec_driver_sql(f"DROP TABLE {name}")
    for name in (_EVENTS, _GUARDS, _RECEIPTS):
        sql, columns, count = captured[name]
        connection.exec_driver_sql(
            sql if name == _RECEIPTS else _table_sql(sql, upgrading=upgrading)
        )
        selected = tuple(c for c in columns if upgrading or c != "caller_audit_event_id")
        field_list = ", ".join(selected)
        connection.exec_driver_sql(
            f"INSERT INTO {name} ({field_list}) SELECT {field_list} FROM __patchouli_0029_{name}"
        )
        if connection.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar_one() != count:
            raise RuntimeError("Incomplete movement history copy.")
        connection.exec_driver_sql(f"DROP TABLE __patchouli_0029_{name}")
    for name, sql in triggers.items():
        connection.exec_driver_sql(_trigger_sql(name, sql, upgrading=upgrading))
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Movement history rebuild violated relationships.")


def upgrade() -> None:
    _rebuild(upgrading=True)


def downgrade() -> None:
    _rebuild(upgrading=False)
