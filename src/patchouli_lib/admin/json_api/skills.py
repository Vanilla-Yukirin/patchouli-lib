"""Library-owned Skill packages through the same browser session boundary."""

from __future__ import annotations

from io import BytesIO
from typing import Any, cast
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from patchouli_lib.admin.json_api.common import HEADERS, ApiError, BrowserAuth, reply
from patchouli_lib.admin.json_api.content import (
    audit,
    fingerprint,
    merge_files,
    operation_key,
    remember,
    replay,
    save_input,
)
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.content.presentation import preview_text
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library_skills.service import (
    LibrarySkillService,
    SkillAction,
    SkillConflictError,
    SkillNotFoundError,
    SkillPreconditionError,
)


def register_skills(router: APIRouter, auth: BrowserAuth) -> None:
    def service_for(connection: Any, session: MasterAdminSession) -> LibrarySkillService:
        def authorize(_library_id: str, _action: SkillAction) -> None:
            auth.authorize(connection, session)

        return LibrarySkillService(connection, authorize=authorize)

    def manifest_files(
        service: LibrarySkillService, library_id: str, skill_id: str, revision_id: str | None
    ) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
        manifest = service.manifest(library_id, skill_id, revision_id=revision_id)
        files = [
            (
                str(entry["path"]),
                service.read_file(
                    library_id,
                    skill_id,
                    str(entry["path"]),
                    revision_id=str(manifest["revision_id"]),
                ),
            )
            for entry in cast(list[dict[str, Any]], manifest["files"])
        ]
        return manifest, files

    @router.get("/libraries/{library_id}/skills")
    def list_skills(
        request: Request, library_id: str, limit: int = 20, offset: int = 0
    ) -> Response:
        with auth.read(request) as (connection, session):
            return reply(
                service_for(connection, session).list(library_id, limit=limit, offset=offset)
            )

    @router.get("/libraries/{library_id}/skills/{skill_id}")
    def detail(
        request: Request, library_id: str, skill_id: str, revision_id: str | None = None
    ) -> Response:
        with auth.read(request) as (connection, session):
            service = service_for(connection, session)
            skill = service.get(library_id, skill_id)
            manifest, contents = manifest_files(service, library_id, skill_id, revision_id)
            by_name = dict(contents)
            files = []
            base = f"/admin/api/libraries/{library_id}/skills/{skill_id}"
            for entry in cast(list[dict[str, Any]], manifest["files"]):
                name = str(entry["path"])
                suffix = "?revision_id=" + quote(str(manifest["revision_id"]), safe="")
                text = preview_text(by_name[name])
                files.append(
                    {
                        "name": name,
                        "size_bytes": entry["bytes"],
                        "sha256": entry["sha256"],
                        "editable": text is not None,
                        "download_href": base + "/files/" + quote(name, safe="/") + suffix,
                        "text_href": base + "/texts/" + quote(name, safe="/") + suffix
                        if text is not None
                        else None,
                    }
                )
            return reply(
                {
                    "skill": skill,
                    "selected_revision_id": manifest["revision_id"],
                    "selected_revision": {
                        key: value for key, value in manifest.items() if key != "files"
                    },
                    "files": files,
                    "versions": service.versions(library_id, skill_id),
                }
            )

    @router.get("/libraries/{library_id}/skills/{skill_id}/versions")
    def versions(
        request: Request, library_id: str, skill_id: str, limit: int = 20, offset: int = 0
    ) -> Response:
        with auth.read(request) as (connection, session):
            return reply(
                service_for(connection, session).versions(
                    library_id, skill_id, limit=limit, offset=offset
                )
            )

    @router.get("/libraries/{library_id}/skills/{skill_id}/manifest")
    def manifest(
        request: Request, library_id: str, skill_id: str, revision_id: str | None = None
    ) -> Response:
        with auth.read(request) as (connection, session):
            value = service_for(connection, session).manifest(
                library_id, skill_id, revision_id=revision_id
            )
            base = f"/admin/api/libraries/{library_id}/skills/{skill_id}/files/"
            for entry in cast(list[dict[str, Any]], value["files"]):
                entry["download_href"] = (
                    base
                    + quote(str(entry["path"]), safe="/")
                    + "?revision_id="
                    + str(value["revision_id"])
                )
            return reply(value)

    @router.get("/libraries/{library_id}/skills/{skill_id}/files/{resource_path:path}")
    def download_file(
        request: Request, library_id: str, skill_id: str, resource_path: str, revision_id: str
    ) -> Response:
        with auth.read(request) as (connection, session):
            body = service_for(connection, session).read_file(
                library_id, skill_id, resource_path, revision_id=revision_id
            )
        return Response(
            body,
            media_type="application/octet-stream",
            headers={
                **HEADERS,
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(resource_path.rsplit("/", 1)[-1], safe=""),
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @router.get("/libraries/{library_id}/skills/{skill_id}/texts/{resource_path:path}")
    def text_file(
        request: Request, library_id: str, skill_id: str, resource_path: str, revision_id: str
    ) -> Response:
        with auth.read(request) as (connection, session):
            value = preview_text(
                service_for(connection, session).read_file(
                    library_id, skill_id, resource_path, revision_id=revision_id
                )
            )
        if value is None:
            raise ApiError(415, "text_unavailable")
        return Response(
            value.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            headers={**HEADERS, "Content-Security-Policy": "sandbox; default-src 'none'"},
        )

    @router.get("/libraries/{library_id}/skills/{skill_id}/download")
    def download_zip(
        request: Request, library_id: str, skill_id: str, revision_id: str | None = None
    ) -> Response:
        with auth.read(request) as (connection, session):
            _, files = manifest_files(
                service_for(connection, session), library_id, skill_id, revision_id
            )
        with BytesIO() as buffer:
            with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
                for name, body in files:
                    archive.writestr(name, body)
            result = buffer.getvalue()
        return Response(
            result,
            media_type="application/zip",
            headers={
                **HEADERS,
                "Content-Disposition": f'attachment; filename="skill-{skill_id}.zip"',
            },
        )

    async def save(request: Request, library_id: str, skill_id: str | None) -> Response:
        session = auth.write_session(request)
        key = operation_key(request)
        metadata, uploads = await save_input(request)
        if (
            set(metadata)
            - {"name", "description", "base_etag", "edits", "remove", "renames", "mode"}
            or not {"name", "description"} <= set(metadata)
            or metadata.get("mode", "overwrite_latest") != "overwrite_latest"
        ):
            raise ApiError(422, "invalid_skill_save")
        digest = fingerprint(f"skill:{library_id}:{skill_id}", metadata, uploads)

        def execute() -> dict[str, Any]:
            with immediate_transaction(auth.engine) as connection:
                auth.authorize(connection, session)
                previous = replay(connection, session, key, digest)
                if previous is not None:
                    return previous
                service = service_for(connection, session)
                current_files: list[tuple[str, bytes]] = []
                overwritten = False
                if skill_id is not None:
                    current = service.get(library_id, skill_id)
                    _, current_files = manifest_files(
                        service, library_id, skill_id, str(current["current_revision_id"])
                    )
                    overwritten = (
                        metadata.get("base_etag") is not None
                        and metadata["base_etag"] != current["etag"]
                    )
                files = merge_files(current_files, metadata, uploads, package=True)
                arguments = {
                    "name": metadata["name"],
                    "description": metadata["description"],
                    "files": files,
                }
                if skill_id is None:
                    result = service.create(library_id, **arguments)
                else:
                    result = service.revise(library_id, skill_id, expected_etag=None, **arguments)
                operation_at = cast(int, result.skill["updated_at"])
                if result.changed:
                    audit(
                        connection,
                        session,
                        "library.skill.workspace_save",
                        "library_skill",
                        f"{library_id}:{result.skill['id']}:{result.skill['current_revision_id']}",
                        operation_at=operation_at,
                    )
                response = {
                    "skill": result.skill,
                    "changed": result.changed,
                    "created": result.created,
                    "replayed": False,
                    "overwritten_newer_revision": overwritten,
                }
                remember(connection, session, key, digest, response, operation_at=operation_at)
                return response

        try:
            return reply(await run_in_threadpool(execute), 201 if skill_id is None else 200)
        except SkillNotFoundError:
            raise ApiError(404, "skill_not_found") from None
        except (SkillConflictError, SkillPreconditionError):
            raise ApiError(409, "skill_conflict") from None

    @router.post("/libraries/{library_id}/skills")
    async def create(request: Request, library_id: str) -> Response:
        return await save(request, library_id, None)

    @router.put("/libraries/{library_id}/skills/{skill_id}")
    async def revise(request: Request, library_id: str, skill_id: str) -> Response:
        return await save(request, library_id, skill_id)

    @router.post("/libraries/{library_id}/skills/{skill_id}/revisions/{revision_id}/restore")
    async def restore(
        request: Request, library_id: str, skill_id: str, revision_id: str
    ) -> Response:
        from patchouli_lib.admin.json_api.common import body_json

        session = auth.write_session(request)
        key = operation_key(request)
        metadata = await body_json(request)
        if (
            set(metadata) != {"expected_etag", "confirm_restore"}
            or metadata["confirm_restore"] != "yes"
            or not isinstance(metadata["expected_etag"], str)
        ):
            raise ApiError(422, "restore_confirmation_required")
        digest = fingerprint(f"skill-restore:{library_id}:{skill_id}:{revision_id}", metadata, [])

        def execute() -> dict[str, Any]:
            with immediate_transaction(auth.engine) as connection:
                auth.authorize(connection, session)
                previous = replay(connection, session, key, digest)
                if previous is not None:
                    return previous
                service = service_for(connection, session)
                source, files = manifest_files(service, library_id, skill_id, revision_id)
                result = service.revise(
                    library_id,
                    skill_id,
                    name=str(source["name"]),
                    description=str(source["description"]),
                    files=files,
                    expected_etag=metadata["expected_etag"],
                )
                operation_at = cast(int, result.skill["updated_at"])
                if result.changed:
                    audit(
                        connection,
                        session,
                        "library.skill.revision_restore",
                        "library_skill",
                        f"{library_id}:{skill_id}:{result.skill['current_revision_id']}",
                        operation_at=operation_at,
                    )
                response = {
                    "skill": result.skill,
                    "changed": result.changed,
                    "created": False,
                    "replayed": False,
                    "overwritten_newer_revision": False,
                }
                remember(connection, session, key, digest, response, operation_at=operation_at)
                return response

        return reply(await run_in_threadpool(execute))
