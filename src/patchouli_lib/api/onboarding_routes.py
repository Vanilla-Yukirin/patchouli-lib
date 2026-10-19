"""Bounded one-use ticket exchange; never expose inputs in diagnostics."""

from __future__ import annotations

from collections import deque
from threading import Lock
from time import monotonic
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from patchouli_lib.admin.contracts import (
    CredentialTtlSeconds,
    MasterProvisionAgentInput,
)
from patchouli_lib.api.errors import ApplicationProblem
from patchouli_lib.auth.onboarding import InvalidOnboardingTicket, OnboardingTicketService
from patchouli_lib.auth.service import Clock, utc_microseconds
from patchouli_lib.library.schemas import BoundedText, OpaqueId, ResourceName
from patchouli_lib.operator.service import ResourceNotFoundError

MAX_TICKET_REQUEST_BYTES = 131_072
_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class TicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    code: SecretStr = Field(min_length=1, max_length=256, repr=False)


class TicketExchangeInput(TicketInput):
    name: ResourceName
    description: BoundedText = ""
    credential_ttl_seconds: CredentialTtlSeconds | None = None
    never_expires: bool = False
    read_mode: Literal["include", "exclude"] = "include"
    write_mode: Literal["include", "exclude"] = "include"
    read_libraries: tuple[OpaqueId, ...] = ()
    write_libraries: tuple[OpaqueId, ...] = ()

    def provision_input(self) -> MasterProvisionAgentInput:
        return MasterProvisionAgentInput(
            agent_name=self.name,
            agent_description=self.description,
            credential_ttl_seconds=self.credential_ttl_seconds,
            never_expires=self.never_expires,
            read_mode=self.read_mode,
            write_mode=self.write_mode,
            read_libraries=self.read_libraries,
            write_libraries=self.write_libraries,
        )


class TicketAttemptLimiter:
    """One fixed-size global bucket per application worker, not per supplied key."""

    def __init__(self, limit: int = 60) -> None:
        self._limit = limit
        self._attempts: deque[float] = deque(maxlen=limit)
        self._lock = Lock()

    def admit(self) -> bool:
        with self._lock:
            now = monotonic()
            while self._attempts and self._attempts[0] <= now - 60:
                self._attempts.popleft()
            if len(self._attempts) >= self._limit:
                return False
            self._attempts.append(now)
            return True


def _problem(status: int, code: str, detail: str) -> ApplicationProblem:
    return ApplicationProblem(
        status_code=status, code=code, title="Onboarding request failed", detail=detail
    )


async def _read_input[Model: BaseModel](request: Request, model: type[Model]) -> Model:
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise _problem(415, "unsupported_media_type", "JSON input is required.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_TICKET_REQUEST_BYTES:
            raise _problem(413, "content_too_large", "The request content is too large.")
        body.extend(chunk)
    try:
        return model.model_validate_json(bytes(body))
    except (ValidationError, ValueError):
        raise _problem(422, "request_validation_failed", "The request input is invalid.") from None


def create_onboarding_router(engine: Engine, *, clock: Clock = utc_microseconds) -> APIRouter:
    router = APIRouter(prefix="/api/v1/onboarding")
    service = OnboardingTicketService(engine, clock=clock)
    limiter = TicketAttemptLimiter()

    def admit_attempt() -> None:
        if not limiter.admit():
            raise _problem(429, "rate_limited", "Retry after one minute.")

    @router.post("/libraries")
    async def libraries(request: Request) -> JSONResponse:
        admit_attempt()
        payload = await _read_input(request, TicketInput)
        try:
            values = await run_in_threadpool(service.discover, payload.code.get_secret_value())
        except InvalidOnboardingTicket:
            raise _problem(401, "invalid_onboarding_ticket", "The ticket is invalid.") from None
        return JSONResponse({"libraries": values}, headers=_HEADERS)

    @router.post("/redeem")
    async def redeem(request: Request) -> JSONResponse:
        admit_attempt()
        payload = await _read_input(request, TicketExchangeInput)
        try:
            provision = payload.provision_input()
        except (ValidationError, ValueError):
            raise _problem(
                422, "request_validation_failed", "The request input is invalid."
            ) from None
        try:
            result = await run_in_threadpool(
                service.redeem, payload.code.get_secret_value(), provision
            )
        except InvalidOnboardingTicket:
            raise _problem(401, "invalid_onboarding_ticket", "The ticket is invalid.") from None
        except ResourceNotFoundError:
            raise _problem(422, "invalid_library_scope", "Select existing libraries.") from None
        except IntegrityError:
            raise _problem(
                409, "credential_conflict", "The credential could not be created."
            ) from None
        except ValueError:
            raise _problem(
                422, "request_validation_failed", "The request input is invalid."
            ) from None
        return JSONResponse(
            {
                "token": result.value,
                "caller_id": result.caller_id,
                "credential_id": result.credential_id,
                "library_id": result.library_id,
            },
            status_code=201,
            headers=_HEADERS,
        )

    return router
