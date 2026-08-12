from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import Engine

from patchouli_lib.api.errors import authentication_required, invalid_token
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    AuthenticatedCaller,
    CallerKind,
    SectionGrantRecord,
)
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthenticationService,
    Clock,
    utc_microseconds,
)
from patchouli_lib.database import immediate_transaction

AUTHORIZATION_HEADER = b"authorization"
MAX_AUTHORIZATION_HEADER_BYTES = 256


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticatedRequestContext:
    """Token-free caller state captured in one completed authentication transaction."""

    authenticated: AuthenticatedCaller
    grants: tuple[SectionGrantRecord, ...]

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(caller_id={self.authenticated.caller.id!r}, "
            f"credential_id={self.authenticated.credential.id!r}, "
            f"kind={self.authenticated.caller.kind!r}, grant_count={len(self.grants)})"
        )


def _authorization_values(request: Request) -> tuple[bytes, ...]:
    headers: Sequence[tuple[bytes, bytes]] = request.scope.get("headers", ())
    return tuple(value for name, value in headers if name.lower() == AUTHORIZATION_HEADER)


def _bearer_credential(request: Request) -> str:
    values = _authorization_values(request)
    if not values:
        raise authentication_required()
    if len(values) != 1:
        raise invalid_token()

    encoded = values[0]
    if not encoded or len(encoded) > MAX_AUTHORIZATION_HEADER_BYTES:
        raise invalid_token()
    try:
        value = encoded.decode("ascii")
    except UnicodeDecodeError:
        raise invalid_token() from None

    parts = value.split(" ")
    if len(parts) != 2 or parts[0].casefold() != "bearer" or not parts[1]:
        raise invalid_token()
    return parts[1]


class BearerAuthentication:
    """Authenticate one request without retaining its raw Authorization value."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Clock = utc_microseconds,
    ) -> None:
        self._engine = engine
        self._clock = clock

    def __call__(self, request: Request) -> AuthenticatedRequestContext:
        credential = _bearer_credential(request)
        try:
            with immediate_transaction(self._engine) as connection:
                repository = AuthRepository(connection)
                authenticated = AuthenticationService(
                    repository,
                    clock=self._clock,
                ).authenticate(credential)
                grants = (
                    repository.list_grants(
                        authenticated.caller.library_id,
                        authenticated.caller.id,
                    )
                    if authenticated.caller.kind is CallerKind.AGENT
                    else ()
                )
                context = AuthenticatedRequestContext(
                    authenticated=authenticated,
                    grants=grants,
                )
        except AuthenticationError:
            raise invalid_token() from None
        return context


__all__ = [
    "AUTHORIZATION_HEADER",
    "MAX_AUTHORIZATION_HEADER_BYTES",
    "AuthenticatedRequestContext",
    "BearerAuthentication",
]
