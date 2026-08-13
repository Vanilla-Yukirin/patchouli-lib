"""Protected HTTP route for the explicitly unavailable search capability."""

from __future__ import annotations

import json
from functools import partial
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Request
from pydantic import Field, TypeAdapter, ValidationError, field_validator
from sqlalchemy import Engine

from patchouli_lib.api.auth_contracts import MAX_QUERY_BYTES
from patchouli_lib.api.authentication import (
    AuthenticatedRequestContext,
    BearerAuthentication,
)
from patchouli_lib.api.contracts import (
    API_V1_PREFIX,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_LIMIT,
    OpaqueCursor,
    WireModel,
)
from patchouli_lib.api.errors import (
    ApplicationProblem,
    insufficient_scope,
    invalid_token,
    resource_not_found,
)
from patchouli_lib.auth.service import Clock, utc_microseconds
from patchouli_lib.library.schemas import OpaqueId
from patchouli_lib.retrieval.repository import RetrievalRepository
from patchouli_lib.retrieval.schemas import ReadWindow
from patchouli_lib.retrieval.service import (
    RetrievalAuthenticationError,
    RetrievalAuthorizationError,
    RetrievalNotFoundError,
    RetrievalPersistenceError,
    RetrievalService,
)

_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)
_MAX_SEARCH_REQUEST_BYTES = (MAX_QUERY_BYTES * 6) + (MAX_CURSOR_LENGTH * 12) + 256


class _SearchValidationFailure(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _SearchValidationFailure
        result[key] = value
    return result


def _reject_non_finite_number(_value: str) -> None:
    raise _SearchValidationFailure


class SearchRequest(WireModel):
    """Accepted search request shape while provider selection remains open."""

    query: Annotated[str, Field(min_length=1, repr=False)]
    limit: Annotated[int, Field(strict=True, ge=1, le=MAX_PAGE_LIMIT)] = 20
    cursor: OpaqueCursor | None = None

    @field_validator("query")
    @classmethod
    def require_bounded_utf8_query(cls, value: str) -> str:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise ValueError("Search query must be valid UTF-8.") from None
        if len(encoded) > MAX_QUERY_BYTES:
            raise ValueError("Search query exceeds the advertised byte limit.")
        return value


def _validation_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=422,
        code="request_validation_failed",
        title="Request validation failed",
        detail="The request did not satisfy the required schema.",
    )


def _search_unavailable_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=503,
        code="search_unavailable",
        title="Service unavailable",
        detail="Search is temporarily unavailable.",
    )


def _validate_section_id(section_id: str) -> str:
    try:
        return _OPAQUE_ID_ADAPTER.validate_python(section_id, strict=True)
    except ValidationError:
        raise _validation_problem() from None


async def _authenticate(
    authenticate: BearerAuthentication,
    request: Request,
) -> AuthenticatedRequestContext:
    return await anyio.to_thread.run_sync(
        partial(authenticate, request),
        abandon_on_cancel=False,
    )


async def _search_request(request: Request) -> SearchRequest:
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > _MAX_SEARCH_REQUEST_BYTES:
                raise _validation_problem()
            body.extend(chunk)
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite_number,
        )
        return SearchRequest.model_validate(value, strict=True)
    except (RecursionError, ValueError):
        raise _validation_problem() from None


def _perform_authorization(
    engine: Engine,
    context: AuthenticatedRequestContext,
    section_id: str,
    *,
    clock: Clock,
) -> None:
    try:
        with engine.connect() as connection, connection.begin():
            service = RetrievalService(
                RetrievalRepository(connection),
                context.authenticated,
                clock=clock,
            )
            service.list_books(section_id, ReadWindow(limit=1))
    except RetrievalAuthenticationError:
        raise invalid_token() from None
    except RetrievalAuthorizationError:
        raise insufficient_scope() from None
    except RetrievalNotFoundError:
        raise resource_not_found() from None
    except RetrievalPersistenceError:
        raise RuntimeError("Retrieval persistence validation failed.") from None


async def _authorize(
    engine: Engine,
    context: AuthenticatedRequestContext,
    section_id: str,
    *,
    clock: Clock,
) -> None:
    await anyio.to_thread.run_sync(
        partial(
            _perform_authorization,
            engine,
            context,
            section_id,
            clock=clock,
        ),
        abandon_on_cancel=False,
    )


def create_search_router(
    engine: Engine,
    *,
    clock: Clock = utc_microseconds,
) -> APIRouter:
    """Create the authorized search-unavailable route without selecting FTS."""

    router = APIRouter(prefix=API_V1_PREFIX)
    authenticate = BearerAuthentication(engine, clock=clock)

    @router.post("/sections/{section_id}/search")
    async def search(section_id: str, request: Request) -> None:
        context = await _authenticate(authenticate, request)
        validated_section_id = _validate_section_id(section_id)
        await _search_request(request)
        await _authorize(
            engine,
            context,
            validated_section_id,
            clock=clock,
        )
        raise _search_unavailable_problem()

    return router


__all__ = ["SearchRequest", "create_search_router"]
