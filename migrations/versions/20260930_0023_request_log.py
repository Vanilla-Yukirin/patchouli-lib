"""Add disposable HTTP request metadata, separate from immutable domain audits.

Revision ID: 20260930_0023
Revises: 20260930_0022
Create Date: 2026-09-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0023"
down_revision: str | None = "20260930_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_request_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("method", sa.String(7), nullable=False),
        sa.Column("route_template", sa.String(240), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column("completion", sa.String(11), nullable=False),
        sa.Column("occurred_at", sa.BigInteger(), nullable=False),
        sa.Column("duration_us", sa.BigInteger(), nullable=False),
        sa.Column("caller_id", sa.String(32)),
        sa.Column("home_library_id", sa.String(32)),
        sa.Column("credential_id", sa.String(32)),
        sa.CheckConstraint(
            "typeof(request_id) = 'text' AND length(request_id) = 36 "
            "AND substr(request_id, 1, 4) = 'req_' "
            "AND substr(request_id, 5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_api_request_log_request_id",
        ),
        sa.CheckConstraint(
            "method IN ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'OTHER')",
            name="ck_api_request_log_method",
        ),
        sa.CheckConstraint(
            "typeof(route_template) = 'text' AND length(route_template) BETWEEN 1 AND 240 "
            "AND (route_template = '<unmatched>' OR "
            "(substr(route_template, 1, 1) = '/' "
            "AND instr(route_template, '?') = 0 AND instr(route_template, '#') = 0 "
            "AND instr(route_template, '\\') = 0))",
            name="ck_api_request_log_route_template",
        ),
        sa.CheckConstraint(
            "completion IN ('completed', 'interrupted') AND "
            "(status_code IS NULL OR (typeof(status_code) = 'integer' "
            "AND status_code BETWEEN 100 AND 599)) AND "
            "(completion = 'interrupted' OR status_code IS NOT NULL)",
            name="ck_api_request_log_completion",
        ),
        sa.CheckConstraint(
            "typeof(occurred_at) = 'integer' AND occurred_at >= 0 "
            "AND typeof(duration_us) = 'integer' AND duration_us >= 0",
            name="ck_api_request_log_times",
        ),
        sa.CheckConstraint(
            "(caller_id IS NULL OR (typeof(caller_id) = 'text' AND length(caller_id) = 32 "
            "AND caller_id NOT GLOB '*[^0-9a-f]*')) AND "
            "(home_library_id IS NULL OR (typeof(home_library_id) = 'text' "
            "AND length(home_library_id) = 32 "
            "AND home_library_id NOT GLOB '*[^0-9a-f]*')) AND "
            "(credential_id IS NULL OR (typeof(credential_id) = 'text' "
            "AND length(credential_id) = 32 "
            "AND credential_id NOT GLOB '*[^0-9a-f]*')) AND "
            "(credential_id IS NULL OR caller_id IS NOT NULL)",
            name="ck_api_request_log_identity",
        ),
        sa.UniqueConstraint("request_id", name="uq_api_request_log_request_id"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_api_request_log_retention", "api_request_log", ["occurred_at", "id"])
    op.create_index(
        "ix_api_request_log_actor_recent",
        "api_request_log",
        ["home_library_id", "caller_id", "occurred_at", "id"],
    )


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("SELECT 1 FROM api_request_log LIMIT 1").first():
        raise RuntimeError("Cannot discard stored HTTP request records.")
    op.drop_index("ix_api_request_log_actor_recent", table_name="api_request_log")
    op.drop_index("ix_api_request_log_retention", table_name="api_request_log")
    op.drop_table("api_request_log")
