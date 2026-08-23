from __future__ import annotations

import base64
import hmac
import json

import pytest

from patchouli_lib.admin.session import AdminSessionCodec


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signed_payload(secret: bytes, value: object) -> str:
    encoded = _encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.digest(secret, encoded.encode("ascii"), "sha256")
    return f"{encoded}.{_encode(signature)}"


def test_session_round_trip_contains_only_expiry_and_csrf() -> None:
    codec = AdminSessionCodec(
        b"s" * 32,
        ttl_seconds=600,
        clock=lambda: 1_000.9,
        token_factory=lambda size: "c" * size,
    )

    encoded, session = codec.issue()

    assert session.expires_at == 1_600
    assert session.csrf_token == "c" * 32
    assert codec.verify(encoded) == session
    assert "password" not in encoded


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "not-a-session",
        "%%%.$$$",
        "a" * 513,
        "e30.invalid",
    ],
)
def test_session_rejects_malformed_or_oversized_values(encoded: str) -> None:
    codec = AdminSessionCodec(b"s" * 32, ttl_seconds=600)

    assert codec.verify(encoded) is None


def test_session_rejects_tampering_wrong_key_and_expiry() -> None:
    clock = [1_000.0]
    codec = AdminSessionCodec(
        b"s" * 32,
        ttl_seconds=300,
        clock=lambda: clock[0],
        token_factory=lambda size: "c" * size,
    )
    encoded, _ = codec.issue()
    payload, signature = encoded.split(".", 1)

    assert codec.verify(f"{payload[:-1]}A.{signature}") is None
    assert AdminSessionCodec(b"x" * 32, ttl_seconds=300).verify(encoded) is None
    clock[0] = 1_300.0
    assert codec.verify(encoded) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"v": 1, "exp": 2_000, "csrf": "c" * 32, "extra": True},
        {"v": 2, "exp": 2_000, "csrf": "c" * 32},
        {"v": True, "exp": 2_000, "csrf": "c" * 32},
        {"v": 1.0, "exp": 2_000, "csrf": "c" * 32},
        {"v": 1, "exp": True, "csrf": "c" * 32},
        {"v": 1, "exp": "2000", "csrf": "c" * 32},
        {"v": 1, "exp": 2_000, "csrf": 42},
        {"v": 1, "exp": 2_000, "csrf": "界" * 32},
        {"v": 1, "exp": 2_000, "csrf": "short"},
        ["not", "an", "object"],
    ],
)
def test_session_rejects_signed_payloads_outside_the_contract(payload: object) -> None:
    secret = b"s" * 32
    codec = AdminSessionCodec(secret, ttl_seconds=300, clock=lambda: 1_000)

    assert codec.verify(_signed_payload(secret, payload)) is None


def test_session_constructor_rejects_weak_key_or_ttl() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        AdminSessionCodec(b"short", ttl_seconds=300)
    with pytest.raises(ValueError, match="TTL"):
        AdminSessionCodec(b"s" * 32, ttl_seconds=299)
    with pytest.raises(ValueError, match="TTL"):
        AdminSessionCodec(b"s" * 32, ttl_seconds=86_401)
