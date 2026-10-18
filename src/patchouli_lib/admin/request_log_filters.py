"""Bounded admin request-log filters and session-bound keyset cursors."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from starlette.datastructures import QueryParams

from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.api.contracts import format_rfc3339_utc, parse_rfc3339_utc

_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "OTHER"})
_ROUTE = re.compile(
    r"/api(?:/(?:[A-Za-z0-9._~-]+|\{[A-Za-z_][A-Za-z0-9_]*(?::path)?\}))*\Z",
    re.ASCII,
)
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+\Z", re.ASCII)
_MAX_SQLITE_INT = (1 << 63) - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CURSOR_DOMAIN = b"patchouli-lib:admin-request-log-cursor:v1\x00"


class InvalidRequestLogFilter(ValueError):
    """A filter is malformed; never include its untrusted value in a response."""


class InvalidRequestLogCursor(ValueError):
    """A cursor is malformed or belongs to another session or filter set."""


@dataclass(frozen=True, slots=True)
class RequestLogFilters:
    route: str | None = None
    method: str | None = None
    status: str | None = None
    since: str | None = None
    until: str | None = None
    since_us: int | None = None
    until_us: int | None = None

    def query_items(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (key, value)
            for key, value in (
                ("route", self.route),
                ("method", self.method),
                ("status", self.status),
                ("since", self.since),
                ("until", self.until),
            )
            if value is not None
        )


def parse_request_log_filters(params: QueryParams) -> RequestLogFilters:
    """Accept exact router templates, never incoming paths or query strings."""

    if any(
        name not in {"route", "method", "status", "since", "until", "before", "lang"}
        for name in params
    ):
        raise InvalidRequestLogFilter

    def one(name: str, *, max_length: int) -> str | None:
        values = params.getlist(name)
        if len(values) > 1:
            raise InvalidRequestLogFilter
        if not values or values[0] == "":
            return None
        value = values[0]
        if len(value) > max_length:
            raise InvalidRequestLogFilter
        return value

    route = one("route", max_length=240)
    if route is not None and route != "<unmatched>" and _ROUTE.fullmatch(route) is None:
        raise InvalidRequestLogFilter
    method = one("method", max_length=7)
    if method is not None and method not in _METHODS:
        raise InvalidRequestLogFilter
    status = one("status", max_length=11)
    if (
        status is not None
        and status != "interrupted"
        and (
            not status.isascii()
            or not status.isdecimal()
            or len(status) != 3
            or not 100 <= int(status) <= 599
        )
    ):
        raise InvalidRequestLogFilter
    since, since_us = _time_filter(one("since", max_length=35))
    until, until_us = _time_filter(one("until", max_length=35))
    if since_us is not None and until_us is not None and since_us >= until_us:
        raise InvalidRequestLogFilter
    return RequestLogFilters(route, method, status, since, until, since_us, until_us)


def _time_filter(value: str | None) -> tuple[str | None, int | None]:
    if value is None:
        return None, None
    try:
        parsed = parse_rfc3339_utc(value)
        delta = parsed - _EPOCH
        micros = ((delta.days * 86_400 + delta.seconds) * 1_000_000) + delta.microseconds
        if not 0 <= micros <= _MAX_SQLITE_INT:
            raise ValueError
        return format_rfc3339_utc(parsed), micros
    except (OverflowError, ValueError) as exc:
        raise InvalidRequestLogFilter from exc


class RequestLogCursorCodec:
    """Domain-separated, session-bound pagination using the existing server secret."""

    def __init__(
        self,
        *,
        signing_secret: bytes,
        session: MasterAdminSession,
        filters: RequestLogFilters,
        actor: tuple[str, str] | None,
        page_size: int,
    ) -> None:
        if len(signing_secret) < 32:
            raise ValueError("Admin signing secret must contain at least 32 bytes.")
        self._key = hmac.digest(signing_secret, _CURSOR_DOMAIN + b"key", "sha256")
        self._binding = _json_bytes(
            {
                "actor": actor,
                "csrf": session.csrf_token,
                "filters": filters.query_items(),
                "identity_id": session.identity_id,
                "page_size": page_size,
                "session_generation": session.session_generation,
            }
        )

    def encode(self, position: tuple[int, int]) -> str:
        timestamp, row_id = position
        if not 0 <= timestamp <= _MAX_SQLITE_INT or not 1 <= row_id <= _MAX_SQLITE_INT:
            raise ValueError("Invalid request-log keyset position.")
        payload = _json_bytes([timestamp, row_id])
        encoded = _encode_base64url(payload)
        tag = hmac.digest(self._key, _CURSOR_DOMAIN + self._binding + b"\x00" + payload, "sha256")
        return f"rl1.{encoded}.{_encode_base64url(tag)}"

    def decode(self, cursor: str) -> tuple[int, int]:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 256:
            raise InvalidRequestLogCursor
        try:
            version, encoded, encoded_tag = cursor.split(".")
            if version != "rl1":
                raise InvalidRequestLogCursor
            payload = _decode_base64url(encoded)
            tag = _decode_base64url(encoded_tag)
            expected = hmac.digest(
                self._key, _CURSOR_DOMAIN + self._binding + b"\x00" + payload, "sha256"
            )
            if not hmac.compare_digest(tag, expected):
                raise InvalidRequestLogCursor
            values = json.loads(payload)
            if (
                not isinstance(values, list)
                or len(values) != 2
                or any(type(value) is not int for value in values)
                or not 0 <= values[0] <= _MAX_SQLITE_INT
                or not 1 <= values[1] <= _MAX_SQLITE_INT
                or _json_bytes(values) != payload
            ):
                raise InvalidRequestLogCursor
            return values[0], values[1]
        except (ValueError, UnicodeError, TypeError, binascii.Error) as exc:
            raise InvalidRequestLogCursor from exc


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_base64url(value: str) -> bytes:
    if _BASE64URL.fullmatch(value) is None:
        raise InvalidRequestLogCursor
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _encode_base64url(decoded) != value:
        raise InvalidRequestLogCursor
    return decoded


__all__ = [
    "InvalidRequestLogCursor",
    "InvalidRequestLogFilter",
    "RequestLogCursorCodec",
    "RequestLogFilters",
    "parse_request_log_filters",
]
