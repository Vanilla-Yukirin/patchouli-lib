from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import replace

import pytest

from patchouli_lib.api.contracts import MAX_CURSOR_LENGTH
from patchouli_lib.retrieval.cursor import (
    CURSOR_VERSION,
    MAX_CURSOR_IDENTITY_BYTES,
    MIN_CURSOR_SECRET_BYTES,
    CursorBinding,
    CursorCodec,
    InvalidCursorError,
)

SECRET = bytes(range(MIN_CURSOR_SECRET_BYTES))
OTHER_SECRET = bytes(reversed(range(MIN_CURSOR_SECRET_BYTES)))
CALLER_ID = "1" * 32
SECTION_ID = "2" * 32


def _binding(**changes: object) -> CursorBinding:
    values: dict[str, object] = {
        "caller_id": CALLER_ID,
        "policy_version": 7,
        "section_id": SECTION_ID,
        "route_identity": "pages.list",
        "limit": 20,
        "query_identity": b"synthetic normalized query",
        "filters_identity": b'{"book_id":"33333333333333333333333333333333"}',
        "sort_identity": b"page_id:asc",
    }
    values.update(changes)
    return CursorBinding(**values)  # type: ignore[arg-type]


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signed_payload(payload: bytes, *, secret: bytes = SECRET) -> str:
    encoded_payload = _b64(payload)
    authenticated = f"plc1.{encoded_payload}"
    tag = hmac.digest(
        secret,
        b"patchouli-lib:retrieval-cursor:authentication:v1\x00" + authenticated.encode("ascii"),
        "sha256",
    )
    return f"{authenticated}.{_b64(tag)}"


def _noncanonical_pad_bits(value: str) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    remainder = len(value) % 4
    assert remainder in {2, 3}
    pad_bits = 4 if remainder == 2 else 2
    original = alphabet.index(value[-1])
    replacement = original | 1
    assert replacement >> pad_bits == original >> pad_bits
    return value[:-1] + alphabet[replacement]


def _decoded_payload(cursor: str) -> dict[str, object]:
    encoded = cursor.split(".")[1]
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    value = json.loads(raw)
    assert isinstance(value, dict)
    return value


def _mutate_payload_segment(cursor: str) -> str:
    prefix, payload, tag = cursor.split(".")
    replacement = "A" if payload[0] != "A" else "B"
    return f"{prefix}.{replacement}{payload[1:]}.{tag}"


def test_round_trip_is_deterministic_url_safe_and_query_private() -> None:
    codec = CursorCodec(SECRET)
    binding = _binding()

    cursor = codec.encode(binding=binding, last_key="pg_雪")

    assert cursor == codec.encode(binding=binding, last_key="pg_雪")
    assert len(cursor) <= MAX_CURSOR_LENGTH
    assert set(cursor) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.")
    assert codec.decode(cursor, binding=binding) == "pg_雪"
    payload = _decoded_payload(cursor)
    assert set(payload) == {"b", "k", "v"}
    assert payload["v"] == CURSOR_VERSION
    assert payload["k"] == "pg_雪"
    serialized = json.dumps(payload, sort_keys=True)
    for private_value in (
        CALLER_ID,
        SECTION_ID,
        "synthetic normalized query",
        "book_id",
        "page_id:asc",
    ):
        assert private_value not in serialized
        assert private_value not in cursor


@pytest.mark.parametrize(
    "changed",
    [
        {"caller_id": "9" * 32},
        {"policy_version": 8},
        {"section_id": "8" * 32},
        {"section_id": None},
        {"route_identity": "books.list"},
        {"limit": 19},
        {"query_identity": b"different normalized query"},
        {"filters_identity": b"{}"},
        {"sort_identity": b"page_id:desc"},
    ],
    ids=[
        "caller",
        "policy",
        "section",
        "missing-section",
        "route",
        "limit",
        "query",
        "filters",
        "sort",
    ],
)
def test_cursor_is_bound_to_the_complete_request_context(changed: dict[str, object]) -> None:
    codec = CursorCodec(SECRET)
    cursor = codec.encode(binding=_binding(), last_key="page-key")

    with pytest.raises(InvalidCursorError) as caught:
        codec.decode(cursor, binding=_binding(**changed))

    assert str(caught.value) == "The pagination cursor is invalid or no longer applicable."
    assert "page-key" not in repr(caught.value)


