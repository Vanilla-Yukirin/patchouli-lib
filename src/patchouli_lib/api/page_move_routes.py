"""Bounded stable-Page movement for explicitly Library-authorized Agents."""

from __future__ import annotations

import json
from functools import partial

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
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, get_request_id
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthorizationError,
    Clock,
    utc_microseconds,
)
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.page_move_core import PageMoveNotFoundError
from patchouli_lib.content.page_move_schemas import PageMoveCommand, PageMoveInput
from patchouli_lib.content.page_move_service import CallerPageMoveService, authorize_page_move
from patchouli_lib.content.schemas import ArchiveIdempotencyKey
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import OriginalResponse, ReplayResponse
from patchouli_lib.idempotency.service import IdempotencyConflictError

MAX_MOVE_BODY_BYTES = 1_024


def _problem(status: int, code: str) -> ApplicationProblem:
    return ApplicationProblem(
        status_code=status,
        code=code,
        title="Page movement failed",
        detail="The request could not be completed.",
    )


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


async def _body(request: Request) -> PageMoveInput:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise _problem(415, "unsupported_media_type")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_MOVE_BODY_BYTES:
            raise _problem(413, "content_too_large")
        data.extend(chunk)
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        return PageMoveInput.model_validate(value, strict=True)
    except (ValueError, TypeError, RecursionError):
        raise _problem(422, "request_validation_failed") from None


def _preflight(engine: Engine, token: str, library_id: str, clock: Clock) -> None:
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        try:
            authorize_page_move(connection, token, library_id, now=clock(), touch_last_used=False)
        finally:
            connection.rollback()


def _move(
    engine: Engine,
    token: str,
    command: PageMoveCommand,
    key: ArchiveIdempotencyKey,
    clock: Clock,
) -> OriginalResponse | ReplayResponse:
    with immediate_transaction(engine) as connection:
        return CallerPageMoveService(connection, clock=clock).move_page(token, command, key)


def create_page_move_router(engine: Engine, *, clock: Clock = utc_microseconds) -> APIRouter:
    router = APIRouter(prefix=API_V1_PREFIX)

    @router.post("/libraries/{library_id}/pages/{page_id}/move", status_code=200)
    async def move_page(library_id: str, page_id: str, request: Request) -> Response:
        token = extract_bearer_token(request)
        library_id = _scope_id(library_id)
        key = _idempotency(request)
        etag = _revision_precondition(request)
        if request.query_params:
            raise _problem(422, "request_validation_failed")
        try:
            # Deny before receiving any body; after the bounded parse the write
            # lock repeats authentication/authorization, closing the revoke gap.
            await anyio.to_thread.run_sync(
                partial(_preflight, engine, token, library_id, clock), abandon_on_cancel=False
            )
            body = await _body(request)
            try:
                command = PageMoveCommand(
                    **body.model_dump(),
                    library_id=library_id,
                    page_id=page_id,
                    expected_etag=etag,
                    request_id=get_request_id(request),
                )
            except (ValidationError, ValueError):
                raise _problem(422, "request_validation_failed") from None
            result = await anyio.to_thread.run_sync(
                partial(_move, engine, token, command, key, clock), abandon_on_cancel=False
            )
        except AuthenticationError:
            raise invalid_token() from None
        except AuthorizationError:
            raise insufficient_scope() from None
        except PageMoveNotFoundError:
            raise resource_not_found() from None
        except FileSetPreconditionFailedError:
            raise _problem(412, "precondition_failed") from None
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

    return router
