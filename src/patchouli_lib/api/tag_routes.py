"""Protected Tag directory and exact Page association routes."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import partial
from typing import Any

import anyio
from fastapi import APIRouter, Request
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine
from starlette.responses import JSONResponse

from patchouli_lib.api.authentication import extract_bearer_token
from patchouli_lib.api.contracts import API_V1_PREFIX, PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import (
    ApplicationProblem,
    insufficient_scope,
    invalid_token,
    resource_not_found,
)
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, get_request_id
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthenticationService,
    Clock,
    utc_microseconds,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.schemas import OpaqueId
from patchouli_lib.tags.schemas import (
    CreateTagInput,
    PageTagCollection,
    TagAssociationResult,
    TagCollection,
    TagDefinitionView,
    TaggedPageCollection,
    TaggedPageView,
    TagView,
)
from patchouli_lib.tags.service import (
    TagAuthorizationError,
    TagNotFoundError,
    TagService,
    TagValidationError,
)

_ID = TypeAdapter(OpaqueId)
_BODY_LIMIT = 1_024


def _invalid_request() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=422,
        code="request_validation_failed",
        title="Request validation failed",
        detail="The request did not satisfy the required schema.",
    )


def _id(value: str) -> str:
    try:
        return _ID.validate_python(value, strict=True)
    except ValidationError:
        raise _invalid_request() from None


def _list_options(request: Request, *, allow_query: bool) -> tuple[str | None, int, int]:
    allowed = {"limit", "offset", "q"} if allow_query else {"limit", "offset"}
    values: dict[str, str] = {}
    for name, value in request.query_params.multi_items():
        if name not in allowed or name in values:
            raise _invalid_request()
        values[name] = value
    try:
        limit = int(values.get("limit", "20"))
        offset = int(values.get("offset", "0"))
    except ValueError:
        raise _invalid_request() from None
    if (
        not 1 <= limit <= 100
        or not 0 <= offset <= 1_000_000
        or ("limit" in values and str(limit) != values["limit"])
        or ("offset" in values and str(offset) != values["offset"])
    ):
        raise _invalid_request()
    return values.get("q"), limit, offset


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON property.")
        result[name] = value
    return result


async def _create_body(request: Request) -> CreateTagInput:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise ApplicationProblem(
            status_code=415,
            code="unsupported_media_type",
            title="Unsupported media type",
            detail="The media type is not supported.",
        )
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _BODY_LIMIT:
            raise ApplicationProblem(
                status_code=413,
                code="content_too_large",
                title="Content too large",
                detail="The request content is too large.",
            )
        body.extend(chunk)
    try:
        parsed = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        return CreateTagInput.model_validate(parsed, strict=True)
    except (UnicodeDecodeError, ValueError, ValidationError, TypeError):
        raise _invalid_request() from None


def _perform[T](
    engine: Engine,
    token: str,
    operation: Callable[[TagService], T],
    *,
    clock: Clock,
    read_only: bool,
) -> T:
    try:
        if read_only:
            # Keep the credential-use write short. Reauthenticate and check grants
            # inside one deferred read snapshot, so concurrent Tag reads do not
            # reserve SQLite's single writer slot while they aggregate Pages.
            with immediate_transaction(engine) as connection:
                AuthenticationService(AuthRepository(connection), clock=clock).authenticate(token)
            with engine.connect() as connection:
                connection.exec_driver_sql("BEGIN")
                try:
                    return operation(TagService(connection, clock=clock, touch_last_used=False))
                finally:
                    connection.rollback()
        with immediate_transaction(engine) as connection:
            return operation(TagService(connection, clock=clock))
    except AuthenticationError:
        raise invalid_token() from None
    except TagAuthorizationError:
        raise insufficient_scope() from None
    except TagNotFoundError:
        raise resource_not_found() from None
    except TagValidationError:
        raise _invalid_request() from None


async def _run[T](
    engine: Engine,
    token: str,
    operation: Callable[[TagService], T],
    *,
    clock: Clock,
    read_only: bool,
) -> T:
    return await anyio.to_thread.run_sync(
        partial(_perform, engine, token, operation, clock=clock, read_only=read_only),
        abandon_on_cancel=False,
    )


def _json(request: Request, value: Any, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        value.model_dump(mode="json"),
        status_code=status_code,
        headers={
            REQUEST_ID_HEADER: get_request_id(request),
            "Cache-Control": PROTECTED_CACHE_CONTROL,
        },
    )


def _tag_view(tag_id: str, name: str, created_at: int, page_count: int) -> TagView:
    return TagView(
        tag_id=tag_id,
        name=name,
        created_at=created_at,
        page_count=page_count,
    )


def _definition_view(tag_id: str, name: str, created_at: int) -> TagDefinitionView:
    return TagDefinitionView(tag_id=tag_id, name=name, created_at=created_at)


def create_tag_router(engine: Engine, *, clock: Clock = utc_microseconds) -> APIRouter:
    """Build routes without registering them on the shared application."""

    router = APIRouter(prefix=API_V1_PREFIX)

    @router.get("/libraries/{library_id}/tags")
    async def list_tags(library_id: str, request: Request) -> JSONResponse:
        token = extract_bearer_token(request)
        validated_library = _id(library_id)
        query, limit, offset = _list_options(request, allow_query=True)
        tags, next_offset = await _run(
            engine,
            token,
            lambda service: service.list_tags(
                token, library_id=validated_library, query=query, limit=limit, offset=offset
            ),
            clock=clock,
            read_only=True,
        )
        return _json(
            request,
            TagCollection(
                items=[
                    _tag_view(
                        item.tag.id, item.tag.display_name, item.tag.created_at, item.page_count
                    )
                    for item in tags
                ],
                next_offset=next_offset,
            ),
        )

    @router.post("/libraries/{library_id}/tags")
    async def create_tag(library_id: str, request: Request) -> JSONResponse:
        token = extract_bearer_token(request)
        validated_library = _id(library_id)
        body = await _create_body(request)
        tag, created = await _run(
            engine,
            token,
            lambda service: service.create_tag(
                token,
                library_id=validated_library,
                name=body.name,
                request_id=get_request_id(request),
            ),
            clock=clock,
            read_only=False,
        )
        return _json(
            request,
            _definition_view(tag.id, tag.display_name, tag.created_at),
            status_code=201 if created else 200,
        )

    @router.get("/libraries/{library_id}/tags/{tag_id}/pages")
    async def list_tag_pages(library_id: str, tag_id: str, request: Request) -> JSONResponse:
        token = extract_bearer_token(request)
        validated_library, validated_tag = _id(library_id), _id(tag_id)
        _, limit, offset = _list_options(request, allow_query=False)
        pages, next_offset = await _run(
            engine,
            token,
            lambda service: service.list_tag_pages(
                token,
                library_id=validated_library,
                tag_id=validated_tag,
                limit=limit,
                offset=offset,
            ),
            clock=clock,
            read_only=True,
        )
        return _json(
            request,
            TaggedPageCollection(
                items=[
                    TaggedPageView(
                        section_id=page.section_id,
                        page_id=page.page_id,
                        title=page.title,
                        occurred_at=page.occurred_at,
                    )
                    for page in pages
                ],
                next_offset=next_offset,
            ),
        )

    @router.get("/libraries/{library_id}/sections/{section_id}/pages/{page_id}/tags")
    async def list_page_tags(
        library_id: str, section_id: str, page_id: str, request: Request
    ) -> JSONResponse:
        token = extract_bearer_token(request)
        validated_library, validated_section = _id(library_id), _id(section_id)
        _, limit, offset = _list_options(request, allow_query=False)
        tags, next_offset = await _run(
            engine,
            token,
            lambda service: service.list_page_tags(
                token,
                library_id=validated_library,
                section_id=validated_section,
                page_id=page_id,
                limit=limit,
                offset=offset,
            ),
            clock=clock,
            read_only=True,
        )
        return _json(
            request,
            PageTagCollection(
                items=[_definition_view(tag.id, tag.display_name, tag.created_at) for tag in tags],
                next_offset=next_offset,
            ),
        )

    @router.put("/libraries/{library_id}/sections/{section_id}/pages/{page_id}/tags/{tag_id}")
    async def attach_tag(
        library_id: str, section_id: str, page_id: str, tag_id: str, request: Request
    ) -> JSONResponse:
        return await _set_association(request, library_id, section_id, page_id, tag_id, attach=True)

    @router.delete("/libraries/{library_id}/sections/{section_id}/pages/{page_id}/tags/{tag_id}")
    async def detach_tag(
        library_id: str, section_id: str, page_id: str, tag_id: str, request: Request
    ) -> JSONResponse:
        return await _set_association(
            request, library_id, section_id, page_id, tag_id, attach=False
        )

    async def _set_association(
        request: Request,
        library_id: str,
        section_id: str,
        page_id: str,
        tag_id: str,
        *,
        attach: bool,
    ) -> JSONResponse:
        token = extract_bearer_token(request)
        validated_library, validated_section, validated_tag = (
            _id(library_id),
            _id(section_id),
            _id(tag_id),
        )
        if request.query_params:
            raise _invalid_request()
        async for chunk in request.stream():
            if chunk:
                raise _invalid_request()
        changed = await _run(
            engine,
            token,
            lambda service: service.set_page_tag(
                token,
                library_id=validated_library,
                section_id=validated_section,
                page_id=page_id,
                tag_id=validated_tag,
                attach=attach,
                request_id=get_request_id(request),
            ),
            clock=clock,
            read_only=False,
        )
        return _json(request, TagAssociationResult(changed=changed))

    return router


__all__ = ["create_tag_router"]
