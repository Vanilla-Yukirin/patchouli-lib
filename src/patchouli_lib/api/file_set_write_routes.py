"""Unified file-set upload routes registered by the development application.

One Markdown file and a flat group of files share the same multipart wire shape.
The existing single-Markdown Archive routes remain compatibility endpoints.
This proposed API has not been merged or deployed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any, Literal, cast

import anyio
from fastapi import APIRouter, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import Connection, Engine
from starlette.responses import Response

from patchouli_lib.api.authentication import (
    AuthenticatedRequestContext,
    BearerAuthentication,
    extract_bearer_token,
)
from patchouli_lib.api.contracts import API_V1_PREFIX, PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import (
    ApplicationProblem,
    insufficient_scope,
    invalid_token,
    resource_not_found,
)
from patchouli_lib.api.file_set_multipart import parse_file_set_multipart
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, get_request_id
from patchouli_lib.auth.library_policy import LegacySectionPolicy, LibraryAction
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, SectionAction
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthorizationError,
    Clock,
    utc_microseconds,
)
from patchouli_lib.content.file_set_create_service import (
    FileSetCreateCommand,
    FileSetCreateNotFoundError,
    FileSetCreateReplay,
    FileSetCreateResult,
    FileSetCreateService,
)
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.file_set_write_service import (
    FileSetAppendCommand,
    FileSetWriteNotFoundError,
    FileSetWritePreconditionRequiredError,
    FileSetWriteReplay,
    FileSetWriteResult,
    FileSetWriteService,
)
from patchouli_lib.content.schemas import (
    ArchiveIdempotencyKey,
    ArchiveSourceInput,
    PageId,
    RevisionId,
    StrongPageETag,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency import (
    MAX_IDEMPOTENCY_KEY_BYTES,
    IdempotencyConflictError,
    digest_idempotency_key,
)
from patchouli_lib.idempotency.schemas import OriginalResponse, ReplayResponse
from patchouli_lib.identifiers import InvalidPageIdError, parse_occurrence_time
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import OpaqueId

_OPAQUE_ID_ADAPTER = TypeAdapter(OpaqueId)
_ETAG_ADAPTER = TypeAdapter(StrongPageETag)
_PAGE_ETAG_PATTERN = re.compile(rb'^"page-v[12]-[0-9a-f]{64}"$', re.ASCII)

type CreateServiceFactory = Callable[[Connection], FileSetCreateService]
type WriteServiceFactory = Callable[[Connection], FileSetWriteService]


class _DocumentedBearer(HTTPBearer):
    """Declare the scheme while leaving strict authentication to the route."""

    async def __call__(self, request: Request) -> HTTPAuthorizationCredentials | None:
        # FastAPI's HTTPBearer parser would collapse duplicate Authorization
        # headers before the existing strict parser can reject them.
        return None


_DOCUMENTED_BEARER = _DocumentedBearer(scheme_name="BearerAuth", auto_error=False)


class _FileSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    size_bytes: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _CreateSuccessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: OpaqueId
    book_id: OpaqueId
    page_id: PageId
    revision_id: RevisionId
    revision_number: Literal[1]
    occurred_at: str = Field(json_schema_extra={"format": "date-time"})
    occurrence_defaulted: bool
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[_FileSummaryResponse] = Field(min_length=1)


class _ReviseSuccessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changed: bool
    section_id: OpaqueId
    page_id: PageId
    revision_id: RevisionId
    revision_number: int = Field(ge=1)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[_FileSummaryResponse] = Field(min_length=1)


_COMMON_SUCCESS_HEADERS: dict[str, Any] = {
    "ETag": {
        "required": True,
        "description": "Strong Page ETag; a replayed response may contain a historical value.",
        "schema": {"type": "string", "pattern": r'^"page-v[12]-[0-9a-f]{64}"$'},
    },
    REQUEST_ID_HEADER: {"required": True, "schema": {"type": "string"}},
    "Cache-Control": {
        "required": True,
        "schema": {"type": "string", "const": PROTECTED_CACHE_CONTROL},
    },
    "Idempotency-Replayed": {
        "description": "Present only when a successful request is replayed.",
        "schema": {"type": "string", "const": "true"},
    },
}

_IDEMPOTENCY_PARAMETER: dict[str, Any] = {
    "name": "Idempotency-Key",
    "in": "header",
    "required": True,
    "description": "Exactly one bounded, visible-ASCII key is required.",
    "schema": {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_IDEMPOTENCY_KEY_BYTES,
        "pattern": r"^[!-~]+$",
    },
}
_IF_MATCH_PARAMETER: dict[str, Any] = {
    "name": "If-Match",
    "in": "header",
    "required": True,
    "description": "The current strong Page ETag; required for revision writes.",
    "schema": {"type": "string", "pattern": r'^"page-v[12]-[0-9a-f]{64}"$'},
}

_SOURCE_METADATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": "Non-empty source kind without leading or trailing whitespace.",
        },
        "locator": {"type": ["string", "null"], "minLength": 1},
        "captured_at": {"type": ["integer", "null"], "description": "UTC microseconds."},
    },
    "required": ["kind"],
    "additionalProperties": False,
}


def _multipart_request_body(
    metadata_properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    """Describe the wire format without invoking FastAPI's form parser."""

    return {
        "requestBody": {
            "required": True,
            "description": (
                "The first part must be metadata containing UTF-8 JSON (without a filename), "
                "followed by one or more repeated file parts with UTF-8 flat filenames "
                "and raw bytes."
            ),
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "metadata": {
                                "type": "object",
                                "properties": metadata_properties,
                                "required": required,
                                "additionalProperties": False,
                            },
                            "file": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                                "minItems": 1,
                            },
                        },
                        "required": ["metadata", "file"],
                        "additionalProperties": False,
                    },
                    "encoding": {"metadata": {"contentType": "application/json"}},
                }
            },
        }
    }


