from __future__ import annotations

import json
from pathlib import Path
from time import time_ns

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from patchouli_lib.app import create_app
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller, NewSectionGrant, SectionAction
from patchouli_lib.auth.service import CredentialIssuer
from patchouli_lib.config import Settings
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
            (
                "/api/v1/sections/{section_id}/books/{book_id}/pages",
                "POST",
            ),
            (
                "/api/v1/sections/{section_id}/pages/{page_id}/revisions",
                "POST",
            ),
        }
    finally:
        application.state.engine.dispose()


def test_integrated_archive_create_replay_and_revise(tmp_path: Path) -> None:
    application: FastAPI = create_app(_settings(tmp_path))
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
        assert capabilities.json()["features"] == ["archive"]
        assert capabilities.json()["idempotency"] == {
            "content_mutations": True,
            "successful_replay_retention": "indefinite-alpha",
        }

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
