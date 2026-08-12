"""Strict and secret-safe idempotency storage-boundary schemas."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBytes, field_validator

from patchouli_lib.api.contracts import format_rfc3339_utc, parse_rfc3339_utc, validate_api_v1_path
from patchouli_lib.idempotency.models import (
    DIGEST_BYTES,
    ETAG_MAX_LENGTH,
    METHOD_MAX_LENGTH,
    REPLAY_BODY_MAX_BYTES,
    ROUTE_TEMPLATE_MAX_LENGTH,
)
from patchouli_lib.library.schemas import OpaqueId

IDEMPOTENCY_KEY_DIGEST_DOMAIN = b"patchouli-lib/idempotency-key/v1\x00"
REQUEST_FINGERPRINT_DOMAIN = b"patchouli-lib/idempotency-request/v1\x00"
MAX_IDEMPOTENCY_KEY_BYTES = 256

Digest32 = Annotated[StrictBytes, Field(min_length=DIGEST_BYTES, max_length=DIGEST_BYTES)]
Method = Annotated[str, Field(min_length=1, max_length=METHOD_MAX_LENGTH)]
RouteTemplate = Annotated[str, Field(min_length=1, max_length=ROUTE_TEMPLATE_MAX_LENGTH)]
ReplayBody = Annotated[StrictBytes, Field(min_length=1, max_length=REPLAY_BODY_MAX_BYTES)]
StrongETag = Annotated[str, Field(min_length=3, max_length=ETAG_MAX_LENGTH)]
RequestId = Annotated[str, Field(pattern=r"^req_[0-9a-f]{32}$")]

_METHOD_PATTERN = re.compile(r"^[A-Z]+$", re.ASCII)
_ROUTE_SEGMENT_PATTERN = re.compile(r"^(?:[a-z0-9][a-z0-9-]*|\{[a-z][a-z0-9_]*\})$", re.ASCII)
_STRONG_ETAG_PATTERN = re.compile(r'^"[!#-~]+"$', re.ASCII)


class IdempotencySchema(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


def digest_idempotency_key(presented_key: str) -> bytes:
    """Digest one bounded header value without retaining or rendering it."""
    if type(presented_key) is not str:
        raise ValueError("Idempotency-Key must be supplied as text.")
    try:
        encoded = presented_key.encode("ascii", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("Idempotency-Key must use bounded visible ASCII.") from exc
    if (
        not 1 <= len(encoded) <= MAX_IDEMPOTENCY_KEY_BYTES
        or encoded[0] in b" \t"
        or encoded[-1] in b" \t"
        or any(value < 0x21 or value > 0x7E for value in encoded)
    ):
        raise ValueError("Idempotency-Key must use bounded visible ASCII.")
    return hashlib.sha256(IDEMPOTENCY_KEY_DIGEST_DOMAIN + encoded).digest()


def digest_request_fingerprint(*semantic_parts: bytes) -> bytes:
    """Hash exact caller-defined semantic byte parts with unambiguous framing."""
    digest = hashlib.sha256(REQUEST_FINGERPRINT_DOMAIN)
    for part in semantic_parts:
        if type(part) is not bytes:
            raise ValueError("Request fingerprint parts must be exact bytes.")
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.digest()


def _validate_method(value: str) -> str:
    if _METHOD_PATTERN.fullmatch(value) is None:
        raise ValueError("HTTP method must already be canonical uppercase ASCII.")
    return value


def _validate_route_template(value: str) -> str:
    prefix = "/api/v1/"
    if not value.startswith(prefix) or any(character in value for character in "%?#\\"):
        raise ValueError("Route template must be a normalized relative API v1 template.")
    segments = value.removeprefix(prefix).split("/")
    if (
        not segments
        or not any(segment.startswith("{") for segment in segments)
        or any(_ROUTE_SEGMENT_PATTERN.fullmatch(segment) is None for segment in segments)
    ):
        raise ValueError("Route template must be a normalized relative API v1 template.")
    return value


def _validate_canonical_timestamp(value: str) -> str:
    if type(value) is not str:
        raise ValueError("Original request timestamp must be canonical RFC 3339 UTC text.")
    try:
        parsed = parse_rfc3339_utc(value)
    except ValueError as exc:
        raise ValueError("Original request timestamp must be canonical RFC 3339 UTC text.") from exc
    if format_rfc3339_utc(parsed) != value:
        raise ValueError("Original request timestamp must be canonical RFC 3339 UTC text.")
    return value


class TransactionValidatedCaller(IdempotencySchema):
    """Stable caller identity revalidated by the caller's current write transaction."""

    library_id: OpaqueId
    caller_id: OpaqueId


