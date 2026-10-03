"""Authorized, atomic first browser setup of the single master identity."""

from __future__ import annotations

import hmac
import math
from collections.abc import Callable
from time import time

from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_setup_session import MasterSetupSession, MasterSetupSessionCodec
from patchouli_lib.admin.master_token_store import (
    MasterTokenAlreadyInitialized,
    MasterTokenRepository,
    MasterTokenState,
)
from patchouli_lib.admin.session import AdminSession, AdminSessionCodec
from patchouli_lib.auth.service import new_opaque_id
from patchouli_lib.config import Settings
from patchouli_lib.database import CURRENT_SCHEMA_REVISION, immediate_transaction

_MAX_SQLITE_INTEGER = (1 << 63) - 1


class MasterSetupUnavailableError(RuntimeError):
    """First setup is disabled or the database is not at the current schema."""


class MasterSetupAuthorizationError(RuntimeError):
    """The submitted browser session and setup authority were not accepted."""


class MasterSetupService:
    """Recheck first-setup authority under the same lock as identity and audit.

    The router still owns same-origin and CSRF admission. Raw cookies are
    verified again here; neither an empty identity table nor a source address
    authorizes this operation. No secret is returned or persisted in audit.
    """

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        *,
        legacy_session_codec: AdminSessionCodec | None = None,
        setup_session_codec: MasterSetupSessionCodec | None = None,
        clock: Callable[[], float] = time,
        identity_factory: Callable[[], str] = new_opaque_id,
        event_id_factory: Callable[[], str] = new_opaque_id,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._clock = clock
        self._identity_factory = identity_factory
        self._event_id_factory = event_id_factory
        secret = settings.admin_session_signing_secret
        secret_bytes = secret.get_secret_value().encode("utf-8") if secret is not None else None
        self._legacy_codec = legacy_session_codec
        self._setup_codec = setup_session_codec
        if secret_bytes is not None:
            self._legacy_codec = legacy_session_codec or AdminSessionCodec(
                secret_bytes,
                ttl_seconds=settings.admin_session_ttl_seconds,
                clock=clock,
            )
            self._setup_codec = setup_session_codec or MasterSetupSessionCodec(
                secret_bytes, clock=clock
            )

    def initialize(
        self,
        token: str,
        confirmation: str,
        *,
        legacy_cookie: str | None = None,
        setup_proof: str | None = None,
        setup_cookie: str | None = None,
    ) -> MasterTokenState:
        with immediate_transaction(self._engine) as connection:
            if not self._settings.admin_enabled:
                raise MasterSetupUnavailableError("Master setup is unavailable.")
            _require_current_schema(connection)
            repository = MasterTokenRepository(connection, identity_factory=self._identity_factory)
            if repository.has_identity():
                raise MasterTokenAlreadyInitialized("Master identity already initialized.")
            session = self._authorize(legacy_cookie, setup_proof, setup_cookie)
            now_seconds = self._clock()
            if (
                type(now_seconds) not in {int, float}
                or not math.isfinite(now_seconds)
                or now_seconds < 0
                or now_seconds * 1_000_000 > _MAX_SQLITE_INTEGER
            ):
                raise ValueError("Invalid master setup timestamp.")
            if session.expires_at <= int(now_seconds):
                raise MasterSetupAuthorizationError("Master setup authorization was rejected.")
            _require_confirmation(token, confirmation)
            now = int(now_seconds * 1_000_000)
            state = repository.initialize_after_authorized_setup(token, now=now)
            MasterAuditRepository(connection).add_success(
                identity_id=state.identity_id,
                session_generation=state.session_generation,
                session_fingerprint=session.audit_fingerprint(),
                action="auth.master.initialize",
                target_type="master_identity",
                target_id=state.identity_id,
                occurred_at=now,
                event_id=self._event_id_factory(),
            )
        return state

    def _authorize(
        self,
        legacy_cookie: str | None,
        setup_proof: str | None,
        setup_cookie: str | None,
    ) -> AdminSession | MasterSetupSession:
        if legacy_cookie is not None:
            if (
                setup_proof is not None
                or setup_cookie is not None
                or self._settings.admin_password_hash is None
                or self._legacy_codec is None
            ):
                raise MasterSetupAuthorizationError("Master setup authorization was rejected.")
            legacy = self._legacy_codec.verify(legacy_cookie)
            if legacy is not None:
                return legacy
        elif (
            setup_proof is not None
            and setup_cookie is not None
            and self._settings.admin_setup_token is not None
            and self._setup_codec is not None
        ):
            try:
                proof_matches = hmac.compare_digest(
                    setup_proof.encode("utf-8"),
                    self._settings.admin_setup_token.get_secret_value().encode("utf-8"),
                )
            except UnicodeError:
                proof_matches = False
            setup = self._setup_codec.verify(setup_cookie)
            if proof_matches and setup is not None:
                return setup
        raise MasterSetupAuthorizationError("Master setup authorization was rejected.")


def _require_current_schema(connection: Connection) -> None:
    try:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
    except SQLAlchemyError:
        raise MasterSetupUnavailableError("Master setup is unavailable.") from None
    if revision != CURRENT_SCHEMA_REVISION or foreign_keys != 1:
        raise MasterSetupUnavailableError("Master setup is unavailable.")


def _require_confirmation(token: str, confirmation: str) -> None:
    try:
        token_bytes = token.encode("utf-8")
        confirmation_bytes = confirmation.encode("utf-8")
        if not 32 <= len(token_bytes) <= 1_024 or not hmac.compare_digest(
            token_bytes, confirmation_bytes
        ):
            raise ValueError
    except (UnicodeError, ValueError):
        raise ValueError("Invalid master token input or confirmation.") from None


__all__ = [
    "MasterSetupAuthorizationError",
    "MasterSetupService",
    "MasterSetupUnavailableError",
]
