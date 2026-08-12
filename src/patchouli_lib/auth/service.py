from __future__ import annotations

from collections.abc import Callable
from time import time_ns
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    AuthenticatedCaller,
    CallerKind,
    CallerRecord,
    IssuedCredential,
    NewCredential,
    SectionAction,
    credential_metadata,
)
from patchouli_lib.auth.tokens import (
    TOKEN_VERSION,
    InvalidTokenError,
    generate_token,
    parse_token,
    verify_token,
)

IdFactory = Callable[[], str]
Clock = Callable[[], int]
LAST_USED_COALESCE_MICROSECONDS = 5 * 60 * 1_000_000


def new_opaque_id() -> str:
    return uuid4().hex


def utc_microseconds() -> int:
    return time_ns() // 1_000


class AuthenticationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Invalid or inactive credential.")


class AuthorizationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Required authorization is not available.")


class CredentialExpiryError(ValueError):
    def __init__(self) -> None:
        super().__init__("Credential expiry must be finite and in the future.")


class CredentialPersistenceError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Credential could not be persisted.")


class CredentialIssuer:
    """Create one-time credentials without storing or replaying their raw value."""

    def __init__(
        self,
        repository: AuthRepository,
        *,
        id_factory: IdFactory = new_opaque_id,
        clock: Clock = utc_microseconds,
    ) -> None:
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock

    def issue(self, caller: CallerRecord, *, expires_at: int) -> IssuedCredential:
        created_at = self._clock()
        if expires_at <= created_at:
            raise CredentialExpiryError
        if caller.disabled_at is not None:
            raise AuthenticationError

        issued = generate_token()
        try:
            stored = self._repository.add_credential(
                NewCredential(
                    id=self._id_factory(),
                    library_id=caller.library_id,
                    caller_id=caller.id,
                    selector=issued.selector,
                    token_version=issued.version,
                    verifier=issued.verifier,
                    expires_at=expires_at,
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
        except SQLAlchemyError:
            pass
        else:
            return IssuedCredential(
                value=issued.value,
                credential=credential_metadata(stored),
            )
        raise CredentialPersistenceError


class AuthenticationService:
    """Authenticate and authorize against current transaction-visible state."""

    def __init__(
        self,
        repository: AuthRepository,
        *,
        clock: Clock = utc_microseconds,
        last_used_coalesce_microseconds: int = LAST_USED_COALESCE_MICROSECONDS,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._last_used_coalesce_microseconds = last_used_coalesce_microseconds

    def authenticate(self, token_value: str) -> AuthenticatedCaller:
        try:
            parsed = parse_token(token_value)
        except InvalidTokenError:
            raise AuthenticationError from None

        stored = self._repository.find_credential_by_selector(parsed.selector)
        expected_verifier = None if stored is None else stored.verifier
        verified = verify_token(parsed, expected_verifier)
        if not verified or stored is None or stored.token_version != TOKEN_VERSION:
            raise AuthenticationError

        caller = self._repository.get_caller(stored.library_id, stored.caller_id)
        now = self._clock()
        if (
            caller is None
            or caller.disabled_at is not None
            or stored.revoked_at is not None
            or stored.rotated_at is not None
            or now < stored.created_at
            or now >= stored.expires_at
        ):
            raise AuthenticationError

        last_used_baseline = stored.last_used_at or stored.created_at
        if (
            self._last_used_coalesce_microseconds >= 0
            and now >= last_used_baseline + self._last_used_coalesce_microseconds
            and now >= stored.updated_at
        ):
            stored = self._repository.touch_credential_last_used(stored, used_at=now)

        return AuthenticatedCaller(
            caller=caller,
            credential=credential_metadata(stored),
        )

    def require_operator(self, token_value: str, *, library_id: str) -> AuthenticatedCaller:
        authenticated = self.authenticate(token_value)
        if (
            authenticated.caller.library_id != library_id
            or authenticated.caller.kind is not CallerKind.OPERATOR
        ):
            raise AuthorizationError
        return authenticated

    def authorize_content(
        self,
        token_value: str,
        *,
        library_id: str,
        section_id: str,
        action: SectionAction,
    ) -> AuthenticatedCaller:
        authenticated = self.authenticate(token_value)
        if (
            authenticated.caller.library_id != library_id
            or authenticated.caller.kind is not CallerKind.AGENT
        ):
            raise AuthorizationError
        grant = self._repository.get_grant(
            library_id,
            authenticated.caller.id,
            section_id,
            action,
        )
        if grant is None:
            raise AuthorizationError
        return authenticated


__all__ = [
    "AuthenticationError",
    "AuthenticationService",
    "AuthorizationError",
    "Clock",
    "CredentialExpiryError",
    "CredentialIssuer",
    "CredentialPersistenceError",
    "IdFactory",
    "LAST_USED_COALESCE_MICROSECONDS",
    "new_opaque_id",
    "utc_microseconds",
]