def test_top_level_section_collection_has_an_explicit_scope_identity() -> None:
    codec = CursorCodec(SECRET)
    binding = _binding(
        section_id=None,
        route_identity="sections.list",
        query_identity=b"",
        filters_identity=b"",
    )
    cursor = codec.encode(binding=binding, last_key=SECTION_ID)

    assert codec.decode(cursor, binding=binding) == SECTION_ID


@pytest.mark.parametrize(
    "mutation",
    [
        _mutate_payload_segment,
        lambda token: token[:-1] + ("A" if token[-1] != "A" else "B"),
        lambda token: token.replace("plc1.", "plc2.", 1),
    ],
    ids=["payload", "tag", "version-prefix"],
)
def test_tampering_and_wrong_key_share_one_safe_failure(
    mutation: Callable[[str], str],
) -> None:
    codec = CursorCodec(SECRET)
    binding = _binding()
    cursor = codec.encode(binding=binding, last_key="internal-key")
    variants = [mutation(cursor), cursor]
    codecs = [codec, CursorCodec(OTHER_SECRET)]

    for candidate, verifier in zip(variants, codecs, strict=True):
        with pytest.raises(InvalidCursorError) as caught:
            verifier.decode(candidate, binding=binding)
        assert str(caught.value) == "The pagination cursor is invalid or no longer applicable."
        assert candidate not in str(caught.value)


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "not-a-cursor",
        "plc1.part.extra.part",
        "plc1.=.tag",
        "plc1.abc+.tag",
        "x" * (MAX_CURSOR_LENGTH + 1),
    ],
)
def test_malformed_and_oversized_input_share_one_safe_failure(cursor: str) -> None:
    with pytest.raises(InvalidCursorError) as caught:
        CursorCodec(SECRET).decode(cursor, binding=_binding())
    assert str(caught.value) == "The pagination cursor is invalid or no longer applicable."
    if cursor:
        assert cursor not in str(caught.value)


def test_non_string_cursor_uses_the_same_safe_failure() -> None:
    with pytest.raises(InvalidCursorError):
        CursorCodec(SECRET).decode(b"not-text", binding=_binding())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "payload",
    [
        b'{"b":"value","k":"key","v":2}',
        b'{"b":"value","k":"key","v":true}',
        b'{"b":"value","k":"key","v":1,"x":0}',
        b'{"b":"value","k":"key","v":1,"v":1}',
        b'{ "b":"value","k":"key","v":1}',
        b'{"b":"value","k":"key","v":NaN}',
        b"\xff",
        b"[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[",
    ],
    ids=[
        "unknown-version",
        "boolean-version",
        "unknown-member",
        "duplicate-member",
        "noncanonical-json",
        "nonfinite-json",
        "invalid-utf8",
        "malformed-deep-json",
    ],
)
def test_authenticated_but_noncanonical_payload_is_rejected(payload: bytes) -> None:
    cursor = _signed_payload(payload)

    with pytest.raises(InvalidCursorError):
        CursorCodec(SECRET).decode(cursor, binding=_binding())


def test_noncanonical_base64url_is_rejected_even_with_a_matching_tag() -> None:
    codec = CursorCodec(SECRET)
    cursor = codec.encode(binding=_binding(), last_key="key")
    prefix, payload, tag = cursor.split(".")
    noncanonical_payload = f"{payload}="
    authenticated = f"{prefix}.{noncanonical_payload}"
    replacement_tag = hmac.digest(
        SECRET,
        b"patchouli-lib:retrieval-cursor:authentication:v1\x00" + authenticated.encode("ascii"),
        "sha256",
    )

    with pytest.raises(InvalidCursorError):
        codec.decode(f"{authenticated}.{_b64(replacement_tag)}", binding=_binding())
    with pytest.raises(InvalidCursorError):
        codec.decode(f"{prefix}.{payload}.{tag}=", binding=_binding())

    noncanonical_payload = _noncanonical_pad_bits(payload)
    authenticated = f"{prefix}.{noncanonical_payload}"
    replacement_tag = hmac.digest(
        SECRET,
        b"patchouli-lib:retrieval-cursor:authentication:v1\x00" + authenticated.encode("ascii"),
        "sha256",
    )
    with pytest.raises(InvalidCursorError):
        codec.decode(f"{authenticated}.{_b64(replacement_tag)}", binding=_binding())


