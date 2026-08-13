from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, cast

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import Engine

from patchouli_lib import __version__
from patchouli_lib.api.archive_routes import create_archive_router
from patchouli_lib.api.auth_contracts import CapabilityConfiguration
from patchouli_lib.api.auth_routes import create_auth_router
from patchouli_lib.api.errors import install_api_exception_handlers
from patchouli_lib.api.request_ids import RequestIDMiddleware
from patchouli_lib.api.retrieval_routes import create_retrieval_router
from patchouli_lib.api.search_routes import create_search_router
from patchouli_lib.config import Settings
from patchouli_lib.database import DatabaseNotReadyError, build_engine, check_database
from patchouli_lib.retrieval.cursor import CursorCodec


class ServiceResponse(BaseModel):
    name: str
    version: str
    status: str


class HealthResponse(BaseModel):
    status: str


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    engine = build_engine(resolved_settings.database_url)
    cursor_secret = resolved_settings.retrieval_cursor_signing_secret
    capabilities = CapabilityConfiguration(
        features=("archive", "retrieval") if cursor_secret is not None else ("archive",),
        content_mutation_idempotency=True,
        successful_replay_retention="indefinite-alpha",
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.engine = engine
        try:
            yield
        finally:
            engine.dispose()

    application = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        lifespan=lifespan,
    )
    application.state.engine = engine
    install_api_exception_handlers(application)
    application.add_middleware(RequestIDMiddleware)
    application.include_router(
        create_auth_router(
            engine,
            capability_configuration=capabilities,
        )
    )
    application.include_router(create_archive_router(engine))
    application.include_router(create_search_router(engine))
    if cursor_secret is not None:
        application.include_router(
            create_retrieval_router(
                engine,
                cursor_codec=CursorCodec(cursor_secret.get_secret_value().encode("utf-8")),
            )
        )

    def get_engine(request: Request) -> Engine:
        return cast(Engine, request.app.state.engine)

    ReadinessEngine = Annotated[Engine, Depends(get_engine)]

    @application.get("/", response_model=ServiceResponse)
    def service_info() -> ServiceResponse:
        return ServiceResponse(
            name=resolved_settings.app_name,
            version=__version__,
            status="design-stage bootstrap",
        )

    @application.get("/health/live", response_model=HealthResponse)
    def liveness() -> HealthResponse:
        return HealthResponse(status="live")

    @application.get("/health/ready", response_model=HealthResponse)
    def readiness(engine: ReadinessEngine) -> HealthResponse:
        try:
            check_database(engine)
        except DatabaseNotReadyError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="database unavailable",
            ) from exc
        return HealthResponse(status="ready")

    return application


app = create_app()
