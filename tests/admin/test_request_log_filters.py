from __future__ import annotations

import base64
import hmac
import json

import pytest
from starlette.datastructures import QueryParams

from patchouli_lib.admin.request_log_filters import (
    InvalidRequestLogCursor,
    InvalidRequestLogFilter,
    RequestLogCursorCodec,
    RequestLogFilters,
    parse_request_log_filters,
)
from patchouli_lib.admin.session import MasterAdminSession

_SIGNING_SECRET = b"synthetic-private-admin-signing-key-32-bytes"
_SESSION = MasterAdminSession(
    expires_at=2_000_000_000,
    csrf_token="s" * 43,
    identity_id="a" * 32,
    session_generation=1,
)


def test_time_offsets_are_normalized_for_cursor_binding() -> None:
    first = parse_request_log_filters(
        QueryParams("since=2026-01-01T08%3A00%3A00%2B08%3A00&until=2026-01-02T00%3A00%3A00Z")
    )
    second = parse_request_log_filters(
        QueryParams("since=2026-01-01T00%3A00%3A00Z&until=2026-01-02T00%3A00%3A00Z")
    )
    assert first == second
    assert first.since == "2026-01-01T00:00:00.000000Z"
    assert first.since_us is not None
    assert first.until_us is not None
    assert first.since_us < first.until_us


@pytest.mark.parametrize(
    "query",
    [
        "route=%2Fapi%2Fv1%2Fa%3Fx%3Dy",
        "route=%2Fapi%2Fv1%2Fa%2Factual-identifier%23fragment",
        "route=%2Fadmin%2Frequests",
        "route=%2Fapi%2Fv1%2Fa&route=%2Fapi%2Fv1%2Fb",
        "method=get",
        "status=200&status=200",
        "stats=200",
        "status=99",
        "status=+200",
        "status=200.0",
        "since=2026-01-01T00%3A00%3A00",
        "since=1969-12-31T23%3A59%3A59Z",
        "since=2026-01-02T00%3A00%3A00Z&until=2026-01-02T00%3A00%3A00Z",
    ],
)
def test_rejects_invalid_or_duplicate_filters(query: str) -> None:
    with pytest.raises(InvalidRequestLogFilter):
        parse_request_log_filters(QueryParams(query))


def test_valid_but_unrecorded_template_is_not_rejected_by_parser() -> None:
    filters = parse_request_log_filters(QueryParams("route=%2Fapi%2Fv1%2Fold%2F%7Bid%7D"))
    assert filters.route == "/api/v1/old/{id}"


def test_cursor_rejects_old_format_tampering_changed_filter_actor_or_session() -> None:
    filters = RequestLogFilters(route="/api/v1/auth/whoami", status="200")
    codec = RequestLogCursorCodec(
        signing_secret=_SIGNING_SECRET,
        session=_SESSION,
        filters=filters,
        actor=None,
        page_size=20,
    )
    cursor = codec.encode((1_000_000, 7))
    assert codec.decode(cursor) == (1_000_000, 7)
    changed_filter = RequestLogCursorCodec(
        signing_secret=_SIGNING_SECRET,
        session=_SESSION,
        filters=RequestLogFilters(route="/api/v1/auth/whoami", status="403"),
        actor=None,
        page_size=20,
    )
    changed_actor = RequestLogCursorCodec(
        signing_secret=_SIGNING_SECRET,
        session=_SESSION,
        filters=filters,
        actor=("a" * 32, "b" * 32),
        page_size=20,
    )
    changed_session = RequestLogCursorCodec(
        signing_secret=_SIGNING_SECRET,
        session=MasterAdminSession(2_000_000_000, "t" * 43, "a" * 32, 1),
        filters=filters,
        actor=None,
        page_size=20,
    )
    changed_generation = RequestLogCursorCodec(
        signing_secret=_SIGNING_SECRET,
        session=MasterAdminSession(2_000_000_000, "s" * 43, "a" * 32, 2),
        filters=filters,
        actor=None,
        page_size=20,
    )
    changed_identity = RequestLogCursorCodec(
        signing_secret=_SIGNING_SECRET,
        session=MasterAdminSession(2_000_000_000, "s" * 43, "b" * 32, 1),
        filters=filters,
        actor=None,
        page_size=20,
    )
    for altered in (
        changed_filter,
        changed_actor,
        changed_session,
        changed_generation,
        changed_identity,
    ):
        with pytest.raises(InvalidRequestLogCursor):
            altered.decode(cursor)
    with pytest.raises(InvalidRequestLogCursor):
        codec.decode("1000000:7")
    with pytest.raises(InvalidRequestLogCursor):
        codec.decode(cursor[:-1] + ("A" if cursor[-1] != "A" else "B"))


def test_public_csrf_value_cannot_sign_a_different_position() -> None:
    filters = RequestLogFilters(route="/api/v1/auth/whoami")
    codec = RequestLogCursorCodec(
        signing_secret=_SIGNING_SECRET,
        session=_SESSION,
        filters=filters,
        actor=None,
        page_size=20,
    )
    payload = json.dumps([1_000_000, 8], separators=(",", ":")).encode("ascii")
    binding = json.dumps(
        {
            "actor": None,
            "csrf": _SESSION.csrf_token,
            "filters": filters.query_items(),
            "identity_id": _SESSION.identity_id,
            "page_size": 20,
            "session_generation": _SESSION.session_generation,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    domain = b"patchouli-lib:admin-request-log-cursor:v1\x00"
    guessed_key = hmac.digest(_SESSION.csrf_token.encode("ascii"), domain + b"key", "sha256")
    guessed_tag = hmac.digest(guessed_key, domain + binding + b"\x00" + payload, "sha256")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    encoded_tag = base64.urlsafe_b64encode(guessed_tag).decode("ascii").rstrip("=")
    forged = f"rl1.{encoded}.{encoded_tag}"
    with pytest.raises(InvalidRequestLogCursor):
        codec.decode(forged)
