"""Independent Markdown usage guides for all three structure levels.

Revision ID: 20261020_0034
Revises: 20261019_0033
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261020_0034"
down_revision: str | None = "20261019_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
_TABLES = ("libraries", "sections", "books")


def _begin() -> None:
    driver = op.get_bind().connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Library README migration requires SQLite.")
    if not driver.in_transaction:
        op.get_bind().exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    _begin()
    for table in _TABLES:
        op.add_column(table, sa.Column("readme", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    _begin()
    for table in _TABLES:
        if (
            op.get_bind()
            .exec_driver_sql(f"SELECT 1 FROM \"{table}\" WHERE readme != '' LIMIT 1")
            .first()
        ):
            raise RuntimeError("Cannot downgrade while Library, Section or Book README exists.")
    for table in reversed(_TABLES):
        # Native DROP COLUMN retains triggers, foreign keys and the exact old table SQL.
        op.get_bind().exec_driver_sql(f'ALTER TABLE "{table}" DROP COLUMN readme')
