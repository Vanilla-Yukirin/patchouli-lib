from __future__ import annotations

import base64
import hmac
from collections.abc import Callable

import pytest

import patchouli_lib.auth.tokens as tokens
from patchouli_lib.auth import (
    SECRET_BYTES,
    SELECTOR_BYTES,
    TOKEN_PREFIX,
    TOKEN_VERSION,
    VERIFIER_BYTES,
    InvalidTokenError,
    ParsedToken,
    TokenGenerationError,
    generate_token,
    parse_token,
    verify_token,
)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _build_token(selector: bytes, secret: bytes, *, prefix: str = TOKEN_PREFIX) -> str:
    return f"{prefix}.{_encode(selector)}.{_encode(secret)}"


def _with_runtime_version(parsed: ParsedToken, version: object) -> ParsedToken:
    # Deliberately bypass the static type to exercise fail-closed runtime validation.
    return ParsedToken(
        version=version,  # type: ignore[arg-type]
        selector=parsed.selector,
        verifier=parsed.verifier,
    )


def test_deterministic_verifier_vector() -> None:
    selector = bytes(range(SELECTOR_BYTES))
    secret = bytes(range(32, 32 + SECRET_BYTES))

    parsed = parse_token(_build_token(selector, secret))

    assert parsed.version == TOKEN_VERSION
    assert parsed.selector == "AAECAwQFBgcICQoLDA0ODw"
    assert parsed.verifier.hex() == (
        "35c3db165b6b673006bc05f1341e17504ba513773ded754635ca0d48393e0e18"
    )


def test_generation_requests_exact_entropy_lengths(monkeypatch: pytest.MonkeyPatch) -> None:
    requested_lengths: list[int] = []

    def deterministic_bytes(length: int) -> bytes:
        requested_lengths.append(length)
        return bytes([len(requested_lengths)]) * length

    monkeypatch.setattr(tokens, "token_bytes", deterministic_bytes)

    issued = generate_token()

    assert requested_lengths == [SELECTOR_BYTES, SECRET_BYTES]
    assert issued.value == _build_token(b"\x01" * SELECTOR_BYTES, b"\x02" * SECRET_BYTES)
    parsed = parse_token(issued.value)
    assert issued.version == parsed.version
    assert issued.selector == parsed.selector
    assert issued.verifier == parsed.verifier


def test_generated_selectors_and_values_are_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def deterministic_unique_bytes(length: int) -> bytes:
        nonlocal calls
        calls += 1
        return calls.to_bytes(length, "big")

    monkeypatch.setattr(tokens, "token_bytes", deterministic_unique_bytes)

    issued = [generate_token() for _ in range(256)]

    assert len({item.selector for item in issued}) == len(issued)
    assert len({item.value for item in issued}) == len(issued)
    assert all(len(item.selector) == 22 for item in issued)
    assert all(len(item.value.split(".")[2]) == 43 for item in issued)
    assert all(len(item.verifier) == VERIFIER_BYTES for item in issued)