_CREATE_MULTIPART_OPENAPI = _multipart_request_body(
    {
        "title": {"type": "string", "minLength": 1},
        "source": _SOURCE_METADATA_SCHEMA,
        "occurred_at": {"type": "string", "format": "date-time"},
    },
    ["title", "source"],
)
_REVISE_MULTIPART_OPENAPI = _multipart_request_body({"source": _SOURCE_METADATA_SCHEMA}, ["source"])
_CREATE_MULTIPART_OPENAPI["parameters"] = [_IDEMPOTENCY_PARAMETER]
_REVISE_MULTIPART_OPENAPI["parameters"] = [_IDEMPOTENCY_PARAMETER, _IF_MATCH_PARAMETER]


class _DuplicateMetadataKey(ValueError):
    pass


def _problem(status: int, code: str, title: str, detail: str) -> ApplicationProblem:
    return ApplicationProblem(status_code=status, code=code, title=title, detail=detail)


def _invalid_request() -> ApplicationProblem:
    return _problem(
        422,
        "request_validation_failed",
        "Request validation failed",
        "The request did not satisfy the required schema.",
    )


def _scope_id(value: str) -> str:
    try:
        return _OPAQUE_ID_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        raise _invalid_request() from None


def _header_values(request: Request, expected: bytes) -> tuple[bytes, ...]:
    headers: Sequence[tuple[bytes, bytes]] = request.scope.get("headers", ())
    return tuple(value for name, value in headers if name.lower() == expected)


def _idempotency(request: Request) -> ArchiveIdempotencyKey:
    values = _header_values(request, b"idempotency-key")
    if len(values) != 1:
        raise _invalid_request()
    try:
        return ArchiveIdempotencyKey(
            key_digest=digest_idempotency_key(values[0].decode("ascii", errors="strict"))
        )
    except (UnicodeDecodeError, ValueError, ValidationError):
        raise _invalid_request() from None


def _create_precondition(request: Request) -> None:
    if _header_values(request, b"if-match"):
        raise _invalid_request()


def _revision_precondition(request: Request) -> str:
    values = _header_values(request, b"if-match")
    if not values:
        raise _problem(
            428,
            "precondition_required",
            "Precondition required",
            "A current Page ETag is required.",
        )
    if len(values) != 1 or _PAGE_ETAG_PATTERN.fullmatch(values[0]) is None:
        raise _invalid_request()
    try:
        return _ETAG_ADAPTER.validate_python(values[0].decode("ascii"), strict=True)
    except (UnicodeDecodeError, ValidationError):
        raise _invalid_request() from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateMetadataKey
        result[key] = value
    return result


def _metadata(raw: bytes) -> dict[str, Any]:
    if not raw:
        raise _invalid_request()
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateMetadataKey,
        RecursionError,
        ValueError,
    ):
        raise _invalid_request() from None
    if not isinstance(value, dict):
        raise _invalid_request()
    return cast(dict[str, Any], value)


def _source(value: object) -> ArchiveSourceInput:
    if not isinstance(value, dict) or not {"kind"} <= set(value) <= {
        "kind",
        "locator",
        "captured_at",
    }:
        raise _invalid_request()
    try:
        return ArchiveSourceInput.model_validate(value, strict=True)
    except ValidationError:
        raise _invalid_request() from None


