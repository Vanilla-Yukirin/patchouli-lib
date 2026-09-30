"""Disposable request records; unlike domain audits, these rows may be deleted."""

from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from patchouli_lib.models import Base


class RequestLogRecord(Base):
    __tablename__ = "api_request_log"
    __table_args__ = (
        CheckConstraint(
            "typeof(request_id) = 'text' AND length(request_id) = 36 "
            "AND substr(request_id, 1, 4) = 'req_' "
            "AND substr(request_id, 5) NOT GLOB '*[^0-9a-f]*'",
            name="ck_api_request_log_request_id",
        ),
        CheckConstraint(
            "method IN ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'OTHER')",
            name="ck_api_request_log_method",
        ),
        CheckConstraint(
            "typeof(route_template) = 'text' AND length(route_template) BETWEEN 1 AND 240 "
            "AND (route_template = '<unmatched>' OR "
            "(substr(route_template, 1, 1) = '/' "
            "AND instr(route_template, '?') = 0 AND instr(route_template, '#') = 0 "
            "AND instr(route_template, '\\') = 0))",
            name="ck_api_request_log_route_template",
        ),
        CheckConstraint(
            "completion IN ('completed', 'interrupted') AND "
            "(status_code IS NULL OR (typeof(status_code) = 'integer' "
            "AND status_code BETWEEN 100 AND 599)) AND "
            "(completion = 'interrupted' OR status_code IS NOT NULL)",
            name="ck_api_request_log_completion",
        ),
        CheckConstraint(
            "typeof(occurred_at) = 'integer' AND occurred_at >= 0 "
            "AND typeof(duration_us) = 'integer' AND duration_us >= 0",
            name="ck_api_request_log_times",
        ),
        CheckConstraint(
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
        UniqueConstraint("request_id", name="uq_api_request_log_request_id"),
        Index("ix_api_request_log_retention", "occurred_at", "id"),
        Index(
            "ix_api_request_log_actor_recent",
            "home_library_id",
            "caller_id",
            "occurred_at",
            "id",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    method: Mapped[str] = mapped_column(String(7), nullable=False)
    route_template: Mapped[str] = mapped_column(String(240), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    completion: Mapped[str] = mapped_column(String(11), nullable=False)
    occurred_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    caller_id: Mapped[str | None] = mapped_column(String(32))
    home_library_id: Mapped[str | None] = mapped_column(String(32))
    credential_id: Mapped[str | None] = mapped_column(String(32))
