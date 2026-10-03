"""Guard and record Archive Page delete/restore transitions.

Revision ID: 20260929_0012
Revises: 20260929_0011
Create Date: 2026-09-29
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0012"
down_revision: str | None = "20260929_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OCCURRENCE_RANGE = "BETWEEN -62135596800000000 AND 253402300799999999"


def _begin_sqlite_transaction() -> None:
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Page lifecycle migration requires SQLite.")
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _create_lifecycle_table(*, guard: bool) -> None:
    name = "page_lifecycle_guards" if guard else "page_lifecycle_events"
    suffix = "guards" if guard else "events"
    constraints: list[sa.Constraint] = [
        sa.PrimaryKeyConstraint(
            "library_id",
            "page_uid",
            *(("sequence",) if not guard else ()),
            name=f"pk_page_lifecycle_{suffix}",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name=f"fk_page_lifecycle_{suffix}_page",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["section_id", "library_id"],
            ["sections.id", "sections.library_id"],
            name=f"fk_page_lifecycle_{suffix}_section",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name=f"fk_page_lifecycle_{suffix}_actor",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "sequence BETWEEN 1 AND 9223372036854775807",
            name=f"ck_page_lifecycle_{suffix}_sequence",
        ),
        sa.CheckConstraint(
            "(action = 'delete' AND old_deleted_at IS NULL) "
            "OR (action = 'restore' AND old_deleted_at IS NOT NULL)",
            name=f"ck_page_lifecycle_{suffix}_action",
        ),
        sa.CheckConstraint(
            "old_updated_at >= 0 AND changed_at > old_updated_at "
            "AND changed_at <= 9223372036854775807 "
            "AND (old_deleted_at IS NULL OR "
            "(old_deleted_at >= 0 AND old_deleted_at <= old_updated_at))",
            name=f"ck_page_lifecycle_{suffix}_time",
        ),
        sa.CheckConstraint(
            "at_revision_number BETWEEN 1 AND 9223372036854775807 "
            f"AND occurred_at_at_event {_OCCURRENCE_RANGE}",
            name=f"ck_page_lifecycle_{suffix}_snapshot",
        ),
        sa.CheckConstraint(
            "length(request_id) = 36 AND substr(request_id, 1, 4) = 'req_' "
            "AND substr(request_id, 5) NOT GLOB '*[^0-9a-f]*'",
            name=f"ck_page_lifecycle_{suffix}_request_id",
        ),
    ]
    if guard:
        constraints.append(
            sa.ForeignKeyConstraint(
                ["library_id", "page_uid", "sequence"],
                [
                    "page_lifecycle_events.library_id",
                    "page_lifecycle_events.page_uid",
                    "page_lifecycle_events.sequence",
                ],
                name="fk_page_lifecycle_guards_completed",
                deferrable=True,
                initially="DEFERRED",
            )
        )
    op.create_table(
        name,
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("section_id", sa.String(length=32), nullable=False),
        sa.Column("old_deleted_at", sa.BigInteger(), nullable=True),
        sa.Column("old_updated_at", sa.BigInteger(), nullable=False),
        sa.Column("changed_at", sa.BigInteger(), nullable=False),
        sa.Column("at_revision_number", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at_at_event", sa.BigInteger(), nullable=False),
        sa.Column("actor_caller_id", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        *constraints,
    )


def upgrade() -> None:
    _begin_sqlite_transaction()
    connection = op.get_bind()
    if connection.exec_driver_sql(
        "SELECT count(*) FROM pages WHERE deleted_at IS NOT NULL"
    ).scalar_one():
        raise RuntimeError("Cannot invent missing history for a pre-existing deleted Page.")

    _create_lifecycle_table(guard=False)
    _create_lifecycle_table(guard=True)
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_pages_lifecycle_initial_live "
            "BEFORE INSERT ON pages WHEN NEW.deleted_at IS NOT NULL BEGIN "
            "SELECT RAISE(ABORT, 'A new Page cannot start deleted.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_lifecycle_guards_validate_insert "
            "BEFORE INSERT ON page_lifecycle_guards WHEN NOT EXISTS ("
            "SELECT 1 FROM pages AS p WHERE p.library_id = NEW.library_id "
            "AND p.page_uid = NEW.page_uid AND p.section_id = NEW.section_id "
            "AND p.deleted_at IS NEW.old_deleted_at "
            "AND p.updated_at = NEW.old_updated_at "
            "AND p.current_revision_number = NEW.at_revision_number "
            "AND p.occurred_at = NEW.occurred_at_at_event "
            "AND NEW.changed_at > p.updated_at "
            "AND NEW.sequence = (SELECT coalesce(max(e.sequence), 0) + 1 "
            "FROM page_lifecycle_events AS e WHERE e.library_id = NEW.library_id "
            "AND e.page_uid = NEW.page_uid)) BEGIN "
            "SELECT RAISE(ABORT, 'Page lifecycle guard is inconsistent.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_lifecycle_guards_no_update "
            "BEFORE UPDATE ON page_lifecycle_guards BEGIN "
            "SELECT RAISE(ABORT, 'Page lifecycle guards are immutable.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_lifecycle_guards_safe_delete "
            "BEFORE DELETE ON page_lifecycle_guards WHEN NOT EXISTS ("
            "SELECT 1 FROM page_lifecycle_events AS e JOIN pages AS p "
            "ON p.library_id = e.library_id AND p.page_uid = e.page_uid "
            "WHERE e.library_id = OLD.library_id AND e.page_uid = OLD.page_uid "
            "AND e.sequence = OLD.sequence AND e.action = OLD.action "
            "AND e.section_id = OLD.section_id "
            "AND e.old_deleted_at IS OLD.old_deleted_at "
            "AND e.old_updated_at = OLD.old_updated_at "
            "AND e.changed_at = OLD.changed_at "
            "AND e.at_revision_number = OLD.at_revision_number "
            "AND e.occurred_at_at_event = OLD.occurred_at_at_event "
            "AND e.actor_caller_id = OLD.actor_caller_id "
            "AND e.request_id = OLD.request_id "
            "AND p.updated_at = e.changed_at "
            "AND p.deleted_at IS CASE WHEN e.action = 'delete' "
            "THEN e.changed_at ELSE NULL END) BEGIN "
            "SELECT RAISE(ABORT, 'Pending Page lifecycle change cannot be discarded.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_lifecycle_events_validate_insert "
            "BEFORE INSERT ON page_lifecycle_events WHEN NOT EXISTS ("
            "SELECT 1 FROM page_lifecycle_guards AS g JOIN pages AS p "
            "ON p.library_id = g.library_id AND p.page_uid = g.page_uid "
            "WHERE g.library_id = NEW.library_id AND g.page_uid = NEW.page_uid "
            "AND g.sequence = NEW.sequence AND g.action = NEW.action "
            "AND g.section_id = NEW.section_id "
            "AND g.old_deleted_at IS NEW.old_deleted_at "
            "AND g.old_updated_at = NEW.old_updated_at "
            "AND g.changed_at = NEW.changed_at "
            "AND g.at_revision_number = NEW.at_revision_number "
            "AND g.occurred_at_at_event = NEW.occurred_at_at_event "
            "AND g.actor_caller_id = NEW.actor_caller_id "
            "AND g.request_id = NEW.request_id "
            "AND p.updated_at = NEW.changed_at "
            "AND p.deleted_at IS CASE WHEN NEW.action = 'delete' "
            "THEN NEW.changed_at ELSE NULL END) BEGIN "
            "SELECT RAISE(ABORT, 'Page lifecycle event is inconsistent.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_lifecycle_events_no_update "
            "BEFORE UPDATE ON page_lifecycle_events BEGIN "
            "SELECT RAISE(ABORT, 'Page lifecycle events are immutable.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_lifecycle_events_no_delete "
            "BEFORE DELETE ON page_lifecycle_events BEGIN "
            "SELECT RAISE(ABORT, 'Page lifecycle events are immutable.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_pages_lifecycle_require_guard "
            "BEFORE UPDATE OF deleted_at ON pages "
            "WHEN NEW.deleted_at IS NOT OLD.deleted_at BEGIN "
            "SELECT CASE WHEN NEW.updated_at <= OLD.updated_at "
            "OR NEW.section_id IS NOT OLD.section_id "
            "OR NEW.book_id IS NOT OLD.book_id "
            "OR NEW.title IS NOT OLD.title "
            "OR NEW.page_type IS NOT OLD.page_type "
            "OR NEW.occurred_at IS NOT OLD.occurred_at "
            "OR NEW.current_revision_id IS NOT OLD.current_revision_id "
            "OR NEW.current_revision_number IS NOT OLD.current_revision_number "
            "OR NOT EXISTS (SELECT 1 FROM page_lifecycle_guards AS g "
            "WHERE g.library_id = OLD.library_id AND g.page_uid = OLD.page_uid "
            "AND g.section_id = OLD.section_id "
            "AND g.old_deleted_at IS OLD.deleted_at "
            "AND g.old_updated_at = OLD.updated_at "
            "AND g.changed_at = NEW.updated_at "
            "AND g.at_revision_number = OLD.current_revision_number "
            "AND g.occurred_at_at_event = OLD.occurred_at "
            "AND NEW.deleted_at IS CASE WHEN g.action = 'delete' "
            "THEN g.changed_at ELSE NULL END) "
            "THEN RAISE(ABORT, 'Page lifecycle change requires a guard.') END; END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_pages_lifecycle_record "
            "AFTER UPDATE OF deleted_at ON pages "
            "WHEN NEW.deleted_at IS NOT OLD.deleted_at BEGIN "
            "INSERT INTO page_lifecycle_events "
            "(library_id, page_uid, sequence, action, section_id, old_deleted_at, "
            "old_updated_at, changed_at, at_revision_number, occurred_at_at_event, "
            "actor_caller_id, request_id) "
            "SELECT g.library_id, g.page_uid, g.sequence, g.action, g.section_id, "
            "OLD.deleted_at, OLD.updated_at, NEW.updated_at, "
            "NEW.current_revision_number, NEW.occurred_at, "
            "g.actor_caller_id, g.request_id FROM page_lifecycle_guards AS g "
            "WHERE g.library_id = OLD.library_id AND g.page_uid = OLD.page_uid; "
            "DELETE FROM page_lifecycle_guards WHERE library_id = OLD.library_id "
            "AND page_uid = OLD.page_uid; END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_pages_lifecycle_no_content_while_deleted "
            "BEFORE UPDATE ON pages WHEN OLD.deleted_at IS NOT NULL "
            "AND (NEW.occurred_at IS NOT OLD.occurred_at "
            "OR NEW.current_revision_id IS NOT OLD.current_revision_id "
            "OR NEW.current_revision_number IS NOT OLD.current_revision_number) BEGIN "
            "SELECT RAISE(ABORT, 'A deleted Page cannot change content or occurrence.'); END"
        )
    )


def downgrade() -> None:
    _begin_sqlite_transaction()
    connection = op.get_bind()
    if connection.exec_driver_sql("SELECT count(*) FROM page_lifecycle_events").scalar_one():
        raise RuntimeError("Cannot discard recorded Page lifecycle events.")
    if connection.exec_driver_sql("SELECT count(*) FROM page_lifecycle_guards").scalar_one():
        raise RuntimeError("Cannot discard pending Page lifecycle guards.")
    for name in (
        "trg_pages_lifecycle_no_content_while_deleted",
        "trg_pages_lifecycle_record",
        "trg_pages_lifecycle_require_guard",
        "trg_page_lifecycle_events_no_delete",
        "trg_page_lifecycle_events_no_update",
        "trg_page_lifecycle_events_validate_insert",
        "trg_page_lifecycle_guards_safe_delete",
        "trg_page_lifecycle_guards_no_update",
        "trg_page_lifecycle_guards_validate_insert",
        "trg_pages_lifecycle_initial_live",
    ):
        op.execute(sa.text(f"DROP TRIGGER {name}"))
    op.drop_table("page_lifecycle_guards")
    op.drop_table("page_lifecycle_events")
