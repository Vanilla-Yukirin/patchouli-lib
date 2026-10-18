"""Add an inactive, opt-in per-credential Library authorization substrate.

Revision ID: 20260929_0014
Revises: 20260929_0013
Create Date: 2026-09-29
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0014"
down_revision: str | None = "20260929_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _begin_sqlite_transaction() -> None:
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Credential Library policy migration requires SQLite.")
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    _begin_sqlite_transaction()
    # No row means that the existing Section-scoped authorization remains in effect.
    # An explicit row switches this credential to the new default-deny mode.
    op.create_table(
        "auth_credential_library_policies",
        sa.Column("credential_id", sa.String(length=32), nullable=False),
        sa.Column("caller_id", sa.String(length=32), nullable=False),
        sa.Column("home_library_id", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "credential_id",
            "caller_id",
            "home_library_id",
            name="pk_auth_credential_library_policies",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id", "caller_id", "home_library_id"],
            ["auth_credentials.id", "auth_credentials.caller_id", "auth_credentials.library_id"],
            name="fk_auth_credential_library_policies_exact_credential",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "mode = 'library_grants'", name="ck_auth_credential_library_policies_mode"
        ),
        sa.CheckConstraint(
            "typeof(created_at) = 'integer' AND created_at >= 0",
            name="ck_auth_credential_library_policies_created_at",
        ),
    )
    op.create_table(
        "auth_credential_library_grants",
        sa.Column("credential_id", sa.String(length=32), nullable=False),
        sa.Column("caller_id", sa.String(length=32), nullable=False),
        sa.Column("home_library_id", sa.String(length=32), nullable=False),
        sa.Column("target_library_id", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=5), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "credential_id",
            "caller_id",
            "home_library_id",
            "target_library_id",
            "action",
            name="pk_auth_credential_library_grants",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id", "caller_id", "home_library_id"],
            [
                "auth_credential_library_policies.credential_id",
                "auth_credential_library_policies.caller_id",
                "auth_credential_library_policies.home_library_id",
            ],
            name="fk_auth_credential_library_grants_exact_policy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_library_id"],
            ["libraries.id"],
            name="fk_auth_credential_library_grants_target_library",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "action IN ('read', 'write')", name="ck_auth_credential_library_grants_action"
        ),
        sa.CheckConstraint(
            "typeof(created_at) = 'integer' AND created_at >= 0",
            name="ck_auth_credential_library_grants_created_at",
        ),
    )
    op.create_index(
        "ix_auth_credential_library_grants_target_action",
        "auth_credential_library_grants",
        ["target_library_id", "action"],
    )
    # A mode switch must never be deleted or altered back into legacy behavior.
    op.execute(
        "CREATE TRIGGER trg_auth_credential_library_policies_immutable_update "
        "BEFORE UPDATE ON auth_credential_library_policies BEGIN "
        "SELECT RAISE(ABORT, 'Credential policy mode is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_auth_credential_library_policies_immutable_delete "
        "BEFORE DELETE ON auth_credential_library_policies BEGIN "
        "SELECT RAISE(ABORT, 'Credential policy mode is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_auth_credential_library_policies_agent_only "
        "BEFORE INSERT ON auth_credential_library_policies "
        "WHEN NOT EXISTS (SELECT 1 FROM auth_credentials AS c "
        "JOIN auth_callers AS a ON a.id = c.caller_id AND a.library_id = c.library_id "
        "WHERE c.id = NEW.credential_id AND c.caller_id = NEW.caller_id "
        "AND c.library_id = NEW.home_library_id AND a.kind = 'agent') "
        "BEGIN SELECT RAISE(ABORT, 'Library grants require an Agent credential'); END"
    )


def downgrade() -> None:
    _begin_sqlite_transaction()
    connection = op.get_bind()
    for table_name in (
        "auth_credential_library_grants",
        "auth_credential_library_policies",
    ):
        if connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).scalar_one_or_none():
            raise RuntimeError("Downgrade would discard credential Library policy data.")
    op.execute("DROP TRIGGER trg_auth_credential_library_policies_agent_only")
    op.execute("DROP TRIGGER trg_auth_credential_library_policies_immutable_delete")
    op.execute("DROP TRIGGER trg_auth_credential_library_policies_immutable_update")
    op.drop_index(
        "ix_auth_credential_library_grants_target_action",
        table_name="auth_credential_library_grants",
    )
    op.drop_table("auth_credential_library_grants")
    op.drop_table("auth_credential_library_policies")
