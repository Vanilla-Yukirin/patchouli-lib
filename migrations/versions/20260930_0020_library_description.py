"""Add an optional description to each Library without losing legacy rows.

Revision ID: 20260930_0020
Revises: 20260930_0019
Create Date: 2026-09-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0020"
down_revision: str | None = "20260930_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column(
            "description",
            sa.Text(),
            sa.CheckConstraint("length(description) <= 4000", name="ck_libraries_description"),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if (
        connection.exec_driver_sql(
            "SELECT 1 FROM libraries WHERE description != '' LIMIT 1"
        ).first()
        is not None
    ):
        raise RuntimeError("Cannot downgrade while Library descriptions would be lost.")
    op.drop_column("libraries", "description")
