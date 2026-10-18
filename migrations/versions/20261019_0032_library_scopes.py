"""Add independent include/exclude scopes without expanding existing grants.

Revision ID: 20261019_0032
Revises: 20261018_0031
Create Date: 2026-10-19
"""

import sqlite3
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261019_0032"
down_revision: str | None = "20261018_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "auth_credential_library_policies"
_TRIGGER = "trg_auth_credential_library_policies_immutable_update"


def _begin() -> None:
    connection = op.get_bind()
    driver = connection.connection.driver_connection
    if not isinstance(driver, sqlite3.Connection):
        raise RuntimeError("Library scope migration requires SQLite.")
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    _begin()
    # Inline checks let SQLite add columns without rebuilding the referenced table.
    for action in ("read", "write"):
        op.add_column(
            _TABLE,
            sa.Column(
                f"{action}_mode",
                sa.String(7),
                sa.CheckConstraint(
                    f"{action}_mode IN ('include', 'exclude')",
                    name=f"ck_auth_credential_library_policies_{action}_mode",
                ),
                nullable=False,
                server_default="include",
            ),
        )
    op.add_column(
        _TABLE,
        sa.Column(
            "scope_revision",
            sa.BigInteger(),
            sa.CheckConstraint(
                "typeof(scope_revision) = 'integer' AND scope_revision >= 0",
                name="ck_auth_credential_library_policies_scope_revision",
            ),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.execute(f"DROP TRIGGER {_TRIGGER}")
    op.execute(
        f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE ON {_TABLE} "
        "WHEN NEW.credential_id != OLD.credential_id OR NEW.caller_id != OLD.caller_id "
        "OR NEW.home_library_id != OLD.home_library_id OR NEW.mode != OLD.mode "
        "OR NEW.created_at != OLD.created_at "
        "OR NEW.scope_revision NOT IN (OLD.scope_revision, OLD.scope_revision + 1) "
        "OR ((NEW.read_mode != OLD.read_mode OR NEW.write_mode != OLD.write_mode) "
        "AND NEW.scope_revision != OLD.scope_revision + 1) "
        "BEGIN SELECT RAISE(ABORT, 'Credential policy mode is immutable'); END"
    )


def downgrade() -> None:
    _begin()
    connection = op.get_bind()
    if connection.exec_driver_sql(
        f"SELECT 1 FROM {_TABLE} WHERE read_mode != 'include' OR write_mode != 'include' LIMIT 1"
    ).first():
        raise RuntimeError("Cannot downgrade while exclude Library scopes exist.")
    op.execute(f"DROP TRIGGER {_TRIGGER}")
    for column in ("scope_revision", "write_mode", "read_mode"):
        op.drop_column(_TABLE, column)
    op.execute(
        f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE ON {_TABLE} BEGIN "
        "SELECT RAISE(ABORT, 'Credential policy mode is immutable'); END"
    )
