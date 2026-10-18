"""Version Library-explicit Caller Page lifecycle without rebuilding tables.

Revision ID: 20261003_0030
Revises: 20261002_0029
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from alembic import op

revision: str = "20261003_0030"
down_revision: str | None = "20261002_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _validate(*, upgrading: bool) -> None:
    connection = op.get_bind()
    raw = connection.connection.driver_connection
    if not isinstance(raw, sqlite3.Connection):
        raise RuntimeError("Library Page lifecycle requires SQLite.")
    if raw.in_transaction:
        connection.commit()
    connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    expected = down_revision if upgrading else revision
    assert expected is not None
    if (
        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        != expected
    ):
        raise RuntimeError("Unexpected Library Page lifecycle behavior version.")
    if not upgrading and (
        connection.exec_driver_sql(
            "SELECT 1 FROM idempotency_records WHERE route_template IN "
            "('/api/v1/libraries/{library_id}/pages/{page_id}', "
            "'/api/v1/libraries/{library_id}/pages/{page_id}/restore') LIMIT 1"
        ).first()
        or connection.exec_driver_sql(
            "SELECT 1 FROM auth_audit_events WHERE action IN "
            "('content.page.delete','content.page.restore') LIMIT 1"
        ).first()
    ):
        raise RuntimeError("Cannot discard Library Page lifecycle history or successes.")
    from patchouli_lib.backup.validation import (
        _require_actor_home_graph,
        _require_idempotency_graph,
        _require_lifecycle_graph,
        _require_master_audit,
        _require_page_moves,
        _require_schema,
    )

    _require_schema(raw, expected)
    _require_lifecycle_graph(raw, expected)
    _require_actor_home_graph(raw, expected)
    _require_idempotency_graph(raw, expected)
    _require_master_audit(raw, expected)
    _require_page_moves(raw, schema_revision=expected)
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Library Page lifecycle relationships are inconsistent.")


def upgrade() -> None:
    _validate(upgrading=True)


def downgrade() -> None:
    _validate(upgrading=False)
