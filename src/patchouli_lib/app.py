from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, cast

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import Engine

from patchouli_lib import __version__
from patchouli_lib.config import Settings
from patchouli_lib.database import DatabaseNotReadyError, build_engine, check_database


class ServiceResponse(BaseModel):
    name: str
    version: str
    status: str


class HealthResponse(BaseModel):
    status: str


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(resolved_settings.database_url)
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
