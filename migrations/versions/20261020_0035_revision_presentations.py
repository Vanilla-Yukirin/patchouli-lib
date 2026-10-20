"""Explicit entry files and immutable write-time titles for exact Revisions.

Revision ID: 20261020_0035
Revises: 20261020_0034
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261020_0035"
down_revision: str | None = "20261020_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _begin() -> None:
    driver = op.get_bind().connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Revision presentation migration requires SQLite.")
    if not driver.in_transaction:
        op.get_bind().exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    _begin()
    op.create_table(
        "revision_presentations",
        sa.Column("library_id", sa.String(32), primary_key=True),
        sa.Column("page_uid", sa.LargeBinary(16), primary_key=True),
        sa.Column("revision_id", sa.String(36), primary_key=True),
        sa.Column("revision_number", sa.BigInteger(), primary_key=True),
        sa.Column("primary_file", sa.Text(), nullable=True),
        sa.Column("display_title", sa.Text(), nullable=False),
        sa.CheckConstraint("length(display_title) >= 1", name="ck_revision_presentations_title"),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            ondelete="RESTRICT",
            name="fk_revision_presentations_revision",
        ),
    )
    for operation in ("UPDATE", "DELETE"):
        op.get_bind().exec_driver_sql(
            f"CREATE TRIGGER revision_presentations_no_{operation.lower()} "
            f"BEFORE {operation} ON revision_presentations BEGIN "
            "SELECT RAISE(ABORT, 'Revision presentation is immutable'); END"
        )


def downgrade() -> None:
    _begin()
    if op.get_bind().exec_driver_sql("SELECT 1 FROM revision_presentations LIMIT 1").first():
        raise RuntimeError("Cannot downgrade while Revision presentation history exists.")
    for operation in ("update", "delete"):
        op.get_bind().exec_driver_sql(f"DROP TRIGGER revision_presentations_no_{operation}")
    op.drop_table("revision_presentations")
