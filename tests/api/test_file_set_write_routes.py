"""Synthetic HTTP checks for the unregistered unified file-set write routes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from patchouli_lib.api.contracts import PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import ProblemDetails, install_api_exception_handlers
from patchouli_lib.api.file_set_read_routes import create_file_set_read_router
from patchouli_lib.api.file_set_write_routes import create_file_set_write_router
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, RequestIDMiddleware
from patchouli_lib.auth.models import AuditEvent
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller, NewSectionGrant, SectionAction
from patchouli_lib.auth.service import CredentialIssuer
from patchouli_lib.backup.validation import validate_database
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.models import Page, PageSource, Revision, RevisionFile
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.idempotency.models import IdempotencyRecord
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService

ROOT = Path(__file__).resolve().parents[2]
REQUEST_ID = f"req_{'8' * 32}"
NOW = 1_776_000_000_000_000
SOURCE = {"kind": "synthetic", "locator": "urn:synthetic:file-set-http"}
OCCURRED_AT = "2026-08-13T10:00:00.123456Z"


@dataclass(frozen=True, slots=True)
class FileSetHttp:
    engine: Engine
    database_path: Path
    library_id: str
    section_id: str
    book_id: str
    writer: str
    reader: str


def _constant_id(value: str) -> Callable[[], str]:
    return lambda: value


@pytest.fixture
def file_set_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FileSetHttp]:
    database_path = tmp_path / "file-set-http.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    engine = build_engine(database_url)
    try:
        with immediate_transaction(engine) as connection:
            ids = iter(("1" * 32, "2" * 32, "3" * 32))
            structure = LibrarySeedService(
                LibraryRepository(connection),
                id_factory=lambda: next(ids),
                clock=lambda: NOW - 1_000_000,
            ).seed(
                LibraryStructureSeed(
                    library_name="Synthetic file-set library",
                    section_name="Synthetic section",
                    book_name="Synthetic book",
                )
            )
            auth = AuthRepository(connection)
            tokens: list[str] = []
            for caller_id, credential_id, actions in (
                (
                    "a" * 32,
                    "b" * 32,
                    (SectionAction.ARCHIVE_WRITE, SectionAction.PAGE_READ),
                ),
                ("c" * 32, "d" * 32, (SectionAction.PAGE_READ,)),
            ):
                caller = auth.add_caller(
                    NewCaller(
                        id=caller_id,
                        library_id=structure.library.id,
                        kind=CallerKind.AGENT,
                        name=f"Synthetic Agent {caller_id[0]}",
                        created_at=NOW - 1_000_000,
                        updated_at=NOW - 1_000_000,
                    )
                )
                tokens.append(
                    CredentialIssuer(
                        auth,
                        id_factory=_constant_id(credential_id),
                        clock=lambda: NOW - 1_000_000,
                    )
                    .issue(caller, expires_at=NOW + 1_000_000_000)
                    .value
                )
                for action in actions:
                    auth.add_grant(
                        NewSectionGrant(
                            library_id=structure.library.id,
                            caller_id=caller_id,
                            section_id=structure.section.id,
                            action=action,
                            created_at=NOW - 1_000_000,
                        )
                    )
        yield FileSetHttp(
            engine=engine,
            database_path=database_path,
            library_id=structure.library.id,
            section_id=structure.section.id,
            book_id=structure.book.id,
            writer=tokens[0],
            reader=tokens[1],
        )
    finally:
        engine.dispose()


def _app(scope: FileSetHttp) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_api_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware, request_id_factory=lambda: REQUEST_ID)
    app.include_router(create_file_set_write_router(scope.engine, clock=lambda: NOW))
    app.include_router(create_file_set_read_router(scope.engine, clock=lambda: NOW))
    return app


def _create_path(scope: FileSetHttp) -> str:
    return (
        f"/api/v1/libraries/{scope.library_id}/sections/{scope.section_id}"
        f"/books/{scope.book_id}/pages"
    )


def _page_path(scope: FileSetHttp, page_id: str) -> str:
    return f"/api/v1/libraries/{scope.library_id}/sections/{scope.section_id}/pages/{page_id}"


def _multipart(
    metadata: object,
    files: Sequence[tuple[str, bytes]],
    *,
    boundary: str = "synthetic-file-set-boundary",
) -> tuple[str, bytes]:
    metadata_bytes = (
        metadata
        if isinstance(metadata, bytes)
        else json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    chunks = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="metadata"\r\n',
        b"Content-Type: application/json; charset=utf-8\r\n\r\n",
        metadata_bytes,
        b"\r\n",
    ]
    for name, content in files:
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                content,
                b"\r\n",
            )
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def _create(
    client: TestClient,
    scope: FileSetHttp,
    *,
    key: str,
    files: Sequence[tuple[str, bytes]],
    metadata: object | None = None,
    token: str | None = None,
    path: str | None = None,
) -> Any:
    media, body = _multipart(
        metadata
        if metadata is not None
        else {"title": "Synthetic Page", "occurred_at": OCCURRED_AT, "source": SOURCE},
        files,
    )
    return client.post(
        path or _create_path(scope),
        headers=[
            ("Authorization", f"Bearer {token or scope.writer}"),
            ("Idempotency-Key", key),
            ("Content-Type", media),
        ],
        content=body,
    )


def _revise(
    client: TestClient,
    scope: FileSetHttp,
    page_id: str,
    *,
    key: str,
    etag: str | None,
    files: Sequence[tuple[str, bytes]],
    token: str | None = None,
    metadata: object | None = None,
) -> Any:
    media, body = _multipart(metadata if metadata is not None else {"source": SOURCE}, files)
    headers = [
        ("Authorization", f"Bearer {token or scope.writer}"),
        ("Idempotency-Key", key),
        ("Content-Type", media),
    ]
    if etag is not None:
        headers.append(("If-Match", etag))
    return client.post(
        f"{_page_path(scope, page_id)}/file-revisions", headers=headers, content=body
    )


def _problem(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    assert response.headers[REQUEST_ID_HEADER] == REQUEST_ID
    body = ProblemDetails.model_validate(response.json())
    assert body.status == status
    assert body.code == code
    assert body.request_id == REQUEST_ID


def _counts(scope: FileSetHttp) -> tuple[int, ...]:
    with scope.engine.connect() as connection:
        return tuple(
            connection.scalar(select(func.count()).select_from(model)) or 0
            for model in (Page, Revision, RevisionFile, PageSource, AuditEvent, IdempotencyRecord)
        )


def test_write_routes_describe_distinct_json_metadata_and_repeated_binary_files(
    file_set_http: FileSetHttp,
) -> None:
    openapi = _app(file_set_http).openapi()
    paths = openapi["paths"]
    assert openapi["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }
    create_path = "/api/v1/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages"
    revise_path = (
        "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/file-revisions"
    )
    for path, required_metadata, metadata_fields in (
        (create_path, {"title", "source"}, {"title", "source", "occurred_at"}),
        (revise_path, {"source"}, {"source"}),
    ):
        body = paths[path]["post"]["requestBody"]
        assert body["required"] is True
        assert "first part" in body["description"]
        assert set(body["content"]) == {"multipart/form-data"}
        multipart = body["content"]["multipart/form-data"]
        assert multipart["encoding"]["metadata"]["contentType"] == "application/json"
        schema = multipart["schema"]
        assert set(schema["required"]) == {"metadata", "file"}
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == {"metadata", "file"}
        metadata = schema["properties"]["metadata"]
        assert set(metadata["required"]) == required_metadata
        assert set(metadata["properties"]) == metadata_fields
        assert metadata["additionalProperties"] is False
        source = metadata["properties"]["source"]
        assert source["required"] == ["kind"]
        assert set(source["properties"]) == {"kind", "locator", "captured_at"}
        assert source["properties"]["captured_at"]["type"] == ["integer", "null"]
        assert source["additionalProperties"] is False
        files = schema["properties"]["file"]
        assert files["type"] == "array"
        assert files["minItems"] == 1
        assert files["items"] == {"type": "string", "format": "binary"}
        operation = paths[path]["post"]
        assert operation["security"] == [{"BearerAuth": []}]
        headers = {
            parameter["name"]: parameter
            for parameter in operation["parameters"]
            if parameter["in"] == "header"
        }
        assert set(headers) == (
            {"Idempotency-Key", "If-Match"} if path == revise_path else {"Idempotency-Key"}
        )
        assert all(parameter["required"] is True for parameter in headers.values())
        assert headers["Idempotency-Key"]["schema"]["maxLength"] == 256
        if path == revise_path:
            assert "page-v" in headers["If-Match"]["schema"]["pattern"]
        status = "200" if path == revise_path else "201"
        success = operation["responses"][status]
        assert set(success["content"]) == {"application/json"}
        response_ref = success["content"]["application/json"]["schema"]["$ref"]
        response_schema = openapi["components"]["schemas"][response_ref.rsplit("/", 1)[-1]]
        expected_fields = (
            {
                "changed",
                "section_id",
                "page_id",
                "revision_id",
                "revision_number",
                "snapshot_sha256",
                "files",
            }
            if path == revise_path
            else {
                "section_id",
                "book_id",
                "page_id",
                "revision_id",
                "revision_number",
                "occurred_at",
                "occurrence_defaulted",
                "snapshot_sha256",
                "files",
            }
        )
        assert set(response_schema["required"]) == expected_fields
        assert set(response_schema["properties"]) == expected_fields
        response_headers = success["headers"]
        assert {"ETag", "Cache-Control", "X-Request-ID", "Idempotency-Replayed"} <= set(
            response_headers
        )
        assert all(
            response_headers[name]["required"] for name in ("ETag", "Cache-Control", "X-Request-ID")
        )
        assert response_headers["Idempotency-Replayed"].get("required") is not True
        if path == create_path:
            assert response_headers["Location"]["required"] is True
        else:
            assert "Location" not in response_headers
    assert paths[create_path]["post"]["requestBody"] != paths[revise_path]["post"]["requestBody"]


def test_openapi_security_declaration_does_not_replace_strict_runtime_headers(
    file_set_http: FileSetHttp,
) -> None:
    media, body = _multipart(
        {"title": "Synthetic Page", "source": SOURCE}, (("content.md", b"# Synthetic\n"),)
    )
    path = _create_path(file_set_http)
    with TestClient(_app(file_set_http), raise_server_exceptions=False) as client:
        missing_auth = client.post(
            path,
            headers=[("Idempotency-Key", "missing-auth"), ("Content-Type", media)],
            content=body,
        )
        _problem(missing_auth, 401, "authentication_required")
        duplicate_auth = client.post(
            path,
            headers=[
                ("Authorization", f"Bearer {file_set_http.writer}"),
                ("Authorization", f"Bearer {file_set_http.writer}"),
                ("Idempotency-Key", "duplicate-auth"),
                ("Content-Type", media),
            ],
            content=body,
        )
        _problem(duplicate_auth, 401, "invalid_token")
        missing_key = client.post(
            path,
            headers=[
                ("Authorization", f"Bearer {file_set_http.writer}"),
                ("Content-Type", media),
            ],
            content=body,
        )
        _problem(missing_key, 422, "request_validation_failed")
    assert _counts(file_set_http) == (0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    "files",
    [
        (("content.md", b"# Synthetic document\n"),),
        (("content.md", b"# Mixed document\n"), ("figure.png", b"\x89PNG\x00\xff")),
        (("slides.pptx", b"\x00\x01\xff"),),
    ],
    ids=("one-markdown", "mixed-files", "binary-only"),
)
def test_all_file_shapes_use_the_same_create_read_download_and_backup_path(
    file_set_http: FileSetHttp, files: tuple[tuple[str, bytes], ...]
) -> None:
    manifest = build_file_manifest(files)
    with TestClient(_app(file_set_http), raise_server_exceptions=False) as client:
        created = _create(client, file_set_http, key="synthetic-create", files=files)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["revision_number"] == 1
        assert body["snapshot_sha256"] == manifest.snapshot_sha256.hex()
        assert body["files"] == [
            {
                "filename": entry.name,
                "size_bytes": entry.content_size_bytes,
                "content_sha256": entry.content_sha256.hex(),
            }
            for entry in manifest.files
        ]
        assert created.headers["ETag"].startswith('"page-v2-')
        assert created.headers["Location"].endswith(
            f"/pages/{body['page_id']}/revisions/{body['revision_id']}/files"
        )
        read = client.get(
            created.headers["Location"],
            headers={"Authorization": f"Bearer {file_set_http.reader}"},
        )
        assert read.status_code == 200, read.text
        assert read.json()["snapshot_sha256"] == manifest.snapshot_sha256.hex()
        assert read.json()["files"] == body["files"]
        for name, content in files:
            download = client.get(
                f"{created.headers['Location']}/{name}",
                headers={"Authorization": f"Bearer {file_set_http.reader}"},
            )
            assert download.status_code == 200, download.text
            assert download.content == content
            assert download.headers["Content-Type"] == "application/octet-stream"
            assert download.headers["Content-Disposition"].startswith("attachment;")
            assert download.headers["X-Content-Type-Options"] == "nosniff"
    validate_database(file_set_http.database_path)


def test_revisions_replay_noop_and_historical_reads_are_exact(
    file_set_http: FileSetHttp,
) -> None:
    first_files = (("content.md", b"# First\n"), ("figure.png", b"\x89PNG"))
    next_files = (("content.md", b"# Second\n"),)
    with TestClient(_app(file_set_http), raise_server_exceptions=False) as client:
        first = _create(client, file_set_http, key="first-key", files=first_files)
        assert first.status_code == 201, first.text
        page_id = first.json()["page_id"]
        first_counts = _counts(file_set_http)
        replay = _create(client, file_set_http, key="first-key", files=tuple(reversed(first_files)))
        assert replay.status_code == 201, replay.text
        assert replay.content == first.content
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.headers["ETag"] == first.headers["ETag"]
        assert _counts(file_set_http) == first_counts
        conflict = _create(
            client, file_set_http, key="first-key", files=(("content.md", b"Changed"),)
        )
        _problem(conflict, 409, "idempotency_mismatch")

        revised = _revise(
            client,
            file_set_http,
            page_id,
            key="second-key",
            etag=first.headers["ETag"],
            files=next_files,
        )
        assert revised.status_code == 200, revised.text
        assert revised.json()["changed"] is True
        assert revised.json()["revision_number"] == 2
        assert revised.json()["files"] == [
            {
                "filename": "content.md",
                "size_bytes": len(next_files[0][1]),
                "content_sha256": hashlib.sha256(next_files[0][1]).hexdigest(),
            }
        ]
        assert revised.headers["ETag"] != first.headers["ETag"]
        changed_counts = _counts(file_set_http)
        exact_replay = _revise(
            client,
            file_set_http,
            page_id,
            key="second-key",
            etag=first.headers["ETag"],
            files=next_files,
        )
        assert exact_replay.status_code == 200
        assert exact_replay.content == revised.content
        assert exact_replay.headers["Idempotency-Replayed"] == "true"
        assert _counts(file_set_http) == changed_counts
        no_change = _revise(
            client,
            file_set_http,
            page_id,
            key="no-change-key",
            etag=revised.headers["ETag"],
            files=next_files,
        )
        assert no_change.status_code == 200, no_change.text
        assert no_change.json()["changed"] is False
        assert no_change.json()["revision_number"] == 2
        assert no_change.json()["files"] == revised.json()["files"]
        assert no_change.headers["ETag"] == revised.headers["ETag"]
        assert _counts(file_set_http) == (
            changed_counts[0],
            changed_counts[1],
            changed_counts[2],
            changed_counts[3],
            changed_counts[4],
            changed_counts[5] + 1,
        )
        old = client.get(
            first.headers["Location"],
            headers={"Authorization": f"Bearer {file_set_http.reader}"},
        )
        new = client.get(
            f"{_page_path(file_set_http, page_id)}/revisions/{revised.json()['revision_id']}/files",
            headers={"Authorization": f"Bearer {file_set_http.reader}"},
        )
        assert old.status_code == new.status_code == 200
        assert [entry["filename"] for entry in old.json()["files"]] == [
            "content.md",
            "figure.png",
        ]
        assert [entry["filename"] for entry in new.json()["files"]] == ["content.md"]
        assert new.json()["files"] == revised.json()["files"]
        assert (
            new.json()["files"][0]["content_sha256"] == hashlib.sha256(next_files[0][1]).hexdigest()
        )
    validate_database(file_set_http.database_path)


def test_authorization_preconditions_and_validation_fail_without_writes(
    file_set_http: FileSetHttp,
) -> None:
    files = (("content.md", b"# Synthetic\n"),)
    with TestClient(_app(file_set_http), raise_server_exceptions=False) as client:
        forbidden = _create(
            client, file_set_http, key="reader-key", files=files, token=file_set_http.reader
        )
        _problem(forbidden, 403, "insufficient_scope")
        foreign = _create(
            client,
            file_set_http,
            key="foreign-key",
            files=files,
            path=_create_path(file_set_http).replace(file_set_http.library_id, "f" * 32, 1),
        )
        _problem(foreign, 404, "resource_not_found")
        no_book = _create(
            client,
            file_set_http,
            key="no-book-key",
            files=files,
            path=_create_path(file_set_http).replace(file_set_http.book_id, "f" * 32, 1),
        )
        _problem(no_book, 404, "resource_not_found")
        bad_metadata = _create(
            client,
            file_set_http,
            key="bad-metadata-key",
            files=files,
            metadata={"title": "Synthetic Page", "source": SOURCE, "unexpected": True},
        )
        _problem(bad_metadata, 422, "request_validation_failed")
        bad_time = _create(
            client,
            file_set_http,
            key="bad-time-key",
            files=files,
            metadata={"title": "Synthetic Page", "occurred_at": "not-a-date", "source": SOURCE},
        )
        _problem(bad_time, 422, "request_validation_failed")
        assert _counts(file_set_http) == (0, 0, 0, 0, 0, 0)
        created = _create(client, file_set_http, key="valid-key", files=files)
        assert created.status_code == 201, created.text
        page_id = created.json()["page_id"]
        baseline = _counts(file_set_http)
        no_precondition = _revise(
            client, file_set_http, page_id, key="missing-etag", etag=None, files=files
        )
        _problem(no_precondition, 428, "precondition_required")
        weak_precondition = _revise(
            client,
            file_set_http,
            page_id,
            key="weak-etag",
            etag=f"W/{created.headers['ETag']}",
            files=files,
        )
        _problem(weak_precondition, 422, "request_validation_failed")
        stale = _revise(
            client,
            file_set_http,
            page_id,
            key="stale-etag",
            etag='"page-v2-' + "f" * 64 + '"',
            files=files,
        )
        _problem(stale, 412, "revision_conflict")
        read_only = _revise(
            client,
            file_set_http,
            page_id,
            key="read-only",
            etag=created.headers["ETag"],
            files=files,
            token=file_set_http.reader,
        )
        _problem(read_only, 403, "insufficient_scope")
        assert _counts(file_set_http) == baseline
        for response in (forbidden, foreign, no_book, bad_metadata, bad_time, stale, read_only):
            for secret in (file_set_http.writer, file_set_http.reader, SOURCE["locator"]):
                assert secret not in response.text
