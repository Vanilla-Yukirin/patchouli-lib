"""Index recent audit events by actor identity and outcome.

Revision ID: 20260930_0021
Revises: 20260930_0020
Create Date: 2026-09-30
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260930_0021"
down_revision: str | None = "20260930_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_auth_audit_events_actor_recent"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "auth_audit_events",
        [
            "actor_home_library_id",
            "actor_caller_id",
            "outcome",
            "occurred_at",
            "id",
        ],
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="auth_audit_events")
