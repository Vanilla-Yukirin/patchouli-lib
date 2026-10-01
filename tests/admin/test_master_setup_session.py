"""First-setup cookies cannot become normal administrator sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from patchouli_lib.admin.master_setup_session import MasterSetupSession, MasterSetupSessionCodec
from patchouli_lib.admin.session import AdminSession, AdminSessionCodec

_SECRET = b"synthetic-setup-signing-secret-0001"
_DOMAIN = b"patchouli.master_setup.v1\0"
_CSRF = "c" * 32


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signed_bytes(raw: bytes, *, domain: bytes = _DOMAIN) -> str:
    encoded = _encode(raw)
    signature = hmac.digest(_SECRET, domain + encoded.encode("ascii"), "sha256")
    return f"{encoded}.{_encode(signature)}"


def _signed(value: object) -> str:
    return _signed_bytes(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _payload() -> dict[str, object]:
    return {"v": 1, "purpose": "master_setup", "exp": 1_300, "csrf": _CSRF}


def test_setup_round_trip_contains_only_bounded_form_metadata() -> None:
    codec = MasterSetupSessionCodec(
        _SECRET, clock=lambda: 1_000.9, token_factory=lambda _size: _CSRF
    )
    encoded, session = codec.issue()
    payload, _signature = encoded.split(".", 1)
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))

    assert session == MasterSetupSession(expires_at=1_300, csrf_token=_CSRF)
    assert not isinstance(session, AdminSession)
    assert decoded == _payload()
    assert len(encoded.encode("ascii")) <= 512
    assert codec.verify(encoded) == session
    assert session.audit_fingerprint() == hashlib.sha256(_CSRF.encode("ascii")).digest()
    assert _SECRET.decode("ascii") not in json.dumps(decoded)
    assert not {"proof", "password", "master_token", "identity_id", "gen", "mode"} & decoded.keys()


def test_setup_and_normal_cookie_admission_are_bidirectionally_separate() -> None:
    setup = MasterSetupSessionCodec(_SECRET, clock=lambda: 1_000)
    normal = AdminSessionCodec(_SECRET, ttl_seconds=300, clock=lambda: 1_000)
    setup_cookie, _ = setup.issue()
    legacy_cookie, _ = normal.issue()
    master_cookie, _ = normal.issue_master("a" * 32, 1)

    assert normal.verify(setup_cookie) is None
    assert normal.verify_master(setup_cookie) is None
    assert setup.verify(legacy_cookie) is None
    assert setup.verify(master_cookie) is None
    assert setup.verify(_signed_bytes(json.dumps(_payload()).encode(), domain=b"")) is None
    assert (
        setup.verify(_signed_bytes(json.dumps(_payload()).encode(), domain=b"other-purpose\0"))
        is None
    )


@pytest.mark.parametrize("ttl", [60, 300, 600])
def test_ttl_bounds_and_exact_expiry(ttl: int) -> None:
    clock = [1_000.0]
    codec = MasterSetupSessionCodec(_SECRET, ttl_seconds=ttl, clock=lambda: clock[0])
    encoded, session = codec.issue()
    assert session.expires_at == 1_000 + ttl
    clock[0] = session.expires_at - 0.001
    assert codec.verify(encoded) == session
    clock[0] = float(session.expires_at)
    assert codec.verify(encoded) is None


@pytest.mark.parametrize("expiry", [1_000, 999, 1_301, 1 << 63, True, 1_300.0, "1300", None])
def test_signed_expiry_must_be_integer_and_within_current_short_window(expiry: object) -> None:
    payload = _payload() | {"exp": expiry}
    codec = MasterSetupSessionCodec(_SECRET, clock=lambda: 1_000)
    assert codec.verify(_signed(payload)) is None


@pytest.mark.parametrize(
    "update",
    [
        {"v": 2},
        {"v": True},
        {"v": 1.0},
        {"v": "1"},
        {"purpose": "master"},
        {"purpose": "legacy"},
        {"purpose": "MASTER_SETUP"},
        {"purpose": None},
        {"mode": "master"},
        {"identity_id": "a" * 32},
        {"gen": 1},
        {"extra": True},
        {"csrf": "short"},
        {"csrf": "c" * 129},
        {"csrf": "界" * 32},
        {"csrf": None},
        {"csrf": 32},
    ],
)
def test_correctly_signed_payload_outside_setup_contract_is_rejected(
    update: dict[str, object],
) -> None:
    codec = MasterSetupSessionCodec(_SECRET, clock=lambda: 1_000)
    assert codec.verify(_signed(_payload() | update)) is None


@pytest.mark.parametrize("removed", ["v", "purpose", "exp", "csrf"])
def test_correctly_signed_payload_requires_all_fields(removed: str) -> None:
    payload = _payload()
    del payload[removed]
    assert MasterSetupSessionCodec(_SECRET, clock=lambda: 1_000).verify(_signed(payload)) is None


@pytest.mark.parametrize("value", [[], ["not", "an", "object"], None, True, "master_setup", 1])
def test_correctly_signed_non_object_payload_is_rejected(value: object) -> None:
    assert MasterSetupSessionCodec(_SECRET, clock=lambda: 1_000).verify(_signed(value)) is None


def test_duplicate_json_key_is_rejected_despite_valid_signature() -> None:
    raw = ('{"v":1,"purpose":"master_setup","exp":1300,"exp":1200,"csrf":"' + _CSRF + '"}').encode(
        "ascii"
    )
    assert MasterSetupSessionCodec(_SECRET, clock=lambda: 1_000).verify(_signed_bytes(raw)) is None


def test_noncanonical_payload_encoding_is_rejected_despite_valid_signature() -> None:
    raw = json.dumps(_payload(), separators=(",", ":")).encode("ascii")
    while len(raw) % 3 == 0:
        raw += b" "
    canonical = _encode(raw)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    noncanonical = canonical[:-1] + alphabet[alphabet.index(canonical[-1]) ^ 1]
    assert base64.urlsafe_b64decode(noncanonical + "==") == raw
    signature = _encode(hmac.digest(_SECRET, _DOMAIN + noncanonical.encode("ascii"), "sha256"))
    assert (
        MasterSetupSessionCodec(_SECRET, clock=lambda: 1_000).verify(f"{noncanonical}.{signature}")
        is None
    )


def test_noncanonical_signature_encoding_is_rejected() -> None:
    codec = MasterSetupSessionCodec(_SECRET, clock=lambda: 1_000)
    cookie, _ = codec.issue()
    payload, canonical = cookie.split(".", 1)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    noncanonical = canonical[:-1] + alphabet[alphabet.index(canonical[-1]) ^ 1]
    assert base64.urlsafe_b64decode(noncanonical + "=") == base64.urlsafe_b64decode(canonical + "=")
    assert codec.verify(f"{payload}.{noncanonical}") is None
    assert codec.verify(f"{payload}.{canonical}=") is None


def test_tampering_and_wrong_key_are_rejected() -> None:
    codec = MasterSetupSessionCodec(_SECRET, clock=lambda: 1_000)
    cookie, _ = codec.issue()
    payload, signature = cookie.split(".", 1)
    changed = "A" if payload[-1] != "A" else "B"
    assert codec.verify(f"{payload[:-1]}{changed}.{signature}") is None
    assert codec.verify(f"{payload}.{signature[:-1]}") is None
    assert MasterSetupSessionCodec(b"x" * 32, clock=lambda: 1_000).verify(cookie) is None


@pytest.mark.parametrize("encoded", ["", "invalid", "%%%.$$$", "e30.bad", "A" * 513, "\ud800"])
def test_malformed_or_oversized_cookie_fails_closed(encoded: str) -> None:
    assert MasterSetupSessionCodec(_SECRET, clock=lambda: 1_000).verify(encoded) is None


@pytest.mark.parametrize("raw", [b"{broken json", b"\xff", b"{}", b"null"])
def test_signed_invalid_json_or_utf8_fails_closed(raw: bytes) -> None:
    assert MasterSetupSessionCodec(_SECRET, clock=lambda: 1_000).verify(_signed_bytes(raw)) is None


@pytest.mark.parametrize("csrf", ["", "c" * 31, "c" * 129, "界" * 32])
def test_issue_rejects_invalid_generated_csrf_without_echo(csrf: str) -> None:
    codec = MasterSetupSessionCodec(_SECRET, token_factory=lambda _size: csrf)
    with pytest.raises(ValueError, match="CSRF") as error:
        codec.issue()
    if csrf:
        assert csrf not in str(error.value)


@pytest.mark.parametrize("csrf", ["c" * 32, "c" * 128])
def test_generated_csrf_length_boundaries_remain_valid(csrf: str) -> None:
    codec = MasterSetupSessionCodec(_SECRET, clock=lambda: 1_000, token_factory=lambda _size: csrf)
    cookie, session = codec.issue()
    assert codec.verify(cookie) == session


@pytest.mark.parametrize("ttl", [59, 601, True, 300.0, "300"])
def test_constructor_rejects_non_integer_or_out_of_bound_ttl(ttl: object) -> None:
    with pytest.raises(ValueError, match="TTL"):
        MasterSetupSessionCodec(_SECRET, ttl_seconds=ttl)  # type: ignore[arg-type]


@pytest.mark.parametrize("secret", [b"", b"s" * 31, b"s" * 1_025])
def test_constructor_rejects_out_of_bound_key(secret: bytes) -> None:
    with pytest.raises(ValueError, match="32 to 1024"):
        MasterSetupSessionCodec(secret)
