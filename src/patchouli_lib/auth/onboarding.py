"""One-use credential issuance, admitted and consumed in the same transaction."""

from __future__ import annotations

import hashlib
import re
import secrets
import string
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, Engine, insert, select, update

from patchouli_lib.admin.contracts import MasterProvisionAgentInput
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.service import AdminActionService, DeliveredCredential
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.auth.models import OnboardingTicket
from patchouli_lib.auth.service import AuthenticationError, Clock, new_opaque_id, utc_microseconds
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.models import Library

TICKET_LIFETIME_MICROSECONDS = 300_000_000
_ALPHABET = string.ascii_letters + string.digits
_CODE_PATTERN = re.compile(r"[A-Za-z0-9]{16}", re.ASCII)


class InvalidOnboardingTicket(ValueError):
    """Uniform invalid, expired, consumed or stale-generation admission failure."""


@dataclass(frozen=True, slots=True)
class DeliveredTicket:
    code: str = field(repr=False)
    expires_at: int


class OnboardingTicketService:
    def __init__(self, engine: Engine, *, clock: Clock = utc_microseconds) -> None:
        self._engine = engine
        self._clock = clock

    def create(self, *, master_session: MasterAdminSession) -> DeliveredTicket:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(16))
        ticket_id = new_opaque_id()
        with immediate_transaction(self._engine) as connection:
            # Lock acquisition may wait past the session boundary. Admission
            # and the ticket lifetime must use time observed after that wait.
            now = self._clock()
            expires_at = now + TICKET_LIFETIME_MICROSECONDS
            if master_session.expires_at * 1_000_000 <= now or not MasterTokenRepository(
                connection
            ).is_session_generation_current(
                master_session.identity_id, master_session.session_generation
            ):
                raise AuthenticationError
            connection.execute(
                insert(OnboardingTicket),
                {
                    "id": ticket_id,
                    "code_hash": hashlib.sha256(code.encode("ascii")).digest(),
                    "identity_id": master_session.identity_id,
                    "session_generation": master_session.session_generation,
                    "session_fingerprint": master_session.audit_fingerprint(),
                    "created_at": now,
                    "expires_at": expires_at,
                },
            )
            MasterAuditRepository(connection).add_success(
                identity_id=master_session.identity_id,
                session_generation=master_session.session_generation,
                session_fingerprint=master_session.audit_fingerprint(),
                action="auth.onboarding.create",
                target_type="onboarding_ticket",
                target_id=ticket_id,
                occurred_at=now,
                event_id=new_opaque_id(),
            )
        return DeliveredTicket(code, expires_at)

    def _admit(self, connection: Connection, code: str, now: int) -> dict[str, Any]:
        if _CODE_PATTERN.fullmatch(code) is None:
            raise InvalidOnboardingTicket
        row = (
            connection.execute(
                select(OnboardingTicket.__table__).where(
                    OnboardingTicket.code_hash == hashlib.sha256(code.encode("ascii")).digest(),
                    OnboardingTicket.created_at <= now,
                    OnboardingTicket.expires_at > now,
                    OnboardingTicket.consumed_at.is_(None),
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or not MasterTokenRepository(connection).is_session_generation_current(
            row["identity_id"], row["session_generation"]
        ):
            raise InvalidOnboardingTicket
        return dict(row)

    def discover(self, code: str) -> tuple[dict[str, str], ...]:
        # A short snapshot makes generation admission and discovery consistent.
        with immediate_transaction(self._engine) as connection:
            self._admit(connection, code, self._clock())
            return tuple(
                {"id": row.id, "name": row.name}
                for row in connection.execute(
                    select(Library.id, Library.name).order_by(Library.created_at, Library.id)
                )
            )

    def redeem(self, code: str, request: MasterProvisionAgentInput) -> DeliveredCredential:
        with immediate_transaction(self._engine) as connection:
            now = self._clock()
            ticket = self._admit(connection, code, now)
            result = AdminActionService(self._engine).provision_agent_in_transaction(
                connection,
                request,
                now=now,
                identity_id=ticket["identity_id"],
                session_generation=ticket["session_generation"],
                session_fingerprint=ticket["session_fingerprint"],
            )
            connection.execute(
                update(OnboardingTicket)
                .where(
                    OnboardingTicket.id == ticket["id"],
                    OnboardingTicket.consumed_at.is_(None),
                )
                .values(consumed_at=now, credential_id=result.credential_id)
            )
        return result