@pytest.mark.parametrize("bad_length_call", [1, 2])
def test_generation_fails_closed_on_invalid_random_length(
    monkeypatch: pytest.MonkeyPatch,
    bad_length_call: int,
) -> None:
    calls = 0

    def invalid_length_once(length: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == bad_length_call:
            return b"synthetic-secret-that-must-not-appear"
        return b"\x00" * length

    monkeypatch.setattr(tokens, "token_bytes", invalid_length_once)

    with pytest.raises(TokenGenerationError) as exc_info:
        generate_token()

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert "synthetic-secret" not in rendered
    assert rendered.count("Secure random source returned an invalid value.") == 2


def _malformed_tokens() -> list[str]:
    selector = b"\x00" * SELECTOR_BYTES
    secret = b"\x01" * SECRET_BYTES
    encoded_selector = _encode(selector)
    encoded_secret = _encode(secret)
    return [
        "",
        f"plb2.{encoded_selector}.{encoded_secret}",
        f"{TOKEN_PREFIX}.{encoded_selector}",
        f"{TOKEN_PREFIX}.{encoded_selector}.{encoded_secret}.extra",
        f"{TOKEN_PREFIX}.{encoded_selector}=.{encoded_secret}",
        f"{TOKEN_PREFIX}.{encoded_selector[:-1]}.{encoded_secret}",
        f"{TOKEN_PREFIX}.{encoded_selector}.{encoded_secret[:-1]}",
        f"{TOKEN_PREFIX}.{encoded_selector}A.{encoded_secret}",
        f"{TOKEN_PREFIX}.{encoded_selector}.{encoded_secret}A",
        f"{TOKEN_PREFIX}.{'/' * 22}.{encoded_secret}",
        f"{TOKEN_PREFIX}.{encoded_selector}.{'/' * 43}",
        f"{TOKEN_PREFIX}.{'é' * 22}.{encoded_secret}",
        f"{TOKEN_PREFIX}.{encoded_selector}.{'é' * 43}",
        f"{TOKEN_PREFIX}.{encoded_selector[:-1]}B.{encoded_secret}",
        f"{TOKEN_PREFIX}.{encoded_selector}.{encoded_secret[:-1]}B",
    ]


@pytest.mark.parametrize("value", _malformed_tokens())
def test_parser_rejects_malformed_tokens_without_echo(value: str) -> None:
    with pytest.raises(InvalidTokenError) as exc_info:
        parse_token(value)

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert value not in rendered or not value
    assert "Invalid bearer token." in rendered


def test_parser_rejects_non_string_without_echo() -> None:
    with pytest.raises(InvalidTokenError, match="^Invalid bearer token\\.$"):
        parse_token(None)  # type: ignore[arg-type]


def test_parser_rejects_overlong_input_without_echo() -> None:
    value = "synthetic-sensitive-token" * 10_000

    with pytest.raises(InvalidTokenError) as exc_info:
        parse_token(value)

    assert value not in str(exc_info.value)


def test_selector_and_secret_tampering_change_the_verifier() -> None:
    selector = b"\x10" * SELECTOR_BYTES
    secret = b"\x20" * SECRET_BYTES
    original = parse_token(_build_token(selector, secret))
    selector_tampered = parse_token(_build_token(b"\x11" * SELECTOR_BYTES, secret))
    secret_tampered = parse_token(_build_token(selector, b"\x21" * SECRET_BYTES))

    assert verify_token(original, original.verifier)
    assert not verify_token(selector_tampered, original.verifier)
    assert not verify_token(secret_tampered, original.verifier)


@pytest.mark.parametrize(
    ("stored_verifier_factory", "expected_result"),
    [
        (lambda parsed: parsed.verifier, True),
        (lambda _parsed: None, False),
        (lambda _parsed: b"wrong", False),
        (lambda _parsed: b"\xff" * VERIFIER_BYTES, False),
    ],
)
def test_verification_makes_one_fixed_length_comparison(
    monkeypatch: pytest.MonkeyPatch,
    stored_verifier_factory: Callable[[ParsedToken], bytes | None],
    expected_result: bool,
) -> None:
    parsed = parse_token(_build_token(b"\x30" * SELECTOR_BYTES, b"\x40" * SECRET_BYTES))
    calls: list[tuple[bytes, bytes]] = []

    def recording_compare(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return left == right

    monkeypatch.setattr(hmac, "compare_digest", recording_compare)

    result = verify_token(parsed, stored_verifier_factory(parsed))

    assert result is expected_result
    assert len(calls) == 1
    assert len(calls[0][0]) == VERIFIER_BYTES
    assert len(calls[0][1]) == VERIFIER_BYTES


@pytest.mark.parametrize(
    "parsed_factory",
    [
        lambda parsed: _with_runtime_version(parsed, TOKEN_VERSION + 1),
        lambda parsed: _with_runtime_version(parsed, True),
        lambda parsed: _with_runtime_version(parsed, 1.0),
        lambda parsed: ParsedToken(
            version=TOKEN_VERSION,
            selector="A" * 21 + "B",
            verifier=parsed.verifier,
        ),
        lambda parsed: ParsedToken(
            version=TOKEN_VERSION,
            selector=parsed.selector,
            verifier=b"short",
        ),
    ],
)
def test_invalid_parsed_metadata_follows_fixed_length_dummy_path(
    monkeypatch: pytest.MonkeyPatch,
    parsed_factory: Callable[[ParsedToken], ParsedToken],
) -> None:
    valid = parse_token(_build_token(b"\x50" * SELECTOR_BYTES, b"\x60" * SECRET_BYTES))
    parsed = parsed_factory(valid)
    calls: list[tuple[bytes, bytes]] = []

    def recording_compare(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return left == right

    monkeypatch.setattr(hmac, "compare_digest", recording_compare)

    assert not verify_token(parsed, valid.verifier)
    assert len(calls) == 1
    assert tuple(map(len, calls[0])) == (VERIFIER_BYTES, VERIFIER_BYTES)


def test_token_objects_do_not_define_value_equality() -> None:
    token = _build_token(b"\x70" * SELECTOR_BYTES, b"\x80" * SECRET_BYTES)
    first = parse_token(token)
    second = parse_token(token)
    issued_first = tokens.IssuedToken(value=token, parsed=first)
    issued_second = tokens.IssuedToken(value=token, parsed=second)

    assert first is not second
    assert first != second
    assert issued_first != issued_second


def test_repr_and_str_redact_token_secret_and_verifier() -> None:
    issued = generate_token()
    encoded_secret = issued.value.split(".")[2]
    verifier_hex = issued.verifier.hex()

    for rendered in (repr(issued), str(issued), repr(issued.parsed), str(issued.parsed)):
        assert issued.value not in rendered
        assert encoded_secret not in rendered
        assert verifier_hex not in rendered
        assert "<redacted>" in rendered

    assert issued.selector in repr(issued)
    assert issued.selector in repr(issued.parsed)
