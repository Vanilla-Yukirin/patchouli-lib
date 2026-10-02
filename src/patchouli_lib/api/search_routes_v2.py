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
from patchouli_lib.api.contracts import (
    API_V1_PREFIX,
    PROTECTED_CACHE_CONTROL,
    OpaqueCursor,
    WireModel,
    build_api_v1_path,
)
from patchouli_lib.api.errors import (
    ApplicationProblem,
    invalid_token,
    resource_not_found,
)
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, get_request_id
from patchouli_lib.auth.service import AuthenticationError, Clock, utc_microseconds
from patchouli_lib.retrieval.cursor import CursorCodec, InvalidCursorError
from patchouli_lib.search.index_v2 import SearchIndexUnavailableError
from patchouli_lib.search.query_v2 import (
    MAX_QUERY_BODY_BYTES,
    InvalidSearchQueryV2,
    parse_query_v2_json,
)
from patchouli_lib.search.service_v2 import SearchPageV2, SearchScopeError, search_pages_v2


class SearchMatchSourceView(WireModel):
    kind: str
    file_name: str | None


class SearchSnippetView(WireModel):
    file_name: str
    text: str
    matched: bool


class SearchPageView(WireModel):
    library_id: str
    section_id: str
    book_id: str
    page_id: str
    revision_id: str
    revision_number: int
    revision_files_href: str
    title: str
    occurred_at: int
    match_sources: list[SearchMatchSourceView]
    snippet: SearchSnippetView | None = None


class SearchResponse(WireModel):
    items: Annotated[list[SearchPageView], Field(max_length=100)]
    next_cursor: OpaqueCursor | None = None


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


def _invalid_cursor() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=400,
        code="invalid_cursor",
        title="Invalid cursor",
        detail="The pagination cursor is invalid or no longer applicable.",
    )


def _search_page_view(item: SearchPageV2) -> SearchPageView:
    # The relative link identifies the exact returned Revision, not a mutable
    # current-Page endpoint. Its target performs its own authorization check.
    href = build_api_v1_path(
        "libraries",
        item.library_id,
        "sections",
        item.section_id,
        "pages",
        item.page_id,
        "revisions",
        item.revision_id,
        "files",
    )
    return SearchPageView.model_validate({**asdict(item), "revision_files_href": href})


async def _read_query(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_QUERY_BODY_BYTES:
            raise _invalid_query()
        body.extend(chunk)
    return bytes(body)


def create_search_v2_router(
    engine: Engine,
    *,
    clock: Clock = utc_microseconds,
    cursor_codec: CursorCodec | None = None,
) -> APIRouter:
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
        if query.cursor is not None and cursor_codec is None:
            raise _invalid_cursor()
        try:
            found = await anyio.to_thread.run_sync(
                partial(
                    search_pages_v2,
                    engine,
                    context,
                    query,
                    clock=clock,
                    cursor_codec=cursor_codec,
                ),
                abandon_on_cancel=False,
            )
        except AuthenticationError:
            raise invalid_token() from None
        except SearchScopeError:
            raise resource_not_found() from None
        except SearchIndexUnavailableError:
            raise _unavailable() from None
        except InvalidCursorError:
            raise _invalid_cursor() from None
        response = SearchResponse(
            items=[_search_page_view(item) for item in found.items],
            next_cursor=found.next_cursor,
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
