"""Protected cross-Library current-Page literal search endpoint."""

from __future__ import annotations

from dataclasses import asdict
from functools import partial
from typing import Annotated

import anyio
from fastapi import APIRouter, Request
from pydantic import Field
from sqlalchemy import Engine
from starlette.responses import JSONResponse

from patchouli_lib.api.authentication import BearerAuthentication
from patchouli_lib.api.contracts import API_V1_PREFIX, PROTECTED_CACHE_CONTROL, WireModel
from patchouli_lib.api.errors import (
    ApplicationProblem,
    invalid_token,
    resource_not_found,
)
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, get_request_id
from patchouli_lib.auth.service import AuthenticationError, Clock, utc_microseconds
from patchouli_lib.search.index_v2 import SearchIndexUnavailableError
from patchouli_lib.search.query_v2 import (
    MAX_QUERY_BODY_BYTES,
    InvalidSearchQueryV2,
    parse_query_v2_json,
)
from patchouli_lib.search.service_v2 import SearchScopeError, search_pages_v2


class SearchMatchSourceView(WireModel):
    kind: str
    file_name: str | None


class SearchPageView(WireModel):
    library_id: str
    section_id: str
    book_id: str
    page_id: str
    revision_id: str
    revision_number: int
    title: str
    occurred_at: int
    match_sources: list[SearchMatchSourceView]


class SearchResponse(WireModel):
    items: Annotated[list[SearchPageView], Field(max_length=100)]


def _invalid_query() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=422,
        code="request_validation_failed",
        title="Request validation failed",
        detail="The search request did not satisfy the required schema.",
    )


def _unavailable() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=503,
        code="search_unavailable",
        title="Service unavailable",
        detail="Search is temporarily unavailable while its index is rebuilt.",
    )


async def _read_query(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_QUERY_BODY_BYTES:
            raise _invalid_query()
        body.extend(chunk)
    return bytes(body)


def create_search_v2_router(engine: Engine, *, clock: Clock = utc_microseconds) -> APIRouter:
    """Create the protected current-Page search endpoint."""

    router = APIRouter(prefix=API_V1_PREFIX)
    authenticate = BearerAuthentication(engine, clock=clock)

    @router.post("/search", response_model=SearchResponse)
    async def search(request: Request) -> JSONResponse:
        context = await anyio.to_thread.run_sync(
            partial(authenticate, request), abandon_on_cancel=False
        )
        try:
            query = parse_query_v2_json(await _read_query(request))
        except InvalidSearchQueryV2:
            raise _invalid_query() from None
        try:
            found = await anyio.to_thread.run_sync(
                partial(search_pages_v2, engine, context, query, clock=clock),
                abandon_on_cancel=False,
            )
        except AuthenticationError:
            raise invalid_token() from None
        except SearchScopeError:
            raise resource_not_found() from None
        except SearchIndexUnavailableError:
            raise _unavailable() from None
        response = SearchResponse(
            items=[SearchPageView.model_validate(asdict(item)) for item in found.items]
        )
        return JSONResponse(
            content=response.model_dump(mode="json"),
            headers={
                REQUEST_ID_HEADER: get_request_id(request),
                "Cache-Control": PROTECTED_CACHE_CONTROL,
            },
        )

    return router


__all__ = ["SearchResponse", "create_search_v2_router"]
