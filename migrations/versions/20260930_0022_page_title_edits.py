"""Record audited Page display-title changes without rewriting content history.

Revision ID: 20260930_0022
Revises: 20260930_0021
Create Date: 2026-09-30
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0022"
down_revision: str | None = "20260930_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _begin_sqlite_transaction() -> None:
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Page title migration requires SQLite.")
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    _begin_sqlite_transaction()
    op.create_table(
        "page_title_events",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("old_title", sa.Text(), nullable=False),
        sa.Column("new_title", sa.Text(), nullable=False),
        sa.Column("old_updated_at", sa.BigInteger(), nullable=False),
        sa.Column("changed_at", sa.BigInteger(), nullable=False),
        sa.Column("at_revision_number", sa.BigInteger(), nullable=False),
        sa.Column("master_audit_event_id", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("library_id", "page_uid", "sequence"),
        sa.UniqueConstraint("master_audit_event_id", name="uq_page_title_events_master_audit"),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["master_audit_event_id"], ["admin_master_audit_events.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint("sequence BETWEEN 1 AND 9223372036854775807"),
        sa.CheckConstraint("at_revision_number BETWEEN 1 AND 9223372036854775807"),
        sa.CheckConstraint(
            "typeof(old_title) = 'text' AND length(old_title) >= 1 "
            "AND instr(old_title, char(0)) = 0 AND "
            "typeof(new_title) = 'text' AND length(new_title) >= 1 "
            "AND instr(new_title, char(0)) = 0 AND old_title != new_title"
        ),
        sa.CheckConstraint(
            "old_updated_at >= 0 AND changed_at > old_updated_at "
            "AND changed_at <= 9223372036854775807"
        ),
    )
    op.execute(
        "CREATE TRIGGER trg_pages_title_require_audit "
        "BEFORE UPDATE OF title ON pages WHEN NEW.title IS NOT OLD.title BEGIN "
        "SELECT CASE WHEN OLD.deleted_at IS NOT NULL "
        "OR NEW.updated_at <= OLD.updated_at "
        "OR NEW.section_id IS NOT OLD.section_id "
        "OR NEW.book_id IS NOT OLD.book_id "
        "OR NEW.page_type IS NOT OLD.page_type "
        "OR NEW.occurred_at IS NOT OLD.occurred_at "
        "OR NEW.current_revision_id IS NOT OLD.current_revision_id "
        "OR NEW.current_revision_number IS NOT OLD.current_revision_number "
        "OR NEW.deleted_at IS NOT OLD.deleted_at "
        "OR (SELECT count(*) FROM admin_master_audit_events AS a "
        "WHERE a.action = 'content.page.title.edit' AND a.target_type = 'page' "
        "AND a.target_id = OLD.library_id || ':' || lower(hex(OLD.page_uid)) "
        "AND a.occurred_at = NEW.updated_at "
        "AND NOT EXISTS (SELECT 1 FROM page_title_events AS e "
        "WHERE e.master_audit_event_id = a.id)) != 1 "
        "THEN RAISE(ABORT, 'Page title change requires one unused master audit.') END; END"
    )
    op.execute(
        "CREATE TRIGGER trg_page_title_events_validate_insert "
        "BEFORE INSERT ON page_title_events WHEN NOT EXISTS ("
        "SELECT 1 FROM pages AS p JOIN admin_master_audit_events AS a "
        "ON a.id = NEW.master_audit_event_id "
        "WHERE p.library_id = NEW.library_id AND p.page_uid = NEW.page_uid "
        "AND p.title = NEW.new_title AND p.updated_at = NEW.changed_at "
        "AND p.current_revision_number = NEW.at_revision_number "
        "AND p.deleted_at IS NULL "
        "AND a.action = 'content.page.title.edit' AND a.target_type = 'page' "
        "AND a.target_id = NEW.library_id || ':' || lower(hex(NEW.page_uid)) "
        "AND a.occurred_at = NEW.changed_at "
        "AND NEW.sequence = (SELECT coalesce(max(e.sequence), 0) + 1 "
        "FROM page_title_events AS e WHERE e.library_id = NEW.library_id "
        "AND e.page_uid = NEW.page_uid)) BEGIN "
        "SELECT RAISE(ABORT, 'Page title event is inconsistent.'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_pages_title_record "
        "AFTER UPDATE OF title ON pages WHEN NEW.title IS NOT OLD.title BEGIN "
        "INSERT INTO page_title_events "
        "(library_id, page_uid, sequence, old_title, new_title, old_updated_at, "
        "changed_at, at_revision_number, master_audit_event_id) "
        "SELECT OLD.library_id, OLD.page_uid, "
        "(SELECT coalesce(max(e.sequence), 0) + 1 FROM page_title_events AS e "
        "WHERE e.library_id = OLD.library_id AND e.page_uid = OLD.page_uid), "
        "OLD.title, NEW.title, OLD.updated_at, NEW.updated_at, "
        "OLD.current_revision_number, a.id FROM admin_master_audit_events AS a "
        "WHERE a.action = 'content.page.title.edit' AND a.target_type = 'page' "
        "AND a.target_id = OLD.library_id || ':' || lower(hex(OLD.page_uid)) "
        "AND a.occurred_at = NEW.updated_at "
        "AND NOT EXISTS (SELECT 1 FROM page_title_events AS e "
        "WHERE e.master_audit_event_id = a.id); END"
    )
    op.execute(
        "CREATE TRIGGER trg_page_title_events_no_update "
        "BEFORE UPDATE ON page_title_events BEGIN "
        "SELECT RAISE(ABORT, 'Page title events are immutable.'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_page_title_events_no_delete "
        "BEFORE DELETE ON page_title_events BEGIN "
        "SELECT RAISE(ABORT, 'Page title events are immutable.'); END"
    )


def downgrade() -> None:
    _begin_sqlite_transaction()
    if op.get_bind().exec_driver_sql("SELECT 1 FROM page_title_events LIMIT 1").first():
        raise RuntimeError("Cannot discard recorded Page title changes.")
    for name in (
        "trg_page_title_events_no_delete",
        "trg_page_title_events_no_update",
        "trg_pages_title_record",
        "trg_page_title_events_validate_insert",
        "trg_pages_title_require_audit",
    ):
        op.execute(sa.text(f"DROP TRIGGER {name}"))
    op.drop_table("page_title_events")
