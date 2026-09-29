"""Draft protected routes for exact historical Page file-set reads.

This router is not registered by the application until the unified file-set
HTTP contract and its migration gates are reviewed. It has no write routes.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Request
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine
from starlette.responses import JSONResponse, Response

from patchouli_lib.api.authentication import AuthenticatedRequestContext, BearerAuthentication
from patchouli_lib.api.contracts import API_V1_PREFIX, PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import (
    ApplicationProblem,
    insufficient_scope,
    invalid_token,
    resource_not_found,
)
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, get_request_id
from patchouli_lib.auth.service import Clock, utc_microseconds
from patchouli_lib.content.file_manifest import normalize_file_name
from patchouli_lib.identifiers import (
    InvalidPageIdError,
    InvalidRevisionIdError,
    validate_revision_id,
)
from patchouli_lib.library.schemas import OpaqueId
from patchouli_lib.retrieval.file_set_read import (
    FileSetReadAuthenticationError,
    FileSetReadAuthorizationError,
    FileSetReadNotFoundError,
    FileSetReadPersistenceError,
    FileSetReadService,
    FileSetRevisionManifestView,
)
from patchouli_lib.retrieval.schemas import RevisionFileRead

_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)
type ReadOperation[ResultT] = Callable[[FileSetReadService], ResultT]


def _validation_problem() -> ApplicationProblem:
    return ApplicationProblem(
        status_code=422,
        code="request_validation_failed",
        title="Request validation failed",
        detail="The request did not satisfy the required schema.",
    )


def _scope_id(value: str) -> str:
    try:
        return _OPAQUE_ID_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        raise _validation_problem() from None


def _revision_id(value: str) -> str:
    try:
        return validate_revision_id(value)
    except InvalidRevisionIdError:
        raise _validation_problem() from None


def _filename(value: str) -> str:
    try:
        if normalize_file_name(value) != value:
            raise ValueError("Noncanonical file name.")
    except (TypeError, ValueError, UnicodeError):
        raise _validation_problem() from None
    return value


def _perform_read[ResultT](
    engine: Engine,
    context: AuthenticatedRequestContext,
    operation: ReadOperation[ResultT],
    *,
    clock: Clock,
) -> ResultT:
    try:
        with engine.connect() as connection:
            # SQLAlchemy's transaction marker alone does not issue a pysqlite
            # BEGIN before SELECT. Make the credential/grant and Page/Revision
            # checks one actual SQLite read snapshot.
            connection.exec_driver_sql("BEGIN")
            try:
                return operation(FileSetReadService(connection, context.authenticated, clock=clock))
            finally:
                connection.rollback()
    except FileSetReadAuthenticationError:
        raise invalid_token() from None
    except FileSetReadAuthorizationError:
        raise insufficient_scope() from None
    except FileSetReadNotFoundError:
        raise resource_not_found() from None
    except InvalidPageIdError:
        raise _validation_problem() from None
    except FileSetReadPersistenceError:
        raise RuntimeError("File-set read persistence validation failed.") from None


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


def _headers(request: Request) -> dict[str, str]:
    return {
        REQUEST_ID_HEADER: get_request_id(request),
        "Cache-Control": PROTECTED_CACHE_CONTROL,
    }


def create_file_set_read_router(
    engine: Engine,
    *,
    clock: Clock = utc_microseconds,
) -> APIRouter:
    """Build the unregistered, read-only unified file-set router."""

    router = APIRouter(prefix=API_V1_PREFIX)
    authenticate = BearerAuthentication(engine, clock=clock)
    base = (
        "/libraries/{library_id}/sections/{section_id}/pages/{page_id}"
        "/revisions/{revision_id}/files"
    )

    @router.get(base)
    async def list_revision_files(
        library_id: str,
        section_id: str,
        page_id: str,
        revision_id: str,
        request: Request,
    ) -> JSONResponse:
        context = await anyio.to_thread.run_sync(
            partial(authenticate, request), abandon_on_cancel=False
        )
        scoped_library_id = _scope_id(library_id)
        scoped_section_id = _scope_id(section_id)
        validated_revision_id = _revision_id(revision_id)
        manifest: FileSetRevisionManifestView = await _read(
            engine,
            context,
            lambda service: service.list_files(
                scoped_library_id, scoped_section_id, page_id, validated_revision_id
            ),
            clock=clock,
        )
        return JSONResponse(content=manifest.model_dump(mode="json"), headers=_headers(request))

    @router.get(f"{base}/{{file_name:path}}")
    async def download_revision_file(
        library_id: str,
        section_id: str,
        page_id: str,
        revision_id: str,
        file_name: str,
        request: Request,
    ) -> Response:
        context = await anyio.to_thread.run_sync(
            partial(authenticate, request), abandon_on_cancel=False
        )
        scoped_library_id = _scope_id(library_id)
        scoped_section_id = _scope_id(section_id)
        validated_revision_id = _revision_id(revision_id)
        validated_file_name = _filename(file_name)
        file: RevisionFileRead = await _read(
            engine,
            context,
            lambda service: service.get_file(
                scoped_library_id,
                scoped_section_id,
                page_id,
                validated_revision_id,
                validated_file_name,
            ),
            clock=clock,
        )
        headers = _headers(request)
        headers.update(
            {
                "Content-Disposition": (
                    "attachment; filename=\"download\"; filename*=UTF-8''"
                    + quote(file.filename, safe="")
                ),
                "X-Content-Type-Options": "nosniff",
            }
        )
        return Response(
            content=file.content,
            media_type="application/octet-stream",
            headers=headers,
        )

    return router


__all__ = ["create_file_set_read_router"]
