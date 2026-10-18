"""Add Library-scoped Tag definitions and exact Page associations.

Revision ID: 20260929_0010
Revises: 20260929_0009
Create Date: 2026-09-29
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0010"
down_revision: str | None = "20260929_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _begin_sqlite_transaction() -> None:
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Tag migration requires SQLite.")
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    _begin_sqlite_transaction()
    op.create_table(
        "tags",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("match_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("library_id", "id", name="pk_tags"),
        sa.UniqueConstraint("library_id", "match_key", name="uq_tags_library_match_key"),
        sa.ForeignKeyConstraint(
            ["library_id"], ["libraries.id"], name="fk_tags_library", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'", name="ck_tags_id"),
        sa.CheckConstraint(
            "typeof(display_name) = 'text' AND length(display_name) BETWEEN 1 AND 100 "
            "AND length(CAST(display_name AS BLOB)) <= 255 "
            "AND display_name = trim(display_name) "
            "AND instr(display_name, char(0)) = 0 "
            "AND instr(display_name, char(10)) = 0 "
            "AND instr(display_name, char(13)) = 0",
            name="ck_tags_display_name",
        ),
        sa.CheckConstraint(
            "typeof(match_key) = 'text' AND length(match_key) BETWEEN 1 AND 100 "
            "AND length(CAST(match_key AS BLOB)) <= 255 "
            "AND match_key = trim(match_key) "
            "AND instr(match_key, char(0)) = 0 "
            "AND instr(match_key, char(10)) = 0 "
            "AND instr(match_key, char(13)) = 0",
            name="ck_tags_match_key",
        ),
        sa.CheckConstraint(
            "typeof(created_at) = 'integer' AND created_at >= 0", name="ck_tags_created_at"
        ),
    )
    op.create_table(
        "page_tags",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("page_uid", sa.LargeBinary(length=16), nullable=False),
        sa.Column("tag_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("library_id", "page_uid", "tag_id", name="pk_page_tags"),
        sa.ForeignKeyConstraint(
            ["library_id", "page_uid"],
            ["pages.library_id", "pages.page_uid"],
            name="fk_page_tags_exact_page",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["library_id", "tag_id"],
            ["tags.library_id", "tags.id"],
            name="fk_page_tags_exact_tag",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "typeof(page_uid) = 'blob' AND length(page_uid) = 16", name="ck_page_tags_page_uid"
        ),
        sa.CheckConstraint(
            "typeof(created_at) = 'integer' AND created_at >= 0", name="ck_page_tags_created_at"
        ),
    )
    op.create_index("ix_page_tags_library_tag", "page_tags", ["library_id", "tag_id"])


def downgrade() -> None:
    _begin_sqlite_transaction()
    connection = op.get_bind()
    for table_name in ("page_tags", "tags"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).scalar_one_or_none():
            raise RuntimeError("Downgrade would discard Tag definitions or Page associations.")
    op.drop_index("ix_page_tags_library_tag", table_name="page_tags")
    op.drop_table("page_tags")
    op.drop_table("tags")
