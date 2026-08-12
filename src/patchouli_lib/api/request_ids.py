import re
import secrets
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from patchouli_lib.api.contracts import API_V1_PREFIX, PROTECTED_CACHE_CONTROL

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_STATE_ATTRIBUTE = "request_id"
INBOUND_CORRELATION_STATE_ATTRIBUTE = "inbound_correlation_id"
MAX_INBOUND_CORRELATION_ID_LENGTH = 64

_REQUEST_ID_PATTERN = re.compile(r"^req_[0-9a-f]{32}$", re.ASCII)
_INBOUND_CORRELATION_PATTERN = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9._:-]{{0,{MAX_INBOUND_CORRELATION_ID_LENGTH - 1}}}$",
    re.ASCII,
)

RequestIDFactory = Callable[[], str]


def generate_request_id() -> str:
    """Return a server-owned opaque request identifier."""
    return f"req_{secrets.token_hex(16)}"


def validate_inbound_correlation_id(value: str) -> str | None:
    """Accept a bounded, injection-safe correlation hint without making it authoritative."""
    if _INBOUND_CORRELATION_PATTERN.fullmatch(value) is None:
        return None
    return value


def _inbound_correlation_id(scope: Scope) -> str | None:
    values = [
        value
        for name, value in scope.get("headers", [])
        if name.lower() == REQUEST_ID_HEADER.lower().encode("ascii")
    ]
    if len(values) != 1:
        return None
    try:
        decoded = values[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    return validate_inbound_correlation_id(decoded)


def _protected_path(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def _safe_generated_request_id(factory: RequestIDFactory) -> str:
    candidate = factory()
    if _REQUEST_ID_PATTERN.fullmatch(candidate) is not None:
        return candidate
    return generate_request_id()


class RequestIDMiddleware:
    """Own response request IDs and protected cache policy without trusting proxy data."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        request_id_factory: RequestIDFactory = generate_request_id,
        protected_path_prefix: str = API_V1_PREFIX,
    ) -> None:
        self.app = app
        self.request_id_factory = request_id_factory
        self.protected_path_prefix = protected_path_prefix

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _safe_generated_request_id(self.request_id_factory)
        state = scope.setdefault("state", {})
        state[REQUEST_ID_STATE_ATTRIBUTE] = request_id
        state[INBOUND_CORRELATION_STATE_ATTRIBUTE] = _inbound_correlation_id(scope)
        protect_response = _protected_path(scope.get("path", ""), self.protected_path_prefix)

        async def send_with_kernel_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                if protect_response:
                    headers["Cache-Control"] = PROTECTED_CACHE_CONTROL
            await send(message)

        await self.app(scope, receive, send_with_kernel_headers)


def ensure_request_id(request: Request) -> str:
    """Return the request's server ID, creating one for standalone handler use."""
    existing = getattr(request.state, REQUEST_ID_STATE_ATTRIBUTE, None)
    if isinstance(existing, str) and _REQUEST_ID_PATTERN.fullmatch(existing) is not None:
        return existing
    request_id = generate_request_id()
    setattr(request.state, REQUEST_ID_STATE_ATTRIBUTE, request_id)
    return request_id


def get_request_id(request: Request) -> str:
    """FastAPI dependency for the current server-owned request ID."""
    return ensure_request_id(request)


RequestIDDependency = Annotated[str, Depends(get_request_id)]
