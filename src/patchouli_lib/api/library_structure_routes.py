"""Discover hierarchy guides and initialize empty Libraries with scoped Agent rights."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from functools import partial

import anyio
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import Connection, Engine, select
from starlette.responses import JSONResponse, Response

from patchouli_lib.api.authentication import extract_bearer_token
from patchouli_lib.api.contracts import API_V1_PREFIX, PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import (
    ApplicationProblem,
    insufficient_scope,
    invalid_token,
    resource_not_found,
)
from patchouli_lib.api.file_set_write_routes import _scope_id
from patchouli_lib.api.request_ids import get_request_id
from patchouli_lib.auth.library_policy import LibraryGrantPolicy, resolve_library_policy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import AuditOutcome, AuthenticatedCaller, CallerKind, NewAuditEvent
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthenticationService,
    AuthorizationError,
    Clock,
    new_opaque_id,
    utc_microseconds,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.guides import GuideRecord, get_guide, guide_etag, update_readme
from patchouli_lib.library.models import Book, Library, Section
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import (
    BookRecord,
    BoundedText,
    CreateBookInput,
    CreateSectionInput,
    LibraryRecord,
    NewBook,
    NewSection,
    ReadmeMarkdown,
    SectionRecord,
)
from patchouli_lib.library.service import (
    LibraryStructureNotFoundError,
    LibraryStructureVersionConflictError,
)

MAX_STRUCTURE_BODY_BYTES = 2 * 1024 * 1024


class _SectionInput(CreateSectionInput):
    description: BoundedText

    @field_validator("description")
    @classmethod
    def purpose_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A purpose description is required.")
        return value


class _BookInput(CreateBookInput):
    summary: BoundedText

    @field_validator("summary")
    @classmethod
    def purpose_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A purpose summary is required.")
        return value


class _ReadmeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    readme: ReadmeMarkdown


def _problem(status: int, code: str) -> ApplicationProblem:
    return ApplicationProblem(
        status_code=status,
        code=code,
        title="Hierarchy request failed",
        detail="The hierarchy request could not be completed.",
    )


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field.")
        result[key] = value
    return result


async def _body[T: BaseModel](request: Request, model: type[T]) -> T:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise _problem(415, "unsupported_media_type")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_STRUCTURE_BODY_BYTES:
            raise _problem(413, "content_too_large")
        data.extend(chunk)
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        return model.model_validate(value, strict=True)
    except (ValueError, TypeError, RecursionError):
        raise _problem(422, "request_validation_failed") from None


def _authenticate(connection: Connection, token: str, now: int) -> AuthenticatedCaller:
    repository = AuthRepository(connection)
    authenticated = AuthenticationService(
        repository, clock=lambda: now, last_used_coalesce_microseconds=-1
    ).authenticate(token)
    caller = authenticated.caller
    if caller.kind is not CallerKind.AGENT or not repository.has_library_grant_policy(
        caller.library_id, caller.id, authenticated.credential.id
    ):
        raise AuthorizationError()
    return authenticated


def _authorize(
    connection: Connection,
    token: str,
    library_id: str,
    *,
    now: int,
    write: bool,
) -> AuthenticatedCaller:
    authenticated = _authenticate(connection, token, now)
    caller = authenticated.caller
    policy = resolve_library_policy(
        connection,
        credential_id=authenticated.credential.id,
        caller_id=caller.id,
        home_library_id=caller.library_id,
        target_library_id=library_id,
        active_at=now,
    )
    if not isinstance(policy, LibraryGrantPolicy) or not (
        policy.write if write else policy.read or policy.write
    ):
        raise AuthorizationError()
    return authenticated


def _read[T](engine: Engine, operation: Callable[[Connection], T]) -> T:
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        try:
            return operation(connection)
        finally:
            connection.rollback()


async def _call[T](operation: Callable[[], T]) -> T:
    try:
        return await anyio.to_thread.run_sync(operation, abandon_on_cancel=False)
    except AuthenticationError:
        raise invalid_token() from None
    except AuthorizationError:
        raise insufficient_scope() from None
    except LibraryStructureNotFoundError:
        raise resource_not_found() from None
    except LibraryStructureVersionConflictError:
        raise _problem(412, "precondition_failed") from None


def _response(record: GuideRecord, request: Request, *, status: int = 200) -> Response:
    return _json(record.model_dump(mode="json"), request, guide_etag(record), status=status)


def _audit(
    connection: Connection,
    authenticated: AuthenticatedCaller,
    record: GuideRecord,
    request: Request,
    *,
    action: str,
    now: int,
) -> None:
    kind = (
        "book"
        if isinstance(record, BookRecord)
        else "section"
        if isinstance(record, SectionRecord)
        else "library"
    )
    library_id = record.id if isinstance(record, LibraryRecord) else record.library_id
    AuthRepository(connection).add_audit_event(
        NewAuditEvent(
            id=new_opaque_id(),
            library_id=library_id,
            actor_home_library_id=authenticated.caller.library_id,
            actor_caller_id=authenticated.caller.id,
            actor_credential_id=authenticated.credential.id,
            action=f"structure.{kind}.{action}",
            resource_type=kind,
            resource_id=record.id,
            outcome=AuditOutcome.SUCCEEDED,
            request_id=get_request_id(request),
            occurred_at=now,
        )
    )


def _json(value: object, request: Request, etag: str, *, status: int = 200) -> Response:
    headers = {"ETag": etag, "Cache-Control": PROTECTED_CACHE_CONTROL}
    if request.method == "GET" and any(
        candidate.strip().removeprefix("W/") in {etag, "*"}
        for candidate in request.headers.get("if-none-match", "").split(",")
    ):
        return Response(status_code=304, headers=headers)
    return JSONResponse(value, status_code=status, headers=headers)


def _pagination(request: Request) -> tuple[int, int]:
    if any(key not in {"limit", "offset"} for key in request.query_params) or any(
        len(request.query_params.getlist(key)) > 1 for key in {"limit", "offset"}
    ):
        raise _problem(422, "request_validation_failed")
    try:
        limit = int(request.query_params.get("limit", "20"))
        offset = int(request.query_params.get("offset", "0"))
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError
    except ValueError:
        raise _problem(422, "request_validation_failed") from None
    return limit, offset


def create_library_structure_router(
    engine: Engine, *, clock: Clock = utc_microseconds
) -> APIRouter:
    router = APIRouter(prefix=API_V1_PREFIX)

    async def get_item(
        request: Request, library_id: str, section_id: str | None = None, book_id: str | None = None
    ) -> Response:
        token = extract_bearer_token(request)
        library_id = _scope_id(library_id)
        section_id = None if section_id is None else _scope_id(section_id)
        book_id = None if book_id is None else _scope_id(book_id)

        def operation(connection: Connection) -> GuideRecord:
            _authorize(connection, token, library_id, now=clock(), write=False)
            return get_guide(
                connection, library_id=library_id, section_id=section_id, book_id=book_id
            )

        return _response(await _call(partial(_read, engine, operation)), request)

    async def list_items(
        request: Request, library_id: str | None = None, section_id: str | None = None
    ) -> Response:
        token = extract_bearer_token(request)
        library_id = None if library_id is None else _scope_id(library_id)
        section_id = None if section_id is None else _scope_id(section_id)
        limit, offset = _pagination(request)

        def operation(connection: Connection) -> dict[str, object]:
            if library_id is None:
                authenticated = _authenticate(connection, token, clock())
                caller = authenticated.caller
                grants = AuthRepository(connection).list_credential_library_grants(
                    home_library_id=caller.library_id,
                    caller_id=caller.id,
                    credential_id=authenticated.credential.id,
                )
                statement = select(Library.__table__).where(
                    Library.id.in_(grant.library_id for grant in grants)
                )
                record_type: type[GuideRecord] = LibraryRecord
                order = Library.id
            else:
                _authorize(connection, token, library_id, now=clock(), write=False)
                get_guide(connection, library_id=library_id, section_id=section_id)
                if section_id is None:
                    statement = select(Section.__table__).where(Section.library_id == library_id)
                    record_type = SectionRecord
                    order = Section.id
                else:
                    statement = select(Book.__table__).where(
                        Book.library_id == library_id, Book.section_id == section_id
                    )
                    record_type = BookRecord
                    order = Book.id
            rows = connection.execute(statement.order_by(order).offset(offset).limit(limit + 1))
            records = [
                record_type.model_validate(row).model_dump(mode="json") for row in rows.mappings()
            ]
            return {
                "items": records[:limit],
                "next_offset": offset + limit if len(records) > limit else None,
            }

        value = await _call(partial(_read, engine, operation))
        etag = '"' + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() + '"'
        return _json(value, request, etag)

    async def create_item(
        request: Request, library_id: str, section_id: str | None = None
    ) -> Response:
        token = extract_bearer_token(request)
        library_id = _scope_id(library_id)
        section_id = None if section_id is None else _scope_id(section_id)
        if request.query_params:
            raise _problem(422, "request_validation_failed")

        def preflight(connection: Connection) -> None:
            _authorize(connection, token, library_id, now=clock(), write=True)

        await _call(partial(_read, engine, preflight))
        body: _SectionInput | _BookInput
        if section_id is None:
            body = await _body(request, _SectionInput)
        else:
            body = await _body(request, _BookInput)

        def operation() -> tuple[GuideRecord, int]:
            with immediate_transaction(engine) as connection:
                now = clock()
                authenticated = _authorize(connection, token, library_id, now=now, write=True)
                repository = LibraryRepository(connection)
                get_guide(connection, library_id=library_id, section_id=section_id)
                if isinstance(body, _SectionInput):
                    existing = repository.find_section_by_name(library_id, body.name)
                    if existing is not None:
                        if (
                            existing.description != body.description
                            or existing.readme != body.readme
                        ):
                            raise _problem(409, "metadata_conflict")
                        return existing, 200
                    created = repository.add_section(
                        NewSection(
                            id=new_opaque_id(),
                            library_id=library_id,
                            **body.model_dump(),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    _audit(connection, authenticated, created, request, action="create", now=now)
                    return created, 201
                assert section_id is not None
                existing_book = repository.find_book_by_name(library_id, section_id, body.name)
                if existing_book is not None:
                    if existing_book.summary != body.summary or existing_book.readme != body.readme:
                        raise _problem(409, "metadata_conflict")
                    return existing_book, 200
                created_book = repository.add_book(
                    NewBook(
                        id=new_opaque_id(),
                        library_id=library_id,
                        section_id=section_id,
                        **body.model_dump(),
                        created_at=now,
                        updated_at=now,
                    )
                )
                _audit(connection, authenticated, created_book, request, action="create", now=now)
                return created_book, 201

        record, status = await _call(operation)
        return _response(record, request, status=status)

    async def put_item(
        request: Request, library_id: str, section_id: str | None = None, book_id: str | None = None
    ) -> Response:
        token = extract_bearer_token(request)
        library_id = _scope_id(library_id)
        section_id = None if section_id is None else _scope_id(section_id)
        book_id = None if book_id is None else _scope_id(book_id)
        if request.query_params:
            raise _problem(422, "request_validation_failed")

        def preflight(connection: Connection) -> None:
            _authorize(connection, token, library_id, now=clock(), write=True)

        await _call(partial(_read, engine, preflight))
        expected = request.headers.get("if-match")
        if expected is None:
            raise _problem(428, "precondition_required")
        body = await _body(request, _ReadmeInput)

        def operation() -> GuideRecord:
            with immediate_transaction(engine) as connection:
                now = clock()
                authenticated = _authorize(connection, token, library_id, now=now, write=True)
                current = get_guide(
                    connection, library_id=library_id, section_id=section_id, book_id=book_id
                )
                if guide_etag(current) != expected:
                    raise _problem(412, "precondition_failed")
                updated = update_readme(
                    connection,
                    library_id=library_id,
                    section_id=section_id,
                    book_id=book_id,
                    readme=body.readme,
                    expected_updated_at=current.updated_at,
                    now=now,
                )
                if updated.readme != current.readme:
                    _audit(
                        connection,
                        authenticated,
                        updated,
                        request,
                        action="readme_update",
                        now=updated.updated_at,
                    )
                return updated

        return _response(await _call(operation), request)

    @router.get("/libraries")
    async def libraries(request: Request) -> Response:
        return await list_items(request)

    @router.get("/libraries/{library_id}")
    async def library(library_id: str, request: Request) -> Response:
        return await get_item(request, library_id)

    @router.get("/libraries/{library_id}/sections")
    async def sections(library_id: str, request: Request) -> Response:
        return await list_items(request, library_id)

    @router.post("/libraries/{library_id}/sections")
    async def create_section(library_id: str, request: Request) -> Response:
        return await create_item(request, library_id)

    @router.get("/libraries/{library_id}/sections/{section_id}")
    async def section(library_id: str, section_id: str, request: Request) -> Response:
        return await get_item(request, library_id, section_id)

    @router.get("/libraries/{library_id}/sections/{section_id}/books")
    async def books(library_id: str, section_id: str, request: Request) -> Response:
        return await list_items(request, library_id, section_id)

    @router.post("/libraries/{library_id}/sections/{section_id}/books")
    async def create_book(library_id: str, section_id: str, request: Request) -> Response:
        return await create_item(request, library_id, section_id)

    @router.get("/libraries/{library_id}/sections/{section_id}/books/{book_id}")
    async def book(library_id: str, section_id: str, book_id: str, request: Request) -> Response:
        return await get_item(request, library_id, section_id, book_id)

    @router.put("/libraries/{library_id}/readme")
    async def library_readme(library_id: str, request: Request) -> Response:
        return await put_item(request, library_id)

    @router.put("/libraries/{library_id}/sections/{section_id}/readme")
    async def section_readme(library_id: str, section_id: str, request: Request) -> Response:
        return await put_item(request, library_id, section_id)

    @router.put("/libraries/{library_id}/sections/{section_id}/books/{book_id}/readme")
    async def book_readme(
        library_id: str, section_id: str, book_id: str, request: Request
    ) -> Response:
        return await put_item(request, library_id, section_id, book_id)

    return router
