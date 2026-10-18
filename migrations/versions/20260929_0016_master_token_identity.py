"""Add one verifier-only administrator identity and session generation.

Revision ID: 20260929_0016
Revises: 20260929_0015
Create Date: 2026-09-29

This migration does not configure a login or enable remote initialization.
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0016"
down_revision: str | None = "20260929_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _begin_sqlite_transaction() -> None:
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Master identity migration requires SQLite.")
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    _begin_sqlite_transaction()
    op.create_table(
        "admin_master_identity",
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("identity_id", sa.String(length=32), nullable=False),
        sa.Column("token_verifier", sa.String(length=256), nullable=False),
        sa.Column("session_generation", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("slot"),
        sa.UniqueConstraint("identity_id", name="uq_admin_master_identity_id"),
        sa.CheckConstraint("slot = 1", name="ck_admin_master_identity_singleton"),
        sa.CheckConstraint(
            "length(identity_id) = 32 AND identity_id NOT GLOB '*[^0-9a-f]*'",
            name="ck_admin_master_identity_id",
        ),
        sa.CheckConstraint(
            "typeof(token_verifier) = 'text' AND length(token_verifier) BETWEEN 1 AND 256 "
            "AND token_verifier LIKE 'pbkdf2_sha256$%'",
            name="ck_admin_master_identity_verifier",
        ),
        sa.CheckConstraint(
            "session_generation >= 1 AND created_at >= 0 AND updated_at >= created_at",
            name="ck_admin_master_identity_state",
        ),
    )


def downgrade() -> None:
    _begin_sqlite_transaction()
    if op.get_bind().execute(sa.text("SELECT 1 FROM admin_master_identity LIMIT 1")).first():
        raise RuntimeError("Downgrade would discard the administrator identity.")
    op.drop_table("admin_master_identity")
