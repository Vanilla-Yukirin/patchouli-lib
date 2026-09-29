"""Synthetic contract checks for the unregistered unified file-set read router."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from fastapi import FastAPI
from fastapi.testclient import TestClient
from retrieval_read.conftest import (
    CALLER_ID,
    SECOND_QUERY_SECTION_ID,
    RetrievalScope,
)
from retrieval_read.conftest import retrieval_engine as retrieval_engine_fixture
from retrieval_read.conftest import retrieval_scope as retrieval_scope_fixture
from sqlalchemy import Engine
from starlette.requests import Request
from starlette.routing import Route

from patchouli_lib.api.authentication import BearerAuthentication
from patchouli_lib.api.contracts import PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import ApplicationProblem, install_api_exception_handlers
from patchouli_lib.api.file_set_read_routes import _perform_read, create_file_set_read_router
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, RequestIDMiddleware
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
)
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.file_set_service import FileSetRevisionService
from patchouli_lib.content.schemas import NewPageSource
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.retrieval.repository import RetrievalRepository

REQUEST_ID = "req_1234567890abcdef1234567890abcdef"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class FileSetApi:
    engine: Engine
    scope: RetrievalScope
    token: str


@pytest.fixture
def file_set_api(tmp_path: Path) -> Iterator[FileSetApi]:
    engine_factory = cast(
        Callable[[Path], Iterator[Engine]],
        cast(Any, retrieval_engine_fixture).__wrapped__,
    )
    scope_factory = cast(
        Callable[[Engine], RetrievalScope],
        cast(Any, retrieval_scope_fixture).__wrapped__,
    )
    engine_iterator = engine_factory(tmp_path)
    retrieval_engine = next(engine_iterator)
    retrieval_scope = scope_factory(retrieval_engine)
    # create_all() does not execute Alembic 0013's legacy manifest backfill.
    with immediate_transaction(retrieval_engine) as connection:
        connection.exec_driver_sql(
            "INSERT INTO revision_file_sets "
            "(library_id, page_uid, revision_id, revision_number, storage_format, "
            "file_count, total_size_bytes, snapshot_sha256) "
            "SELECT library_id, page_uid, revision_id, revision_number, "
            "'legacy_markdown', 1, content_size_bytes, NULL FROM revisions"
        )
        issued = generate_token()
        AuthRepository(connection).add_credential(
            NewCredential(
                id="d" * 32,
                library_id=retrieval_scope.library_id,
                caller_id=CALLER_ID,
                selector=issued.selector,
                token_version=issued.version,
                verifier=issued.verifier,
                expires_at=10_000_000,
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
    try:
        yield FileSetApi(retrieval_engine, retrieval_scope, issued.value)
    finally:
        with suppress(StopIteration):
            next(engine_iterator)


def _app(api: FileSetApi) -> FastAPI:
    app = FastAPI()
    install_api_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware, request_id_factory=lambda: REQUEST_ID)
    app.include_router(create_file_set_read_router(api.engine, clock=lambda: 2_000_000))
    return app


def _base(api: FileSetApi, *, revision_id: str | None = None) -> str:
    scope = api.scope
    return (
        f"/api/v1/libraries/{scope.library_id}/sections/{scope.query_section_id}"
        f"/pages/{scope.first_page_id}/revisions/{revision_id or scope.first_revision_id}/files"
    )


def _get(api: FileSetApi, path: str, *, authenticated: bool = True) -> Any:
    headers = {"Authorization": f"Bearer {api.token}"} if authenticated else None
    with TestClient(_app(api), raise_server_exceptions=False) as client:
        return client.get(path, headers=headers)


def _problem(response: Any, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    assert response.headers[REQUEST_ID_HEADER] == REQUEST_ID


def _install_file_set(api: FileSetApi) -> tuple[str, dict[str, bytes]]:
    files = {
        "image.png": b"\x89PNG\r\n\x1a\n",
        "note.md": b"# New\n",
        "资料.txt": b"synthetic UTF-8 filename",
    }
    manifest = build_file_manifest(files.items())
    scope = api.scope
    with immediate_transaction(api.engine) as connection:
        connection.exec_driver_sql(
            "DELETE FROM revision_file_sets WHERE library_id = ? AND revision_id = ?",
            (scope.library_id, scope.second_revision_id),
        )
        connection.exec_driver_sql(
            "DELETE FROM revision_files WHERE library_id = ? AND revision_id = ?",
            (scope.library_id, scope.second_revision_id),
        )
        connection.exec_driver_sql(
            "UPDATE revisions SET content_md = NULL, content_size_bytes = NULL, "
            "content_sha256 = NULL WHERE library_id = ? AND revision_id = ?",
            (scope.library_id, scope.second_revision_id),
        )
        connection.exec_driver_sql(
            "INSERT INTO revision_file_sets "
            "(library_id, page_uid, revision_id, revision_number, storage_format, "
            "file_count, total_size_bytes, snapshot_sha256) "
            "VALUES (?, ?, ?, 2, 'file_set_v1', ?, ?, ?)",
            (
                scope.library_id,
                scope.first_page_uid,
                scope.second_revision_id,
                len(manifest.files),
                manifest.total_size_bytes,
                manifest.snapshot_sha256,
            ),
        )
        for entry in manifest.files:
            connection.exec_driver_sql(
                "INSERT INTO revision_files "
                "(library_id, page_uid, revision_id, revision_number, filename, "
                "content_bytes, size_bytes, content_sha256) VALUES (?, ?, ?, 2, ?, ?, ?, ?)",
                (
                    scope.library_id,
                    scope.first_page_uid,
                    scope.second_revision_id,
                    entry.name,
                    entry.content,
                    entry.content_size_bytes,
                    entry.content_sha256,
                ),
            )
    return scope.second_revision_id, files


def test_router_only_exposes_two_exact_file_reads(file_set_api: FileSetApi) -> None:
    router = create_file_set_read_router(file_set_api.engine)
    assert {
        (route.path, tuple(sorted(route.methods or ())))
        for route in router.routes
        if isinstance(route, Route)
    } == {
        (
            "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}"
            "/revisions/{revision_id}/files",
            ("GET",),
        ),
        (
            "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}"
            "/revisions/{revision_id}/files/{file_name:path}",
            ("GET",),
        ),
    }


def test_legacy_revision_is_one_file_in_the_unified_contract(file_set_api: FileSetApi) -> None:
    base = _base(file_set_api)
    listing = _get(file_set_api, base)
    assert listing.status_code == 200
    assert listing.json() == {
        "page_id": file_set_api.scope.first_page_id,
        "revision_id": file_set_api.scope.first_revision_id,
        "revision_number": 1,
        "snapshot_sha256": build_file_manifest(
            [("content.md", file_set_api.scope.historical_content.encode())]
        ).snapshot_sha256.hex(),
        "files": [
            {
                "filename": "content.md",
                "size_bytes": len(file_set_api.scope.historical_content.encode()),
                "content_sha256": hashlib.sha256(
                    file_set_api.scope.historical_content.encode()
                ).hexdigest(),
            }
        ],
    }
    assert file_set_api.scope.historical_content not in listing.text
    assert listing.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    download = _get(file_set_api, f"{base}/content.md")
    assert download.status_code == 200
    assert download.content == file_set_api.scope.historical_content.encode()
    assert download.headers["Content-Type"] == "application/octet-stream"
    assert download.headers["Content-Disposition"] == (
        "attachment; filename=\"download\"; filename*=UTF-8''content.md"
    )
    assert download.headers["X-Content-Type-Options"] == "nosniff"
    assert download.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL


def test_multifile_current_and_legacy_history_use_same_routes(file_set_api: FileSetApi) -> None:
    revision_id, files = _install_file_set(file_set_api)
    base = _base(file_set_api, revision_id=revision_id)
    listing = _get(file_set_api, base)
    assert listing.status_code == 200
    assert listing.json()["revision_number"] == 2
    assert (
        listing.json()["snapshot_sha256"]
        == build_file_manifest(files.items()).snapshot_sha256.hex()
    )
    assert [item["filename"] for item in listing.json()["files"]] == sorted(files)
    for filename, content in files.items():
        downloaded = _get(file_set_api, f"{base}/{filename}")
        assert downloaded.status_code == 200
        assert downloaded.content == content
        assert downloaded.headers["Content-Type"] == "application/octet-stream"
        assert downloaded.headers["Content-Disposition"].startswith("attachment;")
        if filename == "资料.txt":
            assert (
                "filename*=UTF-8''%E8%B5%84%E6%96%99.txt"
                in downloaded.headers["Content-Disposition"]
            )
    assert _get(file_set_api, _base(file_set_api)).status_code == 200


def test_migrated_database_and_real_append_are_read_by_one_file_set_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'migrated-file-read.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(REPOSITORY_ROOT / "alembic.ini")), "head")
    engine = build_engine(database_url)
    try:
        library_id, section_id, book_id = seed_library_structure(engine)
        page, legacy, identifier, counter, source = page_graph_values(
            library_id=library_id,
            section_id=section_id,
            book_id=book_id,
        )
        issued = generate_token()
        with immediate_transaction(engine) as connection:
            insert_page_graph(connection, (page, legacy, identifier, counter, source))
            auth = AuthRepository(connection)
            auth.add_caller(
                NewCaller(
                    id="4" * 32,
                    library_id=library_id,
                    kind=CallerKind.AGENT,
                    name="Synthetic Read Agent",
                    created_at=1_000_000,
                    updated_at=1_000_000,
                )
            )
            auth.add_credential(
                NewCredential(
                    id="5" * 32,
                    library_id=library_id,
                    caller_id="4" * 32,
                    selector=issued.selector,
                    token_version=issued.version,
                    verifier=issued.verifier,
                    expires_at=10_000_000,
                    created_at=1_000_000,
                    updated_at=1_000_000,
                )
            )
            auth.add_grant(
                NewSectionGrant(
                    library_id=library_id,
                    caller_id="4" * 32,
                    section_id=section_id,
                    action=SectionAction.PAGE_READ,
                    created_at=1_000_000,
                )
            )

        next_revision_id = "rev_" + "a" * 32
        files = (("content.md", legacy.content_md), ("figure.png", b"\x89PNG\x00\xff"))
        with immediate_transaction(engine) as connection:
            appended = FileSetRevisionService(connection).append_existing_page(
                library_id=library_id,
                page_id=page.page_id,
                expected_etag=page_current_etag(
                    page.page_uid,
                    legacy.revision_id,
                    1,
                    page.occurred_at,
                    page.updated_at,
                ),
                files=files,
                revision_id=next_revision_id,
                revision_at=3_000_000,
                source=NewPageSource(
                    library_id=library_id,
                    source_id="7" * 32,
                    page_uid=page.page_uid,
                    revision_id=next_revision_id,
                    revision_number=2,
                    kind="synthetic",
                    locator="urn:synthetic:file-set",
                    created_at=3_000_000,
                ),
            )
            assert appended.changed

        app = FastAPI()
        install_api_exception_handlers(app)
        app.add_middleware(RequestIDMiddleware, request_id_factory=lambda: REQUEST_ID)
        app.include_router(create_file_set_read_router(engine, clock=lambda: 4_000_000))
        page_path = f"/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page.page_id}"
        base = f"{page_path}/revisions"
        with TestClient(app, raise_server_exceptions=False) as client:
            headers = {"Authorization": f"Bearer {issued.value}"}
            prior = client.get(f"{base}/{legacy.revision_id}/files", headers=headers)
            current = client.get(f"{base}/{next_revision_id}/files", headers=headers)
            binary = client.get(f"{base}/{next_revision_id}/files/figure.png", headers=headers)
        assert prior.status_code == current.status_code == binary.status_code == 200
        assert prior.json()["files"][0]["filename"] == "content.md"
        assert current.json()["snapshot_sha256"] == appended.manifest.snapshot_sha256.hex()
        assert [item["filename"] for item in current.json()["files"]] == [
            "content.md",
            "figure.png",
        ]
        assert binary.content == b"\x89PNG\x00\xff"
    finally:
        engine.dispose()


def test_auth_scope_deleted_and_wrong_revision_do_not_leak(file_set_api: FileSetApi) -> None:
    base = _base(file_set_api)
    _problem(_get(file_set_api, base, authenticated=False), 401, "authentication_required")
    other_library = base.replace(file_set_api.scope.library_id, "f" * 32, 1)
    _problem(_get(file_set_api, other_library), 404, "resource_not_found")
    no_read_grant = base.replace(file_set_api.scope.query_section_id, SECOND_QUERY_SECTION_ID, 1)
    _problem(_get(file_set_api, no_read_grant), 403, "insufficient_scope")
    hidden = base.replace(
        file_set_api.scope.query_section_id, file_set_api.scope.hidden_section_id, 1
    )
    _problem(_get(file_set_api, hidden), 404, "resource_not_found")
    deleted = base.replace(file_set_api.scope.first_page_id, file_set_api.scope.deleted_page_id, 1)
    _problem(_get(file_set_api, deleted), 404, "resource_not_found")
    wrong_revision = base.replace(file_set_api.scope.first_revision_id, "rev_" + "f" * 32, 1)
    _problem(_get(file_set_api, wrong_revision), 404, "resource_not_found")
    other_page_revision = base.replace(file_set_api.scope.first_revision_id, "rev_" + "3" * 32, 1)
    _problem(_get(file_set_api, other_page_revision), 404, "resource_not_found")


@pytest.mark.parametrize("suffix", ["missing.md", "..%2Fsecret", "CON", "%0D%0Aevil"])
def test_download_rejects_unknown_or_unsafe_filename(
    file_set_api: FileSetApi,
    suffix: str,
) -> None:
    response = _get(file_set_api, f"{_base(file_set_api)}/{suffix}")
    expected_status = 404 if suffix in {"missing.md", "%0D%0Aevil"} else 422
    expected_code = "resource_not_found" if expected_status == 404 else "request_validation_failed"
    _problem(response, expected_status, expected_code)
    assert file_set_api.scope.historical_content not in response.text


@pytest.mark.parametrize("kind", ["manifest", "seal", "file_hash", "snapshot_digest"])
def test_corrupt_snapshot_fails_closed(file_set_api: FileSetApi, kind: str) -> None:
    revision_id, _ = _install_file_set(file_set_api)
    scope = file_set_api.scope
    with immediate_transaction(file_set_api.engine) as connection:
        if kind == "manifest":
            connection.exec_driver_sql(
                "DELETE FROM revision_file_sets WHERE revision_id = ?", (revision_id,)
            )
        elif kind == "seal":
            connection.exec_driver_sql(
                "DELETE FROM revision_file_seal_guards WHERE revision_id = ?", (revision_id,)
            )
            connection.exec_driver_sql(
                "DELETE FROM revision_file_seals WHERE revision_id = ?", (revision_id,)
            )
        elif kind == "file_hash":
            connection.exec_driver_sql(
                "UPDATE revision_files SET content_sha256 = ? "
                "WHERE revision_id = ? AND filename = 'note.md'",
                (b"x" * 32, revision_id),
            )
        else:
            connection.exec_driver_sql(
                "UPDATE revision_file_sets SET snapshot_sha256 = ? WHERE revision_id = ?",
                (b"x" * 32, revision_id),
            )
    base = _base(file_set_api, revision_id=revision_id)
    for path in (base, f"{base}/note.md"):
        response = _get(file_set_api, path)
        _problem(response, 500, "internal_error")
        assert scope.current_content not in response.text
        assert "# New" not in response.text


def test_corrupt_legacy_mirror_fails_closed(file_set_api: FileSetApi) -> None:
    scope = file_set_api.scope
    with immediate_transaction(file_set_api.engine) as connection:
        connection.exec_driver_sql(
            "UPDATE revisions SET content_sha256 = ? WHERE revision_id = ?",
            (b"x" * 32, scope.first_revision_id),
        )
    base = _base(file_set_api)
    for path in (base, f"{base}/content.md"):
        response = _get(file_set_api, path)
        _problem(response, 500, "internal_error")
        assert scope.historical_content not in response.text


def test_revoked_grant_is_rechecked_after_authentication(file_set_api: FileSetApi) -> None:
    scope = file_set_api.scope
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": _base(file_set_api),
            "headers": [(b"authorization", f"Bearer {file_set_api.token}".encode())],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "http_version": "1.1",
        }
    )
    context = BearerAuthentication(file_set_api.engine, clock=lambda: 2_000_000)(request)
    with immediate_transaction(file_set_api.engine) as connection:
        assert AuthRepository(connection).remove_grant(
            scope.library_id, CALLER_ID, scope.query_section_id, SectionAction.PAGE_READ
        )
    with pytest.raises(ApplicationProblem) as raised:
        _perform_read(
            file_set_api.engine,
            context,
            lambda service: service.list_files(
                scope.library_id,
                scope.query_section_id,
                scope.first_page_id,
                scope.first_revision_id,
            ),
            clock=lambda: 2_000_000,
        )
    assert raised.value.status_code == 403


def test_read_uses_one_real_sqlite_snapshot_for_grant_and_page(
    file_set_api: FileSetApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = file_set_api.scope
    with file_set_api.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one() == "wal"
    with immediate_transaction(file_set_api.engine) as connection:
        connection.exec_driver_sql(
            "UPDATE pages SET deleted_at = 4000000, updated_at = 4000000 "
            "WHERE library_id = ? AND page_uid = ?",
            (scope.library_id, scope.first_page_uid),
        )

    original_actions = RetrievalRepository.section_actions
    observed_transactions: list[bool] = []

    def actions_then_switch_state(
        repository: RetrievalRepository,
        library_id: str,
        caller_id: str,
        section_id: str,
    ) -> tuple[SectionAction, ...]:
        actions = original_actions(repository, library_id, caller_id, section_id)
        raw = repository._connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
        observed_transactions.append(raw.in_transaction)
        with immediate_transaction(file_set_api.engine) as writer:
            assert AuthRepository(writer).remove_grant(
                scope.library_id, CALLER_ID, scope.query_section_id, SectionAction.PAGE_READ
            )
            writer.exec_driver_sql(
                "UPDATE pages SET deleted_at = NULL, updated_at = 5000000 "
                "WHERE library_id = ? AND page_uid = ?",
                (scope.library_id, scope.first_page_uid),
            )
        return actions

    monkeypatch.setattr(RetrievalRepository, "section_actions", actions_then_switch_state)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": _base(file_set_api),
            "headers": [(b"authorization", f"Bearer {file_set_api.token}".encode())],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "http_version": "1.1",
        }
    )
    context = BearerAuthentication(file_set_api.engine, clock=lambda: 2_000_000)(request)
    with pytest.raises(ApplicationProblem) as raised:
        _perform_read(
            file_set_api.engine,
            context,
            lambda service: service.list_files(
                scope.library_id,
                scope.query_section_id,
                scope.first_page_id,
                scope.first_revision_id,
            ),
            clock=lambda: 2_000_000,
        )
    assert observed_transactions == [True]
    assert raised.value.status_code == 404
