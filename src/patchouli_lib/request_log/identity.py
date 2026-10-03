"""Per-request authenticated identity for the HTTP metadata recorder.

The mutable cell is intentional: AnyIO copies context into worker threads, so
replacing a ContextVar value in a worker would not update the ASGI parent task.
Only opaque, already-verified identifiers may be placed here; never a token.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchouli_lib.auth.schemas import AuthenticatedCaller


@dataclass(slots=True)
class RequestIdentity:
    caller_id: str | None = None
    home_library_id: str | None = None
    credential_id: str | None = None


_current_identity: ContextVar[RequestIdentity | None] = ContextVar(
    "patchouli_request_log_identity", default=None
)


def begin_request_identity(identity: RequestIdentity) -> Token[RequestIdentity | None]:
    return _current_identity.set(identity)


def end_request_identity(token: Token[RequestIdentity | None]) -> None:
    _current_identity.reset(token)


def note_authenticated_identity(authenticated: AuthenticatedCaller) -> None:
    """Remember successful Bearer authentication without retaining its value."""
    identity = _current_identity.get()
    if identity is not None:
        identity.caller_id = authenticated.caller.id
        identity.home_library_id = authenticated.caller.library_id
        identity.credential_id = authenticated.credential.id
