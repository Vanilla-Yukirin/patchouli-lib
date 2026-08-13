"""Protected HTTP routes for exact, non-search retrieval reads."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any, Final, TypeVar

import anyio
from fastapi import APIRouter, Request
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine
from starlette.responses import JSONResponse

from patchouli_lib.api.authentication import (
    AuthenticatedRequestContext,
    BearerAuthentication,
)
from patchouli_lib.api.contracts import (
    API_V1_PREFIX,
    PROTECTED_CACHE_CONTROL,
    PaginatedResponse,
    PaginationParameters,
)
from patchouli_lib.api.errors import (
    ApplicationProblem,
    insufficient_scope,
    invalid_token,
    resource_not_found,
)
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, get_request_id
from patchouli_lib.auth.service import Clock, utc_microseconds
from patchouli_lib.identifiers import InvalidPageIdError, InvalidRevisionNumberError
from patchouli_lib.library.schemas import OpaqueId
from patchouli_lib.retrieval.cursor import CursorBinding, CursorCodec, InvalidCursorError
from patchouli_lib.retrieval.repository import RetrievalRepository
from patchouli_lib.retrieval.schemas import (
    CurrentPageRead,
    KeysetPage,
    PageDocument,
    ReadWindow,
)
from patchouli_lib.retrieval.service import (
    RetrievalAuthenticationError,
    RetrievalAuthorizationError,
    RetrievalNotFoundError,
    RetrievalPersistenceError,
    RetrievalService,
)

_SECTIONS_ROUTE: Final = "sections.list"
_BOOKS_ROUTE: Final = "books.list"
_PAGES_ROUTE: Final = "pages.list"
_NO_QUERY: Final = b"query:none"
_SECTION_FILTERS: Final = b"grant:section-query"
_BOOK_FILTERS: Final = b"visibility:section"
_PAGE_FILTERS: Final = b"deleted:false;revision:current"
_SECTION_SORT: Final = b"section-id:ascending"
_BOOK_SORT: Final = b"book-id:ascending"
_PAGE_SORT: Final = b"page-id:ascending"
_PAGINATION_NAMES: Final = frozenset({"limit", "cursor"})
_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)
_MAX_REVISION_NUMBER: Final = (1 << 63) - 1

ItemT = TypeVar("ItemT")
ResultT = TypeVar("ResultT")
ReadOperation = Callable[[RetrievalService], ResultT]


def _validation_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=422,
        code="request_validation_failed",
        title="Request validation failed",
        detail="The request did not satisfy the required schema.",
    )


def _invalid_cursor_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=400,
        code="invalid_cursor",
        title="Invalid cursor",
        detail="The pagination cursor is invalid or no longer applicable.",
    )


def _pagination_parameters(request: Request) -> PaginationParameters:
    values: dict[str, list[str]] = {}
    for name, value in request.query_params.multi_items():
        if name not in _PAGINATION_NAMES:
            raise _validation_problem()
        values.setdefault(name, []).append(value)
    if any(len(items) != 1 for items in values.values()):
        raise _validation_problem()

    raw: dict[str, object] = {}
    if "limit" in values:
        raw["limit"] = values["limit"][0]
    if "cursor" in values:
        raw["cursor"] = values["cursor"][0]
    try:
        return PaginationParameters.model_validate(raw)
    except ValidationError:
        raise _validation_problem() from None


def _validate_section_id(section_id: str) -> str:
    try:
        return _OPAQUE_ID_ADAPTER.validate_python(section_id, strict=True)
    except ValidationError:
        raise _validation_problem() from None


def _validate_revision_number(revision_number: str) -> int:
    if (
        not revision_number
        or not revision_number.isascii()
        or not revision_number.isdecimal()
        or revision_number.startswith("0")
    ):
        raise _validation_problem()
    try:
        parsed = int(revision_number)
    except ValueError:  # pragma: no cover - guarded ASCII decimal
        raise _validation_problem() from None
    if not 1 <= parsed <= _MAX_REVISION_NUMBER or str(parsed) != revision_number:
        raise _validation_problem()
    return parsed


def _binding(
    context: AuthenticatedRequestContext,
    *,
    section_id: str | None,
    route_identity: str,
    limit: int,
    filters_identity: bytes,
    sort_identity: bytes,
) -> CursorBinding:
    caller = context.authenticated.caller
    return CursorBinding(
        caller_id=caller.id,
        policy_version=caller.policy_version,
        section_id=section_id,
        route_identity=route_identity,
        limit=limit,
        query_identity=_NO_QUERY,
        filters_identity=filters_identity,
        sort_identity=sort_identity,
    )


def _read_window(
    pagination: PaginationParameters,
    *,
    cursor_codec: CursorCodec,
    binding: CursorBinding,
) -> ReadWindow:
    after_key: str | None = None
    if pagination.cursor is not None:
        try:
            after_key = cursor_codec.decode(pagination.cursor, binding=binding)
        except InvalidCursorError:
            raise _invalid_cursor_problem() from None
    return ReadWindow(limit=pagination.limit, after_key=after_key)


def _perform_read[ResultT](
    engine: Engine,
    context: AuthenticatedRequestContext,
    operation: ReadOperation[ResultT],
    *,
    clock: Clock,
) -> ResultT:
    try:
        with engine.connect() as connection, connection.begin():
            service = RetrievalService(
                RetrievalRepository(connection),
                context.authenticated,
                clock=clock,
            )
            return operation(service)
    except RetrievalAuthenticationError:
        raise invalid_token() from None
    except RetrievalAuthorizationError:
        raise insufficient_scope() from None
    except RetrievalNotFoundError:
        raise resource_not_found() from None
    except (InvalidPageIdError, InvalidRevisionNumberError):
        raise _validation_problem() from None
    except RetrievalPersistenceError:
        raise RuntimeError("Retrieval persistence validation failed.") from None


async def _authenticate(
    authenticate: BearerAuthentication,
    request: Request,
) -> AuthenticatedRequestContext:
    return await anyio.to_thread.run_sync(
        partial(authenticate, request),
        abandon_on_cancel=False,
    )


async def _read[ResultT](
    engine: Engine,
    context: AuthenticatedRequestContext,
    operation: ReadOperation[ResultT],
    *,
    clock: Clock,
) -> ResultT:
    return await anyio.to_thread.run_sync(
        partial(_perform_read, engine, context, operation, clock=clock),
        abandon_on_cancel=False,
    )


def _collection[ItemT](
    page: KeysetPage[ItemT],
    *,
    cursor_codec: CursorCodec,
    binding: CursorBinding,
) -> PaginatedResponse[ItemT]:
    next_cursor = (
        None
        if page.next_key is None
        else cursor_codec.encode(binding=binding, last_key=page.next_key)
    )
    return PaginatedResponse[ItemT](items=list(page.items), next_cursor=next_cursor)


def _json_response(
    request: Request,
    value: Any,
    *,
    etag: str | None = None,
) -> JSONResponse:
    headers = {
        REQUEST_ID_HEADER: get_request_id(request),
        "Cache-Control": PROTECTED_CACHE_CONTROL,
    }
    if etag is not None:
        headers["ETag"] = etag
    return JSONResponse(
        content=value.model_dump(mode="json"),
        headers=headers,
    )


def create_retrieval_router(
    engine: Engine,
    *,
    cursor_codec: CursorCodec,
    clock: Clock = utc_microseconds,
) -> APIRouter:
    """Create five protected, non-search read routes.

    The caller supplies the cursor codec so key management remains outside this
    target-neutral router.
    """

    router = APIRouter(prefix=API_V1_PREFIX)
    authenticate = BearerAuthentication(engine, clock=clock)

    @router.get("/sections")
    async def list_sections(request: Request) -> JSONResponse:
        context = await _authenticate(authenticate, request)
        pagination = _pagination_parameters(request)
        binding = _binding(
            context,
            section_id=None,
            route_identity=_SECTIONS_ROUTE,
            limit=pagination.limit,
            filters_identity=_SECTION_FILTERS,
            sort_identity=_SECTION_SORT,
        )
        window = _read_window(pagination, cursor_codec=cursor_codec, binding=binding)
        page = await _read(
            engine,
            context,
            lambda service: service.list_sections(window),
            clock=clock,
        )
        response = _collection(page, cursor_codec=cursor_codec, binding=binding)
        return _json_response(request, response)

    @router.get("/sections/{section_id}/books")
    async def list_books(section_id: str, request: Request) -> JSONResponse:
        context = await _authenticate(authenticate, request)
        validated_section_id = _validate_section_id(section_id)
        pagination = _pagination_parameters(request)
        binding = _binding(
            context,
            section_id=validated_section_id,
            route_identity=_BOOKS_ROUTE,
            limit=pagination.limit,
            filters_identity=_BOOK_FILTERS,
            sort_identity=_BOOK_SORT,
        )
        window = _read_window(pagination, cursor_codec=cursor_codec, binding=binding)
        page = await _read(
            engine,
            context,
            lambda service: service.list_books(validated_section_id, window),
            clock=clock,
        )
        response = _collection(page, cursor_codec=cursor_codec, binding=binding)
        return _json_response(request, response)

    @router.get("/sections/{section_id}/pages")
    async def list_pages(section_id: str, request: Request) -> JSONResponse:
        context = await _authenticate(authenticate, request)
        validated_section_id = _validate_section_id(section_id)
        pagination = _pagination_parameters(request)
        binding = _binding(
            context,
            section_id=validated_section_id,
            route_identity=_PAGES_ROUTE,
            limit=pagination.limit,
            filters_identity=_PAGE_FILTERS,
            sort_identity=_PAGE_SORT,
        )
        window = _read_window(pagination, cursor_codec=cursor_codec, binding=binding)
        page = await _read(
            engine,
            context,
            lambda service: service.list_pages(validated_section_id, window),
            clock=clock,
        )
        response = _collection(page, cursor_codec=cursor_codec, binding=binding)
        return _json_response(request, response)

    @router.get("/sections/{section_id}/pages/{page_id}")
    async def get_current_page(
        section_id: str,
        page_id: str,
        request: Request,
    ) -> JSONResponse:
        context = await _authenticate(authenticate, request)
        validated_section_id = _validate_section_id(section_id)
        current: CurrentPageRead = await _read(
            engine,
            context,
            lambda service: service.get_current_page(validated_section_id, page_id),
            clock=clock,
        )
        return _json_response(request, current.document, etag=current.etag)

    @router.get(
        "/sections/{section_id}/pages/{page_id}/revisions/{revision_number}",
    )
    async def get_revision(
        section_id: str,
        page_id: str,
        revision_number: str,
        request: Request,
    ) -> JSONResponse:
        context = await _authenticate(authenticate, request)
        validated_section_id = _validate_section_id(section_id)
        validated_revision_number = _validate_revision_number(revision_number)
        document: PageDocument = await _read(
            engine,
            context,
            lambda service: service.get_revision(
                validated_section_id,
                page_id,
                validated_revision_number,
            ),
            clock=clock,
        )
        return _json_response(request, document)

    return router


__all__ = ["create_retrieval_router"]
