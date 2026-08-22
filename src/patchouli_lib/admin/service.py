from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

from sqlalchemy import Engine

from patchouli_lib.admin.contracts import (
    BootstrapInput,
    ProvisionAgentInput,
    RecoverOperatorInput,
    RevokeAgentCredentialInput,
)
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    MAX_RFC3339_TIMESTAMP_MICROSECONDS,
    CallerKind,
    LocalOperatorRecovery,
    OperatorBootstrap,
)
from patchouli_lib.auth.service import utc_microseconds
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.operator.service import (
    LocalOperatorRecoveryService,
    OperatorBootstrapService,
    OperatorService,
    ResourceNotFoundError,
)

Clock = Callable[[], int]
RequestIdFactory = Callable[[], str]
_MICROSECONDS_PER_SECOND = 1_000_000


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class DeliveredCredential:
    value: str = field(repr=False)
    library_id: str
    caller_id: str
    credential_id: str

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(value=<redacted>, library_id={self.library_id!r}, "
            f"caller_id={self.caller_id!r}, credential_id={self.credential_id!r})"
        )


class AdminActionService:
    """Adapt the existing operator services to short web-owned transactions."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Clock = utc_microseconds,
        request_id_factory: RequestIdFactory | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._request_id_factory = request_id_factory or _request_id

    def bootstrap(self, request: BootstrapInput) -> DeliveredCredential:
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            structure = LibrarySeedService(
                LibraryRepository(connection),
                clock=lambda: now,
            ).seed(
                LibraryStructureSeed(
                    library_name=request.library_name,
                    section_name=request.section_name,
                    section_description=request.section_description,
                    book_name=request.book_name,
                    book_summary=request.book_summary,
                )
            )
            result = OperatorBootstrapService(
                AuthRepository(connection),
                clock=lambda: now,
            ).bootstrap(
                OperatorBootstrap(
                    library_id=structure.library.id,
                    operator_name=request.operator_name,
                    operator_description=request.operator_description,
                    credential_expires_at=_expires_at(
                        now,
                        request.credential_ttl_seconds,
                    ),
                    request_id=self._request_id_factory(),
                )
            )
        return DeliveredCredential(
            value=result.credential.value,
            library_id=result.caller.library_id,
            caller_id=result.caller.id,
            credential_id=result.credential.credential.id,
        )

    def recover_operator(self, request: RecoverOperatorInput) -> DeliveredCredential:
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            library_id = _require_library(
                LibraryRepository(connection),
                request.library_name,
            )
            result = LocalOperatorRecoveryService(
                AuthRepository(connection),
                clock=lambda: now,
            ).recover(
                LocalOperatorRecovery(
                    library_id=library_id,
                    credential_expires_at=_expires_at(
                        now,
                        request.credential_ttl_seconds,
                    ),
                    request_id=self._request_id_factory(),
                )
            )
        return DeliveredCredential(
            value=result.credential.value,
            library_id=result.caller.library_id,
            caller_id=result.caller.id,
            credential_id=result.credential.credential.id,
        )

    def provision_agent(self, request: ProvisionAgentInput) -> DeliveredCredential:
        actor_token = request.operator_token.get_secret_value()
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            library_repository = LibraryRepository(connection)
            library_id = _require_library(library_repository, request.library_name)
            section = library_repository.find_section_by_name(
                library_id,
                request.section_name,
            )
            if section is None:
                raise ResourceNotFoundError
            service = OperatorService(
                AuthRepository(connection),
                clock=lambda: now,
            )
            caller = service.create_agent_caller(
                actor_token,
                library_id=library_id,
                name=request.agent_name,
                description=request.agent_description,
                request_id=self._request_id_factory(),
            )
            issued = service.create_credential(
                actor_token,
                library_id=library_id,
                caller_id=caller.id,
                expires_at=_expires_at(now, request.credential_ttl_seconds),
                request_id=self._request_id_factory(),
            )
            for action in request.grants:
                service.add_grant(
                    actor_token,
                    library_id=library_id,
                    caller_id=caller.id,
                    section_id=section.id,
                    action=action,
                    request_id=self._request_id_factory(),
                )
        return DeliveredCredential(
            value=issued.value,
            library_id=library_id,
            caller_id=caller.id,
            credential_id=issued.credential.id,
        )

    def revoke_agent_credential(self, request: RevokeAgentCredentialInput) -> None:
        actor_token = request.operator_token.get_secret_value()
        now = self._clock()
        with immediate_transaction(self._engine) as connection:
            library_id = _require_library(
                LibraryRepository(connection),
                request.library_name,
            )
            repository = AuthRepository(connection)
            caller = repository.get_caller(library_id, request.caller_id)
            if caller is None or caller.kind is not CallerKind.AGENT:
                raise ResourceNotFoundError
            OperatorService(
                repository,
                clock=lambda: now,
            ).revoke_credential(
                actor_token,
                library_id=library_id,
                caller_id=request.caller_id,
                credential_id=request.credential_id,
                request_id=self._request_id_factory(),
            )


def _expires_at(now: int, ttl_seconds: int) -> int:
    expires_at = now + ttl_seconds * _MICROSECONDS_PER_SECOND
    if expires_at > MAX_RFC3339_TIMESTAMP_MICROSECONDS:
        raise ValueError("Credential expiry exceeds the supported timestamp range.")
    return expires_at


def _require_library(repository: LibraryRepository, name: str) -> str:
    library = repository.find_library_by_name(name)
    if library is None:
        raise ResourceNotFoundError
    return library.id


def _request_id() -> str:
    return f"req_admin_{uuid4().hex}"


__all__ = ["AdminActionService", "DeliveredCredential"]
