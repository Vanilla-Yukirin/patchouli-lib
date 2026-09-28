"""Audit password-session structure creation without inventing a bearer actor.

Revision ID: 20260929_0009
Revises: 20260929_0008
Create Date: 2026-09-29
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0009"
down_revision: str | None = "20260929_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _begin_sqlite_transaction() -> None:
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Admin structure audit migration requires SQLite.")
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    _begin_sqlite_transaction()
    op.create_table(
        "admin_structure_audit_events",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("session_fingerprint", sa.LargeBinary(length=32), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("section_id", sa.String(length=32)),
        sa.Column("book_id", sa.String(length=32)),
        sa.Column("request_id", sa.String(length=100), nullable=False),
        sa.Column("occurred_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_admin_structure_audit_library",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["section_id", "library_id"],
            ["sections.id", "sections.library_id"],
            name="fk_admin_structure_audit_section",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["book_id", "section_id", "library_id"],
            ["books.id", "books.section_id", "books.library_id"],
            name="fk_admin_structure_audit_book",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_admin_structure_audit_id",
        ),
        sa.CheckConstraint(
            "typeof(session_fingerprint) = 'blob' AND length(session_fingerprint) = 32",
            name="ck_admin_structure_audit_session_fingerprint",
        ),
        sa.CheckConstraint(
            "(action = 'library.create' AND section_id IS NULL AND book_id IS NULL) "
            "OR (action = 'section.create' AND section_id IS NOT NULL AND book_id IS NULL) "
            "OR (action = 'book.create' AND section_id IS NOT NULL AND book_id IS NOT NULL)",
            name="ck_admin_structure_audit_action_resource",
        ),
        sa.CheckConstraint(
            "length(request_id) BETWEEN 5 AND 100 AND request_id = trim(request_id) "
            "AND occurred_at >= 0",
            name="ck_admin_structure_audit_metadata",
        ),
    )
    op.execute(
        "CREATE TRIGGER trg_admin_structure_audit_no_update "
        "BEFORE UPDATE ON admin_structure_audit_events BEGIN "
        "SELECT RAISE(ABORT, 'Admin structure audit is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_admin_structure_audit_no_delete "
        "BEFORE DELETE ON admin_structure_audit_events BEGIN "
        "SELECT RAISE(ABORT, 'Admin structure audit is immutable'); END"
    )


def downgrade() -> None:
    _begin_sqlite_transaction()
    existing = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM admin_structure_audit_events LIMIT 1"))
        .scalar_one_or_none()
    )
    if existing is not None:
        raise RuntimeError("Downgrade would discard admin structure audit events.")
    op.execute("DROP TRIGGER trg_admin_structure_audit_no_delete")
    op.execute("DROP TRIGGER trg_admin_structure_audit_no_update")
    op.drop_table("admin_structure_audit_events")