class IdempotencyRequest(IdempotencySchema):
    """Already-digested request identity; it intentionally cannot retain the raw key."""

    method: Method
    route_template: RouteTemplate
    key_digest: Digest32 = Field(repr=False)
    request_fingerprint: Digest32 = Field(repr=False)

    @field_validator("method")
    @classmethod
    def require_canonical_method(cls, value: str) -> str:
        return _validate_method(value)

    @field_validator("route_template")
    @classmethod
    def require_normalized_template(cls, value: str) -> str:
        return _validate_route_template(value)


class OriginalResponse(IdempotencySchema):
    """Safe, bounded wire response fields eligible for durable replay."""

    response_status: Annotated[int, Field(ge=200, le=299)]
    response_media_type: Literal["application/json"] = "application/json"
    response_body: ReplayBody = Field(repr=False)
    response_location: str | None = Field(default=None, repr=False)
    response_etag: StrongETag = Field(repr=False)
    original_request_id: RequestId
    original_request_timestamp: str

    @field_validator("response_body")
    @classmethod
    def require_json_object(cls, value: bytes) -> bytes:
        if b"\x00" in value:
            raise ValueError("Replay response body must be a valid JSON object.")
        try:
            decoded = value.decode("utf-8", errors="strict")
            parsed = json.loads(
                decoded,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Replay response body must be a valid JSON object.") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Replay response body must be a valid JSON object.")
        return value

    @field_validator("response_location")
    @classmethod
    def require_safe_relative_location(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_api_v1_path(value)
        except ValueError as exc:
            raise ValueError("Replay Location must be a canonical relative API v1 path.") from exc

    @field_validator("response_etag")
    @classmethod
    def require_strong_etag(cls, value: str) -> str:
        if _STRONG_ETAG_PATTERN.fullmatch(value) is None:
            raise ValueError("Replay ETag must be one strong ASCII entity tag.")
        return value

    @field_validator("original_request_timestamp")
    @classmethod
    def require_canonical_timestamp(cls, value: str) -> str:
        return _validate_canonical_timestamp(value)


class NewIdempotencyRecord(IdempotencyRequest, OriginalResponse):
    library_id: OpaqueId
    caller_id: OpaqueId


class StoredIdempotencyRecord(NewIdempotencyRecord):
    pass


class ReplayResponse(OriginalResponse):
    """Presentation-only replay; the replay marker is never persisted."""

    def presentation_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": self.response_media_type,
            "ETag": self.response_etag,
            "X-Request-ID": self.original_request_id,
            "Idempotency-Replayed": "true",
        }
        if self.response_location is not None:
            headers["Location"] = self.response_location
        return headers


__all__ = [
    "IDEMPOTENCY_KEY_DIGEST_DOMAIN",
    "MAX_IDEMPOTENCY_KEY_BYTES",
    "REQUEST_FINGERPRINT_DOMAIN",
    "IdempotencyRequest",
    "NewIdempotencyRecord",
    "OriginalResponse",
    "ReplayResponse",
    "StoredIdempotencyRecord",
    "TransactionValidatedCaller",
    "digest_idempotency_key",
    "digest_request_fingerprint",
]
