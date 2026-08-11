from __future__ import annotations

from dataclasses import dataclass

import httpx

from patchouli_client.errors import ProtocolError


@dataclass(frozen=True, slots=True)
class CacheControl:
    directives: tuple[str, ...]

    @classmethod
    def parse(cls, value: str) -> CacheControl:
        directives = tuple(part.strip().lower() for part in value.split(",") if part.strip())
        return cls(directives=directives)

    def contains(self, name: str) -> bool:
        expected = name.lower()
        return any(directive.split("=", 1)[0] == expected for directive in self.directives)


def require_strong_etag(value: str) -> str:
    if value.startswith("W/") or len(value) < 2 or not value.startswith('"'):
        raise ProtocolError("response did not contain a strong ETag")
    if not value.endswith('"'):
        raise ProtocolError("response did not contain a strong ETag")
    opaque = value[1:-1]
    if any(
        character == '"' or ord(character) < 0x21 or ord(character) == 0x7F for character in opaque
    ):
        raise ProtocolError("response did not contain a strong ETag")
    return value


@dataclass(frozen=True, slots=True)
class ResponseMetadata:
    request_id: str
    cache_control: CacheControl
    etag: str | None
    location: str | None
    idempotency_replayed: bool

    @classmethod
    def from_headers(
        cls,
        headers: httpx.Headers,
        *,
        request_id_fallback: str | None = None,
    ) -> ResponseMetadata:
        request_id = headers.get("X-Request-ID") or request_id_fallback
        if not request_id:
            raise ProtocolError("response did not contain a request ID")

        cache_control_value = headers.get("Cache-Control")
        if cache_control_value is None:
            raise ProtocolError("protected response did not contain Cache-Control")
        cache_control = CacheControl.parse(cache_control_value)
        if not cache_control.contains("private") or not cache_control.contains("no-store"):
            raise ProtocolError("protected response did not prohibit shared or persistent caching")

        replayed_value = headers.get("Idempotency-Replayed")
        if replayed_value is None:
            replayed = False
        elif replayed_value.lower() == "true":
            replayed = True
        elif replayed_value.lower() == "false":
            replayed = False
        else:
            raise ProtocolError("response contained an invalid idempotency replay marker")

        return cls(
            request_id=request_id,
            cache_control=cache_control,
            etag=headers.get("ETag"),
            location=headers.get("Location"),
            idempotency_replayed=replayed,
        )


@dataclass(frozen=True, slots=True)
class ClientResponse[T]:
    value: T
    metadata: ResponseMetadata
