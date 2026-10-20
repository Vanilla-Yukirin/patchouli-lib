"""Thin cookie-only adapters over existing domain actions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, TypeAdapter
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from patchouli_lib.admin.contracts import (
    MasterCorrectOccurrenceInput,
    MasterDeletePageFormInput,
    MasterPageTagFormInput,
    MasterProvisionAgentInput,
    MasterRestoreArchiveFormInput,
    MasterRotateAgentCredentialInput,
    MasterSetAgentLibraryScopeInput,
    MasterTagBatchInput,
    MasterTagFormInput,
    MasterUpdateAgentInput,
)
from patchouli_lib.admin.json_api.common import ApiError, BrowserAuth, body_json, reply
from patchouli_lib.admin.json_api.content import (
    audit,
    operation_key,
    restore_revision,
    save_input,
    save_page,
)
from patchouli_lib.admin.json_api.reads import book_location, found, section_location
from patchouli_lib.admin.json_api.schemas import PageDeleteResult
from patchouli_lib.admin.page_move_service import MasterPageMoveCommand, MasterPageMoveService
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.auth.onboarding import OnboardingTicketService
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.service import utc_microseconds
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveIdempotencyKey
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import canonical_utc_wire
from patchouli_lib.library.guides import update_readme
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import (
    CreateBookInput,
    CreateLibraryInput,
    CreateSectionInput,
    ReadmeMarkdown,
    UpdateBookInput,
    UpdateLibraryInput,
    UpdateSectionInput,
)
from patchouli_lib.library.service import LibraryStructureService


def register_actions(router: APIRouter, auth: BrowserAuth) -> None:
    service = AdminActionService(auth.engine)

    @router.post("/libraries")
    async def create_library(request: Request) -> Response:
        session = auth.write_session(request)
        form = await body_json(request, CreateLibraryInput)
        result = await run_in_threadpool(
            service.create_library,
            form,
            session_fingerprint=session.audit_fingerprint(),
            master_session=session,
        )
        return reply(result, 201)

    @router.post("/libraries/{library_id}/sections")
    async def create_section(request: Request, library_id: str) -> Response:
        session = auth.write_session(request)
        form = await body_json(request, CreateSectionInput)
        result = await run_in_threadpool(
            service.create_section,
            library_id,
            form,
            session_fingerprint=session.audit_fingerprint(),
            master_session=session,
        )
        return reply(result, 201)

    @router.post("/sections/{section_id}/books")
    async def create_book(request: Request, section_id: str) -> Response:
        session = auth.write_session(request)
        form = await body_json(request, CreateBookInput)
        with auth.read(request) as (connection, _):
            library_id = section_location(connection, section_id)
        result = await run_in_threadpool(
            service.create_book,
            library_id,
            section_id,
            form,
            session_fingerprint=session.audit_fingerprint(),
            master_session=session,
        )
        return reply(result, 201)

    async def edit_structure(request: Request, kind: str, item_id: str) -> Response:
        session = auth.write_session(request)
        raw = await body_json(request)
        readme = (
            TypeAdapter(ReadmeMarkdown).validate_python(raw.pop("readme"))
            if "readme" in raw
            else None
        )
        schemas: dict[str, type[BaseModel]] = {
            "library": UpdateLibraryInput,
            "section": UpdateSectionInput,
            "book": UpdateBookInput,
        }
        form = schemas[kind].model_validate(raw)

        def execute() -> Any:
            with immediate_transaction(auth.engine) as connection:
                auth.authorize(connection, session)
                structure = LibraryStructureService(LibraryRepository(connection))
                section_id = book_id = None
                result: Any
                if kind == "library":
                    library_id = item_id
                    result, changed = structure.update_library(
                        item_id, UpdateLibraryInput.model_validate(raw)
                    )
                elif kind == "section":
                    library_id = section_location(connection, item_id)
                    section_id = item_id
                    result, changed = structure.update_section(
                        library_id, item_id, UpdateSectionInput.model_validate(raw)
                    )
                else:
                    library_id, section_id = book_location(connection, item_id)
                    book_id = item_id
                    result, changed = structure.update_book(
                        library_id, section_id, item_id, UpdateBookInput.model_validate(raw)
                    )
                if readme is not None:
                    changed = changed or result.readme != readme
                    result = update_readme(
                        connection,
                        library_id=library_id,
                        section_id=section_id,
                        book_id=book_id,
                        readme=readme,
                        expected_updated_at=result.updated_at,
                        now=utc_microseconds(),
                    )
                if changed:
                    audit(connection, session, f"{kind}.update", kind, item_id)
                return result

        # Validate before the thread boundary without exposing validation inputs.
        del form
        return reply(await run_in_threadpool(execute))

    @router.put("/libraries/{library_id}")
    async def update_library(request: Request, library_id: str) -> Response:
        return await edit_structure(request, "library", library_id)

    @router.put("/sections/{section_id}")
    async def update_section(request: Request, section_id: str) -> Response:
        return await edit_structure(request, "section", section_id)

    @router.put("/books/{book_id}")
    async def update_book(request: Request, book_id: str) -> Response:
        return await edit_structure(request, "book", book_id)

    @router.post("/books/{book_id}/pages")
    async def create_page(request: Request, book_id: str) -> Response:
        session = auth.write_session(request)
        key = operation_key(request)
        metadata, uploads = await save_input(request)
        return reply(
            await run_in_threadpool(
                save_page, auth, session, key, metadata, uploads, book_id=book_id
            ),
            201,
        )

    @router.put("/libraries/{library_id}/pages/{page_id}/files")
    async def revise_page(request: Request, library_id: str, page_id: str) -> Response:
        session = auth.write_session(request)
        key = operation_key(request)
        metadata, uploads = await save_input(request)
        return reply(
            await run_in_threadpool(
                save_page,
                auth,
                session,
                key,
                metadata,
                uploads,
                library_id=library_id,
                page_id=page_id,
            )
        )

    def page_location(request: Request, library_id: str, page_id: str) -> tuple[str, str]:
        with auth.read(request) as (connection, _):
            page = found(ContentRepository(connection).get_page(library_id, page_id))
            return page.section_id, page.book_id

    @router.delete("/libraries/{library_id}/pages/{page_id}", response_model=PageDeleteResult)
    async def delete_page(request: Request, library_id: str, page_id: str) -> Response:
        session = auth.write_session(request)
        form = await body_json(request, MasterDeletePageFormInput)
        section_id, book_id = page_location(request, library_id, page_id)
        restore_etag = await run_in_threadpool(
            service.delete_page_as_master,
            library_id,
            section_id,
            book_id,
            page_id,
            form,
            master_session=session,
        )
        return reply(PageDeleteResult(changed=True, restore_etag=restore_etag))

    @router.post("/libraries/{library_id}/pages/{page_id}/restore")
    async def restore_page(request: Request, library_id: str, page_id: str) -> Response:
        session = auth.write_session(request)
        form = await body_json(request, MasterRestoreArchiveFormInput)
        section_id, _ = page_location(request, library_id, page_id)
        await run_in_threadpool(
            service.restore_archive_page_as_master,
            library_id,
            section_id,
            page_id,
            form,
            master_session=session,
        )
        return reply({"changed": True})

    @router.post("/libraries/{library_id}/pages/{page_id}/move")
    async def move(request: Request, library_id: str, page_id: str) -> Response:
        session = auth.write_session(request)
        key = ArchiveIdempotencyKey(key_digest=operation_key(request))
        form = MasterPageMoveCommand.model_validate(
            {**await body_json(request), "library_id": library_id, "page_id": page_id}
        )
        result = await run_in_threadpool(
            MasterPageMoveService(auth.engine).move_page, form, key, master_session=session
        )
        return reply({"changed": True, "replayed": result.replayed, "page_id": page_id})

    @router.post("/libraries/{library_id}/pages/{page_id}/revisions/{revision_number}/restore")
    async def restore_page_revision(
        request: Request, library_id: str, page_id: str, revision_number: int
    ) -> Response:
        session = auth.write_session(request)
        key = operation_key(request)
        metadata = await body_json(request)
        return reply(
            await run_in_threadpool(
                restore_revision, auth, session, key, library_id, page_id, revision_number, metadata
            )
        )

    @router.post("/libraries/{library_id}/pages/{page_id}/occurrence")
    async def occurrence(request: Request, library_id: str, page_id: str) -> Response:
        session = auth.write_session(request)
        form = await body_json(request, MasterCorrectOccurrenceInput)
        section_id, book_id = page_location(request, library_id, page_id)
        changed = await run_in_threadpool(
            service.correct_page_occurrence_as_master,
            library_id,
            section_id,
            book_id,
            page_id,
            form,
            master_session=session,
        )
        return reply({"changed": changed})

    @router.post("/libraries/{library_id}/pages/{page_id}/tags")
    async def page_tag(request: Request, library_id: str, page_id: str) -> Response:
        session = auth.write_session(request)
        form = await body_json(request, MasterPageTagFormInput)
        section_id, book_id = page_location(request, library_id, page_id)
        changed = await run_in_threadpool(
            service.set_page_tag_as_master,
            library_id,
            section_id,
            book_id,
            page_id,
            form,
            master_session=session,
        )
        return reply({"changed": changed})

    @router.post("/libraries/{library_id}/tags")
    async def create_tag(request: Request, library_id: str) -> Response:
        session = auth.write_session(request)
        form = await body_json(request, MasterTagFormInput)
        tag_id, created = await run_in_threadpool(
            service.create_tag_as_master, library_id, form, master_session=session
        )
        return reply({"id": tag_id, "created": created}, 201 if created else 200)

    @router.post("/tags/batch")
    async def tags_batch(request: Request) -> Response:
        session = auth.write_session(request)
        form = await body_json(request, MasterTagBatchInput)
        return reply(
            {
                "changed": await run_in_threadpool(
                    service.manage_tags_as_master, form, master_session=session
                )
            }
        )

    @router.post("/credentials")
    async def create_credential(request: Request) -> Response:
        session = auth.write_session(request)
        form = await body_json(request, MasterProvisionAgentInput)
        result = await run_in_threadpool(
            service.provision_agent_as_master, form, master_session=session
        )
        return reply(
            {
                "library_id": result.library_id,
                "caller_id": result.caller_id,
                "credential_id": result.credential_id,
                "token": result.value,
            },
            201,
        )

    @router.put("/credentials/{caller_id}")
    async def update_credential(request: Request, caller_id: str, library_id: str) -> Response:
        session = auth.write_session(request)
        form = await body_json(request, MasterUpdateAgentInput)
        return reply(
            {
                "changed": await run_in_threadpool(
                    service.update_agent_as_master,
                    library_id,
                    caller_id,
                    form,
                    master_session=session,
                )
            }
        )

    @router.put("/credentials/{caller_id}/{credential_id}/scope")
    async def scope(
        request: Request, caller_id: str, credential_id: str, library_id: str
    ) -> Response:
        session = auth.write_session(request)
        form = await body_json(request, MasterSetAgentLibraryScopeInput)
        return reply(
            {
                "changed": await run_in_threadpool(
                    service.set_agent_scope_as_master,
                    library_id,
                    caller_id,
                    credential_id,
                    form,
                    master_session=session,
                )
            }
        )

    @router.post("/credentials/{caller_id}/{credential_id}/rotate")
    async def rotate(
        request: Request, caller_id: str, credential_id: str, library_id: str
    ) -> Response:
        session = auth.write_session(request)
        form = await body_json(request, MasterRotateAgentCredentialInput)
        result = await run_in_threadpool(
            service.rotate_agent_credential_as_master,
            library_id,
            caller_id,
            credential_id,
            form,
            master_session=session,
        )
        return reply(
            {
                "library_id": result.library_id,
                "caller_id": result.caller_id,
                "credential_id": result.credential_id,
                "token": result.value,
            }
        )

    @router.post("/credentials/{caller_id}/{credential_id}/revoke")
    async def revoke(
        request: Request, caller_id: str, credential_id: str, library_id: str
    ) -> Response:
        session = auth.write_session(request)
        await body_json(request)
        return reply(
            {
                "changed": await run_in_threadpool(
                    service.revoke_agent_credential_as_master,
                    library_id,
                    caller_id,
                    credential_id,
                    master_session=session,
                )
            }
        )

    @router.post("/credentials/{caller_id}/disable")
    async def disable(request: Request, caller_id: str, library_id: str) -> Response:
        session = auth.write_session(request)
        await body_json(request)
        return reply(
            {
                "changed": await run_in_threadpool(
                    service.disable_agent_as_master, library_id, caller_id, master_session=session
                )
            }
        )

    @router.post("/credentials/{caller_id}/{credential_id}/reveal")
    async def reveal(
        request: Request, caller_id: str, credential_id: str, library_id: str
    ) -> Response:
        session = auth.write_session(request)
        await body_json(request)

        def execute() -> str:
            with immediate_transaction(auth.engine) as connection:
                auth.authorize(connection, session)
                repository = AuthRepository(connection)
                caller = found(repository.get_caller(library_id, caller_id))
                credential = found(repository.get_credential(library_id, caller_id, credential_id))
                now = utc_microseconds()
                if (
                    caller.kind.value != "agent"
                    or caller.disabled_at is not None
                    or credential.created_at > now
                    or (credential.expires_at is not None and credential.expires_at <= now)
                    or credential.revoked_at is not None
                    or credential.rotated_at is not None
                ):
                    raise ApiError(410, "credential_inactive")
                value = repository.get_active_agent_token_value(
                    library_id, caller_id, credential_id, active_at=now
                )
                if value is None:
                    raise ApiError(410, "token_unavailable")
                audit(connection, session, "auth.agent_token.reveal", "credential", credential_id)
                return value

        return reply({"token": await run_in_threadpool(execute)})

    @router.post("/onboarding-tickets")
    async def ticket(request: Request) -> Response:
        session = auth.write_session(request)
        if await body_json(request) != {}:
            raise ApiError(422, "invalid_input")
        result = await run_in_threadpool(
            OnboardingTicketService(auth.engine).create, master_session=session
        )
        return reply(
            {
                "code": result.code,
                "expires_at": canonical_utc_wire(result.expires_at),
                "remaining_ms": max(0, (result.expires_at - utc_microseconds()) // 1000),
                "origin": str(request.base_url).rstrip("/"),
            },
            201,
        )
