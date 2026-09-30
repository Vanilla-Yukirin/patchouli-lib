import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, cast

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import Engine

from patchouli_lib import __version__
from patchouli_lib.admin.router import create_admin_router
from patchouli_lib.api.agent_skill_routes import create_agent_skill_router
from patchouli_lib.api.archive_routes import create_archive_router
from patchouli_lib.api.auth_contracts import FILE_SET_FEATURE, CapabilityConfiguration
from patchouli_lib.api.auth_routes import create_auth_router
from patchouli_lib.api.errors import install_api_exception_handlers
from patchouli_lib.api.file_set_read_routes import create_file_set_read_router
from patchouli_lib.api.file_set_write_routes import create_file_set_write_router
from patchouli_lib.api.request_ids import RequestIDMiddleware
from patchouli_lib.api.retrieval_routes import create_retrieval_router
from patchouli_lib.api.search_routes_v2 import create_search_v2_router
from patchouli_lib.api.tag_routes import create_tag_router
from patchouli_lib.config import Settings
from patchouli_lib.database import DatabaseNotReadyError, build_engine, check_database
from patchouli_lib.request_log.middleware import RequestLogMiddleware, run_request_log_retention
from patchouli_lib.retrieval.cursor import CursorCodec
from patchouli_lib.search.index_v2 import SearchIndexUnavailableError, require_ready_index


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
    cursor_codec = (
        CursorCodec(cursor_secret.get_secret_value().encode("utf-8"))
        if cursor_secret is not None
        else None
    )
    capabilities = CapabilityConfiguration(
        features=("archive", FILE_SET_FEATURE, "retrieval", "tags")
        if cursor_secret is not None
        else ("archive", FILE_SET_FEATURE, "tags"),
        content_mutation_idempotency=True,
        successful_replay_retention="indefinite-alpha",
    )

    def search_ready() -> bool:
        try:
            with engine.connect() as connection:
                require_ready_index(connection)
        except SearchIndexUnavailableError:
            return False
        return True

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.engine = engine
        retention = asyncio.create_task(run_request_log_retention(engine))
        try:
            yield
        finally:
            retention.cancel()
            with suppress(asyncio.CancelledError):
                await retention
            engine.dispose()

    application = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        lifespan=lifespan,
    )
    application.state.engine = engine
    install_api_exception_handlers(application)
    application.add_middleware(RequestIDMiddleware)
    application.add_middleware(RequestLogMiddleware, engine=engine)
    application.include_router(
        create_auth_router(
            engine,
            capability_configuration=capabilities,
            search_ready=search_ready,
        )
    )
    application.include_router(create_archive_router(engine, cursor_codec=cursor_codec))
    application.include_router(create_file_set_read_router(engine))
    application.include_router(create_file_set_write_router(engine))
    application.include_router(create_tag_router(engine))
    application.include_router(create_search_v2_router(engine))
    application.include_router(create_agent_skill_router(engine))
    if cursor_codec is not None:
        application.include_router(
            create_retrieval_router(
                engine,
                cursor_codec=cursor_codec,
            )
        )
    if resolved_settings.admin_enabled:
        application.include_router(
            create_admin_router(
                engine,
                resolved_settings,
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
