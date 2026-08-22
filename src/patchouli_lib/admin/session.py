from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from time import time
from typing import Final

Clock = Callable[[], float]
TokenFactory = Callable[[int], str]

_SESSION_VERSION: Final[int] = 1
_MAX_ENCODED_SESSION_BYTES: Final[int] = 512


@dataclass(frozen=True, slots=True)
class AdminSession:
    expires_at: int
    csrf_token: str


class AdminSessionCodec:
    """Issue and verify bounded stateless sessions containing no credentials."""

    def __init__(
        self,
        signing_secret: bytes,
        *,
        ttl_seconds: int,
        clock: Clock = time,
        token_factory: TokenFactory = secrets.token_urlsafe,
    ) -> None:
        if len(signing_secret) < 32:
            raise ValueError("Admin session signing secret must contain at least 32 bytes.")
        if ttl_seconds < 300 or ttl_seconds > 86_400:
            raise ValueError("Admin session TTL must be between 300 and 86400 seconds.")
        self._signing_secret = signing_secret
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._token_factory = token_factory

    def issue(self) -> tuple[str, AdminSession]:
        session = AdminSession(
            expires_at=int(self._clock()) + self._ttl_seconds,
            csrf_token=self._token_factory(32),
        )
        payload = json.dumps(
            {
                "csrf": session.csrf_token,
                "exp": session.expires_at,
                "v": _SESSION_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded_payload = _encode(payload)
        signature = hmac.digest(
            self._signing_secret,
            encoded_payload.encode("ascii"),
            "sha256",
        )
        return f"{encoded_payload}.{_encode(signature)}", session

    def verify(self, encoded_session: str) -> AdminSession | None:
        if not encoded_session or len(encoded_session.encode("utf-8")) > _MAX_ENCODED_SESSION_BYTES:
            return None
        try:
            encoded_payload, encoded_signature = encoded_session.split(".", 1)
            payload = _decode(encoded_payload)
            signature = _decode(encoded_signature)
        except (ValueError, UnicodeError, binascii.Error):
            return None
        expected = hmac.digest(
            self._signing_secret,
            encoded_payload.encode("ascii"),
            "sha256",
        )
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(decoded, dict) or set(decoded) != {"csrf", "exp", "v"}:
            return None
        if decoded["v"] != _SESSION_VERSION:
            return None
        expires_at = decoded["exp"]
        csrf_token = decoded["csrf"]
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or expires_at <= int(self._clock())
            or not isinstance(csrf_token, str)
            or len(csrf_token) < 32
            or len(csrf_token) > 128
        ):
            return None
        return AdminSession(expires_at=expires_at, csrf_token=csrf_token)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or any(character not in _BASE64URL_CHARACTERS for character in value):
        raise ValueError("Invalid base64url value.")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


_BASE64URL_CHARACTERS: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