def _authorize_scope(
    engine: Engine,
    context: AuthenticatedRequestContext,
    library_id: str,
    section_id: str,
    *,
    clock: Clock,
) -> None:
    if context.authenticated.caller.kind is not CallerKind.AGENT:
        raise insufficient_scope()
    with immediate_transaction(engine) as connection:
        policy = AuthRepository(connection).get_library_policy(
            credential_id=context.authenticated.credential.id,
            caller_id=context.authenticated.caller.id,
            home_library_id=context.authenticated.caller.library_id,
            target_library_id=library_id,
            active_at=clock(),
        )
        if policy is None:
            raise invalid_token()
        if isinstance(policy, LegacySectionPolicy):
            if context.authenticated.caller.library_id != library_id:
                raise resource_not_found()
            current_grants = AuthRepository(connection).list_grants(
                library_id, context.authenticated.caller.id
            )
            grants = {grant.action for grant in current_grants if grant.section_id == section_id}
            if not grants:
                raise resource_not_found()
            if SectionAction.ARCHIVE_WRITE not in grants:
                raise insufficient_scope()
        elif not policy.allows(LibraryAction.WRITE):
            raise insufficient_scope()
        if LibraryRepository(connection).get_section(library_id, section_id) is None:
            raise resource_not_found()


def _perform_create(
    engine: Engine,
    service_factory: CreateServiceFactory,
    token: str,
    command: FileSetCreateCommand,
    idempotency: ArchiveIdempotencyKey,
) -> FileSetCreateResult:
    try:
        with immediate_transaction(engine) as connection:
            return service_factory(connection).create_page(token, command, idempotency)
    except AuthenticationError:
        raise invalid_token() from None
    except AuthorizationError:
        raise insufficient_scope() from None
    except FileSetCreateNotFoundError:
        raise resource_not_found() from None
    except IdempotencyConflictError:
        raise _problem(
            409,
            "idempotency_mismatch",
            "Idempotency conflict",
            "The idempotency key was already used for a different request.",
        ) from None


def _perform_append(
    engine: Engine,
    service_factory: WriteServiceFactory,
    token: str,
    command: FileSetAppendCommand,
    idempotency: ArchiveIdempotencyKey,
) -> FileSetWriteResult:
    try:
        with immediate_transaction(engine) as connection:
            return service_factory(connection).append_existing_page(token, command, idempotency)
    except AuthenticationError:
        raise invalid_token() from None
    except AuthorizationError:
        raise insufficient_scope() from None
    except FileSetWriteNotFoundError:
        raise resource_not_found() from None
    except FileSetWritePreconditionRequiredError:
        raise _problem(
            428,
            "precondition_required",
            "Precondition required",
            "A current Page ETag is required.",
        ) from None
    except FileSetPreconditionFailedError:
        raise _problem(
            412,
            "revision_conflict",
            "Precondition failed",
            "The Page has a newer current Revision.",
        ) from None
    except IdempotencyConflictError:
        raise _problem(
            409,
            "idempotency_mismatch",
            "Idempotency conflict",
            "The idempotency key was already used for a different request.",
        ) from None


def _response(
    request: Request,
    result: FileSetCreateResult | FileSetWriteResult,
    *,
    library_id: str,
) -> Response:
    replay = isinstance(result, (FileSetCreateReplay, FileSetWriteReplay))
    stored: OriginalResponse | ReplayResponse = result.response
    if stored.response_status not in (200, 201):
        raise RuntimeError("Unexpected file-set response status.")
    body = json.loads(stored.response_body)
    headers = {
        "ETag": stored.response_etag,
        REQUEST_ID_HEADER: get_request_id(request),
        "Cache-Control": PROTECTED_CACHE_CONTROL,
    }
    if stored.response_status == 201:
        headers["Location"] = (
            f"{API_V1_PREFIX}/libraries/{library_id}/sections/{body['section_id']}"
            f"/pages/{body['page_id']}/revisions/{body['revision_id']}/files"
        )
    if replay:
        headers["Idempotency-Replayed"] = "true"
    return Response(
        content=stored.response_body,
        status_code=stored.response_status,
        media_type=stored.response_media_type,
        headers=headers,
    )


