from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from time import time_ns

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from patchouli_lib.app import create_app
from patchouli_lib.auth.models import AuditEvent
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller, NewSectionGrant, SectionAction
from patchouli_lib.auth.service import CredentialIssuer
from patchouli_lib.config import Settings
from patchouli_lib.content.models import Page, PageOccurrenceCorrection, Revision
from patchouli_lib.content.service import legacy_page_current_etag
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.models import Base


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "environment": "test",
            "database_url": f"sqlite:///{(tmp_path / 'agent-access.db').as_posix()}",
            "retrieval_cursor_signing_secret": "synthetic-cursor-secret-value-32-bytes",
        }
    )


def _seed_agent(engine: Engine) -> tuple[str, str, str]:
    Base.metadata.create_all(engine)
    now = time_ns() // 1_000
    with immediate_transaction(engine) as connection:
        identifiers = iter(("1" * 32, "2" * 32, "3" * 32))
        structure = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: now,
        ).seed(
            LibraryStructureSeed(
                library_name="Synthetic Integrated Library",
                section_name="Synthetic Integrated Section",
                book_name="Synthetic Integrated Book",
            )
        )
        auth = AuthRepository(connection)
        caller = auth.add_caller(
            NewCaller(
                id="4" * 32,
                library_id=structure.library.id,
                kind=CallerKind.AGENT,
                name="Synthetic Integrated Agent",
                created_at=now,
                updated_at=now,
            )
        )
        token = (
            CredentialIssuer(
                auth,
                id_factory=lambda: "5" * 32,
                clock=lambda: now,
            )
            .issue(caller, expires_at=now + 3_600_000_000)
            .value
        )
        auth.add_grant(
            NewSectionGrant(
                library_id=structure.library.id,
                caller_id=caller.id,
                section_id=structure.section.id,
                action=SectionAction.ARCHIVE_WRITE,
                created_at=now,
            )
        )
        auth.add_grant(
            NewSectionGrant(
                library_id=structure.library.id,
                caller_id=caller.id,
                section_id=structure.section.id,
                action=SectionAction.QUERY,
                created_at=now,
            )
        )
        auth.add_grant(
            NewSectionGrant(
                library_id=structure.library.id,
                caller_id=caller.id,
                section_id=structure.section.id,
                action=SectionAction.PAGE_READ,
                created_at=now,
            )
        )
    return structure.section.id, structure.book.id, token


