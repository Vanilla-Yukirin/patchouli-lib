"""Separate the actor's home Library from the content target Library.

Revision ID: 20260929_0017
Revises: 20260929_0016
Create Date: 2026-09-29

This is a storage-only migration. Content services and backup validation must
be upgraded before this revision can be used by a running application.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence

from alembic import op

revision: str = "20260929_0017"
down_revision: str | None = "20260929_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "auth_audit_events",
    "idempotency_records",
    "page_occurrence_corrections",
    "page_occurrence_correction_guards",
    "page_lifecycle_events",
    "page_lifecycle_guards",
)
_DROP_ORDER = (
    "page_occurrence_correction_guards",
    "page_lifecycle_guards",
    "page_occurrence_corrections",
    "page_lifecycle_events",
    "auth_audit_events",
    "idempotency_records",
)
_CREATE_ORDER = (
    "auth_audit_events",
    "idempotency_records",
    "page_occurrence_corrections",
    "page_lifecycle_events",
    "page_occurrence_correction_guards",
    "page_lifecycle_guards",
)
_EXPECTED_TRIGGERS = frozenset(
    {
        "trg_idempotency_records_immutable_update",
        "trg_idempotency_records_no_delete",
        "trg_page_occurrence_guards_validate_insert",
        "trg_page_occurrence_guards_no_update",
        "trg_page_occurrence_guards_safe_delete",
        "trg_page_occurrence_corrections_validate_insert",
        "trg_page_occurrence_corrections_no_update",
        "trg_page_occurrence_corrections_no_delete",
        "trg_pages_occurrence_require_guard",
        "trg_pages_occurrence_record",
        "trg_page_lifecycle_guards_validate_insert",
        "trg_page_lifecycle_guards_no_update",
        "trg_page_lifecycle_guards_safe_delete",
        "trg_page_lifecycle_events_validate_insert",
        "trg_page_lifecycle_events_no_update",
        "trg_page_lifecycle_events_no_delete",
        "trg_pages_lifecycle_require_guard",
        "trg_pages_lifecycle_record",
    }
)
_EXPECTED_INDEXES = frozenset({"ix_auth_audit_events_library_request_id"})
_SHADOW_PREFIX = "__patchouli_0017_"


def _begin_rebuild() -> None:
    """Keep FK enforcement on throughout the leaf-table rebuild.

    Alembic may have just upgraded an empty database through earlier revisions.
    Their completed version stamps must be committed before our immediate
    transaction. A prior FK-off migration can leave this *migration connection*
    with enforcement disabled, so enable it outside that transaction. The two
    guard tables are required to be empty, leaving no live child rows during
    DROP TABLE's implicit delete of the six rebuilt tables.
    """

    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Actor-home migration requires SQLite.")
    if driver.in_transaction:
        connection.commit()
    connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
        raise RuntimeError("Actor-home migration requires enabled foreign keys.")
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def _check_foreign_keys() -> None:
    if op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Actor-home relationships are inconsistent.")


def _replace_once(value: str, old: str, new: str) -> str:
    if value.count(old) != 1:
        raise RuntimeError("Unexpected actor-home schema prevents a safe rebuild.")
    return value.replace(old, new, 1)


def _preflight(*, upgrading: bool) -> None:
    connection = op.get_bind()
    expected = down_revision if upgrading else revision
    version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    if version != expected:
        raise RuntimeError("Unexpected schema revision for actor-home rebuild.")
    _check_foreign_keys()
    for table in ("page_occurrence_correction_guards", "page_lifecycle_guards"):
        if connection.exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1").first() is not None:
            raise RuntimeError("Cannot rebuild with a pending Page transition guard.")
    for table in _TABLES:
        home = "library_id" if upgrading else "actor_home_library_id"
        actor = "caller_id" if table == "idempotency_records" else "actor_caller_id"
        mismatch = connection.exec_driver_sql(
            f"SELECT 1 FROM {table} AS row "
            f"LEFT JOIN auth_callers AS caller ON caller.id = row.{actor} "
            f"AND caller.library_id = row.{home} "
            "WHERE caller.id IS NULL LIMIT 1"
        ).first()
        if mismatch is not None:
            raise RuntimeError("An actor does not match its home Library.")
        if (
            not upgrading
            and connection.exec_driver_sql(
                f"SELECT 1 FROM {table} WHERE actor_home_library_id != library_id LIMIT 1"
            ).first()
            is not None
        ):
            raise RuntimeError("Cannot downgrade cross-Library actor history.")
    audit_mismatch = connection.exec_driver_sql(
        "SELECT 1 FROM auth_audit_events AS event "
        "LEFT JOIN auth_credentials AS credential ON credential.id = event.actor_credential_id "
        "AND credential.caller_id = event.actor_caller_id "
        "AND credential.library_id = event."
        f"{'library_id' if upgrading else 'actor_home_library_id'} "
        "WHERE credential.id IS NULL LIMIT 1"
    ).first()
    if audit_mismatch is not None:
        raise RuntimeError("An audit actor credential does not match its home Library.")


def _capture_tables() -> dict[str, tuple[str, tuple[str, ...], int]]:
    connection = op.get_bind()
    captured: dict[str, tuple[str, tuple[str, ...], int]] = {}
    for table in _TABLES:
        sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
        ).scalar_one()
        if not isinstance(sql, str):
            raise RuntimeError("Missing actor-home table SQL.")
        columns = tuple(row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})"))
        count = connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one()
        if type(count) is not int:
            raise RuntimeError("Invalid actor-home table count.")
        captured[table] = sql, columns, count
    return captured


def _capture_triggers() -> dict[str, str]:
    rows = op.get_bind().exec_driver_sql(
        "SELECT name, sql FROM sqlite_schema WHERE type = 'trigger'"
    )
    pattern = re.compile(r"\b(?:" + "|".join(_TABLES) + r")\b", re.ASCII)
    captured = {name: sql for name, sql in rows if sql and pattern.search(sql)}
    if set(captured) != _EXPECTED_TRIGGERS:
        raise RuntimeError("Unexpected actor-home trigger set prevents a safe rebuild.")
    return captured


def _capture_indexes() -> dict[str, str]:
    rows = op.get_bind().exec_driver_sql(
        "SELECT name, sql FROM sqlite_schema WHERE type = 'index' AND sql IS NOT NULL"
    )
    pattern = re.compile(r"\b(?:" + "|".join(_TABLES) + r")\b", re.ASCII)
    captured = {name: sql for name, sql in rows if sql and pattern.search(sql)}
    if set(captured) != _EXPECTED_INDEXES:
        raise RuntimeError("Unexpected actor-home index set prevents a safe rebuild.")
    return captured


def _table_sql(table: str, source: str, *, upgrading: bool) -> str:
    prefix = f"CREATE TABLE {table} ("
    quoted_prefix = f'CREATE TABLE "{table}" ('
    if source.startswith(prefix):
        source = prefix + source[len(prefix) :]
    elif source.startswith(quoted_prefix):
        source = prefix + source[len(quoted_prefix) :]
    else:
        raise RuntimeError("Unexpected actor-home table declaration.")

    column = "\tlibrary_id VARCHAR(32) NOT NULL, \n"
    addition = column + "\tactor_home_library_id VARCHAR(32) NOT NULL, \n"
    source = (
        _replace_once(source, column, addition)
        if upgrading
        else _replace_once(source, addition, column)
    )
    if table == "auth_audit_events":
        old_fk = "FOREIGN KEY(actor_credential_id, actor_caller_id, library_id)"
    elif table == "idempotency_records":
        old_fk = "FOREIGN KEY(caller_id, library_id) REFERENCES auth_callers"
    else:
        old_fk = "FOREIGN KEY(actor_caller_id, library_id) REFERENCES auth_callers"
    new_fk = old_fk.replace(", library_id)", ", actor_home_library_id)")
    source = (
        _replace_once(source, old_fk, new_fk)
        if upgrading
        else _replace_once(source, new_fk, old_fk)
    )
    if table in {"auth_audit_events", "idempotency_records"}:
        target_fk = (
            f", \n\tCONSTRAINT fk_{table}_target_library "
            "FOREIGN KEY(library_id) REFERENCES libraries (id) ON DELETE RESTRICT\n)"
        )
        source = (
            _replace_once(source, "\n)", target_fk)
            if upgrading
            else _replace_once(source, target_fk, "\n)")
        )
    return source


def _trigger_sql(name: str, source: str, *, upgrading: bool) -> str:
    changes: dict[str, tuple[tuple[str, str], ...]] = {
        "trg_page_occurrence_guards_safe_delete": (
            (
                "AND c.actor_caller_id = OLD.actor_caller_id ",
                "AND c.actor_caller_id = OLD.actor_caller_id "
                "AND c.actor_home_library_id = OLD.actor_home_library_id ",
            ),
        ),
        "trg_page_occurrence_corrections_validate_insert": (
            (
                "AND g.actor_caller_id = NEW.actor_caller_id ",
                "AND g.actor_caller_id = NEW.actor_caller_id "
                "AND g.actor_home_library_id = NEW.actor_home_library_id ",
            ),
        ),
        "trg_pages_occurrence_record": (
            (
                "at_revision_number, actor_caller_id, corrected_at)",
                "at_revision_number, actor_caller_id, actor_home_library_id, corrected_at)",
            ),
            ("g.actor_caller_id, ", "g.actor_caller_id, g.actor_home_library_id, "),
        ),
        "trg_page_lifecycle_guards_safe_delete": (
            (
                "AND e.actor_caller_id = OLD.actor_caller_id ",
                "AND e.actor_caller_id = OLD.actor_caller_id "
                "AND e.actor_home_library_id = OLD.actor_home_library_id ",
            ),
        ),
        "trg_page_lifecycle_events_validate_insert": (
            (
                "AND g.actor_caller_id = NEW.actor_caller_id ",
                "AND g.actor_caller_id = NEW.actor_caller_id "
                "AND g.actor_home_library_id = NEW.actor_home_library_id ",
            ),
        ),
        "trg_pages_lifecycle_record": (
            (
                "actor_caller_id, request_id)",
                "actor_caller_id, actor_home_library_id, request_id)",
            ),
            (
                "g.actor_caller_id, g.request_id",
                "g.actor_caller_id, g.actor_home_library_id, g.request_id",
            ),
        ),
    }
    for old, new in changes.get(name, ()):
        source = _replace_once(source, old, new) if upgrading else _replace_once(source, new, old)
    return source


def _rebuild(*, upgrading: bool) -> None:
    _begin_rebuild()
    _preflight(upgrading=upgrading)
    tables = _capture_tables()
    triggers = _capture_triggers()
    indexes = _capture_indexes()
    connection = op.get_bind()
    for name in triggers:
        connection.exec_driver_sql(f"DROP TRIGGER {name}")
    for table in _CREATE_ORDER:
        connection.exec_driver_sql(
            f'CREATE TABLE "{_SHADOW_PREFIX}{table}" AS SELECT * FROM {table}'
        )
        copied = connection.exec_driver_sql(
            f'SELECT count(*) FROM "{_SHADOW_PREFIX}{table}"'
        ).scalar_one()
        if copied != tables[table][2]:
            raise RuntimeError("Actor-home history copy was incomplete.")
    for table in _DROP_ORDER:
        connection.exec_driver_sql(f"DROP TABLE {table}")
    for table in _CREATE_ORDER:
        sql, columns, _count = tables[table]
        connection.exec_driver_sql(_table_sql(table, sql, upgrading=upgrading))
        source_columns = ", ".join(columns)
        if upgrading:
            connection.exec_driver_sql(
                f"INSERT INTO {table} ({source_columns}, actor_home_library_id) "
                f'SELECT {source_columns}, library_id FROM "{_SHADOW_PREFIX}{table}"'
            )
        else:
            target_columns = ", ".join(
                column for column in columns if column != "actor_home_library_id"
            )
            connection.exec_driver_sql(
                f"INSERT INTO {table} ({target_columns}) "
                f'SELECT {target_columns} FROM "{_SHADOW_PREFIX}{table}"'
            )
    for table in _CREATE_ORDER:
        connection.exec_driver_sql(f'DROP TABLE "{_SHADOW_PREFIX}{table}"')
    for sql in indexes.values():
        connection.exec_driver_sql(sql)
    for name, sql in triggers.items():
        connection.exec_driver_sql(_trigger_sql(name, sql, upgrading=upgrading))
    _check_foreign_keys()


def upgrade() -> None:
    _rebuild(upgrading=True)


def downgrade() -> None:
    _rebuild(upgrading=False)
