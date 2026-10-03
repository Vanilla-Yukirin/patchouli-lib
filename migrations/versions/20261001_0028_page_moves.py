"""Persist audited Page moves, consumed guards and frozen success receipts.

Revision ID: 20261001_0028
Revises: 20261001_0027
Create Date: 2026-10-01

An audit inserted on its own cannot be rejected at SQLite COMMIT by ordinary
triggers. Service precommit and backup validation enforce reverse coverage.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261001_0028"
down_revision: str | None = "20261001_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENTS = "page_move_events"
_GUARDS = "page_move_guards"
_RECEIPTS = "admin_master_move_receipts"
_FIELDS = (
    "library_id",
    "page_uid",
    "sequence",
    "old_section_id",
    "old_book_id",
    "new_section_id",
    "new_book_id",
    "old_updated_at",
    "changed_at",
    "at_revision_id",
    "at_revision_number",
    "occurred_at_at_event",
    "master_audit_event_id",
)
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


def _begin(*, upgrading: bool) -> sqlite3.Connection:
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Page move migration requires SQLite.")
    # 0027 rebuild may leave a driver transaction at the end of a fresh upgrade.
    if driver.in_transaction:
        connection.commit()
    connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
        raise RuntimeError("Page moves require enabled foreign keys.")
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    if connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() != (
        down_revision if upgrading else revision
    ):
        raise RuntimeError("Unexpected schema for Page moves.")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Page relationships are inconsistent.")
    for table in ("page_occurrence_correction_guards", "page_lifecycle_guards"):
        if connection.exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1").first():
            raise RuntimeError("Cannot migrate a pending Page mutation.")
    return driver


def _move_table(name: str, *, guard: bool) -> None:
    constraints: list[sa.schema.Constraint] = [
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid"], ["pages.library_id", "pages.page_uid"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "at_revision_id", "at_revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["master_audit_event_id"], ["admin_master_audit_events.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("master_audit_event_id"),
        sa.CheckConstraint("typeof(page_uid) = 'blob' AND length(page_uid) = 16"),
        sa.CheckConstraint(
            "typeof(sequence) = 'integer' AND sequence BETWEEN 1 AND 9223372036854775807"
        ),
        sa.CheckConstraint("old_section_id != new_section_id OR old_book_id != new_book_id"),
        sa.CheckConstraint(
            "typeof(old_updated_at) = 'integer' AND old_updated_at >= 0 "
            "AND typeof(changed_at) = 'integer' AND changed_at > old_updated_at "
            "AND changed_at <= 253402300799999999"
        ),
        sa.CheckConstraint(
            "typeof(at_revision_number) = 'integer' "
            "AND at_revision_number BETWEEN 1 AND 9223372036854775807 "
            "AND typeof(occurred_at_at_event) = 'integer' "
            "AND occurred_at_at_event BETWEEN -62135596800000000 AND 253402300799999999"
        ),
    ]
    for prefix in ("old", "new"):
        constraints.append(
            sa.ForeignKeyConstraint(
                [f"{prefix}_book_id", f"{prefix}_section_id", "library_id"],
                ["books.id", "books.section_id", "books.library_id"],
                ondelete="RESTRICT",
            )
        )
    if guard:
        constraints.append(
            sa.ForeignKeyConstraint(
                ["library_id", "page_uid", "sequence"],
                [f"{_EVENTS}.library_id", f"{_EVENTS}.page_uid", f"{_EVENTS}.sequence"],
                deferrable=True,
                initially="DEFERRED",
            )
        )
    op.create_table(
        name,
        sa.Column("library_id", sa.String(32), primary_key=True),
        sa.Column("page_uid", sa.LargeBinary(16), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), primary_key=not guard, nullable=False),
        *(
            sa.Column(field, sa.String(32), nullable=False)
            for field in ("old_section_id", "old_book_id", "new_section_id", "new_book_id")
        ),
        sa.Column("old_updated_at", sa.BigInteger(), nullable=False),
        sa.Column("changed_at", sa.BigInteger(), nullable=False),
        sa.Column("at_revision_id", sa.String(36), nullable=False),
        sa.Column("at_revision_number", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at_at_event", sa.BigInteger(), nullable=False),
        sa.Column("master_audit_event_id", sa.String(32), nullable=False),
        *constraints,
    )


def _receipt_table() -> None:
    op.create_table(
        _RECEIPTS,
        sa.Column("identity_id", sa.String(32), primary_key=True),
        sa.Column("operation", sa.String(4), primary_key=True),
        sa.Column("key_digest", sa.LargeBinary(32), primary_key=True),
        sa.Column("request_fingerprint", sa.LargeBinary(32), nullable=False),
        sa.Column("library_id", sa.String(32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(16), nullable=False),
        sa.Column("page_id", sa.String(80), nullable=False),
        *(
            sa.Column(field, sa.String(32), nullable=False)
            for field in (
                "source_section_id",
                "source_book_id",
                "target_section_id",
                "target_book_id",
            )
        ),
        sa.Column("revision_id", sa.String(36), nullable=False),
        *(
            sa.Column(field, sa.BigInteger(), nullable=False)
            for field in (
                "revision_number",
                "original_occurred_at",
                "original_page_updated_at",
                "result_updated_at",
            )
        ),
        sa.Column("request_etag", sa.String(100), nullable=False),
        sa.Column("response_etag", sa.String(100), nullable=False),
        sa.Column("operation_at", sa.BigInteger(), nullable=False),
        sa.Column("changed", sa.BigInteger(), nullable=False),
        sa.Column("move_sequence", sa.BigInteger()),
        sa.Column("master_audit_event_id", sa.String(32)),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            ondelete="RESTRICT",
        ),
        *(
            sa.ForeignKeyConstraint(
                [f"{prefix}_book_id", f"{prefix}_section_id", "library_id"],
                ["books.id", "books.section_id", "books.library_id"],
                ondelete="RESTRICT",
            )
            for prefix in ("source", "target")
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "move_sequence"],
            [f"{_EVENTS}.library_id", f"{_EVENTS}.page_uid", f"{_EVENTS}.sequence"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["master_audit_event_id"], ["admin_master_audit_events.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("library_id", "page_uid", "move_sequence"),
        sa.UniqueConstraint("master_audit_event_id"),
        sa.CheckConstraint("length(identity_id) = 32 AND identity_id NOT GLOB '*[^0-9a-f]*'"),
        sa.CheckConstraint("operation = 'move'"),
        sa.CheckConstraint(
            "typeof(key_digest) = 'blob' AND length(key_digest) = 32 "
            "AND typeof(request_fingerprint) = 'blob' AND length(request_fingerprint) = 32"
        ),
        sa.CheckConstraint("typeof(page_uid) = 'blob' AND length(page_uid) = 16"),
        sa.CheckConstraint("typeof(changed) = 'integer' AND changed IN (0, 1)"),
        sa.CheckConstraint(
            "typeof(operation_at) = 'integer' AND operation_at BETWEEN 0 AND 253402300799999999 "
            "AND typeof(original_page_updated_at) = 'integer' AND original_page_updated_at >= 0 "
            "AND typeof(result_updated_at) = 'integer' "
            "AND result_updated_at BETWEEN original_page_updated_at AND 253402300799999999"
        ),
        sa.CheckConstraint(
            "typeof(original_occurred_at) = 'integer' "
            "AND original_occurred_at BETWEEN -62135596800000000 AND 253402300799999999"
        ),
        sa.CheckConstraint(
            "(changed = 1 AND move_sequence IS NOT NULL AND master_audit_event_id IS NOT NULL "
            "AND result_updated_at > original_page_updated_at "
            "AND (source_section_id != target_section_id OR source_book_id != target_book_id)) "
            "OR (changed = 0 AND move_sequence IS NULL AND master_audit_event_id IS NULL "
            "AND result_updated_at = original_page_updated_at "
            "AND source_section_id = target_section_id AND source_book_id = target_book_id "
            "AND request_etag = response_etag)"
        ),
    )


def _same(left: str, right: str) -> str:
    return " AND ".join(f"{left}.{field} = {right}.{field}" for field in _FIELDS)


def _poststate(prefix: str) -> str:
    return (
        f"p.library_id = {prefix}.library_id AND p.page_uid = {prefix}.page_uid "
        f"AND p.section_id = {prefix}.new_section_id AND p.book_id = {prefix}.new_book_id "
        f"AND p.updated_at = {prefix}.changed_at AND p.deleted_at IS NULL "
        f"AND p.current_revision_id = {prefix}.at_revision_id "
        f"AND p.current_revision_number = {prefix}.at_revision_number "
        f"AND p.occurred_at = {prefix}.occurred_at_at_event"
    )


def _create_triggers() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql(
        "CREATE TRIGGER trg_page_move_guards_validate_insert BEFORE INSERT ON page_move_guards "
        "WHEN EXISTS (SELECT 1 FROM page_move_guards g WHERE g.library_id = NEW.library_id "
        "AND g.page_uid = NEW.page_uid) OR NOT EXISTS (SELECT 1 FROM pages p "
        "JOIN admin_master_audit_events a ON a.id = NEW.master_audit_event_id "
        "WHERE p.library_id = NEW.library_id AND p.page_uid = NEW.page_uid "
        "AND p.deleted_at IS NULL AND p.section_id = NEW.old_section_id "
        "AND p.book_id = NEW.old_book_id AND p.updated_at = NEW.old_updated_at "
        "AND p.current_revision_id = NEW.at_revision_id "
        "AND p.current_revision_number = NEW.at_revision_number "
        "AND p.occurred_at = NEW.occurred_at_at_event "
        "AND NEW.sequence = 1 + coalesce((SELECT max(sequence) FROM page_move_events e "
        "WHERE e.library_id = NEW.library_id AND e.page_uid = NEW.page_uid), 0) "
        "AND a.action = 'content.page.move' AND a.target_type = 'page' "
        "AND a.target_id = NEW.library_id || ':' || lower(hex(NEW.page_uid)) "
        "AND a.occurred_at = NEW.changed_at AND NOT EXISTS (SELECT 1 FROM page_move_events e "
        "WHERE e.master_audit_event_id = NEW.master_audit_event_id)) BEGIN "
        "SELECT RAISE(ABORT, 'Invalid Page move guard'); END"
    )
    connection.exec_driver_sql(
        "CREATE TRIGGER trg_page_move_events_validate_insert BEFORE INSERT ON page_move_events "
        "WHEN EXISTS (SELECT 1 FROM page_move_events e WHERE "
        "(e.library_id = NEW.library_id AND e.page_uid = NEW.page_uid "
        "AND e.sequence = NEW.sequence) "
        "OR e.master_audit_event_id = NEW.master_audit_event_id) OR NOT EXISTS "
        f"(SELECT 1 FROM page_move_guards g JOIN pages p ON {_poststate('g')} "
        f"WHERE {_same('g', 'NEW')}) BEGIN "
        "SELECT RAISE(ABORT, 'Page move event requires its exact guard'); END"
    )
    connection.exec_driver_sql(
        "CREATE TRIGGER trg_page_move_guards_safe_delete BEFORE DELETE ON page_move_guards "
        f"WHEN NOT EXISTS (SELECT 1 FROM page_move_events e JOIN pages p ON {_poststate('e')} "
        f"WHERE {_same('e', 'OLD')}) BEGIN "
        "SELECT RAISE(ABORT, 'Incomplete Page move guard'); END"
    )
    connection.exec_driver_sql(
        "CREATE TRIGGER trg_pages_move_require_guard BEFORE UPDATE OF section_id, book_id ON pages "
        "WHEN NEW.section_id IS NOT OLD.section_id OR NEW.book_id IS NOT OLD.book_id BEGIN "
        "SELECT CASE WHEN OLD.deleted_at IS NOT NULL OR NEW.deleted_at IS NOT OLD.deleted_at "
        "OR NEW.title IS NOT OLD.title OR NEW.occurred_at IS NOT OLD.occurred_at "
        "OR NEW.current_revision_id IS NOT OLD.current_revision_id "
        "OR NEW.current_revision_number IS NOT OLD.current_revision_number "
        "OR NEW.library_id IS NOT OLD.library_id OR NEW.page_uid IS NOT OLD.page_uid "
        "OR NEW.page_id IS NOT OLD.page_id OR NEW.id_scheme IS NOT OLD.id_scheme "
        "OR NEW.created_at IS NOT OLD.created_at OR NEW.page_type IS NOT OLD.page_type "
        "OR NOT EXISTS (SELECT 1 FROM page_move_guards g WHERE g.library_id = OLD.library_id "
        "AND g.page_uid = OLD.page_uid AND g.old_section_id = OLD.section_id "
        "AND g.old_book_id = OLD.book_id AND g.new_section_id = NEW.section_id "
        "AND g.new_book_id = NEW.book_id AND g.old_updated_at = OLD.updated_at "
        "AND g.changed_at = NEW.updated_at AND g.at_revision_id = OLD.current_revision_id "
        "AND g.at_revision_number = OLD.current_revision_number "
        "AND g.occurred_at_at_event = OLD.occurred_at) "
        "THEN RAISE(ABORT, 'Page movement requires its exact guard') END; END"
    )
    fields = ", ".join(_FIELDS)
    connection.exec_driver_sql(
        "CREATE TRIGGER trg_pages_move_record AFTER UPDATE OF section_id, book_id ON pages "
        "WHEN NEW.section_id IS NOT OLD.section_id OR NEW.book_id IS NOT OLD.book_id BEGIN "
        f"INSERT INTO page_move_events ({fields}) SELECT {fields} FROM page_move_guards "
        "WHERE library_id = NEW.library_id AND page_uid = NEW.page_uid; "
        "DELETE FROM page_move_guards WHERE library_id = NEW.library_id "
        "AND page_uid = NEW.page_uid; END"
    )
    connection.exec_driver_sql(
        "CREATE TRIGGER trg_master_move_receipts_validate_insert "
        "BEFORE INSERT ON admin_master_move_receipts WHEN "
        "EXISTS (SELECT 1 FROM admin_master_move_receipts r WHERE "
        "(r.identity_id = NEW.identity_id AND r.operation = "
        "NEW.operation AND r.key_digest = NEW.key_digest) "
        "OR (r.library_id = NEW.library_id AND r.page_uid = "
        "NEW.page_uid AND r.move_sequence = NEW.move_sequence) "
        "OR r.master_audit_event_id = NEW.master_audit_event_id) "
        "OR NOT EXISTS (SELECT 1 FROM pages p WHERE p.library_id = NEW.library_id "
        "AND p.page_uid = NEW.page_uid AND p.page_id = NEW.page_id AND p.deleted_at IS NULL "
        "AND p.section_id = NEW.target_section_id AND p.book_id = NEW.target_book_id "
        "AND p.updated_at = NEW.result_updated_at AND p.occurred_at = NEW.original_occurred_at "
        "AND p.current_revision_id = NEW.revision_id AND "
        "p.current_revision_number = NEW.revision_number) "
        "OR (NEW.changed = 1 AND NOT EXISTS (SELECT 1 FROM page_move_events e "
        "JOIN admin_master_audit_events a ON a.id = e.master_audit_event_id "
        "WHERE e.library_id = NEW.library_id AND e.page_uid = NEW.page_uid "
        "AND e.sequence = NEW.move_sequence AND e.master_audit_event_id "
        "= NEW.master_audit_event_id "
        "AND e.old_section_id = NEW.source_section_id AND e.old_book_id = NEW.source_book_id "
        "AND e.new_section_id = NEW.target_section_id AND e.new_book_id = NEW.target_book_id "
        "AND e.old_updated_at = NEW.original_page_updated_at AND "
        "e.changed_at = NEW.result_updated_at "
        "AND e.at_revision_id = NEW.revision_id AND e.at_revision_number = NEW.revision_number "
        "AND e.occurred_at_at_event = NEW.original_occurred_at AND "
        "a.identity_id = NEW.identity_id)) "
        "BEGIN SELECT RAISE(ABORT, 'Invalid frozen Page move success'); END"
    )
    for table, prefix, actions in (
        (_GUARDS, "page_move_guards", ("update",)),
        (_EVENTS, "page_move_events", ("update", "delete")),
        (_RECEIPTS, "master_move_receipts", ("update", "delete")),
    ):
        for action in actions:
            connection.exec_driver_sql(
                f"CREATE TRIGGER trg_{prefix}_no_{action} BEFORE {action.upper()} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'Page move history is immutable'); END"
            )


def upgrade() -> None:
    driver = _begin(upgrading=True)
    # Exact 0027 schema and preexisting history must already be valid. These
    # domain checks do not enable query_only (the closed-backup entrypoint does).
    from patchouli_lib.backup.validation import (
        _require_idempotency_graph,
        _require_lifecycle_graph,
        _require_master_file_set_receipts,
        _require_schema,
    )

    _require_schema(driver, "20261001_0027")
    _require_lifecycle_graph(driver, "20261001_0027")
    _require_idempotency_graph(driver, "20261001_0027")
    _require_master_file_set_receipts(driver)
    _move_table(_EVENTS, guard=False)
    _move_table(_GUARDS, guard=True)
    _receipt_table()
    _create_triggers()


def downgrade() -> None:
    _begin(upgrading=False)
    connection = op.get_bind()
    for table in (_RECEIPTS, _GUARDS, _EVENTS):
        if connection.exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1").first():
            raise RuntimeError("Cannot discard Page move history or successes.")
    if connection.exec_driver_sql(
        "SELECT 1 FROM admin_master_audit_events WHERE action = 'content.page.move' LIMIT 1"
    ).first():
        raise RuntimeError("Cannot discard Page move audit history.")
    for name in _TRIGGERS:
        connection.exec_driver_sql(f"DROP TRIGGER {name}")
    for table in (_RECEIPTS, _GUARDS, _EVENTS):
        op.drop_table(table)
