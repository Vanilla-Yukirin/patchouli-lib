"""Audit declared-time corrections without changing stable Page identifiers.

Revision ID: 20260929_0011
Revises: 20260929_0010
Create Date: 2026-09-29
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0011"
down_revision: str | None = "20260929_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OCCURRENCE_RANGE = "BETWEEN -62135596800000000 AND 253402300799999999"


def _begin_sqlite_transaction() -> None:
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Page occurrence migration requires SQLite.")
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _stable_identity_trigger(*, initial: bool) -> str:
    occurred_clause = "OR NEW.occurred_at IS NOT OLD.occurred_at " if initial else ""
    return (
        "CREATE TRIGGER trg_pages_stable_identity "
        "BEFORE UPDATE ON pages WHEN "
        "NEW.library_id IS NOT OLD.library_id "
        "OR NEW.page_uid IS NOT OLD.page_uid "
        "OR NEW.page_id IS NOT OLD.page_id "
        "OR NEW.id_scheme IS NOT OLD.id_scheme "
        "OR NEW.id_timestamp_micros IS NOT OLD.id_timestamp_micros "
        "OR NEW.base_slug IS NOT OLD.base_slug "
        "OR NEW.collision_ordinal IS NOT OLD.collision_ordinal "
        f"{occurred_clause}"
        "OR NEW.created_at IS NOT OLD.created_at BEGIN "
        "SELECT RAISE(ABORT, 'Page identity is stable.'); END"
    )


def upgrade() -> None:
    _begin_sqlite_transaction()
    op.create_table(
        "page_occurrence_corrections",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("old_occurred_at", sa.BigInteger(), nullable=False),
        sa.Column("new_occurred_at", sa.BigInteger(), nullable=False),
        sa.Column("at_revision_number", sa.BigInteger(), nullable=False),
        sa.Column("actor_caller_id", sa.String(length=32), nullable=False),
        sa.Column("corrected_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "library_id", "page_uid", "sequence", name="pk_page_occurrence_corrections"
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_page_occurrence_corrections_page",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_page_occurrence_corrections_actor",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "sequence BETWEEN 1 AND 9223372036854775807",
            name="ck_page_occurrence_corrections_sequence",
        ),
        sa.CheckConstraint(
            f"old_occurred_at {_OCCURRENCE_RANGE} "
            f"AND new_occurred_at {_OCCURRENCE_RANGE} "
            "AND old_occurred_at != new_occurred_at",
            name="ck_page_occurrence_corrections_values",
        ),
        sa.CheckConstraint(
            "at_revision_number BETWEEN 1 AND 9223372036854775807",
            name="ck_page_occurrence_corrections_revision",
        ),
        sa.CheckConstraint("corrected_at >= 0", name="ck_page_occurrence_corrections_time"),
    )
    op.create_table(
        "page_occurrence_correction_guards",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("old_occurred_at", sa.BigInteger(), nullable=False),
        sa.Column("new_occurred_at", sa.BigInteger(), nullable=False),
        sa.Column("actor_caller_id", sa.String(length=32), nullable=False),
        sa.Column("corrected_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "library_id", "page_uid", name="pk_page_occurrence_correction_guards"
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_page_occurrence_correction_guards_page",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_page_occurrence_correction_guards_actor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "sequence"],
            [
                "page_occurrence_corrections.library_id",
                "page_occurrence_corrections.page_uid",
                "page_occurrence_corrections.sequence",
            ],
            name="fk_page_occurrence_correction_guards_completed",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "sequence BETWEEN 1 AND 9223372036854775807",
            name="ck_page_occurrence_correction_guards_sequence",
        ),
        sa.CheckConstraint(
            f"old_occurred_at {_OCCURRENCE_RANGE} "
            f"AND new_occurred_at {_OCCURRENCE_RANGE} "
            "AND old_occurred_at != new_occurred_at",
            name="ck_page_occurrence_correction_guards_values",
        ),
        sa.CheckConstraint("corrected_at >= 0", name="ck_page_occurrence_correction_guards_time"),
    )

    op.execute(sa.text("DROP TRIGGER trg_pages_stable_identity"))
    op.execute(sa.text(_stable_identity_trigger(initial=False)))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_pages_updated_at_monotonic "
            "BEFORE UPDATE OF updated_at ON pages "
            "WHEN NEW.updated_at < OLD.updated_at BEGIN "
            "SELECT RAISE(ABORT, 'Page update time cannot move backwards.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_occurrence_guards_validate_insert "
            "BEFORE INSERT ON page_occurrence_correction_guards WHEN "
            "NOT EXISTS (SELECT 1 FROM pages AS p WHERE p.library_id = NEW.library_id "
            "AND p.page_uid = NEW.page_uid AND p.occurred_at = NEW.old_occurred_at "
            "AND NEW.corrected_at > p.updated_at "
            "AND NEW.sequence = (SELECT coalesce(max(c.sequence), 0) + 1 "
            "FROM page_occurrence_corrections AS c WHERE c.library_id = NEW.library_id "
            "AND c.page_uid = NEW.page_uid)) BEGIN "
            "SELECT RAISE(ABORT, 'Page occurrence correction guard is inconsistent.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_occurrence_guards_no_update "
            "BEFORE UPDATE ON page_occurrence_correction_guards BEGIN "
            "SELECT RAISE(ABORT, 'Page occurrence correction guards are immutable.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_occurrence_guards_safe_delete "
            "BEFORE DELETE ON page_occurrence_correction_guards WHEN NOT EXISTS ("
            "SELECT 1 FROM page_occurrence_corrections AS c JOIN pages AS p "
            "ON p.library_id = c.library_id AND p.page_uid = c.page_uid "
            "WHERE c.library_id = OLD.library_id AND c.page_uid = OLD.page_uid "
            "AND c.sequence = OLD.sequence AND c.old_occurred_at = OLD.old_occurred_at "
            "AND c.new_occurred_at = OLD.new_occurred_at "
            "AND c.actor_caller_id = OLD.actor_caller_id "
            "AND c.corrected_at = OLD.corrected_at "
            "AND p.occurred_at = c.new_occurred_at "
            "AND p.updated_at = c.corrected_at) BEGIN "
            "SELECT RAISE(ABORT, 'Pending Page occurrence correction cannot be discarded.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_occurrence_corrections_validate_insert "
            "BEFORE INSERT ON page_occurrence_corrections WHEN NOT EXISTS ("
            "SELECT 1 FROM page_occurrence_correction_guards AS g JOIN pages AS p "
            "ON p.library_id = g.library_id AND p.page_uid = g.page_uid "
            "WHERE g.library_id = NEW.library_id AND g.page_uid = NEW.page_uid "
            "AND g.sequence = NEW.sequence "
            "AND g.old_occurred_at = NEW.old_occurred_at "
            "AND g.new_occurred_at = NEW.new_occurred_at "
            "AND g.actor_caller_id = NEW.actor_caller_id "
            "AND g.corrected_at = NEW.corrected_at "
            "AND p.occurred_at = NEW.new_occurred_at "
            "AND p.updated_at = NEW.corrected_at "
            "AND p.current_revision_number = NEW.at_revision_number) BEGIN "
            "SELECT RAISE(ABORT, 'Page occurrence correction is inconsistent.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_occurrence_corrections_no_update "
            "BEFORE UPDATE ON page_occurrence_corrections BEGIN "
            "SELECT RAISE(ABORT, 'Page occurrence corrections are immutable.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_page_occurrence_corrections_no_delete "
            "BEFORE DELETE ON page_occurrence_corrections BEGIN "
            "SELECT RAISE(ABORT, 'Page occurrence corrections are immutable.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_pages_occurrence_require_guard "
            "BEFORE UPDATE OF occurred_at ON pages "
            "WHEN NEW.occurred_at IS NOT OLD.occurred_at BEGIN "
            "SELECT CASE WHEN NEW.updated_at <= OLD.updated_at "
            "OR NEW.section_id IS NOT OLD.section_id "
            "OR NEW.book_id IS NOT OLD.book_id "
            "OR NEW.title IS NOT OLD.title "
            "OR NEW.page_type IS NOT OLD.page_type "
            "OR NEW.current_revision_id IS NOT OLD.current_revision_id "
            "OR NEW.current_revision_number IS NOT OLD.current_revision_number "
            "OR NEW.deleted_at IS NOT OLD.deleted_at "
            "OR NOT EXISTS (SELECT 1 FROM page_occurrence_correction_guards AS g "
            "WHERE g.library_id = OLD.library_id AND g.page_uid = OLD.page_uid "
            "AND g.old_occurred_at = OLD.occurred_at "
            "AND g.new_occurred_at = NEW.occurred_at "
            "AND g.corrected_at = NEW.updated_at) "
            "THEN RAISE(ABORT, 'Page occurrence correction requires a guard.') END; END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_pages_occurrence_record "
            "AFTER UPDATE OF occurred_at ON pages "
            "WHEN NEW.occurred_at IS NOT OLD.occurred_at BEGIN "
            "INSERT INTO page_occurrence_corrections "
            "(library_id, page_uid, sequence, old_occurred_at, new_occurred_at, "
            "at_revision_number, actor_caller_id, corrected_at) "
            "SELECT g.library_id, g.page_uid, g.sequence, OLD.occurred_at, "
            "NEW.occurred_at, NEW.current_revision_number, g.actor_caller_id, "
            "NEW.updated_at FROM page_occurrence_correction_guards AS g "
            "WHERE g.library_id = OLD.library_id AND g.page_uid = OLD.page_uid; "
            "DELETE FROM page_occurrence_correction_guards "
            "WHERE library_id = OLD.library_id AND page_uid = OLD.page_uid; END"
        )
    )


def downgrade() -> None:
    _begin_sqlite_transaction()
    connection = op.get_bind()
    if connection.exec_driver_sql("SELECT count(*) FROM page_occurrence_corrections").scalar_one():
        raise RuntimeError("Cannot discard recorded Page occurrence corrections.")
    if connection.exec_driver_sql(
        "SELECT count(*) FROM page_occurrence_correction_guards"
    ).scalar_one():
        raise RuntimeError("Cannot discard pending Page occurrence corrections.")
    for name in (
        "trg_pages_occurrence_record",
        "trg_pages_occurrence_require_guard",
        "trg_pages_updated_at_monotonic",
        "trg_page_occurrence_corrections_no_delete",
        "trg_page_occurrence_corrections_no_update",
        "trg_page_occurrence_corrections_validate_insert",
        "trg_page_occurrence_guards_safe_delete",
        "trg_page_occurrence_guards_no_update",
        "trg_page_occurrence_guards_validate_insert",
        "trg_pages_stable_identity",
    ):
        op.execute(sa.text(f"DROP TRIGGER {name}"))
    op.execute(sa.text(_stable_identity_trigger(initial=True)))
    op.drop_table("page_occurrence_correction_guards")
    op.drop_table("page_occurrence_corrections")
