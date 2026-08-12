"""Versioned opaque bearer-token generation and verification primitives."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass, field
from secrets import token_bytes
from typing import Final

TOKEN_VERSION: Final = 1
TOKEN_PREFIX: Final = "plb1"
SELECTOR_BYTES: Final = 16
SECRET_BYTES: Final = 32
VERIFIER_BYTES: Final = hashlib.sha256().digest_size

_SELECTOR_ENCODED_LENGTH: Final = 22
_SECRET_ENCODED_LENGTH: Final = 43
_TOKEN_ENCODED_LENGTH: Final = (
    len(TOKEN_PREFIX) + 1 + _SELECTOR_ENCODED_LENGTH + 1 + _SECRET_ENCODED_LENGTH
)
_VERIFIER_DOMAIN: Final = b"patchouli-lib:opaque-bearer-token:v1\x00"
_UNKNOWN_SELECTOR_VERIFIER: Final = hashlib.sha256(
    b"patchouli-lib:opaque-bearer-token:unknown-selector:v1"
).digest()
_INVALID_TOKEN_MESSAGE: Final = "Invalid bearer token."
_GENERATION_ERROR_MESSAGE: Final = "Secure random source returned an invalid value."


class InvalidTokenError(ValueError):
    """A generic, secret-safe token parsing error."""

    def __init__(self) -> None:
        super().__init__(_INVALID_TOKEN_MESSAGE)


class TokenGenerationError(RuntimeError):
    """A generic, secret-safe token generation error."""

    def __init__(self) -> None:
        super().__init__(_GENERATION_ERROR_MESSAGE)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ParsedToken:
    """Public lookup metadata and the non-reversible verifier for one token."""

    version: int
    selector: str
    verifier: bytes = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(version={self.version!r}, "
            f"selector={self.selector!r}, verifier=<redacted>)"
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class IssuedToken:
    """A one-time token value plus metadata safe to persist separately."""

    value: str = field(repr=False)
    parsed: ParsedToken

    @property
    def version(self) -> int:
        return self.parsed.version

    @property
    def selector(self) -> str:
        return self.parsed.selector

    @property
    def verifier(self) -> bytes:
        return self.parsed.verifier

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(version={self.version!r}, "
            f"selector={self.selector!r}, value=<redacted>, verifier=<redacted>)"
        )

    def __str__(self) -> str:
        return repr(self)


def _encode_component(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_component(value: str, *, expected_bytes: int) -> bytes:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise InvalidTokenError from None

    padding = b"=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise InvalidTokenError from None

    if len(decoded) != expected_bytes or _encode_component(decoded) != value:
        raise InvalidTokenError
    return decoded


def _derive_verifier(selector: bytes, secret: bytes) -> bytes:
    return hashlib.sha256(_VERIFIER_DOMAIN + selector + secret).digest()


def _constant_time_compare(left: bytes, right: bytes) -> bool:
    return hmac.compare_digest(left, right)


def _is_canonical_selector(value: object) -> bool:
    if not isinstance(value, str) or len(value) != _SELECTOR_ENCODED_LENGTH:
        return False
    try:
        _decode_component(value, expected_bytes=SELECTOR_BYTES)
    except InvalidTokenError:
        return False
    return True


def generate_token() -> IssuedToken:
    """Generate a version-1 token and return its one-time value and metadata."""

    selector = token_bytes(SELECTOR_BYTES)
    secret = token_bytes(SECRET_BYTES)
    if len(selector) != SELECTOR_BYTES or len(secret) != SECRET_BYTES:
        raise TokenGenerationError

    encoded_selector = _encode_component(selector)
    encoded_secret = _encode_component(secret)
    value = f"{TOKEN_PREFIX}.{encoded_selector}.{encoded_secret}"
    parsed = ParsedToken(
        version=TOKEN_VERSION,
        selector=encoded_selector,
        verifier=_derive_verifier(selector, secret),
    )
    return IssuedToken(value=value, parsed=parsed)


def parse_token(value: str) -> ParsedToken:
    """Strictly parse a token without retaining or returning its raw secret."""

    if not isinstance(value, str) or len(value) != _TOKEN_ENCODED_LENGTH:
        raise InvalidTokenError

    parts = value.split(".")
    if len(parts) != 3:
        raise InvalidTokenError
    prefix, encoded_selector, encoded_secret = parts
    if (
        prefix != TOKEN_PREFIX
        or len(encoded_selector) != _SELECTOR_ENCODED_LENGTH
        or len(encoded_secret) != _SECRET_ENCODED_LENGTH
    ):
        raise InvalidTokenError

    selector = _decode_component(encoded_selector, expected_bytes=SELECTOR_BYTES)
    secret = _decode_component(encoded_secret, expected_bytes=SECRET_BYTES)
    return ParsedToken(
        version=TOKEN_VERSION,
        selector=encoded_selector,
        verifier=_derive_verifier(selector, secret),
    )


def verify_token(parsed: ParsedToken, stored_verifier: bytes | None) -> bool:
    """Compare one fixed-size digest for both known and unknown selectors.

    The caller first looks up ``parsed.selector``. It passes the stored verifier
    for a known selector or ``None`` for an unknown selector. Invalid persisted
    metadata follows the same dummy-comparison path and fails closed.
    """

    metadata_is_valid = (
        type(parsed.version) is int
        and parsed.version == TOKEN_VERSION
        and _is_canonical_selector(parsed.selector)
    )
    if (
        metadata_is_valid
        and isinstance(parsed.verifier, bytes)
        and len(parsed.verifier) == VERIFIER_BYTES
    ):
        presented_is_valid = True
        presented = parsed.verifier
    else:
        presented_is_valid = False
        presented = _UNKNOWN_SELECTOR_VERIFIER

    if isinstance(stored_verifier, bytes) and len(stored_verifier) == VERIFIER_BYTES:
        stored_is_valid = True
        expected = stored_verifier
    else:
        stored_is_valid = False
        expected = _UNKNOWN_SELECTOR_VERIFIER

    matches = _constant_time_compare(presented, expected)
    return presented_is_valid and stored_is_valid and matches


__all__ = [
    "SELECTOR_BYTES",
    "SECRET_BYTES",
    "TOKEN_PREFIX",
    "TOKEN_VERSION",
    "VERIFIER_BYTES",
    "InvalidTokenError",
    "IssuedToken",
    "ParsedToken",
    "TokenGenerationError",
    "generate_token",
    "parse_token",
    "verify_token",
]
