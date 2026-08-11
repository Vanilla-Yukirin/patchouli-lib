from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchouli_client.headers import ResponseMetadata
    from patchouli_client.models import ProblemDetails


class PatchouliClientError(Exception):
    """Base class for errors raised by the wire client."""


class ProtocolError(PatchouliClientError):
    """The server response did not satisfy the accepted wire contract."""


class TransportError(PatchouliClientError):
    """A bounded transport operation failed without exposing request secrets."""

    def __init__(self, *, operation: str, attempts: int) -> None:
        self.operation = operation
        self.attempts = attempts
        super().__init__(f"{operation} transport failed after {attempts} attempt(s)")


class ProblemError(PatchouliClientError):
    """An RFC 9457 application problem with a deliberately safe message."""

    def __init__(self, problem: ProblemDetails, metadata: ResponseMetadata) -> None:
        self.problem = problem
        self.metadata = metadata
        super().__init__(
            f"PatchouliLib request failed with {problem.status} {problem.code} "
            f"(request_id={problem.request_id})"
        )

    def __repr__(self) -> str:
        return (
            f"ProblemError(status={self.problem.status}, code={self.problem.code!r}, "
            f"request_id={self.problem.request_id!r})"
        )
