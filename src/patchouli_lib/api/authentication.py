from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from fastapi import Request
from sqlalchemy import Connection, Engine

from patchouli_lib.api.errors import authentication_required, invalid_token
from patchouli_lib.auth.repository import AuthRepository, CredentialLibraryGrantSummary
from patchouli_lib.auth.schemas import (
    AuthenticatedCaller,
    CallerKind,
    SectionGrantRecord,
)
from patchouli_lib.auth.service import (
    LAST_USED_COALESCE_MICROSECONDS,
    AuthenticationError,
    AuthenticationService,
    Clock,
    utc_microseconds,
)
from patchouli_lib.database import immediate_transaction

AUTHORIZATION_HEADER = b"authorization"
MAX_AUTHORIZATION_HEADER_BYTES = 256
PolicyMode = Literal["operator", "legacy_section", "library_grants"]


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticatedRequestContext:
    """Token-free caller state captured in one completed authentication transaction."""

    authenticated: AuthenticatedCaller
    grants: tuple[SectionGrantRecord, ...]
    # Defaults preserve manually constructed legacy contexts in route tests.
    policy_mode: PolicyMode = "legacy_section"
    library_grants: tuple[CredentialLibraryGrantSummary, ...] = ()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(caller_id={self.authenticated.caller.id!r}, "
            f"credential_id={self.authenticated.credential.id!r}, "
            f"kind={self.authenticated.caller.kind!r}, grant_count={len(self.grants)})"
        )


def _authorization_values(request: Request) -> tuple[bytes, ...]:
    headers: Sequence[tuple[bytes, bytes]] = request.scope.get("headers", ())
    return tuple(value for name, value in headers if name.lower() == AUTHORIZATION_HEADER)


def extract_bearer_token(request: Request) -> str:
    """Return one strictly parsed bearer token for immediate authentication only.

    The caller must keep the returned raw token in a short-lived local variable and
    pass it directly to the transaction-owned authentication or application service.
    It must never be retained on the request, an authenticated context, a model, a
    response, a log record, or durable storage.
    """

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
        credential = extract_bearer_token(request)
        try:
            with self._engine.connect() as connection:
                # A real read snapshot admits credential and grants together without
                # reserving the single writer merely to read coalesced metadata.
                connection.exec_driver_sql("BEGIN")
                try:
                    now = self._clock()
                    authenticated = AuthenticationService(
                        AuthRepository(connection),
                        clock=lambda: now,
                        last_used_coalesce_microseconds=-1,
                    ).authenticate(credential)
                    stored = authenticated.credential
                    baseline = stored.last_used_at or stored.created_at
                    touch_due = (
                        now >= baseline + LAST_USED_COALESCE_MICROSECONDS
                        and now >= stored.updated_at
                    )
                    if not touch_due:
                        return self._context(connection, authenticated)
                finally:
                    connection.rollback()

            # Never upgrade the read snapshot to a writer. The original full
            # transaction reauthenticates and rereads grants after that snapshot
            # is closed, retaining last-used commit/rollback and race semantics.
            with immediate_transaction(self._engine) as connection:
                authenticated = AuthenticationService(
                    AuthRepository(connection),
                    clock=self._clock,
                ).authenticate(credential)
                context = self._context(connection, authenticated)
        except AuthenticationError:
            raise invalid_token() from None
        return context

    @staticmethod
    def _context(
        connection: Connection, authenticated: AuthenticatedCaller
    ) -> AuthenticatedRequestContext:
        repository = AuthRepository(connection)
        grants: tuple[SectionGrantRecord, ...] = ()
        library_grants: tuple[CredentialLibraryGrantSummary, ...] = ()
        policy_mode: PolicyMode = "operator"
        if authenticated.caller.kind is CallerKind.AGENT:
            home_library_id = authenticated.caller.library_id
            caller_id = authenticated.caller.id
            credential_id = authenticated.credential.id
            if repository.has_library_grant_policy(home_library_id, caller_id, credential_id):
                policy_mode = "library_grants"
                library_grants = repository.list_credential_library_grants(
                    home_library_id=home_library_id,
                    caller_id=caller_id,
                    credential_id=credential_id,
                )
            else:
                policy_mode = "legacy_section"
                grants = repository.list_grants(home_library_id, caller_id)
        return AuthenticatedRequestContext(
            authenticated=authenticated,
            grants=grants,
            policy_mode=policy_mode,
            library_grants=library_grants,
        )


__all__ = [
    "AUTHORIZATION_HEADER",
    "MAX_AUTHORIZATION_HEADER_BYTES",
    "PolicyMode",
    "AuthenticatedRequestContext",
    "BearerAuthentication",
    "extract_bearer_token",
]
