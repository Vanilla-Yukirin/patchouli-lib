from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from time import time
from typing import Final, Literal

Clock = Callable[[], float]
TokenFactory = Callable[[int], str]

_SESSION_VERSION: Final[int] = 1
_MASTER_SESSION_VERSION: Final[int] = 2
_MAX_ENCODED_SESSION_BYTES: Final[int] = 512
_MAX_SQLITE_INTEGER: Final[int] = (1 << 63) - 1
_IDENTITY_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class AdminSession:
    expires_at: int
    csrf_token: str

    def audit_fingerprint(self) -> bytes:
        """Correlate local structure writes without storing a bearer or CSRF value."""

        return hashlib.sha256(self.csrf_token.encode("ascii")).digest()


@dataclass(frozen=True, slots=True)
class MasterAdminSession(AdminSession):
    identity_id: str
    session_generation: int
    auth_mode: Literal["master"] = field(default="master", init=False)


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
        return self._sign(payload), session

    def issue_master(
        self, identity_id: str, session_generation: int
    ) -> tuple[str, MasterAdminSession]:
        """Issue a v2 cookie after the caller has authenticated a master token."""

        if not _valid_identity_id(identity_id) or not _valid_generation(session_generation):
            raise ValueError("Invalid master session identity or generation.")
        csrf_token = self._token_factory(32)
        if not _valid_csrf(csrf_token):
            raise ValueError("Invalid master session CSRF token.")
        session = MasterAdminSession(
            expires_at=int(self._clock()) + self._ttl_seconds,
            csrf_token=csrf_token,
            identity_id=identity_id,
            session_generation=session_generation,
        )
        payload = json.dumps(
            {
                "csrf": session.csrf_token,
                "exp": session.expires_at,
                "gen": session.session_generation,
                "identity_id": session.identity_id,
                "mode": session.auth_mode,
                "v": _MASTER_SESSION_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return self._sign(payload), session

    def verify(self, encoded_session: str) -> AdminSession | None:
        """Verify only legacy v1 password cookies; v2 is never admitted here."""

        decoded = self._decode_verified_payload(encoded_session)
        if not isinstance(decoded, dict) or set(decoded) != {"csrf", "exp", "v"}:
            return None
        if type(decoded["v"]) is not int or decoded["v"] != _SESSION_VERSION:
            return None
        expires_at = decoded["exp"]
        csrf_token = decoded["csrf"]
        if (
            type(expires_at) is not int
            or expires_at <= int(self._clock())
            or not _valid_csrf(csrf_token)
        ):
            return None
        return AdminSession(expires_at=expires_at, csrf_token=csrf_token)

    def verify_master(self, encoded_session: str) -> MasterAdminSession | None:
        """Verify a v2 cookie, but do not admit it as an authenticated session.

        The router must separately check identity_id and session_generation
        against the current database state on every request before admission.
        """

        decoded = self._decode_verified_payload(encoded_session)
        if not isinstance(decoded, dict) or set(decoded) != {
            "csrf",
            "exp",
            "gen",
            "identity_id",
            "mode",
            "v",
        }:
            return None
        if type(decoded["v"]) is not int or decoded["v"] != _MASTER_SESSION_VERSION:
            return None
        expires_at = decoded["exp"]
        csrf_token = decoded["csrf"]
        identity_id = decoded["identity_id"]
        generation = decoded["gen"]
        if (
            decoded["mode"] != "master"
            or type(decoded["mode"]) is not str
            or type(expires_at) is not int
            or expires_at <= int(self._clock())
            or not _valid_csrf(csrf_token)
            or not _valid_identity_id(identity_id)
            or not _valid_generation(generation)
        ):
            return None
        return MasterAdminSession(
            expires_at=expires_at,
            csrf_token=csrf_token,
            identity_id=identity_id,
            session_generation=generation,
        )

    def _sign(self, payload: bytes) -> str:
        encoded_payload = _encode(payload)
        signature = hmac.digest(
            self._signing_secret,
            encoded_payload.encode("ascii"),
            "sha256",
        )
        return f"{encoded_payload}.{_encode(signature)}"

    def _decode_verified_payload(self, encoded_session: str) -> object:
        try:
            if (
                not encoded_session
                or len(encoded_session.encode("utf-8")) > _MAX_ENCODED_SESSION_BYTES
            ):
                return None
            encoded_payload, encoded_signature = encoded_session.split(".", 1)
            payload = _decode(encoded_payload)
            signature = _decode(encoded_signature)
            expected = hmac.digest(
                self._signing_secret,
                encoded_payload.encode("ascii"),
                "sha256",
            )
            if not hmac.compare_digest(signature, expected):
                return None
            return json.loads(payload, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, binascii.Error, json.JSONDecodeError):
            return None


def _valid_identity_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in _IDENTITY_HEX_DIGITS for character in value)
    )


def _valid_generation(value: object) -> bool:
    return type(value) is int and 1 <= value <= _MAX_SQLITE_INTEGER


def _valid_csrf(value: object) -> bool:
    return isinstance(value, str) and value.isascii() and 32 <= len(value) <= 128


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate session field.")
        value[key] = item
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


_BASE64URL_CHARACTERS: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
