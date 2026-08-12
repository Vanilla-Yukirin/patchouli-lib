from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import Engine

from patchouli_lib.api.auth_contracts import (
    DEFAULT_CAPABILITY_CONFIGURATION,
    CapabilitiesResponse,
    CapabilityConfiguration,
    WhoAmIResponse,
    capabilities_response,
    whoami_response,
)
from patchouli_lib.api.authentication import (
    AuthenticatedRequestContext,
    BearerAuthentication,
)
from patchouli_lib.api.contracts import API_V1_PREFIX
from patchouli_lib.auth.service import Clock, utc_microseconds


def create_auth_router(
    engine: Engine,
    *,
    capability_configuration: CapabilityConfiguration = DEFAULT_CAPABILITY_CONFIGURATION,
    clock: Clock = utc_microseconds,
) -> APIRouter:
    """Create protected diagnostic routes bound to an application-owned Engine."""

    router = APIRouter(prefix=API_V1_PREFIX)
    authenticate = BearerAuthentication(engine, clock=clock)

    @router.get("/capabilities", response_model=CapabilitiesResponse)
    def capabilities(
        _context: Annotated[AuthenticatedRequestContext, Depends(authenticate)],
    ) -> CapabilitiesResponse:
        return capabilities_response(capability_configuration)

    @router.get("/auth/whoami", response_model=WhoAmIResponse)
    def whoami(
        context: Annotated[AuthenticatedRequestContext, Depends(authenticate)],
    ) -> WhoAmIResponse:
        return whoami_response(context)

    return router


__all__ = ["create_auth_router"]