def test_wrong_tag_length_is_rejected_before_comparison() -> None:
    cursor = CursorCodec(SECRET).encode(binding=_binding(), last_key="key")
    prefix, payload, _tag = cursor.split(".")
    with pytest.raises(InvalidCursorError):
        CursorCodec(SECRET).decode(
            f"{prefix}.{payload}.{_b64(bytes(31))}",
            binding=_binding(),
        )


def test_authenticated_payload_rejects_invalid_binding_digest_and_key() -> None:
    codec = CursorCodec(SECRET)
    valid = _decoded_payload(codec.encode(binding=_binding(), last_key="key"))

    for changes in (
        {"b": _b64(hashlib.sha256(b"wrong").digest())},
        {"b": 1},
        {"k": ""},
        {"k": 1},
        {"k": "x" * 256},
    ):
        payload = {**valid, **changes}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
        with pytest.raises(InvalidCursorError):
            codec.decode(_signed_payload(raw), binding=_binding())


@pytest.mark.parametrize("size", [0, MIN_CURSOR_SECRET_BYTES - 1])
def test_codec_rejects_weak_secrets_without_exposing_them(size: int) -> None:
    with pytest.raises(ValueError, match="256 bits") as caught:
        CursorCodec(b"s" * size)
    assert "ssss" not in str(caught.value)
    with pytest.raises(TypeError, match="immutable bytes"):
        CursorCodec(bytearray(SECRET))  # type: ignore[arg-type]
    assert repr(CursorCodec(SECRET)) == "CursorCodec(secret=<redacted>, version=1)"


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"caller_id": ""}, "caller_id"),
        ({"caller_id": "x" * 256}, "caller_id"),
        ({"section_id": "\ud800"}, "section_id"),
        ({"route_identity": "Pages/List"}, "route_identity"),
        ({"route_identity": "x" * 129}, "route_identity"),
        ({"policy_version": True}, "policy_version"),
        ({"policy_version": 0}, "policy_version"),
        ({"limit": True}, "limit"),
        ({"limit": 101}, "limit"),
        ({"query_identity": b"x" * (MAX_CURSOR_IDENTITY_BYTES + 1)}, "query_identity"),
        ({"filters_identity": "{}"}, "filters_identity"),
    ],
)
def test_binding_rejects_noncanonical_or_unbounded_context(
    changes: dict[str, object],
    error: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=error):
        _binding(**changes)


def test_multibyte_binding_identity_uses_the_documented_byte_limit() -> None:
    accepted = _binding(caller_id="界" * 85)
    assert accepted.caller_id == "界" * 85
    with pytest.raises(ValueError, match="caller_id"):
        _binding(caller_id="界" * 86)


@pytest.mark.parametrize("last_key", ["", "x" * 256, "\ud800"])
def test_encode_rejects_invalid_internal_key(last_key: str) -> None:
    with pytest.raises(ValueError, match="last_key"):
        CursorCodec(SECRET).encode(binding=_binding(), last_key=last_key)


def test_encode_rejects_a_valid_key_that_cannot_fit_the_wire_limit() -> None:
    with pytest.raises(ValueError, match="public cursor limit"):
        CursorCodec(SECRET).encode(binding=_binding(), last_key="😀" * 255)


def test_binding_private_identities_are_redacted_from_repr() -> None:
    binding = _binding()
    rendered = repr(binding)
    assert "synthetic normalized query" not in rendered
    assert "book_id" not in rendered
    assert "page_id:asc" not in rendered
    assert "query_identity=<redacted>" not in rendered


def test_binding_objects_are_required_at_codec_boundary() -> None:
    codec = CursorCodec(SECRET)
    with pytest.raises(TypeError, match="CursorBinding"):
        codec.encode(binding=object(), last_key="key")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="CursorBinding"):
        codec.decode("cursor", binding=object())  # type: ignore[arg-type]


def test_replace_preserves_normalized_identity_bytes() -> None:
    binding = _binding()
    updated = replace(binding, limit=10)
    assert updated.query_identity == binding.query_identity
    assert updated.limit == 10
