from __future__ import annotations

import base64
import hmac
import json

import pytest

from patchouli_lib.admin.session import AdminSessionCodec, MasterAdminSession

_IDENTITY_ID = "a" * 32


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


def test_master_session_round_trip_contains_only_admission_metadata() -> None:
    codec = AdminSessionCodec(
        b"s" * 32,
        ttl_seconds=600,
        clock=lambda: 1_000.9,
        token_factory=lambda size: "c" * size,
    )

    encoded, session = codec.issue_master(_IDENTITY_ID, 7)
    payload = json.loads(base64.urlsafe_b64decode(encoded.split(".", 1)[0] + "=="))

    assert session == MasterAdminSession(
        expires_at=1_600,
        csrf_token="c" * 32,
        identity_id=_IDENTITY_ID,
        session_generation=7,
    )
    assert session.auth_mode == "master"
    verified = codec.verify_master(encoded)
    assert verified is not None
    assert session.audit_fingerprint() == verified.audit_fingerprint()
    assert payload == {
        "csrf": "c" * 32,
        "exp": 1_600,
        "gen": 7,
        "identity_id": _IDENTITY_ID,
        "mode": "master",
        "v": 2,
    }
    assert codec.verify_master(encoded) == session
    assert codec.verify(encoded) is None
    legacy, _ = codec.issue()
    assert codec.verify_master(legacy) is None


@pytest.mark.parametrize("identity_id", ["", "a" * 31, "A" * 32, "g" * 32, 42])
def test_master_session_issue_rejects_invalid_identity(identity_id: object) -> None:
    codec = AdminSessionCodec(b"s" * 32, ttl_seconds=600)

    with pytest.raises(ValueError, match="identity or generation"):
        codec.issue_master(identity_id, 1)  # type: ignore[arg-type]


@pytest.mark.parametrize("generation", [0, -1, True, 1.5, "1", 1 << 63])
def test_master_session_issue_rejects_invalid_generation(generation: object) -> None:
    codec = AdminSessionCodec(b"s" * 32, ttl_seconds=600)

    with pytest.raises(ValueError, match="identity or generation"):
        codec.issue_master(_IDENTITY_ID, generation)  # type: ignore[arg-type]


@pytest.mark.parametrize("csrf", ["short", "界" * 32, "a" * 129])
def test_master_session_issue_rejects_invalid_generated_csrf(csrf: str) -> None:
    codec = AdminSessionCodec(b"s" * 32, ttl_seconds=600, token_factory=lambda _size: csrf)

    with pytest.raises(ValueError, match="CSRF"):
        codec.issue_master(_IDENTITY_ID, 1)


def test_master_session_rejects_tampering_wrong_key_and_expiry() -> None:
    clock = [1_000.0]
    codec = AdminSessionCodec(
        b"s" * 32,
        ttl_seconds=300,
        clock=lambda: clock[0],
        token_factory=lambda size: "c" * size,
    )
    encoded, _ = codec.issue_master(_IDENTITY_ID, 1)
    payload, signature = encoded.split(".", 1)

    assert codec.verify_master(f"{payload[:-1]}A.{signature}") is None
    assert AdminSessionCodec(b"x" * 32, ttl_seconds=300).verify_master(encoded) is None
    clock[0] = 1_300.0
    assert codec.verify_master(encoded) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"v": 2, "exp": 2_000, "csrf": "c" * 32, "gen": 1, "identity_id": _IDENTITY_ID},
        {
            "v": 2,
            "mode": "legacy",
            "exp": 2_000,
            "csrf": "c" * 32,
            "gen": 1,
            "identity_id": _IDENTITY_ID,
        },
        {
            "v": 2,
            "mode": "master",
            "exp": 2_000,
            "csrf": "c" * 32,
            "gen": 1,
            "identity_id": _IDENTITY_ID,
            "extra": True,
        },
        {
            "v": True,
            "mode": "master",
            "exp": 2_000,
            "csrf": "c" * 32,
            "gen": 1,
            "identity_id": _IDENTITY_ID,
        },
        {
            "v": 2.0,
            "mode": "master",
            "exp": 2_000,
            "csrf": "c" * 32,
            "gen": 1,
            "identity_id": _IDENTITY_ID,
        },
        {
            "v": 2,
            "mode": "master",
            "exp": True,
            "csrf": "c" * 32,
            "gen": 1,
            "identity_id": _IDENTITY_ID,
        },
        {
            "v": 2,
            "mode": "master",
            "exp": 2_000,
            "csrf": "界" * 32,
            "gen": 1,
            "identity_id": _IDENTITY_ID,
        },
        {
            "v": 2,
            "mode": "master",
            "exp": 2_000,
            "csrf": "c" * 32,
            "gen": True,
            "identity_id": _IDENTITY_ID,
        },
        {
            "v": 2,
            "mode": "master",
            "exp": 2_000,
            "csrf": "c" * 32,
            "gen": 0,
            "identity_id": _IDENTITY_ID,
        },
        {
            "v": 2,
            "mode": "master",
            "exp": 2_000,
            "csrf": "c" * 32,
            "gen": 1 << 63,
            "identity_id": _IDENTITY_ID,
        },
        {
            "v": 2,
            "mode": "master",
            "exp": 2_000,
            "csrf": "c" * 32,
            "gen": 1,
            "identity_id": "A" * 32,
        },
        ["not", "an", "object"],
    ],
)
def test_master_session_rejects_signed_payloads_outside_contract(payload: object) -> None:
    secret = b"s" * 32
    codec = AdminSessionCodec(secret, ttl_seconds=300, clock=lambda: 1_000)

    assert codec.verify_master(_signed_payload(secret, payload)) is None


def test_session_rejects_duplicate_json_keys() -> None:
    secret = b"s" * 32
    codec = AdminSessionCodec(secret, ttl_seconds=300, clock=lambda: 1_000)
    duplicate = (
        '{"v":2,"mode":"master","identity_id":"'
        + _IDENTITY_ID
        + '","gen":1,"gen":2,"exp":2000,"csrf":"'
        + "c" * 32
        + '"}'
    )
    encoded = _encode(duplicate.encode("utf-8"))
    signature = _encode(hmac.digest(secret, encoded.encode("ascii"), "sha256"))

    assert codec.verify_master(f"{encoded}.{signature}") is None


def test_master_session_rejects_noncanonical_base64() -> None:
    secret = b"s" * 32
    codec = AdminSessionCodec(secret, ttl_seconds=300, clock=lambda: 1_000)
    payload = json.dumps(
        {
            "v": 2,
            "mode": "master",
            "identity_id": _IDENTITY_ID,
            "gen": 1,
            "exp": 2_000,
            "csrf": "c" * 32,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    while len(payload) % 3 == 0:
        payload += b" "
    encoded = _encode(payload)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    noncanonical = encoded[:-1] + alphabet[alphabet.index(encoded[-1]) ^ 1]
    assert base64.urlsafe_b64decode(noncanonical + "==") == payload
    signature = _encode(hmac.digest(secret, noncanonical.encode("ascii"), "sha256"))

    assert codec.verify_master(f"{noncanonical}.{signature}") is None


def test_master_session_rejects_oversized_or_invalid_unicode_cookie() -> None:
    codec = AdminSessionCodec(b"s" * 32, ttl_seconds=300)

    assert codec.verify_master("A" * 513) is None
    assert codec.verify_master("\ud800") is None


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
