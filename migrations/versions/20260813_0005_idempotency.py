"""Create durable content-mutation idempotency replay storage.

Revision ID: 20260813_0005
Revises: 20260813_0004
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0005"
down_revision: str | None = "20260813_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("library_id", sa.String(length=32), nullable=False),
        sa.Column("caller_id", sa.String(length=32), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("route_template", sa.String(length=2048), nullable=False),
        sa.Column("key_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("request_fingerprint", sa.LargeBinary(length=32), nullable=False),
        sa.Column("response_status", sa.BigInteger(), nullable=False),
        sa.Column("response_media_type", sa.String(length=100), nullable=False),
        sa.Column("response_body", sa.LargeBinary(length=16777216), nullable=False),
        sa.Column("response_location", sa.String(length=2048), nullable=True),
        sa.Column("response_etag", sa.String(length=128), nullable=False),
        sa.Column("original_request_id", sa.String(length=36), nullable=False),
        sa.Column("original_request_timestamp", sa.String(length=27), nullable=False),
        sa.CheckConstraint(
            "length(library_id) = 32 AND library_id NOT GLOB '*[^0-9a-f]*'",
            name="ck_idempotency_records_library_id_lower_hex",
        ),
        sa.CheckConstraint(
            "length(caller_id) = 32 AND caller_id NOT GLOB '*[^0-9a-f]*'",
            name="ck_idempotency_records_caller_id_lower_hex",
        ),
        sa.CheckConstraint(
            "typeof(method) = 'text' AND length(method) BETWEEN 1 AND 16 "
            "AND method NOT GLOB '*[^A-Z]*'",
            name="ck_idempotency_records_method",
        ),
        sa.CheckConstraint(
            "typeof(route_template) = 'text' "
            "AND length(route_template) BETWEEN 1 AND 2048 "
            "AND substr(route_template, 1, 8) = '/api/v1/' "
            "AND instr(route_template, '{') > 0 AND instr(route_template, '}') > 0 "
            "AND instr(route_template, '%') = 0 AND instr(route_template, '?') = 0 "
            "AND instr(route_template, '#') = 0 AND instr(route_template, char(92)) = 0 "
            "AND instr(route_template, char(0)) = 0 "
            "AND route_template NOT GLOB '*[^!-~]*'",
            name="ck_idempotency_records_route_template",
        ),
        sa.CheckConstraint(
            "typeof(key_digest) = 'blob' AND length(key_digest) = 32",
            name="ck_idempotency_records_key_digest",
        ),
        sa.CheckConstraint(
            "typeof(request_fingerprint) = 'blob' AND length(request_fingerprint) = 32",
            name="ck_idempotency_records_request_fingerprint",
        ),
        sa.CheckConstraint(
            "response_status BETWEEN 200 AND 299",
            name="ck_idempotency_records_response_status",
        ),
        sa.CheckConstraint(
            "response_media_type = 'application/json'",
            name="ck_idempotency_records_response_media_type",
        ),
        sa.CheckConstraint(
            "typeof(response_body) = 'blob' "
            "AND length(response_body) BETWEEN 1 AND 16777216 "
            "AND instr(response_body, x'00') = 0 "
            "AND json_valid(CAST(response_body AS TEXT)) = 1 "
            "AND json_type(CAST(response_body AS TEXT)) = 'object'",
            name="ck_idempotency_records_response_body",
        ),
        sa.CheckConstraint(
            "response_location IS NULL OR (typeof(response_location) = 'text' "
            "AND length(response_location) BETWEEN 9 AND 2048 "
            "AND substr(response_location, 1, 8) = '/api/v1/' "
            "AND instr(response_location, '%') = 0 "
            "AND instr(response_location, '?') = 0 "
            "AND instr(response_location, '#') = 0 "
            "AND instr(response_location, char(92)) = 0 "
            "AND instr(response_location, char(0)) = 0 "
            "AND response_location NOT GLOB '*[^!-~]*')",
            name="ck_idempotency_records_response_location",
        ),
        sa.CheckConstraint(
            "typeof(response_etag) = 'text' "
            "AND length(response_etag) BETWEEN 3 AND 128 "
            "AND substr(response_etag, 1, 1) = char(34) "
            "AND substr(response_etag, -1, 1) = char(34) "
            "AND substr(response_etag, 2, length(response_etag) - 2) "
            "NOT GLOB '*[^!#-~]*'",
            name="ck_idempotency_records_response_etag",
        ),
        sa.CheckConstraint(
            "typeof(original_request_id) = 'text' "
            "AND length(original_request_id) = 36 "
            "AND substr(original_request_id, 1, 4) = 'req_' "
            "AND substr(original_request_id, 5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_idempotency_records_original_request_id",
        ),
        sa.CheckConstraint(
            "typeof(original_request_timestamp) = 'text' "
            "AND length(original_request_timestamp) = 27 "
            "AND original_request_timestamp GLOB "
            "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T"
            "[0-9][0-9]:[0-9][0-9]:[0-9][0-9]."
            "[0-9][0-9][0-9][0-9][0-9][0-9]Z' "
            "AND CAST(substr(original_request_timestamp, 1, 4) AS INTEGER) "
            "BETWEEN 1 AND 9999 "
            "AND date(substr(original_request_timestamp, 1, 10), '+0 days') IS NOT NULL "
            "AND date(substr(original_request_timestamp, 1, 10), '+0 days') "
            "= substr(original_request_timestamp, 1, 10) "
            "AND time(substr(original_request_timestamp, 12, 8), '+0 seconds') IS NOT NULL "
            "AND time(substr(original_request_timestamp, 12, 8), '+0 seconds') "
            "= substr(original_request_timestamp, 12, 8)",
            name="ck_idempotency_records_original_request_timestamp",
        ),
        sa.ForeignKeyConstraint(
            ["caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_idempotency_records_caller_library_callers",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "library_id",
            "caller_id",
            "method",
            "route_template",
            "key_digest",
            name="pk_idempotency_records",
        ),
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_idempotency_records_immutable_update "
            "BEFORE UPDATE ON idempotency_records BEGIN "
            "SELECT RAISE(ABORT, 'Idempotency records are immutable.'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_idempotency_records_no_delete "
            "BEFORE DELETE ON idempotency_records BEGIN "
            "SELECT RAISE(ABORT, 'Idempotency records do not expire automatically.'); END"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER trg_idempotency_records_no_delete"))
    op.execute(sa.text("DROP TRIGGER trg_idempotency_records_immutable_update"))
    op.execute(sa.text("DELETE FROM idempotency_records"))
    op.drop_table("idempotency_records")
