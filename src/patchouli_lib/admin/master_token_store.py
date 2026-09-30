"""Storage primitives for the single-person administration identity.

An empty table never authorizes remote initialization. Only local commands
may initialize or recover the identity after verifying their execution
context. The HTTP login reads the verifier but cannot create or recover it.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import Connection, insert, select, update
from sqlalchemy.exc import IntegrityError

from patchouli_lib.admin.passwords import hash_password, password_matches
from patchouli_lib.auth.models import MasterIdentity

_MAX_SQLITE_INTEGER = (1 << 63) - 1
_IDENTITY_HEX_DIGITS = frozenset("0123456789abcdef")


def _new_identity_id() -> str:
    return secrets.token_hex(16)


def _valid_time(value: int) -> bool:
    return type(value) is int and 0 <= value <= _MAX_SQLITE_INTEGER


def _hash_new_token(value: str) -> str:
    try:
        length = len(value.encode("utf-8"))
        if length < 32 or length > 1_024:
            raise ValueError
        return hash_password(value)
    except (UnicodeError, ValueError):
        raise ValueError("Invalid master token input.") from None


def _matches(candidate: str, verifier: str) -> bool:
    try:
        return password_matches(candidate, verifier)
    except (UnicodeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class MasterTokenState:
    """Non-secret identity and generation for session admission checks."""

    identity_id: str
    session_generation: int


@dataclass(frozen=True, slots=True)
class _StoredMasterToken:
    state: MasterTokenState
    verifier: str = field(repr=False)
    updated_at: int


class MasterTokenAlreadyInitialized(RuntimeError):
    """The single administrator identity already exists."""


class MasterTokenRepository:
    """Persist the human identity without owning or committing a transaction."""

    def __init__(
        self,
        connection: Connection,
        *,
        identity_factory: Callable[[], str] = _new_identity_id,
    ) -> None:
        self._connection = connection
        self._identity_factory = identity_factory

    def initialize_from_local_cli(self, token: str, *, now: int) -> MasterTokenState:
        """Set the first verifier; the caller MUST be a local-only CLI.

        No storage method can establish that a process is local. Never expose
        this operation through an empty-database HTTP registration route.
        """

        if not _valid_time(now):
            raise ValueError("Invalid master identity timestamp.")
        verifier = _hash_new_token(token)
        identity_id = self._identity_factory()
        if (
            not isinstance(identity_id, str)
            or len(identity_id) != 32
            or any(character not in _IDENTITY_HEX_DIGITS for character in identity_id)
        ):
            raise ValueError("Invalid master identity identifier.")
        state = MasterTokenState(identity_id=identity_id, session_generation=1)
        try:
            with self._connection.begin_nested():
                self._connection.execute(
                    insert(MasterIdentity),
                    {
                        "slot": 1,
                        "identity_id": identity_id,
                        "token_verifier": verifier,
                        "session_generation": 1,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
        except IntegrityError:
            raise MasterTokenAlreadyInitialized("Master identity already initialized.") from None
        return state

    def authenticate(self, token: str) -> MasterTokenState | None:
        """Verify the master token without accepting an operator bearer."""

        stored = self._current()
        if stored is None or not _matches(token, stored.verifier):
            return None
        return stored.state

    def has_identity(self) -> bool:
        """Fail closed for legacy login once the master identity slot exists."""

        statement = select(MasterIdentity.slot).where(MasterIdentity.slot == 1)
        return self._connection.execute(statement).scalar_one_or_none() == 1

    def rotate(self, old_token: str, new_token: str, *, now: int) -> MasterTokenState | None:
        """Replace the verifier and increment the generation in one CAS write."""

        stored = self._current()
        if stored is None or not _matches(old_token, stored.verifier):
            return None
        return self._replace_token(stored, new_token, now=now)

    def recover_from_local_cli(self, new_token: str, *, now: int) -> MasterTokenState | None:
        """Reset a lost token using local database access, not an HTTP session.

        The caller MUST be a local-only CLI with explicit reset confirmation.
        This never creates an identity or changes Agent/operator credentials.
        """

        stored = self._current()
        if stored is None:
            return None
        return self._replace_token(stored, new_token, now=now)

    def _replace_token(
        self, stored: _StoredMasterToken, new_token: str, *, now: int
    ) -> MasterTokenState | None:
        if not _valid_time(now) or now < stored.updated_at:
            raise ValueError("Invalid master identity timestamp.")
        if stored.state.session_generation >= _MAX_SQLITE_INTEGER:
            raise ValueError("Master session generation is exhausted.")
        if _matches(new_token, stored.verifier):
            raise ValueError("New master token must differ from the current token.")
        new_verifier = _hash_new_token(new_token)
        next_generation = stored.state.session_generation + 1
        result = self._connection.execute(
            update(MasterIdentity)
            .where(
                MasterIdentity.slot == 1,
                MasterIdentity.identity_id == stored.state.identity_id,
                MasterIdentity.session_generation == stored.state.session_generation,
                MasterIdentity.token_verifier == stored.verifier,
            )
            .values(
                token_verifier=new_verifier,
                session_generation=next_generation,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            return None
        return MasterTokenState(stored.state.identity_id, next_generation)

    def is_session_generation_current(self, identity_id: str, generation: int) -> bool:
        """Recheck this against the database on every future session admission."""

        if type(generation) is not int or generation < 1 or not isinstance(identity_id, str):
            return False
        statement = select(MasterIdentity.slot).where(
            MasterIdentity.slot == 1,
            MasterIdentity.identity_id == identity_id,
            MasterIdentity.session_generation == generation,
        )
        return self._connection.execute(statement).scalar_one_or_none() == 1

    def _current(self) -> _StoredMasterToken | None:
        row = (
            self._connection.execute(
                select(
                    MasterIdentity.identity_id,
                    MasterIdentity.token_verifier,
                    MasterIdentity.session_generation,
                    MasterIdentity.updated_at,
                ).where(MasterIdentity.slot == 1)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        identity_id = row["identity_id"]
        verifier = row["token_verifier"]
        generation = row["session_generation"]
        updated_at = row["updated_at"]
        if (
            not isinstance(identity_id, str)
            or not isinstance(verifier, str)
            or type(generation) is not int
            or generation < 1
            or not _valid_time(updated_at)
        ):
            return None
        return _StoredMasterToken(MasterTokenState(identity_id, generation), verifier, updated_at)


__all__ = ["MasterTokenAlreadyInitialized", "MasterTokenRepository", "MasterTokenState"]
