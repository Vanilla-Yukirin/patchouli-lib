from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from collections.abc import Callable
from typing import Final

SaltFactory = Callable[[int], bytes]

PASSWORD_SCHEME: Final[str] = "pbkdf2_sha256"
DEFAULT_PASSWORD_ITERATIONS: Final[int] = 600_000
_MIN_PASSWORD_ITERATIONS: Final[int] = 300_000
_MAX_PASSWORD_ITERATIONS: Final[int] = 1_000_000
_PASSWORD_SALT_BYTES: Final[int] = 16
_PASSWORD_DIGEST_BYTES: Final[int] = 32
_BASE64URL_CHARACTERS: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def hash_password(
    password: str,
    *,
    salt_factory: SaltFactory = secrets.token_bytes,
    iterations: int = DEFAULT_PASSWORD_ITERATIONS,
) -> str:
    password_bytes = _password_bytes(password)
    if iterations < _MIN_PASSWORD_ITERATIONS or iterations > _MAX_PASSWORD_ITERATIONS:
        raise ValueError("Password hash iteration count is outside the supported range.")
    salt = salt_factory(_PASSWORD_SALT_BYTES)
    if len(salt) != _PASSWORD_SALT_BYTES:
        raise ValueError("Password salt factory returned an invalid value.")
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password_bytes,
        salt,
        iterations,
        dklen=_PASSWORD_DIGEST_BYTES,
    )
    return f"{PASSWORD_SCHEME}${iterations}${_encode(salt)}${_encode(digest)}"


def password_matches(candidate: str, encoded_hash: str) -> bool:
    try:
        password_bytes = _password_bytes(candidate)
        iterations, salt, expected = parse_password_hash(encoded_hash)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password_bytes,
        salt,
        iterations,
        dklen=_PASSWORD_DIGEST_BYTES,
    )
    return hmac.compare_digest(actual, expected)


def parse_password_hash(encoded_hash: str) -> tuple[int, bytes, bytes]:
    if len(encoded_hash) > 256:
        raise ValueError("Admin password hash is invalid.")
    try:
        scheme, raw_iterations, encoded_salt, encoded_digest = encoded_hash.split("$")
        iterations = int(raw_iterations)
        salt = _decode(encoded_salt)
        digest = _decode(encoded_digest)
    except (ValueError, binascii.Error):
        raise ValueError("Admin password hash is invalid.") from None
    if (
        scheme != PASSWORD_SCHEME
        or str(iterations) != raw_iterations
        or iterations < _MIN_PASSWORD_ITERATIONS
        or iterations > _MAX_PASSWORD_ITERATIONS
        or len(salt) != _PASSWORD_SALT_BYTES
        or len(digest) != _PASSWORD_DIGEST_BYTES
    ):
        raise ValueError("Admin password hash is invalid.")
    return iterations, salt, digest


def _password_bytes(password: str) -> bytes:
    value = password.encode("utf-8")
    if len(value) < 12 or len(value) > 1_024:
        raise ValueError("Admin password length is outside the supported range.")
    return value


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or any(character not in _BASE64URL_CHARACTERS for character in value):
        raise ValueError("Invalid base64url value.")
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if _encode(decoded) != value:
        raise ValueError("Non-canonical base64url value.")
    return decoded


__all__ = [
    "DEFAULT_PASSWORD_ITERATIONS",
    "PASSWORD_SCHEME",
    "hash_password",
    "parse_password_hash",
    "password_matches",
]
