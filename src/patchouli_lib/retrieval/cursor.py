"""Integrity-protected keyset cursors for scoped retrieval routes.

Cursor payloads are authenticated, not encrypted. Callers must therefore treat
the complete cursor as non-secret even though request identity text is reduced
to a domain-separated digest before it is bound into the payload.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Final, cast

from patchouli_lib.api.contracts import MAX_CURSOR_LENGTH, MAX_PAGE_LIMIT

CURSOR_VERSION: Final = 1
MIN_CURSOR_SECRET_BYTES: Final = 32
MAX_CURSOR_IDENTITY_BYTES: Final = 4_096

_CURSOR_PREFIX: Final = "plc1"
_MAX_ROUTE_IDENTITY_BYTES: Final = 128
_MAX_SCOPE_ID_BYTES: Final = 255
_MAX_INTERNAL_KEY_CHARACTERS: Final = 255
_TAG_BYTES: Final = hashlib.sha256().digest_size
_AUTHENTICATION_DOMAIN: Final = b"patchouli-lib:retrieval-cursor:authentication:v1\x00"
_BINDING_DOMAIN: Final = b"patchouli-lib:retrieval-cursor:binding:v1\x00"
_REQUEST_IDENTITY_DOMAIN: Final = b"patchouli-lib:retrieval-cursor:request:v1\x00"
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$", re.ASCII)
_ROUTE_IDENTITY_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
    re.ASCII,
)


class InvalidCursorError(ValueError):
    """One client-safe failure for every untrusted cursor rejection."""

    def __init__(self) -> None:
        super().__init__("The pagination cursor is invalid or no longer applicable.")


@dataclass(frozen=True, slots=True)
class CursorBinding:
    """Normalized request context that an issued cursor cannot cross.

    ``None`` is reserved for the top-level Section collection. Section-scoped
    collections pass the exact Section ID. Route code owns normalization of the
    query, filters, and sort identities; these bounded bytes are hashed and are
    never serialized into the cursor.
    """

    caller_id: str
    policy_version: int
    section_id: str | None
    route_identity: str
    limit: int
    query_identity: bytes = field(default=b"", repr=False)
    filters_identity: bytes = field(default=b"", repr=False)
    sort_identity: bytes = field(default=b"", repr=False)

    def __post_init__(self) -> None:
        _require_bounded_text(self.caller_id, field_name="caller_id")
        if self.section_id is not None:
            _require_bounded_text(self.section_id, field_name="section_id")
        if (
            not isinstance(self.route_identity, str)
            or _ROUTE_IDENTITY_PATTERN.fullmatch(self.route_identity) is None
            or len(self.route_identity.encode("ascii")) > _MAX_ROUTE_IDENTITY_BYTES
        ):
            raise ValueError("route_identity must be a bounded canonical route name")
        if (
            isinstance(self.policy_version, bool)
            or not isinstance(self.policy_version, int)
            or not 1 <= self.policy_version <= (1 << 63) - 1
        ):
            raise ValueError("policy_version must be a positive 64-bit integer")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= MAX_PAGE_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")

        identities = (
            ("query_identity", self.query_identity),
            ("filters_identity", self.filters_identity),
            ("sort_identity", self.sort_identity),
        )
        for name, value in identities:
            if not isinstance(value, bytes):
                raise TypeError(f"{name} must be immutable bytes")
            if len(value) > MAX_CURSOR_IDENTITY_BYTES:
                raise ValueError(f"{name} exceeds the cursor identity limit")


class CursorCodec:
    """Encode and verify deterministic URL-safe cursor tokens with HMAC-SHA256."""

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        if not isinstance(secret, bytes):
            raise TypeError("cursor secret must be immutable bytes")
        if len(secret) < MIN_CURSOR_SECRET_BYTES:
            raise ValueError("cursor secret must contain at least 256 bits")
        self._secret = secret

    def __repr__(self) -> str:
        return f"{type(self).__name__}(secret=<redacted>, version={CURSOR_VERSION})"

    def encode(self, *, binding: CursorBinding, last_key: str) -> str:
        """Return a deterministic cursor for one normalized request context."""
        if not isinstance(binding, CursorBinding):
            raise TypeError("binding must be a CursorBinding")
        _require_internal_key(last_key)

        payload = _canonical_payload(
            {
                "b": _base64url_encode(self._binding_digest(binding)),
                "k": last_key,
                "v": CURSOR_VERSION,
            }
        )
        encoded_payload = _base64url_encode(payload)
        authenticated = f"{_CURSOR_PREFIX}.{encoded_payload}"
        tag = hmac.digest(
            self._secret,
            _AUTHENTICATION_DOMAIN + authenticated.encode("ascii"),
            "sha256",
        )
        token = f"{authenticated}.{_base64url_encode(tag)}"
        if len(token) > MAX_CURSOR_LENGTH:
            raise ValueError("encoded cursor exceeds the public cursor limit")
        return token

    def decode(self, cursor: str, *, binding: CursorBinding) -> str:
        """Verify one untrusted cursor and return its internal key.

        Every attacker-controlled failure maps to ``InvalidCursorError`` without
        preserving or echoing input details.
        """
        if not isinstance(binding, CursorBinding):
            raise TypeError("binding must be a CursorBinding")
        try:
            return self._decode(cursor, binding=binding)
        except InvalidCursorError:
            raise
        except (binascii.Error, json.JSONDecodeError, UnicodeError, ValueError, TypeError):
            raise InvalidCursorError from None

    def _decode(self, cursor: str, *, binding: CursorBinding) -> str:
        if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH:
            raise InvalidCursorError
        try:
            prefix, encoded_payload, encoded_tag = cursor.split(".")
        except ValueError:
            raise InvalidCursorError from None
        if prefix != _CURSOR_PREFIX:
            raise InvalidCursorError

        payload_bytes = _base64url_decode(encoded_payload)
        tag = _base64url_decode(encoded_tag)
        if len(tag) != _TAG_BYTES:
            raise InvalidCursorError
        authenticated = f"{prefix}.{encoded_payload}"
        expected_tag = hmac.digest(
            self._secret,
            _AUTHENTICATION_DOMAIN + authenticated.encode("ascii"),
            "sha256",
        )
        if not hmac.compare_digest(tag, expected_tag):
            raise InvalidCursorError

        payload = json.loads(
            payload_bytes.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
        if not isinstance(payload, dict) or set(payload) != {"b", "k", "v"}:
            raise InvalidCursorError
        if _canonical_payload(cast(dict[str, object], payload)) != payload_bytes:
            raise InvalidCursorError
        if payload["v"] != CURSOR_VERSION or isinstance(payload["v"], bool):
            raise InvalidCursorError
        encoded_binding = payload["b"]
        last_key = payload["k"]
        if not isinstance(encoded_binding, str) or not isinstance(last_key, str):
            raise InvalidCursorError
        supplied_binding = _base64url_decode(encoded_binding)
        expected_binding = self._binding_digest(binding)
        if len(supplied_binding) != len(expected_binding) or not hmac.compare_digest(
            supplied_binding,
            expected_binding,
        ):
            raise InvalidCursorError
        _require_internal_key(last_key)
        return last_key

    def _binding_digest(self, binding: CursorBinding) -> bytes:
        request_identity = _framed_digest(
            _REQUEST_IDENTITY_DOMAIN,
            binding.query_identity,
            binding.filters_identity,
            binding.sort_identity,
        )
        section_identity = (
            b"\x00" if binding.section_id is None else b"\x01" + binding.section_id.encode("utf-8")
        )
        material = _framed_bytes(
            binding.caller_id.encode("utf-8"),
            str(binding.policy_version).encode("ascii"),
            section_identity,
            binding.route_identity.encode("ascii"),
            str(binding.limit).encode("ascii"),
            request_identity,
        )
        return hmac.digest(self._secret, _BINDING_DOMAIN + material, "sha256")


def _require_bounded_text(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid Unicode") from exc
    if len(encoded) > _MAX_SCOPE_ID_BYTES:
        raise ValueError(f"{field_name} exceeds the cursor binding limit")


def _require_internal_key(value: object) -> None:
    if not isinstance(value, str) or not value or len(value) > _MAX_INTERNAL_KEY_CHARACTERS:
        raise ValueError("last_key must be a non-empty bounded string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("last_key must be valid Unicode") from exc


def _framed_bytes(*values: bytes) -> bytes:
    framed = bytearray()
    for value in values:
        framed.extend(len(value).to_bytes(4, "big"))
        framed.extend(value)
    return bytes(framed)


def _framed_digest(domain: bytes, *values: bytes) -> bytes:
    digest = hashlib.sha256()
    digest.update(domain)
    digest.update(_framed_bytes(*values))
    return digest.digest()


def _canonical_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or _BASE64URL_PATTERN.fullmatch(value) is None or len(value) % 4 == 1:
        raise InvalidCursorError
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4),
        altchars=b"-_",
        validate=True,
    )
    if _base64url_encode(decoded) != value:
        raise InvalidCursorError
    return decoded


def _reject_json_constant(_value: str) -> None:
    raise InvalidCursorError


__all__ = [
    "CURSOR_VERSION",
    "MAX_CURSOR_IDENTITY_BYTES",
    "MIN_CURSOR_SECRET_BYTES",
    "CursorBinding",
    "CursorCodec",
    "InvalidCursorError",
]
