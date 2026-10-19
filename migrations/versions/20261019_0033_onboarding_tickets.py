"""Persist short-lived, single-use credential onboarding tickets.

Revision ID: 20261019_0033
Revises: 20261019_0032
Create Date: 2026-10-19
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261019_0033"
down_revision: str | None = "20261019_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auth_onboarding_tickets"
_TRIGGER = "trg_auth_onboarding_tickets_consume_once"
_DELETE_TRIGGER = "trg_auth_onboarding_tickets_no_delete"


def _begin() -> None:
    driver = op.get_bind().connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Onboarding ticket migration requires SQLite.")
    if not driver.in_transaction:
        op.get_bind().exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    _begin()
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("code_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("identity_id", sa.String(32), nullable=False),
        sa.Column("session_generation", sa.BigInteger(), nullable=False),
        sa.Column("session_fingerprint", sa.LargeBinary(32), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("consumed_at", sa.BigInteger(), nullable=True),
        sa.Column("credential_id", sa.String(32), nullable=True),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["auth_credentials.id"],
            name="fk_auth_onboarding_tickets_credential",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("code_hash", name="uq_auth_onboarding_tickets_code_hash"),
        sa.UniqueConstraint("credential_id", name="uq_auth_onboarding_tickets_credential"),
        sa.CheckConstraint(
            "length(id) = 32 AND id NOT GLOB '*[^0-9a-f]*'",
            name="ck_auth_onboarding_tickets_id",
        ),
        sa.CheckConstraint(
            "typeof(code_hash) = 'blob' AND length(code_hash) = 32",
            name="ck_auth_onboarding_tickets_hash",
        ),
        sa.CheckConstraint(
            "length(identity_id) = 32 AND identity_id NOT GLOB '*[^0-9a-f]*' "
            "AND typeof(session_generation) = 'integer' AND session_generation >= 1",
            name="ck_auth_onboarding_tickets_identity",
        ),
        sa.CheckConstraint(
            "typeof(session_fingerprint) = 'blob' AND length(session_fingerprint) = 32",
            name="ck_auth_onboarding_tickets_fingerprint",
        ),
        sa.CheckConstraint(
            "typeof(created_at) = 'integer' AND created_at >= 0 "
            "AND typeof(expires_at) = 'integer' AND expires_at = created_at + 300000000",
            name="ck_auth_onboarding_tickets_clock",
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL AND credential_id IS NULL) OR "
            "(typeof(consumed_at) = 'integer' AND consumed_at >= created_at "
            "AND consumed_at < expires_at AND credential_id IS NOT NULL)",
            name="ck_auth_onboarding_tickets_consumption",
        ),
    )
    op.execute(
        f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE ON {_TABLE} "
        "WHEN OLD.consumed_at IS NOT NULL OR NEW.consumed_at IS NULL "
        "OR NEW.credential_id IS NULL OR NEW.id != OLD.id OR NEW.code_hash != OLD.code_hash "
        "OR NEW.identity_id != OLD.identity_id "
        "OR NEW.session_generation != OLD.session_generation "
        "OR NEW.session_fingerprint != OLD.session_fingerprint "
        "OR NEW.created_at != OLD.created_at OR NEW.expires_at != OLD.expires_at "
        "BEGIN SELECT RAISE(ABORT, 'Onboarding tickets may only be consumed once'); END"
    )
    op.execute(
        f"CREATE TRIGGER {_DELETE_TRIGGER} BEFORE DELETE ON {_TABLE} BEGIN "
        "SELECT RAISE(ABORT, 'Onboarding ticket history is immutable'); END"
    )


def downgrade() -> None:
    _begin()
    if op.get_bind().exec_driver_sql(f"SELECT 1 FROM {_TABLE} LIMIT 1").first():
        raise RuntimeError("Cannot downgrade while onboarding ticket records exist.")
    op.execute(f"DROP TRIGGER {_TRIGGER}")
    op.execute(f"DROP TRIGGER {_DELETE_TRIGGER}")
    op.drop_table(_TABLE)