def create_file_set_write_router(
    engine: Engine,
    *,
    clock: Clock = utc_microseconds,
    create_service_factory: CreateServiceFactory | None = None,
    write_service_factory: WriteServiceFactory | None = None,
) -> APIRouter:
    """Build draft routes that share one multipart parser for every file count."""

    router = APIRouter(prefix=API_V1_PREFIX)
    authenticate = BearerAuthentication(engine, clock=clock)
    create_factory = create_service_factory or (
        lambda connection: FileSetCreateService(connection, clock=clock)
    )
    write_factory = write_service_factory or (
        lambda connection: FileSetWriteService(connection, clock=clock)
    )

    @router.post(
        "/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages",
        status_code=201,
        dependencies=[Security(_DOCUMENTED_BEARER)],
        response_model=_CreateSuccessResponse,
        responses={
            201: {
                "headers": {
                    **_COMMON_SUCCESS_HEADERS,
                    "Location": {
                        "required": True,
                        "description": "Exact manifest URL for the created Revision.",
                        "schema": {"type": "string"},
                    },
                }
            }
        },
        openapi_extra=_CREATE_MULTIPART_OPENAPI,
    )
    async def create_page(
        library_id: str,
        section_id: str,
        book_id: str,
        request: Request,
    ) -> Response:
        scoped_library = _scope_id(library_id)
        scoped_section = _scope_id(section_id)
        scoped_book = _scope_id(book_id)
        idempotency = _idempotency(request)
        _create_precondition(request)
        context = await anyio.to_thread.run_sync(
            partial(authenticate, request), abandon_on_cancel=False
        )
        await anyio.to_thread.run_sync(
            partial(_authorize_scope, engine, context, scoped_library, scoped_section, clock=clock),
            abandon_on_cancel=False,
        )
        upload = await parse_file_set_multipart(request)
        metadata = _metadata(upload.metadata)
        if set(metadata) not in ({"title", "source"}, {"title", "occurred_at", "source"}):
            raise _invalid_request()
        try:
            occurred_at = (
                parse_occurrence_time(metadata["occurred_at"]).utc_microseconds
                if "occurred_at" in metadata
                else None
            )
            command = FileSetCreateCommand(
                library_id=scoped_library,
                section_id=scoped_section,
                book_id=scoped_book,
                title=metadata["title"],
                occurred_at=occurred_at,
                files=tuple((file.name, file.content) for file in upload.manifest.files),
                source=_source(metadata["source"]),
                request_id=get_request_id(request),
            )
        except (ValidationError, ValueError, TypeError):
            raise _invalid_request() from None
        result = await anyio.to_thread.run_sync(
            partial(
                _perform_create,
                engine,
                create_factory,
                extract_bearer_token(request),
                command,
                idempotency,
            ),
            abandon_on_cancel=False,
        )
        return _response(request, result, library_id=scoped_library)

    @router.post(
        "/libraries/{library_id}/sections/{section_id}/pages/{page_id}/file-revisions",
        status_code=200,
        dependencies=[Security(_DOCUMENTED_BEARER)],
        response_model=_ReviseSuccessResponse,
        responses={200: {"headers": _COMMON_SUCCESS_HEADERS}},
        openapi_extra=_REVISE_MULTIPART_OPENAPI,
    )
    async def revise_page(
        library_id: str,
        section_id: str,
        page_id: str,
        request: Request,
    ) -> Response:
        scoped_library = _scope_id(library_id)
        scoped_section = _scope_id(section_id)
        idempotency = _idempotency(request)
        expected_etag = _revision_precondition(request)
        context = await anyio.to_thread.run_sync(
            partial(authenticate, request), abandon_on_cancel=False
        )
        await anyio.to_thread.run_sync(
            partial(_authorize_scope, engine, context, scoped_library, scoped_section, clock=clock),
            abandon_on_cancel=False,
        )
        upload = await parse_file_set_multipart(request)
        metadata = _metadata(upload.metadata)
        if set(metadata) != {"source"}:
            raise _invalid_request()
        try:
            command = FileSetAppendCommand(
                library_id=scoped_library,
                section_id=scoped_section,
                page_id=page_id,
                expected_etag=expected_etag,
                files=tuple((file.name, file.content) for file in upload.manifest.files),
                source=_source(metadata["source"]),
                request_id=get_request_id(request),
            )
        except (ValidationError, ValueError, TypeError, InvalidPageIdError):
            raise _invalid_request() from None
        result = await anyio.to_thread.run_sync(
            partial(
                _perform_append,
                engine,
                write_factory,
                extract_bearer_token(request),
                command,
                idempotency,
            ),
            abandon_on_cancel=False,
        )
        return _response(request, result, library_id=scoped_library)

    return router


__all__ = ["create_file_set_write_router"]
