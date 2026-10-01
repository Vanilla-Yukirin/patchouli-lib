"""Short-lived, purpose-bound browser protection for first master setup.

This cookie grants no administration access and contains no setup proof or
master Token. The server must still authorize the submitted setup operation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from time import time

_DOMAIN = b"patchouli.master_setup.v1\0"
_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


@dataclass(frozen=True, slots=True)
class MasterSetupSession:
    expires_at: int
    csrf_token: str

    def audit_fingerprint(self) -> bytes:
        return hashlib.sha256(self.csrf_token.encode("ascii")).digest()


class MasterSetupSessionCodec:
    """Sign a restricted setup form, never a normal administration session."""

    def __init__(
        self,
        signing_secret: bytes,
        *,
        ttl_seconds: int = 300,
        clock: Callable[[], float] = time,
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        if not 32 <= len(signing_secret) <= 1_024:
            raise ValueError("Setup session signing secret must contain 32 to 1024 bytes.")
        if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 600:
            raise ValueError("Setup session TTL must be between 60 and 600 seconds.")
        self._secret = signing_secret
        self._ttl = ttl_seconds
        self._clock = clock
        self._tokens = token_factory

    def issue(self) -> tuple[str, MasterSetupSession]:
        csrf = self._tokens(32)
        if not _valid_csrf(csrf):
            raise ValueError("Invalid setup session CSRF token.")
        session = MasterSetupSession(int(self._clock()) + self._ttl, csrf)
        payload = _encode(
            json.dumps(
                {"v": 1, "purpose": "master_setup", "exp": session.expires_at, "csrf": csrf},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        signature = hmac.digest(self._secret, _DOMAIN + payload.encode("ascii"), "sha256")
        return f"{payload}.{_encode(signature)}", session

    def verify(self, encoded_session: str) -> MasterSetupSession | None:
        try:
            if not encoded_session or len(encoded_session.encode("utf-8")) > 512:
                return None
            payload, encoded_signature = encoded_session.split(".", 1)
            signature = _decode(encoded_signature)
            expected = hmac.digest(self._secret, _DOMAIN + payload.encode("ascii"), "sha256")
            if not hmac.compare_digest(signature, expected):
                return None
            decoded = json.loads(_decode(payload), object_pairs_hook=_unique_object)
            if not isinstance(decoded, dict) or set(decoded) != {"v", "purpose", "exp", "csrf"}:
                return None
            now = int(self._clock())
            if (
                type(decoded["v"]) is not int
                or decoded["v"] != 1
                or decoded["purpose"] != "master_setup"
                or type(decoded["exp"]) is not int
                or not now < decoded["exp"] <= now + self._ttl
                or not _valid_csrf(decoded["csrf"])
            ):
                return None
            return MasterSetupSession(decoded["exp"], decoded["csrf"])
        except (ValueError, UnicodeError, binascii.Error, json.JSONDecodeError):
            return None


def _valid_csrf(value: object) -> bool:
    return isinstance(value, str) and value.isascii() and 32 <= len(value) <= 128


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate setup session field.")
        result[key] = value
    return result


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or any(character not in _ALPHABET for character in value):
        raise ValueError("Invalid setup session encoding.")
    decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _encode(decoded) != value:
        raise ValueError("Non-canonical setup session encoding.")
    return decoded
