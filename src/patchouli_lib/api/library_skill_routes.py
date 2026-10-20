"""Bearer-only Library Skill package discovery, snapshots and durable writes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from functools import partial
from typing import Any, cast
from urllib.parse import quote
from uuid import uuid4

import anyio
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, Engine
from starlette.datastructures import UploadFile
from starlette.responses import Response

from patchouli_lib.api.authentication import BearerAuthentication, extract_bearer_token
from patchouli_lib.api.contracts import API_V1_PREFIX, PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import (
    ApplicationProblem,
    insufficient_scope,
    invalid_token,
    resource_not_found,
)
from patchouli_lib.api.file_set_write_routes import _scope_id
from patchouli_lib.api.library_structure_routes import _authenticate
from patchouli_lib.api.request_ids import get_request_id
from patchouli_lib.auth.library_policy import LibraryGrantPolicy, resolve_library_policy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import AuditOutcome, AuthenticatedCaller, NewAuditEvent
from patchouli_lib.auth.service import AuthenticationError, AuthorizationError, utc_microseconds
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.repository import IdempotencyRepository
from patchouli_lib.idempotency.schemas import (
    IdempotencyRequest,
    OriginalResponse,
    ReplayResponse,
    TransactionValidatedCaller,
    digest_idempotency_key,
    digest_request_fingerprint,
)
from patchouli_lib.idempotency.service import IdempotencyConflictError, IdempotencyService
from patchouli_lib.identifiers import canonical_utc_wire
from patchouli_lib.library.schemas import BoundedText, ResourceName
from patchouli_lib.library_skills.service import (
    LibrarySkillService,
    SkillAction,
    SkillConflictError,
    SkillNotFoundError,
    SkillPreconditionError,
    SkillValidationError,
    build_skill_snapshot,
)

CREATE_SKILL_ROUTE = API_V1_PREFIX + "/libraries/{library_id}/skills"
REVISE_SKILL_ROUTE = CREATE_SKILL_ROUTE + "/{skill_id}/revisions"
_HEADERS = {"Cache-Control": PROTECTED_CACHE_CONTROL, "X-Content-Type-Options": "nosniff"}
_MAX_BODY_BYTES = 64 * 1024 * 1024 + 512 * 1024


class _Metadata(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    name: ResourceName
    description: BoundedText


def _problem(status: int, code: str) -> ApplicationProblem:
    return ApplicationProblem(
        status_code=status,
        code=code,
        title="Skill request failed",
        detail="The Skill request could not be completed.",
    )


def _authority(
    connection: Connection, token: str, library_id: str, action: SkillAction
) -> AuthenticatedCaller:
    now = utc_microseconds()
    identity = _authenticate(connection, token, now)
    policy = resolve_library_policy(
        connection,
        credential_id=identity.credential.id,
        caller_id=identity.caller.id,
        home_library_id=identity.caller.library_id,
        target_library_id=library_id,
        active_at=now,
    )
    if (
        not isinstance(policy, LibraryGrantPolicy)
        or not {
            "metadata": policy.read or policy.write,
            "read": policy.read,
            "write": policy.write,
        }[action]
    ):
        raise AuthorizationError
    return identity


def _service(connection: Connection, token: str) -> LibrarySkillService:
    def authorize(library_id: str, action: SkillAction) -> None:
        _authority(connection, token, library_id, action)

    return LibrarySkillService(connection, authorize=authorize)


async def _call[T](operation: Callable[[], T]) -> T:
    try:
        return await anyio.to_thread.run_sync(operation, abandon_on_cancel=False)
    except AuthenticationError:
        raise invalid_token() from None
    except AuthorizationError:
        raise insufficient_scope() from None
    except SkillNotFoundError:
        raise resource_not_found() from None
    except (SkillConflictError, IdempotencyConflictError):
        raise _problem(409, "skill_conflict") from None
    except SkillPreconditionError:
        raise _problem(412, "precondition_failed") from None
    except (SkillValidationError, ValueError):
        raise _problem(422, "request_validation_failed") from None


def _read[T](engine: Engine, operation: Callable[[Connection], T]) -> T:
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        try:
            return operation(connection)
        finally:
            connection.rollback()


async def _upload(request: Request) -> tuple[_Metadata, tuple[tuple[str, bytes], ...]]:
    total = 0

    async def receive() -> Any:
        nonlocal total
        message = await request.receive()
        total += len(message.get("body", b""))
        if total > _MAX_BODY_BYTES:
            raise _problem(413, "content_too_large")
        return message

    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "multipart/form-data":
        raise _problem(415, "unsupported_media_type")
    bounded = Request(request.scope, receive)
    try:
        async with bounded.form(max_files=64, max_fields=1, max_part_size=65536) as form:
            metadata = form.getlist("metadata")
            if (
                len(metadata) != 1
                or not isinstance(metadata[0], str)
                or set(form) != {"metadata", "file"}
            ):
                raise ValueError

            def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result

            parsed = _Metadata.model_validate(
                json.loads(metadata[0], object_pairs_hook=unique), strict=True
            )
            files: list[tuple[str, bytes]] = []
            for item in form.getlist("file"):
                if not isinstance(item, UploadFile) or item.filename is None:
                    raise ValueError
                body = await item.read(16 * 1024 * 1024 + 1)
                if len(body) > 16 * 1024 * 1024:
                    raise _problem(413, "content_too_large")
                files.append((item.filename, body))
            build_skill_snapshot(files)
            return parsed, tuple(files)
    except (ValueError, TypeError, UnicodeError):
        raise _problem(422, "request_validation_failed") from None


def create_library_skill_router(engine: Engine) -> APIRouter:
    router = APIRouter(prefix=API_V1_PREFIX)
    authenticate = BearerAuthentication(engine)

    async def read(
        request: Request, library_id: str, operation: Callable[[LibrarySkillService, str], object]
    ) -> Response:
        library_id = _scope_id(library_id)
        token = extract_bearer_token(request)
        await _call(partial(authenticate, request))
        value = await _call(
            partial(
                _read, engine, lambda connection: operation(_service(connection, token), library_id)
            )
        )
        if isinstance(value, bytes):
            return Response(
                value,
                media_type="application/octet-stream",
                headers={**_HEADERS, "Content-Disposition": "attachment"},
            )
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
        if isinstance(value, dict) and "etag" in value:
            etag = str(value["etag"])
        if request.headers.get("if-none-match") in {etag, "*"}:
            return Response(status_code=304, headers={**_HEADERS, "ETag": etag})
        return Response(payload, media_type="application/json", headers={**_HEADERS, "ETag": etag})

    def pagination(request: Request) -> dict[str, int]:
        if any(
            key not in {"limit", "offset"} or len(request.query_params.getlist(key)) != 1
            for key in request.query_params
        ):
            raise _problem(422, "request_validation_failed")
        try:
            return {
                "limit": int(request.query_params.get("limit", "20")),
                "offset": int(request.query_params.get("offset", "0")),
            }
        except ValueError:
            raise _problem(422, "request_validation_failed") from None

    def manifest(
        service: LibrarySkillService, library_id: str, skill_id: str, revision_id: str | None
    ) -> object:
        value = service.manifest(library_id, skill_id, revision_id=revision_id)
        base = (
            f"{API_V1_PREFIX}/libraries/{library_id}/skills/{skill_id}/revisions/"
            f"{value['revision_id']}/files/"
        )
        for entry in cast(list[dict[str, Any]], value["files"]):
            entry["href"] = base + quote(str(entry["path"]), safe="/")
        return value

    async def save(request: Request, library_id: str, skill_id: str | None = None) -> Response:
        library_id = _scope_id(library_id)
        token = extract_bearer_token(request)
        await _call(partial(authenticate, request))
        await _call(
            partial(
                _read, engine, lambda connection: _authority(connection, token, library_id, "write")
            )
        )
        keys = request.headers.getlist("idempotency-key")
        if len(keys) != 1 or request.query_params:
            raise _problem(422, "request_validation_failed")
        try:
            key = digest_idempotency_key(keys[0])
        except ValueError:
            raise _problem(422, "request_validation_failed") from None
        expected = request.headers.get("if-match")
        if skill_id is not None and (
            expected is None or len(request.headers.getlist("if-match")) != 1
        ):
            raise _problem(428, "precondition_required")
        body, files = await _upload(request)
        snapshot = build_skill_snapshot(files)
        fingerprint = digest_request_fingerprint(
            b"library-skill-save-v1",
            json.dumps(
                [
                    library_id,
                    skill_id,
                    body.name,
                    body.description,
                    expected,
                    snapshot.snapshot_sha256.hex(),
                ],
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode(),
        )

        def operation() -> OriginalResponse | ReplayResponse:
            with immediate_transaction(engine) as connection:
                identity = _authority(connection, token, library_id, "write")
                caller = TransactionValidatedCaller(
                    library_id=library_id,
                    actor_home_library_id=identity.caller.library_id,
                    caller_id=identity.caller.id,
                )
                command = IdempotencyRequest(
                    method="POST",
                    route_template=CREATE_SKILL_ROUTE if skill_id is None else REVISE_SKILL_ROUTE,
                    key_digest=key,
                    request_fingerprint=fingerprint,
                )
                replay = IdempotencyService(IdempotencyRepository(connection))
                previous = replay.lookup(caller, command)
                if previous is not None:
                    return previous
                service = _service(connection, token)
                result = (
                    service.create(
                        library_id, name=body.name, description=body.description, files=files
                    )
                    if skill_id is None
                    else service.revise(
                        library_id,
                        skill_id,
                        name=body.name,
                        description=body.description,
                        files=files,
                        expected_etag=expected,
                    )
                )
                now = int(str(result.skill["updated_at"]))
                if result.changed:
                    AuthRepository(connection).add_audit_event(
                        NewAuditEvent(
                            id=uuid4().hex,
                            library_id=library_id,
                            actor_home_library_id=identity.caller.library_id,
                            actor_caller_id=identity.caller.id,
                            actor_credential_id=identity.credential.id,
                            action="library.skill.create"
                            if result.created
                            else "library.skill.revise",
                            resource_type="library_skill",
                            resource_id=str(result.skill["id"]),
                            outcome=AuditOutcome.SUCCEEDED,
                            request_id=get_request_id(request),
                            occurred_at=now,
                        )
                    )
                location = (
                    f"{API_V1_PREFIX}/libraries/{library_id}/skills/{result.skill['id']}/"
                    f"revisions/{result.skill['revision_id']}/manifest"
                )
                response = OriginalResponse(
                    response_status=201 if result.created else 200,
                    response_body=json.dumps(
                        {**result.skill, "changed": result.changed},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode(),
                    response_location=location,
                    response_etag=str(result.skill["etag"]),
                    original_request_id=get_request_id(request),
                    original_request_timestamp=canonical_utc_wire(utc_microseconds()),
                )
                replay.record_success(caller, command, response)
                return response

        result = await _call(operation)
        headers = {**_HEADERS, "ETag": result.response_etag}
        if result.response_location is not None:
            headers["Location"] = result.response_location
        if isinstance(result, ReplayResponse):
            headers["Idempotency-Replayed"] = "true"
        return Response(
            result.response_body,
            status_code=result.response_status,
            media_type="application/json",
            headers=headers,
        )

    @router.get("/libraries/{library_id}/skills")
    async def skills(library_id: str, request: Request) -> Response:
        return await read(
            request, library_id, lambda service, lib: service.list(lib, **pagination(request))
        )

    @router.post("/libraries/{library_id}/skills")
    async def create(library_id: str, request: Request) -> Response:
        return await save(request, library_id)

    @router.get("/libraries/{library_id}/skills/{skill_id}")
    async def skill(library_id: str, skill_id: str, request: Request) -> Response:
        return await read(
            request, library_id, lambda service, lib: service.get(lib, _scope_id(skill_id))
        )

    @router.get("/libraries/{library_id}/skills/{skill_id}/revisions")
    async def versions(library_id: str, skill_id: str, request: Request) -> Response:
        return await read(
            request,
            library_id,
            lambda service, lib: service.versions(lib, _scope_id(skill_id), **pagination(request)),
        )

    @router.post("/libraries/{library_id}/skills/{skill_id}/revisions")
    async def revise(library_id: str, skill_id: str, request: Request) -> Response:
        return await save(request, library_id, _scope_id(skill_id))

    @router.get("/libraries/{library_id}/skills/{skill_id}/manifest")
    async def current_manifest(library_id: str, skill_id: str, request: Request) -> Response:
        return await read(
            request,
            library_id,
            lambda service, lib: manifest(service, lib, _scope_id(skill_id), None),
        )

    @router.get("/libraries/{library_id}/skills/{skill_id}/revisions/{revision_id}/manifest")
    async def version_manifest(
        library_id: str, skill_id: str, revision_id: str, request: Request
    ) -> Response:
        return await read(
            request,
            library_id,
            lambda service, lib: manifest(service, lib, _scope_id(skill_id), revision_id),
        )

    @router.get(
        "/libraries/{library_id}/skills/{skill_id}/revisions/{revision_id}/files/{resource_path:path}"
    )
    async def file(
        library_id: str, skill_id: str, revision_id: str, resource_path: str, request: Request
    ) -> Response:
        return await read(
            request,
            library_id,
            lambda service, lib: service.read_file(
                lib, _scope_id(skill_id), resource_path, revision_id=revision_id
            ),
        )

    return router
