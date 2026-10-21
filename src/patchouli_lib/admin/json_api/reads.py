"""Snapshot-consistent browser projections and exact-version safe files."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.openapi.utils import get_openapi
from sqlalchemy import Connection, select, tuple_
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from patchouli_lib.admin.json_api.common import (
    HEADERS,
    SESSION_COOKIE,
    ApiError,
    BrowserAuth,
    body_json,
    encode,
    reply,
)
from patchouli_lib.admin.legacy_presentation import RevisionKey, resolve_presentations
from patchouli_lib.admin.raster_preview import build_raster_preview
from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.admin.request_log_filters import RequestLogCursorCodec, parse_request_log_filters
from patchouli_lib.content.models import Page, Revision
from patchouli_lib.content.presentation import preview_text
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.library.models import Book, Section
from patchouli_lib.retrieval.cursor import CursorCodec
from patchouli_lib.retrieval.file_set_read import read_verified_revision_snapshot
from patchouli_lib.search.query_v2 import SearchQueryV2Wire
from patchouli_lib.search.service_v2 import SearchResultV2, search_pages_for_master


def section_location(connection: Connection, section_id: str) -> str:
    result = connection.scalar(select(Section.library_id).where(Section.id == section_id))
    if result is None:
        raise ApiError(404, "section_not_found")
    return str(result)


def book_location(connection: Connection, book_id: str) -> tuple[str, str]:
    row = connection.execute(
        select(Book.library_id, Book.section_id).where(Book.id == book_id)
    ).first()
    if row is None:
        raise ApiError(404, "book_not_found")
    return row.library_id, row.section_id


def found[T](value: T | None) -> T:
    if value is None:
        raise ApiError(404, "not_found")
    return value


def current_titles(connection: Connection, library_id: str, page_ids: list[str]) -> dict[str, str]:
    rows = connection.execute(
        select(Page.page_id, Page.page_uid, Page.current_revision_id).where(
            Page.library_id == library_id, Page.page_id.in_(page_ids)
        )
    ).all()
    presentations = resolve_presentations(
        connection, ((library_id, row.page_uid, row.current_revision_id) for row in rows)
    )
    return {
        row.page_id: presentations[
            (library_id, row.page_uid, row.current_revision_id)
        ].display_title
        for row in rows
    }


def activity_payload(connection: Connection, value: Any) -> Any:
    payload = encode(value)
    items = payload if isinstance(payload, list) else payload["items"]
    page_keys = {(item["library_id"], item["page_id"]) for item in items if item["page_id"]}
    pages = (
        {
            (row.library_id, row.page_id): row
            for row in connection.execute(
                select(
                    Page.library_id, Page.page_id, Page.page_uid, Page.current_revision_id
                ).where(tuple_(Page.library_id, Page.page_id).in_(page_keys))
            )
        }
        if page_keys
        else {}
    )
    historical = {
        (
            item["library_id"],
            pages[(item["library_id"], item["page_id"])].page_uid,
            item["revision_number"],
        )
        for item in items
        if (item["library_id"], item["page_id"]) in pages and item["revision_number"] is not None
    }
    revisions = (
        {
            (row.library_id, row.page_uid, row.revision_number): row.revision_id
            for row in connection.execute(
                select(
                    Revision.library_id,
                    Revision.page_uid,
                    Revision.revision_number,
                    Revision.revision_id,
                ).where(
                    tuple_(Revision.library_id, Revision.page_uid, Revision.revision_number).in_(
                        historical
                    )
                )
            )
        }
        if historical
        else {}
    )
    selected: dict[int, RevisionKey] = {}
    for index, item in enumerate(items):
        if item["page_id"] is None:
            if item.get("revision_id") is not None:
                item["stored_title"] = item.get("skill_name") or item["page_title"]
            continue
        page = pages.get((item["library_id"], item["page_id"]))
        if page is None:
            continue
        revision_id = item.get("revision_id") or revisions.get(
            (page.library_id, page.page_uid, item["revision_number"]), page.current_revision_id
        )
        selected[index] = (page.library_id, page.page_uid, revision_id)
    presentations = resolve_presentations(connection, selected.values())
    for index, key in selected.items():
        item = items[index]
        title = presentations[key].display_title
        item["stored_title"] = item["page_title"] = title
    return payload


def register_reads(router: APIRouter, auth: BrowserAuth) -> None:
    @router.get("/openapi.json", include_in_schema=False)
    def schema(request: Request) -> Response:
        with auth.read(request):
            return reply(
                get_openapi(title="PatchouliLib administration", version="1", routes=router.routes)
            )

    @router.get("/dashboard")
    def dashboard(request: Request) -> Response:
        with auth.read(request) as (connection, _):
            model = AdminReadModel.from_connection(connection)
            return reply(
                {
                    "libraries": model.list_libraries(),
                    "activity": activity_payload(connection, model.recent_content_activity()),
                }
            )

    @router.get("/libraries")
    def libraries(request: Request) -> Response:
        with auth.read(request) as (connection, _):
            return reply({"items": AdminReadModel.from_connection(connection).list_libraries()})

    @router.get("/libraries/{library_id}")
    def library(request: Request, library_id: str) -> Response:
        with auth.read(request) as (connection, _):
            return reply(found(AdminReadModel.from_connection(connection).get_library(library_id)))

    @router.get("/sections/{section_id}")
    def section(request: Request, section_id: str) -> Response:
        with auth.read(request) as (connection, _):
            library_id = section_location(connection, section_id)
            return reply(
                found(
                    AdminReadModel.from_connection(connection).get_section(library_id, section_id)
                )
            )

    @router.get("/books/{book_id}")
    def book(request: Request, book_id: str, cursor: str | None = None) -> Response:
        with auth.read(request) as (connection, _):
            library_id, section_id = book_location(connection, book_id)
            view = found(
                AdminReadModel.from_connection(connection).get_book_page(
                    library_id, section_id, book_id, cursor
                )
            )
            payload = encode(view)
            titles = current_titles(connection, library_id, [item.id for item in view.pages])
            for item in payload["pages"]:
                item["stored_title"] = titles[item["id"]]
            return reply(payload)

    @router.get("/libraries/{library_id}/pages/{page_id}")
    def page(
        request: Request,
        library_id: str,
        page_id: str,
        revision_number: int | None = None,
        before_revision_number: int | None = None,
    ) -> Response:
        with auth.read(request) as (connection, session):

            def admitted(current: Connection) -> bool:
                auth.authorize(current, session)
                return True

            view = found(
                AdminReadModel.from_connection(connection).get_page_preview_by_id(
                    library_id,
                    page_id,
                    revision_number,
                    before_revision_number,
                    authorize=admitted,
                )
            )
            record = found(ContentRepository(connection).get_page(library_id, page_id))
            snapshot = view.snapshot
            key = (library_id, record.page_uid, snapshot.revision_id)
            presentation = resolve_presentations(
                connection, [key], manifests={key: snapshot.manifest}
            )[key]
            base = f"/admin/api/libraries/{library_id}/pages/{page_id}"
            files = []
            for entry in snapshot.manifest.files:
                suffix = "?revision_number=" + str(snapshot.revision_number)
                path = quote(entry.name, safe="")
                text = preview_text(entry.content)
                raster = entry.name.lower().endswith((".png", ".jpg", ".jpeg"))
                files.append(
                    {
                        "name": entry.name,
                        "size_bytes": entry.content_size_bytes,
                        "sha256": entry.content_sha256.hex(),
                        "editable": text is not None,
                        "download_href": base + "/files/" + path + suffix,
                        "text_href": base + "/texts/" + path + suffix if text is not None else None,
                        "preview_href": base + "/preview-images/" + path + suffix
                        if raster
                        else None,
                    }
                )
            payload = encode(view.view)
            payload.update(
                revision_number=snapshot.revision_number,
                revision_id=snapshot.revision_id,
                primary_file=presentation.primary_file,
                stored_title=presentation.display_title,
                files=files,
                tag_choices=encode(view.view.tag_choices),
            )
            versions = connection.execute(
                select(Revision.revision_id, Revision.revision_number, Revision.created_at)
                .where(
                    Revision.library_id == library_id,
                    Revision.page_uid == record.page_uid,
                    Revision.revision_number.in_([item.number for item in view.view.revisions]),
                )
                .order_by(Revision.revision_number.desc())
            ).all()
            payload["revisions"] = []
            saved_presentations = resolve_presentations(
                connection,
                ((library_id, record.page_uid, version.revision_id) for version in versions),
                manifests={key: snapshot.manifest},
            )
            for version in versions:
                saved = saved_presentations[(library_id, record.page_uid, version.revision_id)]
                payload["revisions"].append(
                    {
                        "number": version.revision_number,
                        "revision_number": version.revision_number,
                        "revision_id": version.revision_id,
                        "created_at": version.created_at,
                        "primary_file": saved.primary_file,
                        "stored_title": saved.display_title,
                    }
                )
            return reply(payload, headers={"ETag": view.view.current_etag})

    def exact_file(
        request: Request, library_id: str, page_id: str, filename: str, revision_number: int
    ) -> bytes:
        with auth.read(request) as (connection, _):
            page = found(ContentRepository(connection).get_page(library_id, page_id))
            if page.deleted_at is not None:
                raise ApiError(404, "page_not_found")
            revision_id = found(
                connection.scalar(
                    select(Revision.revision_id).where(
                        Revision.library_id == library_id,
                        Revision.page_uid == page.page_uid,
                        Revision.revision_number == revision_number,
                    )
                )
            )
            snapshot = read_verified_revision_snapshot(connection, page, revision_id)
            return found(
                next(
                    (entry.content for entry in snapshot.manifest.files if entry.name == filename),
                    None,
                )
            )

    @router.get("/libraries/{library_id}/pages/{page_id}/files/{filename:path}")
    def download(
        request: Request, library_id: str, page_id: str, filename: str, revision_number: int
    ) -> Response:
        body = exact_file(request, library_id, page_id, filename, revision_number)
        return Response(
            body,
            media_type="application/octet-stream",
            headers={
                **HEADERS,
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe=""),
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @router.get("/libraries/{library_id}/pages/{page_id}/texts/{filename:path}")
    def text(
        request: Request, library_id: str, page_id: str, filename: str, revision_number: int
    ) -> Response:
        value = preview_text(exact_file(request, library_id, page_id, filename, revision_number))
        if value is None:
            raise ApiError(415, "text_unavailable")
        return Response(
            value.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            headers={**HEADERS, "Content-Security-Policy": "sandbox; default-src 'none'"},
        )

    @router.get("/libraries/{library_id}/pages/{page_id}/preview-images/{filename:path}")
    def image(
        request: Request, library_id: str, page_id: str, filename: str, revision_number: int
    ) -> Response:
        preview = build_raster_preview(
            exact_file(request, library_id, page_id, filename, revision_number)
        )
        return Response(
            preview.content,
            media_type=preview.media_type,
            headers={**HEADERS, "Content-Security-Policy": "sandbox; default-src 'none'"},
        )

    @router.get("/tags")
    def tags(request: Request) -> Response:
        with auth.read(request) as (connection, _):
            return reply({"items": AdminReadModel.from_connection(connection).list_tags()})

    @router.get("/libraries/{library_id}/tags/{tag_id}")
    def tag(request: Request, library_id: str, tag_id: str) -> Response:
        with auth.read(request) as (connection, _):
            payload = encode(
                found(
                    AdminReadModel.from_connection(connection).get_library_tag(library_id, tag_id)
                )
            )
            titles = current_titles(
                connection, library_id, [item["id"] for item in payload["pages"]]
            )
            for item in payload["pages"]:
                item["stored_title"] = titles[item["id"]]
            return reply(payload)

    @router.get("/trash")
    def trash(
        request: Request, library_id: str | None = None, before: str | None = None
    ) -> Response:
        with auth.read(request) as (connection, _):
            model = AdminReadModel.from_connection(connection)
            if library_id is None:
                return reply({"libraries": model.list_libraries()})
            view = found(model.list_trash(library_id, before=before))
            payload = encode(view)
            titles = current_titles(
                connection, library_id, [item["id"] for item in payload["pages"]]
            )
            for item in payload["pages"]:
                detail = found(model.get_trash_page(library_id, item["section_id"], item["id"]))
                item["expected_etag"] = detail.restore_etag
                item["restore_etag"] = detail.restore_etag
                item["stored_title"] = titles[item["id"]]
            return reply(payload)

    @router.get("/credentials")
    def credentials(request: Request) -> Response:
        with auth.read(request) as (connection, _):
            return reply({"items": AdminReadModel.from_connection(connection).list_callers()})

    @router.get("/credentials/{caller_id}")
    def credential(request: Request, caller_id: str, library_id: str) -> Response:
        with auth.read(request) as (connection, _):
            return reply(
                found(AdminReadModel.from_connection(connection).get_caller(library_id, caller_id))
            )

    @router.get("/activity")
    def activity(request: Request, before: str | None = None) -> Response:
        with auth.read(request) as (connection, _):
            return reply(
                activity_payload(
                    connection,
                    AdminReadModel.from_connection(connection).content_activity_page(before=before),
                )
            )

    @router.get("/requests")
    def requests(request: Request) -> Response:
        with auth.read(request) as (connection, session):
            filters = parse_request_log_filters(request.query_params)
            secret = auth.settings.admin_session_signing_secret
            assert secret is not None
            codec = RequestLogCursorCodec(
                signing_secret=secret.get_secret_value().encode(),
                session=session,
                filters=filters,
                actor=None,
                page_size=20,
            )
            before = request.query_params.get("before")
            model = AdminReadModel.from_connection(connection)
            result = model.request_log_page(
                filters=filters, before=codec.decode(before) if before else None
            )
            return reply(
                {
                    "items": result.items,
                    "next_cursor": codec.encode(result.next_position)
                    if result.next_position
                    else None,
                    "routes": model.request_log_routes(),
                }
            )

    @router.post("/search")
    async def search(request: Request) -> Response:
        auth.write_session(request)
        query = await body_json(request, SearchQueryV2Wire)
        secret = auth.settings.retrieval_cursor_signing_secret
        codec = CursorCodec(secret.get_secret_value().encode()) if secret else None

        def project(connection: Connection, result: SearchResultV2) -> SearchResultV2:
            page_keys = {(item.library_id, item.page_id) for item in result.items}
            uids = (
                {
                    (row.library_id, row.page_id): row.page_uid
                    for row in connection.execute(
                        select(Page.library_id, Page.page_id, Page.page_uid).where(
                            tuple_(Page.library_id, Page.page_id).in_(page_keys)
                        )
                    )
                }
                if page_keys
                else {}
            )
            presentations = resolve_presentations(
                connection,
                (
                    (item.library_id, uids[(item.library_id, item.page_id)], item.revision_id)
                    for item in result.items
                ),
            )
            items = []
            for item in result.items:
                presentation = presentations[
                    (item.library_id, uids[(item.library_id, item.page_id)], item.revision_id)
                ]
                items.append(replace(item, title=presentation.display_title))
            return SearchResultV2(tuple(items), result.next_cursor)

        result = await run_in_threadpool(
            search_pages_for_master,
            auth.engine,
            request.cookies.get(SESSION_COOKIE, ""),
            auth.codec,
            query.to_query(),
            cursor_codec=codec,
            project=project,
        )
        payload = encode(result)
        for item in payload["items"]:
            item["stored_title"] = item["title"]
        return reply(payload)
