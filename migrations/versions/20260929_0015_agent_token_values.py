"""Store revealable values only for newly issued Agent credentials.

Revision ID: 20260929_0015
Revises: 20260929_0014
Create Date: 2026-09-29

Existing credentials remain verifier-only and cannot be backfilled. The new
table and every SQLite backup containing it must be treated as secret data.
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0015"
down_revision: str | None = "20260929_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _begin_sqlite_transaction() -> None:
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Agent token value migration requires SQLite.")
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    _begin_sqlite_transaction()
    op.create_table(
        "auth_agent_token_values",
        sa.Column("credential_id", sa.String(length=32), nullable=False),
        sa.Column("token_value", sa.String(length=71), nullable=False),
        sa.PrimaryKeyConstraint("credential_id", name="pk_auth_agent_token_values"),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["auth_credentials.id"],
            name="fk_auth_agent_token_values_credential",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "typeof(token_value) = 'text' AND length(token_value) = 71 "
            "AND substr(token_value, 1, 5) = 'plb1.'",
            name="ck_auth_agent_token_values_format",
        ),
    )
    # A foreign key only identifies a credential. Enforce the Agent kind and
    # selector match in the database so direct inserts cannot attach a value to
    # an operator credential or another Agent's token selector.
    op.execute(
        "CREATE TRIGGER trg_auth_agent_token_values_agent_only "
        "BEFORE INSERT ON auth_agent_token_values "
        "WHEN NOT EXISTS ("
        "SELECT 1 FROM auth_credentials AS c "
        "JOIN auth_callers AS a ON a.id = c.caller_id AND a.library_id = c.library_id "
        "WHERE c.id = NEW.credential_id AND a.kind = 'agent' "
        "AND a.disabled_at IS NULL AND c.revoked_at IS NULL AND c.rotated_at IS NULL "
        "AND c.token_version = 1 "
        "AND substr(NEW.token_value, 6, 22) = c.selector) "
        "BEGIN SELECT RAISE(ABORT, 'Agent token value requires active Agent credential'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_auth_agent_token_values_immutable "
        "BEFORE UPDATE ON auth_agent_token_values "
        "BEGIN SELECT RAISE(ABORT, 'Agent token value is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_auth_agent_token_values_revoke "
        "AFTER UPDATE OF revoked_at, rotated_at ON auth_credentials "
        "WHEN NEW.revoked_at IS NOT NULL OR NEW.rotated_at IS NOT NULL "
        "BEGIN DELETE FROM auth_agent_token_values "
        "WHERE credential_id = NEW.id; END"
    )
    op.execute(
        "CREATE TRIGGER trg_auth_agent_token_values_disable "
        "AFTER UPDATE OF disabled_at ON auth_callers "
        "WHEN NEW.disabled_at IS NOT NULL "
        "BEGIN DELETE FROM auth_agent_token_values "
        "WHERE credential_id IN (SELECT id FROM auth_credentials "
        "WHERE caller_id = NEW.id AND library_id = NEW.library_id); END"
    )


def downgrade() -> None:
    _begin_sqlite_transaction()
    if op.get_bind().execute(sa.text("SELECT 1 FROM auth_agent_token_values LIMIT 1")).first():
        raise RuntimeError("Downgrade would discard revealable Agent token values.")
    op.execute("DROP TRIGGER trg_auth_agent_token_values_disable")
    op.execute("DROP TRIGGER trg_auth_agent_token_values_revoke")
    op.execute("DROP TRIGGER trg_auth_agent_token_values_immutable")
    op.execute("DROP TRIGGER trg_auth_agent_token_values_agent_only")
    op.drop_table("auth_agent_token_values")
