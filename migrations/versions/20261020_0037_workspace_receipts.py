"""Durable, identity-scoped receipts for browser file-set saves.

Revision ID: 20261020_0037
Revises: 20261020_0036
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261020_0037"
down_revision: str | None = "20261020_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _begin() -> None:
    driver = op.get_bind().connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Workspace receipt migration requires SQLite.")
    if not driver.in_transaction:
        op.get_bind().exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    _begin()
    op.create_table(
        "admin_workspace_save_receipts",
        sa.Column("identity_id", sa.Text(), primary_key=True),
        sa.Column("key_digest", sa.LargeBinary(32), primary_key=True),
        sa.Column("request_fingerprint", sa.LargeBinary(32), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("response_sha256", sa.LargeBinary(32), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("length(identity_id) = 32 AND identity_id NOT GLOB '*[^0-9a-f]*'"),
        sa.CheckConstraint("typeof(key_digest) = 'blob' AND length(key_digest) = 32"),
        sa.CheckConstraint(
            "typeof(request_fingerprint) = 'blob' AND length(request_fingerprint) = 32"
        ),
        sa.CheckConstraint("typeof(created_at) = 'integer' AND created_at >= 0"),
        sa.CheckConstraint("json_valid(response_json)"),
        sa.CheckConstraint("typeof(response_sha256) = 'blob' AND length(response_sha256) = 32"),
    )
    for operation in ("UPDATE", "DELETE"):
        op.get_bind().exec_driver_sql(
            f"CREATE TRIGGER admin_workspace_save_receipts_no_{operation.lower()} "
            f"BEFORE {operation} ON admin_workspace_save_receipts "
            "BEGIN SELECT RAISE(ABORT, 'Workspace save receipt is immutable'); END"
        )


def downgrade() -> None:
    _begin()
    if op.get_bind().exec_driver_sql("SELECT 1 FROM admin_workspace_save_receipts LIMIT 1").first():
        raise RuntimeError("Cannot downgrade while browser save receipts exist.")
    for operation in ("update", "delete"):
        op.get_bind().exec_driver_sql(f"DROP TRIGGER admin_workspace_save_receipts_no_{operation}")
    op.drop_table("admin_workspace_save_receipts")