def _multipart(metadata: object, content: bytes, *, boundary: str) -> tuple[str, bytes]:
    metadata_bytes = json.dumps(
        metadata,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    body = b"".join(
        (
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="metadata"\r\n',
            b"Content-Type: application/json\r\n\r\n",
            metadata_bytes,
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="content"\r\n',
            b"Content-Type: text/markdown; charset=utf-8\r\n\r\n",
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    return f"multipart/form-data; boundary={boundary}", body


def _agent_headers(token: str, key: str, content_type: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
        "Content-Type": content_type,
    }


def _file_set_multipart(
    metadata: object,
    files: Sequence[tuple[str, bytes]],
) -> tuple[str, bytes]:
    boundary = "synthetic-integrated-file-set"
    chunks = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="metadata"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(metadata, separators=(",", ":")).encode(),
        b"\r\n",
    ]
    for filename, content in files:
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                content,
                b"\r\n",
            )
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def test_application_registers_exact_agent_access_routes(tmp_path: Path) -> None:
    application = create_app(_settings(tmp_path))
    try:
        routes = {
            (path, method.upper())
            for path, operations in application.openapi()["paths"].items()
            if path.startswith("/api/v1")
            for method in operations
        }
        assert routes == {
            ("/api/v1/capabilities", "GET"),
            ("/api/v1/auth/whoami", "GET"),
            ("/api/v1/agent/skill/manifest", "GET"),
            ("/api/v1/agent/skill/files/{resource_path}", "GET"),
            (
                "/api/v1/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages",
                "POST",
            ),
            (
                "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}",
                "GET",
            ),
            (
                "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}"
                "/file-revisions",
                "POST",
            ),
            (
                "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/revisions",
                "GET",
            ),
            (
                "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}"
                "/revisions/{revision_id}/files",
                "GET",
            ),
            (
                "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}"
                "/revisions/{revision_id}/files/{file_name}",
                "GET",
            ),
            ("/api/v1/libraries/{library_id}/tags", "GET"),
            ("/api/v1/libraries/{library_id}/tags", "POST"),
            ("/api/v1/libraries/{library_id}/tags/{tag_id}/pages", "GET"),
            ("/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/tags", "GET"),
            (
                "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/tags/"
                "{tag_id}",
                "PUT",
            ),
            (
                "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/tags/"
                "{tag_id}",
                "DELETE",
            ),
            (
                "/api/v1/sections/{section_id}/pages/{page_id}/revisions/{revision_number}/files",
                "GET",
            ),
            (
                "/api/v1/sections/{section_id}/pages/{page_id}/revisions/"
                "{revision_number}/files/{filename}",
                "GET",
            ),
            ("/api/v1/sections", "GET"),
            ("/api/v1/sections/{section_id}/books", "GET"),
            ("/api/v1/sections/{section_id}/pages", "GET"),
            ("/api/v1/sections/{section_id}/pages/{page_id}", "GET"),
            ("/api/v1/sections/{section_id}/pages/{page_id}", "DELETE"),
            ("/api/v1/sections/{section_id}/pages/{page_id}/restore", "POST"),
            ("/api/v1/sections/{section_id}/trash", "GET"),
            ("/api/v1/sections/{section_id}/trash/{page_id}", "GET"),
            (
                "/api/v1/sections/{section_id}/pages/{page_id}/revisions/{revision_number}",
                "GET",
            ),
            (
                "/api/v1/sections/{section_id}/books/{book_id}/pages",
                "POST",
            ),
            (
                "/api/v1/sections/{section_id}/pages/{page_id}/revisions",
                "POST",
            ),
            (
                "/api/v1/sections/{section_id}/pages/{page_id}/occurrence",
                "PATCH",
            ),
            ("/api/v1/sections/{section_id}/search", "POST"),
            ("/api/v1/search", "POST"),
        }
    finally:
        application.state.engine.dispose()


def test_registered_file_set_routes_share_one_shape_for_markdown_and_multiple_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    application = create_app(settings)
    try:
        section_id, book_id, token = _seed_agent(application.state.engine)
        library_id = "1" * 32
        create_path = f"/api/v1/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages"
        headers = {"Authorization": f"Bearer {token}"}
        source = {"kind": "synthetic", "locator": "urn:synthetic:integrated-file-set"}
        initial_file = ("content.md", b"# Single Markdown\n")
        media, body = _file_set_multipart(
            {"title": "Synthetic File Set", "source": source}, (initial_file,)
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            capabilities = client.get("/api/v1/capabilities", headers=headers)
            assert capabilities.status_code == 200
            assert "file-sets" in capabilities.json()["features"]
            assert capabilities.json()["limits"]["file_set"] == {
                "max_file_bytes": 16 * 1024 * 1024,
                "max_page_bytes": 64 * 1024 * 1024,
                "max_files_per_page": 64,
            }
            created = client.post(
                create_path,
                headers={
                    **headers,
                    "Idempotency-Key": "integrated-create",
                    "Content-Type": media,
                },
                content=body,
            )
            assert created.status_code == 201, created.text
            first = created.json()
            page_path = (
                f"/api/v1/libraries/{library_id}/sections/{section_id}/pages/{first['page_id']}"
            )
            current = client.get(page_path, headers=headers)
            assert current.status_code == 200
            assert current.headers["ETag"] == created.headers["ETag"]
            assert current.json()["files"] == first["files"]
            historical = client.get(created.headers["Location"], headers=headers)
            assert historical.status_code == 200
            assert historical.json()["files"] == first["files"]
            downloaded = client.get(f"{created.headers['Location']}/content.md", headers=headers)
            assert downloaded.status_code == 200
            assert downloaded.content == initial_file[1]
            assert downloaded.headers["Content-Disposition"].startswith("attachment;")
            assert downloaded.headers["X-Content-Type-Options"] == "nosniff"

            media, body = _file_set_multipart(
                {"source": source},
                (("content.md", b"# Mixed Page\n"), ("figure.png", b"\x89PNG\x00\xff")),
            )
            revised = client.post(
                f"{page_path}/file-revisions",
                headers={
                    **headers,
                    "Idempotency-Key": "integrated-revision",
                    "If-Match": current.headers["ETag"],
                    "Content-Type": media,
                },
                content=body,
            )
            assert revised.status_code == 200, revised.text
            assert revised.json()["changed"] is True
            latest = client.get(page_path, headers=headers)
            assert latest.status_code == 200
            assert latest.headers["ETag"] == revised.headers["ETag"]
            assert latest.json()["files"] == revised.json()["files"]
            assert [entry["filename"] for entry in latest.json()["files"]] == [
                "content.md",
                "figure.png",
            ]
            old_revision = client.get(created.headers["Location"], headers=headers)
            assert old_revision.status_code == 200
            assert old_revision.json()["files"] == first["files"]
            legacy_revision = client.get(
                f"/api/v1/sections/{section_id}/pages/{first['page_id']}/revisions/2",
                headers=headers,
            )
            assert legacy_revision.status_code == 409
            assert legacy_revision.json()["code"] == "revision_format_unsupported"
            legacy_media, legacy_body = _multipart(
                {"source": source},
                b"# A legacy client must not overwrite a file set\n",
                boundary="legacy-on-file-set",
            )
            legacy_write = client.post(
                f"/api/v1/sections/{section_id}/pages/{first['page_id']}/revisions",
                headers={
                    **headers,
                    "Idempotency-Key": "legacy-on-file-set",
                    "If-Match": latest.headers["ETag"],
                    "Content-Type": legacy_media,
                },
                content=legacy_body,
            )
            assert legacy_write.status_code == 409
            assert legacy_write.json()["code"] == "revision_format_unsupported"
            assert client.get(page_path, headers=headers).headers["ETag"] == latest.headers["ETag"]
    finally:
        application.state.engine.dispose()


def test_application_does_not_register_retrieval_without_cursor_secret(tmp_path: Path) -> None:
    application = create_app(
        Settings.model_validate(
            {
                "environment": "test",
                "database_url": f"sqlite:///{(tmp_path / 'archive-only.db').as_posix()}",
            }
        )
    )
    try:
        paths = application.openapi()["paths"]
        assert "/api/v1/sections" not in paths
        assert set(paths["/api/v1/sections/{section_id}/pages/{page_id}"]) == {"delete"}
        assert "/api/v1/sections/{section_id}/trash" not in paths
        assert "/api/v1/sections/{section_id}/trash/{page_id}" in paths
        assert "/api/v1/sections/{section_id}/search" in paths
        section_id, _book_id, token = _seed_agent(application.state.engine)
        with TestClient(application, raise_server_exceptions=False) as client:
            capabilities = client.get(
                "/api/v1/capabilities",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert capabilities.status_code == 200
            assert capabilities.json()["features"] == ["archive", "file-sets", "tags"]
            assert (
                client.get(
                    "/api/v1/sections",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code
                == 404
            )
            unavailable = client.post(
                f"/api/v1/sections/{section_id}/search",
                headers={"Authorization": f"Bearer {token}"},
                json={"query": "synthetic query"},
            )
            assert unavailable.status_code == 503
            assert unavailable.headers["content-type"].startswith("application/problem+json")
            assert unavailable.json()["code"] == "search_unavailable"
    finally:
        application.state.engine.dispose()


def test_integrated_archive_create_replay_and_revise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    application: FastAPI = create_app(settings)
    engine: Engine = application.state.engine
    section_id, book_id, token = _seed_agent(engine)
    create_type, create_body = _multipart(
        {
            "title": "Synthetic Integrated Archive",
            "occurred_at": "2026-08-13T05:00:00.123456Z",
            "source": {"kind": "synthetic"},
        },
        b"# Synthetic integrated archive\n",
        boundary="integrated-create-boundary",
    )

    with TestClient(application, raise_server_exceptions=False) as client:
        capabilities = client.get(
            "/api/v1/capabilities",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert capabilities.status_code == 200
        assert capabilities.json()["features"] == ["archive", "file-sets", "retrieval", "tags"]
        assert capabilities.json()["idempotency"] == {
            "content_mutations": True,
            "successful_replay_retention": "indefinite-alpha",
        }

        unavailable = client.post(
            f"/api/v1/sections/{section_id}/search",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "synthetic query"},
        )
        assert unavailable.status_code == 503
        assert unavailable.headers["content-type"].startswith("application/problem+json")
        assert unavailable.json()["code"] == "search_unavailable"

        whoami = client.get(
            "/api/v1/auth/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert whoami.status_code == 200
        assert whoami.json()["caller_id"] == "4" * 32

        create_path = f"/api/v1/sections/{section_id}/books/{book_id}/pages"
        create_headers = _agent_headers(token, "integrated-create-key", create_type)
        created = client.post(create_path, headers=create_headers, content=create_body)
        assert created.status_code == 201
        assert created.headers["cache-control"] == "private, no-store"
        assert created.headers["location"].endswith(created.json()["page"]["page_id"])
        first_etag = created.headers["etag"]
        page_id = created.json()["page"]["page_id"]
        assert created.json()["citation"]["revision_number"] == 1

        replayed = client.post(create_path, headers=create_headers, content=create_body)
        assert replayed.status_code == 201
        assert replayed.headers["idempotency-replayed"] == "true"
        assert replayed.headers["etag"] == first_etag
        assert replayed.json() == created.json()

        revise_type, revise_body = _multipart(
            {"source": {"kind": "synthetic-revision"}},
            b"# Synthetic integrated archive\n\nRevision two.\n",
            boundary="integrated-revise-boundary",
        )
        revised = client.post(
            f"/api/v1/sections/{section_id}/pages/{page_id}/revisions",
            headers={
                **_agent_headers(token, "integrated-revise-key", revise_type),
                "If-Match": first_etag,
            },
            content=revise_body,
        )
        assert revised.status_code == 201
        assert revised.json()["citation"]["revision_number"] == 2
        assert revised.headers["location"] == revised.json()["citation"]["href"]
        assert revised.headers["etag"] != first_etag

        listed_sections = client.get(
            "/api/v1/sections",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert listed_sections.status_code == 200
        assert [item["section_id"] for item in listed_sections.json()["items"]] == [section_id]

        listed_books = client.get(
            f"/api/v1/sections/{section_id}/books",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert listed_books.status_code == 200
        assert [item["book_id"] for item in listed_books.json()["items"]] == [book_id]

        listed_pages = client.get(
            f"/api/v1/sections/{section_id}/pages",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert listed_pages.status_code == 200
        assert listed_pages.json()["items"][0]["citation"]["revision_number"] == 2

        current = client.get(
            f"/api/v1/sections/{section_id}/pages/{page_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert current.status_code == 200
        assert current.headers["etag"] == revised.headers["etag"]
        assert current.json()["citation"]["revision_number"] == 2
        assert current.json()["revision"]["content"] == (
            "# Synthetic integrated archive\n\nRevision two.\n"
        )

        revision_one = client.get(
            f"/api/v1/sections/{section_id}/pages/{page_id}/revisions/1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert revision_one.status_code == 200
        assert revision_one.json()["citation"] == created.json()["citation"]
        assert revision_one.json()["revision"]["content"] == ("# Synthetic integrated archive\n")


def test_correct_occurrence_preserves_page_revision_and_replays_exact_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    application = create_app(settings)
    engine: Engine = application.state.engine
    section_id, book_id, token = _seed_agent(engine)
    media, body = _multipart(
        {
            "title": "Synthetic Date Correction",
            "occurred_at": "2026-08-13T05:00:00.123456Z",
            "source": {"kind": "synthetic"},
        },
        b"# Content unchanged\n",
        boundary="occurrence-create-boundary",
    )
    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            created = client.post(
                f"/api/v1/sections/{section_id}/books/{book_id}/pages",
                headers=_agent_headers(token, "occurrence-create", media),
                content=body,
            )
            assert created.status_code == 201
            page_id = created.json()["page"]["page_id"]
            path = f"/api/v1/sections/{section_id}/pages/{page_id}/occurrence"
            headers = {
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "occurrence-update",
                "If-Match": created.headers["etag"],
            }
            requested = {"occurred_at": "2026-08-14T01:00:00.123456+08:00"}
            changed = client.patch(
                path,
                headers={**headers, "Content-Type": "application/json; charset=utf-8"},
                content=json.dumps(requested).encode("utf-8"),
            )
            assert changed.status_code == 200
            assert changed.headers["etag"].startswith('"page-v2-')
            assert changed.headers["etag"] != created.headers["etag"]
            assert changed.headers["location"] == created.headers["location"]
            assert changed.headers["cache-control"] == "private, no-store"
            assert changed.json()["previous_occurred_at"] == "2026-08-13T05:00:00.123456Z"
            assert changed.json()["occurred_at"] == "2026-08-13T17:00:00.123456Z"
            assert changed.json()["page_id"] == page_id
            assert changed.json()["current_revision_number"] == 1
            assert "content" not in changed.json()

            current = client.get(
                f"/api/v1/sections/{section_id}/pages/{page_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert current.status_code == 200
            assert current.headers["etag"] == changed.headers["etag"]
            assert current.json()["page"]["occurred_at"] == changed.json()["occurred_at"]
            assert current.json()["revision"]["content"] == "# Content unchanged\n"

            replay = client.patch(path, headers=headers, json=requested)
            assert replay.status_code == 200
            assert replay.headers["idempotency-replayed"] == "true"
            assert replay.headers["etag"] == changed.headers["etag"]
            assert replay.json() == changed.json()

            assert (
                client.patch(
                    path,
                    headers=headers,
                    json={"occurred_at": "2026-08-15T01:00:00Z"},
                ).json()["code"]
                == "idempotency_mismatch"
            )
            stale = client.patch(
                path,
                headers={**headers, "Idempotency-Key": "occurrence-stale"},
                json={"occurred_at": "2026-08-15T01:00:00Z"},
            )
            assert stale.status_code == 412
            assert stale.json()["code"] == "page_conflict"
            with engine.connect() as connection:
                page = connection.execute(select(Page.__table__)).mappings().one()
                old_etag = legacy_page_current_etag(
                    page["page_uid"], page["current_revision_id"], 1
                )
            assert (
                client.patch(
                    path,
                    headers={
                        **headers,
                        "Idempotency-Key": "occurrence-v1",
                        "If-Match": old_etag,
                    },
                    json={"occurred_at": "2026-08-15T01:00:00Z"},
                ).status_code
                == 412
            )
            assert (
                client.patch(
                    path,
                    headers={
                        **headers,
                        "Idempotency-Key": "occurrence-same",
                        "If-Match": changed.headers["etag"],
                    },
                    json={"occurred_at": changed.json()["occurred_at"]},
                ).status_code
                == 409
            )
            for invalid in ("invalid", "2026-02-30T01:00:00Z", 123):
                response = client.patch(
                    path,
                    headers={**headers, "Idempotency-Key": "occurrence-invalid"},
                    json={"occurred_at": invalid},
                )
                assert response.status_code == 422
            assert (
                client.patch(
                    path, headers={"Authorization": f"Bearer {token}"}, json=requested
                ).status_code
                == 422
            )

            with immediate_transaction(engine) as connection:
                auth = AuthRepository(connection)
                page = connection.execute(select(Page.__table__)).mappings().one()
                assert auth.remove_grant(
                    page["library_id"], "4" * 32, section_id, SectionAction.ARCHIVE_WRITE
                )
            denied_replay = client.patch(path, headers=headers, json=requested)
            assert denied_replay.status_code == 403
            with immediate_transaction(engine) as connection:
                auth = AuthRepository(connection)
                credential = auth.get_credential(page["library_id"], "4" * 32, "5" * 32)
                assert credential is not None
                auth.revoke_credential(credential, revoked_at=time_ns() // 1_000)
            assert client.patch(path, headers=headers, json=requested).status_code == 401

        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(Revision)) == 1
            assert (
                connection.scalar(select(func.count()).select_from(PageOccurrenceCorrection)) == 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.action == "content.archive.correct_occurrence")
                )
                == 1
            )
    finally:
        engine.dispose()
