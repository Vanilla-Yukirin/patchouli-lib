"""Record successful master-session administration without a bearer actor.

Revision ID: 20260929_0018
Revises: 20260929_0017
Create Date: 2026-09-29

The opaque identity is a historical snapshot, not a foreign key to the
singleton verifier row: a future local recovery may replace that row without
discarding earlier audit history.
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0018"
down_revision: str | None = "20260929_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _begin_sqlite_transaction() -> None:
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Master audit migration requires SQLite.")
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    _begin_sqlite_transaction()
    op.create_table(
        "admin_master_audit_events",
        sa.Column("id", sa.String(length=32), primary_key=True, nullable=False),
        sa.Column("identity_id", sa.String(length=32), nullable=False),
        sa.Column("session_generation", sa.BigInteger(), nullable=False),
        sa.Column("session_fingerprint", sa.LargeBinary(length=32), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("target_type", sa.String(length=100), nullable=False),
        sa.Column("target_id", sa.String(length=200), nullable=False),
        sa.Column("occurred_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_admin_master_audit_id",
        ),
        sa.CheckConstraint(
            "length(identity_id) = 32 AND identity_id NOT GLOB '*[^0-9a-f]*'",
            name="ck_admin_master_audit_identity",
        ),
        sa.CheckConstraint(
            "session_generation >= 1 AND occurred_at >= 0",
            name="ck_admin_master_audit_clock",
        ),
        sa.CheckConstraint(
            "typeof(session_fingerprint) = 'blob' AND length(session_fingerprint) = 32",
            name="ck_admin_master_audit_session_fingerprint",
        ),
        sa.CheckConstraint(
            "length(action) BETWEEN 1 AND 100 AND action = trim(action) "
            "AND action NOT GLOB '*[^!-~]*'",
            name="ck_admin_master_audit_action",
        ),
        sa.CheckConstraint(
            "length(target_type) BETWEEN 1 AND 100 AND target_type = trim(target_type) "
            "AND target_type NOT GLOB '*[^a-z_]*'",
            name="ck_admin_master_audit_target_type",
        ),
        sa.CheckConstraint(
            "length(target_id) BETWEEN 1 AND 200 AND target_id = trim(target_id) "
            "AND target_id NOT GLOB '*[^!-~]*'",
            name="ck_admin_master_audit_target_id",
        ),
        sa.CheckConstraint(
            "action != 'auth.agent_token.reveal' OR "
            "(target_type = 'credential' AND length(target_id) = 32 "
            "AND target_id NOT GLOB '*[^0-9a-f]*')",
            name="ck_admin_master_audit_reveal_target",
        ),
    )
    op.execute(
        "CREATE TRIGGER trg_admin_master_audit_no_update "
        "BEFORE UPDATE ON admin_master_audit_events BEGIN "
        "SELECT RAISE(ABORT, 'Master audit is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_admin_master_audit_no_delete "
        "BEFORE DELETE ON admin_master_audit_events BEGIN "
        "SELECT RAISE(ABORT, 'Master audit is immutable'); END"
    )


def downgrade() -> None:
    _begin_sqlite_transaction()
    existing = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM admin_master_audit_events LIMIT 1"))
        .scalar_one_or_none()
    )
    if existing is not None:
        raise RuntimeError("Downgrade would discard master audit events.")
    op.execute("DROP TRIGGER trg_admin_master_audit_no_delete")
    op.execute("DROP TRIGGER trg_admin_master_audit_no_update")
    op.drop_table("admin_master_audit_events")
