"""Management JSON endpoints. No Agent credential or legacy HTML dependency."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from patchouli_lib.admin.json_api.common import (
    SETUP_COOKIE,
    ApiError,
    BrowserAuth,
    body_json,
    reply,
    require_csrf,
    require_origin,
)
from patchouli_lib.admin.json_api.content import audit
from patchouli_lib.admin.json_api.schemas import SessionAuth, SessionState
from patchouli_lib.admin.master_setup import (
    MasterSetupAuthorizationError,
    MasterSetupService,
    MasterSetupUnavailableError,
)
from patchouli_lib.admin.master_token_store import (
    MasterTokenAlreadyInitialized,
    MasterTokenRepository,
)
from patchouli_lib.admin.page_move_service import MasterPageMoveConflictError
from patchouli_lib.admin.service import (
    AgentMetadataVersionConflictError,
    AgentNameConflictError,
    GrantVersionConflictError,
)
from patchouli_lib.admin.tag_management import TagBatchConflictError
from patchouli_lib.api.onboarding_routes import TicketAttemptLimiter
from patchouli_lib.auth.service import AuthenticationError, utc_microseconds
from patchouli_lib.config import Settings
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.service import ArchiveNotFoundError, ArchivePreconditionFailedError
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.service import (
    LibraryStructureNotFoundError,
    LibraryStructureVersionConflictError,
)
from patchouli_lib.library_skills.service import (
    SkillConflictError,
    SkillNotFoundError,
    SkillPreconditionError,
)
from patchouli_lib.operator.service import (
    CredentialLifecycleError,
    PolicyConflictError,
    ResourceNotFoundError,
)
from patchouli_lib.search.index_v2 import SearchIndexUnavailableError
from patchouli_lib.search.service_v2 import SearchScopeError
from patchouli_lib.tags.service import TagNotFoundError, TagValidationError


class JsonRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                return await original(request)
            except HTTPException as error:
                detail = (
                    error.detail
                    if isinstance(error.detail, dict)
                    else {"code": "request_failed", "message": "Request failed"}
                )
                return reply({"error": detail}, error.status_code)
            except AuthenticationError:
                return reply(
                    {"error": {"code": "sign_in_required", "message": "Sign in again"}}, 401
                )
            except (
                LibraryStructureNotFoundError,
                ArchiveNotFoundError,
                ResourceNotFoundError,
                TagNotFoundError,
                SkillNotFoundError,
            ):
                return reply({"error": {"code": "not_found", "message": "Item not found"}}, 404)
            except (
                LibraryStructureVersionConflictError,
                IntegrityError,
                AgentMetadataVersionConflictError,
                AgentNameConflictError,
                GrantVersionConflictError,
                TagBatchConflictError,
                MasterPageMoveConflictError,
                CredentialLifecycleError,
                PolicyConflictError,
                SkillConflictError,
                SkillPreconditionError,
            ):
                return reply(
                    {
                        "error": {
                            "code": "conflict",
                            "message": "Item changed or name already exists",
                        }
                    },
                    409,
                )
            except (ArchivePreconditionFailedError, FileSetPreconditionFailedError):
                return reply(
                    {
                        "error": {
                            "code": "version_changed",
                            "message": "Item changed; reload before applying this action",
                        }
                    },
                    412,
                )
            except SearchIndexUnavailableError:
                return reply(
                    {
                        "error": {
                            "code": "search_unavailable",
                            "message": "Search index is not ready",
                        }
                    },
                    503,
                )
            except (
                ValidationError,
                RequestValidationError,
                ValueError,
                TypeError,
                UnicodeError,
                TagValidationError,
                SearchScopeError,
            ):
                return reply({"error": {"code": "invalid_input", "message": "Invalid input"}}, 422)

        return guarded


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr


class Setup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    setup_token: SecretStr
    master_token: SecretStr
    confirmation: SecretStr


class RotateMaster(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_token: SecretStr
    new_token: SecretStr
    confirmation: SecretStr


def create_admin_json_router(engine: Engine, settings: Settings) -> APIRouter:
    from patchouli_lib.admin.json_api.actions import register_actions
    from patchouli_lib.admin.json_api.reads import register_reads
    from patchouli_lib.admin.json_api.skills import register_skills

    router = APIRouter(prefix="/admin/api", tags=["administration"], route_class=JsonRoute)
    auth = BrowserAuth(engine, settings)
    # Fixed-size per-worker admission, shared by sensitive authentication actions.
    attempts = TicketAttemptLimiter(limit=30)

    def admit_sensitive(request: Request) -> None:
        auth.require_transport(request)
        require_origin(request)
        if not attempts.admit():
            raise ApiError(429, "authentication_rate_limited")

    @router.get("/session", response_model=SessionState)
    def session_state(request: Request) -> Response:
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            repository = MasterTokenRepository(connection)
            initialized = repository.has_identity()
            authenticated = None
            try:
                candidate = auth.session(request)
                auth.authorize(connection, candidate)
                authenticated = candidate
            except (ApiError, AuthenticationError):
                pass
        available = not initialized and settings.admin_setup_token is not None
        payload = {
            "authenticated": authenticated is not None,
            "setup_required": not initialized,
            "setup_available": available,
            "csrf_token": authenticated.csrf_token if authenticated else None,
            "style_nonce": getattr(request.state, "style_nonce", ""),
            "setup_csrf_token": None,
        }
        setup_cookie = None
        if available:
            setup = auth.setup_codec.verify(request.cookies.get(SETUP_COOKIE, ""))
            if setup is None:
                setup_cookie, setup = auth.setup_codec.issue()
            payload["setup_csrf_token"] = setup.csrf_token
        response = reply(payload)
        if setup_cookie is not None:
            auth.set_cookie(response, request, setup_cookie, setup=True)
        return response

    @router.post(
        "/session",
        response_model=SessionAuth,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": Login.model_json_schema()}},
            }
        },
    )
    async def sign_in(request: Request) -> Response:
        admit_sensitive(request)
        if request.headers.getlist("authorization"):
            raise ApiError(401, "master_session_required")
        form = await body_json(request, Login)

        def authenticate() -> Any:
            with engine.connect() as connection:
                return MasterTokenRepository(connection).authenticate(form.token.get_secret_value())

        state = await run_in_threadpool(authenticate)
        if state is None:
            raise ApiError(401, "invalid_master_token")
        value, session = auth.codec.issue_master(state.identity_id, state.session_generation)
        response = reply({"authenticated": True, "csrf_token": session.csrf_token})
        auth.set_cookie(response, request, value)
        return response

    @router.delete("/session", response_model=SessionAuth)
    def sign_out(request: Request) -> Response:
        session = auth.write_session(request)
        with engine.connect() as connection:
            auth.authorize(connection, session)
        response = reply({"authenticated": False})
        auth.clear_cookie(response, request)
        return response

    @router.post(
        "/setup",
        response_model=SessionAuth,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": Setup.model_json_schema()}},
            }
        },
    )
    async def initialize(request: Request) -> Response:
        admit_sensitive(request)
        setup_cookie = request.cookies.get(SETUP_COOKIE, "")
        session = auth.setup_codec.verify(setup_cookie)
        if session is None:
            raise ApiError(403, "setup_session_required")
        require_csrf(request, session.csrf_token)
        form = await body_json(request, Setup)
        try:
            state = await run_in_threadpool(
                MasterSetupService(engine, settings).initialize,
                form.master_token.get_secret_value(),
                form.confirmation.get_secret_value(),
                setup_proof=form.setup_token.get_secret_value(),
                setup_cookie=setup_cookie,
            )
        except MasterSetupAuthorizationError:
            raise ApiError(403, "setup_rejected") from None
        except (MasterSetupUnavailableError, MasterTokenAlreadyInitialized):
            raise ApiError(409, "setup_unavailable") from None
        value, master = auth.codec.issue_master(state.identity_id, state.session_generation)
        response = reply({"authenticated": True, "csrf_token": master.csrf_token})
        auth.set_cookie(response, request, value)
        auth.clear_cookie(response, request, setup=True)
        return response

    @router.post(
        "/master-token/rotate",
        response_model=SessionAuth,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": RotateMaster.model_json_schema()}},
            }
        },
    )
    async def rotate_master(request: Request) -> Response:
        admit_sensitive(request)
        session = auth.write_session(request)
        form = await body_json(request, RotateMaster)
        if not hmac.compare_digest(
            form.new_token.get_secret_value().encode("utf-8"),
            form.confirmation.get_secret_value().encode("utf-8"),
        ):
            raise ApiError(422, "confirmation_mismatch")

        def execute() -> None:
            with immediate_transaction(engine) as connection:
                auth.authorize(connection, session)
                state = MasterTokenRepository(connection).rotate(
                    form.current_token.get_secret_value(),
                    form.new_token.get_secret_value(),
                    now=utc_microseconds(),
                )
                if state is None:
                    raise ApiError(401, "invalid_master_token")
                audit(
                    connection, session, "auth.master.rotate", "master_identity", state.identity_id
                )

        await run_in_threadpool(execute)
        response = reply({"authenticated": False, "requires_sign_in": True})
        auth.clear_cookie(response, request)
        return response

    register_reads(router, auth)
    register_actions(router, auth)
    register_skills(router, auth)
    return router
