"""Empty-body lifecycle writes for explicitly Library-authorized Agents."""

from __future__ import annotations

from functools import partial
from typing import Literal

import anyio
from fastapi import APIRouter, Request
from pydantic import ValidationError
from sqlalchemy import Engine
from starlette.responses import Response

from patchouli_lib.api.authentication import extract_bearer_token
from patchouli_lib.api.contracts import API_V1_PREFIX, PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import (
    ApplicationProblem,
    insufficient_scope,
    invalid_token,
    resource_not_found,
)
from patchouli_lib.api.file_set_write_routes import _idempotency, _revision_precondition, _scope_id
from patchouli_lib.api.page_move_routes import _preflight
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, get_request_id
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthorizationError,
    Clock,
    utc_microseconds,
)
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.page_lifecycle_schemas import PageLifecycleCommand
from patchouli_lib.content.page_lifecycle_service import (
    CallerPageLifecycleService,
    PageLifecycleStateConflictError,
)
from patchouli_lib.content.page_move_core import PageMoveNotFoundError
from patchouli_lib.content.schemas import ArchiveIdempotencyKey
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import OriginalResponse, ReplayResponse
from patchouli_lib.idempotency.service import IdempotencyConflictError


def _problem(status: int, code: str) -> ApplicationProblem:
    return ApplicationProblem(
        status_code=status,
        code=code,
        title="Page lifecycle failed",
        detail="The request could not be completed.",
    )


def _transition(
    engine: Engine,
    token: str,
    command: PageLifecycleCommand,
    key: ArchiveIdempotencyKey,
    clock: Clock,
) -> OriginalResponse | ReplayResponse:
    with immediate_transaction(engine) as connection:
        return CallerPageLifecycleService(connection, clock=clock).transition(token, command, key)


def create_page_lifecycle_router(engine: Engine, *, clock: Clock = utc_microseconds) -> APIRouter:
    router = APIRouter(prefix=API_V1_PREFIX)

    async def execute(
        library_id: str, page_id: str, request: Request, action: Literal["delete", "restore"]
    ) -> Response:
        token = extract_bearer_token(request)
        library_id = _scope_id(library_id)
        key = _idempotency(request)
        etag = _revision_precondition(request)
        if request.query_params:
            raise _problem(422, "request_validation_failed")
        try:
            await anyio.to_thread.run_sync(
                partial(_preflight, engine, token, library_id, clock), abandon_on_cancel=False
            )
            # Reject the first nonempty chunk, without buffering an arbitrary body.
            async for chunk in request.stream():
                if chunk:
                    raise _problem(422, "request_validation_failed")
            try:
                command = PageLifecycleCommand(
                    library_id=library_id,
                    page_id=page_id,
                    action=action,
                    expected_etag=etag,
                    request_id=get_request_id(request),
                )
            except (ValidationError, ValueError):
                raise _problem(422, "request_validation_failed") from None
            result = await anyio.to_thread.run_sync(
                partial(_transition, engine, token, command, key, clock), abandon_on_cancel=False
            )
        except AuthenticationError:
            raise invalid_token() from None
        except AuthorizationError:
            raise insufficient_scope() from None
        except PageMoveNotFoundError:
            raise resource_not_found() from None
        except FileSetPreconditionFailedError:
            raise _problem(412, "precondition_failed") from None
        except PageLifecycleStateConflictError:
            raise _problem(409, "lifecycle_state_conflict") from None
        except IdempotencyConflictError:
            raise _problem(409, "idempotency_conflict") from None
        headers = {
            "ETag": result.response_etag,
            "Cache-Control": PROTECTED_CACHE_CONTROL,
            REQUEST_ID_HEADER: get_request_id(request),
        }
        if isinstance(result, ReplayResponse):
            headers["Idempotency-Replayed"] = "true"
        return Response(
            result.response_body,
            status_code=result.response_status,
            media_type=result.response_media_type,
            headers=headers,
        )

    @router.delete("/libraries/{library_id}/pages/{page_id}", status_code=200)
    async def delete_page(library_id: str, page_id: str, request: Request) -> Response:
        return await execute(library_id, page_id, request, "delete")

    @router.post("/libraries/{library_id}/pages/{page_id}/restore", status_code=200)
    async def restore_page(library_id: str, page_id: str, request: Request) -> Response:
        return await execute(library_id, page_id, request, "restore")

    return router
