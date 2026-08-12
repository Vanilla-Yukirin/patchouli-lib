"""Library- and caller-scoped durable content-mutation replay records."""

from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from patchouli_lib.library.models import OPAQUE_ID_LENGTH
from patchouli_lib.models import Base

DIGEST_BYTES = 32
METHOD_MAX_LENGTH = 16
ROUTE_TEMPLATE_MAX_LENGTH = 2_048
MEDIA_TYPE_MAX_LENGTH = 100
REPLAY_BODY_MAX_BYTES = 16 * 1024 * 1024
LOCATION_MAX_LENGTH = 2_048
ETAG_MAX_LENGTH = 128
REQUEST_ID_LENGTH = 36
CANONICAL_TIMESTAMP_LENGTH = 27


class IdempotencyRecord(Base):
    """One immutable successful response, keyed without retaining the raw key."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["caller_id", "library_id"],
            ["auth_callers.id", "auth_callers.library_id"],
            name="fk_idempotency_records_caller_library_callers",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(library_id) = 32 AND library_id NOT GLOB '*[^0-9a-f]*'",
            name="ck_idempotency_records_library_id_lower_hex",
        ),
        CheckConstraint(
            "length(caller_id) = 32 AND caller_id NOT GLOB '*[^0-9a-f]*'",
            name="ck_idempotency_records_caller_id_lower_hex",
        ),
        CheckConstraint(
            "typeof(method) = 'text' AND length(method) BETWEEN 1 AND 16 "
            "AND method NOT GLOB '*[^A-Z]*'",
            name="ck_idempotency_records_method",
        ),
        CheckConstraint(
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
        CheckConstraint(
            "typeof(key_digest) = 'blob' AND length(key_digest) = 32",
            name="ck_idempotency_records_key_digest",
        ),
        CheckConstraint(
            "typeof(request_fingerprint) = 'blob' AND length(request_fingerprint) = 32",
            name="ck_idempotency_records_request_fingerprint",
        ),
        CheckConstraint(
            "response_status BETWEEN 200 AND 299",
            name="ck_idempotency_records_response_status",
        ),
        CheckConstraint(
            "response_media_type = 'application/json'",
            name="ck_idempotency_records_response_media_type",
        ),
        CheckConstraint(
            "typeof(response_body) = 'blob' "
            "AND length(response_body) BETWEEN 1 AND 16777216 "
            "AND instr(response_body, x'00') = 0 "
            "AND json_valid(CAST(response_body AS TEXT)) = 1 "
            "AND json_type(CAST(response_body AS TEXT)) = 'object'",
            name="ck_idempotency_records_response_body",
        ),
        CheckConstraint(
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
        CheckConstraint(
            "typeof(response_etag) = 'text' "
            "AND length(response_etag) BETWEEN 3 AND 128 "
            "AND substr(response_etag, 1, 1) = char(34) "
            "AND substr(response_etag, -1, 1) = char(34) "
            "AND substr(response_etag, 2, length(response_etag) - 2) "
            "NOT GLOB '*[^!#-~]*'",
            name="ck_idempotency_records_response_etag",
        ),
        CheckConstraint(
            "typeof(original_request_id) = 'text' "
            "AND length(original_request_id) = 36 "
            "AND substr(original_request_id, 1, 4) = 'req_' "
            "AND substr(original_request_id, 5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_idempotency_records_original_request_id",
        ),
        CheckConstraint(
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
    )

    library_id: Mapped[str] = mapped_column(
        String(OPAQUE_ID_LENGTH),
        primary_key=True,
    )
    caller_id: Mapped[str] = mapped_column(
        String(OPAQUE_ID_LENGTH),
        primary_key=True,
    )
    method: Mapped[str] = mapped_column(String(METHOD_MAX_LENGTH), primary_key=True)
    route_template: Mapped[str] = mapped_column(
        String(ROUTE_TEMPLATE_MAX_LENGTH),
        primary_key=True,
    )
    key_digest: Mapped[bytes] = mapped_column(LargeBinary(DIGEST_BYTES), primary_key=True)
    request_fingerprint: Mapped[bytes] = mapped_column(
        LargeBinary(DIGEST_BYTES),
        nullable=False,
    )
    response_status: Mapped[int] = mapped_column(BigInteger, nullable=False)
    response_media_type: Mapped[str] = mapped_column(
        String(MEDIA_TYPE_MAX_LENGTH),
        nullable=False,
    )
    response_body: Mapped[bytes] = mapped_column(
        LargeBinary(REPLAY_BODY_MAX_BYTES),
        nullable=False,
    )
    response_location: Mapped[str | None] = mapped_column(String(LOCATION_MAX_LENGTH))
    response_etag: Mapped[str] = mapped_column(String(ETAG_MAX_LENGTH), nullable=False)
    original_request_id: Mapped[str] = mapped_column(
        String(REQUEST_ID_LENGTH),
        nullable=False,
    )
    original_request_timestamp: Mapped[str] = mapped_column(
        String(CANONICAL_TIMESTAMP_LENGTH),
        nullable=False,
    )


__all__ = ["IdempotencyRecord"]
