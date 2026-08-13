from __future__ import annotations

import importlib
import json
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import anyio
import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, func, select

from patchouli_lib.api.archive_routes import (
    MAX_ARCHIVE_METADATA_BYTES,
    MAX_ARCHIVE_MULTIPART_BYTES,
    ArchiveServiceFactory,
    create_archive_router,
)
from patchouli_lib.api.contracts import PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import ProblemDetails, install_api_exception_handlers
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, RequestIDMiddleware
from patchouli_lib.auth.models import AuditEvent, Credential
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller, NewSectionGrant, SectionAction
from patchouli_lib.auth.service import CredentialIssuer
from patchouli_lib.content import (
    ArchiveIdempotencyKey,
    ArchiveMutationSuccess,
    ArchiveService,
    ArchiveSourceInput,
    CreateArchiveCommand,
)
from patchouli_lib.content.models import MAX_MARKDOWN_BYTES, Page, PageSource, Revision
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.idempotency.models import IdempotencyRecord
from patchouli_lib.idempotency.schemas import digest_idempotency_key
from patchouli_lib.identifiers import parse_occurrence_time
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed, NewBook, NewSection
from patchouli_lib.library.service import LibrarySeedService

REQUEST_ID = f"req_{'9' * 32}"
OPERATION_TIME = 1_776_000_000_000_000
ISSUED_TIME = OPERATION_TIME - 1_000_000_000
EXPIRY_TIME = OPERATION_TIME + 10_000_000_000
CREATE_METADATA = {
    "title": "Synthetic Archive",
    "occurred_at": "2026-08-13T10:00:00.123456Z",
    "source": {"kind": "synthetic", "locator": "urn:synthetic:archive"},
}
CONTENT = b"# Synthetic archive\r\n\r\nExact bytes.\n"


@dataclass(frozen=True, slots=True)
class ArchiveApiFixture:
    engine: Engine
    library_id: str
    section_id: str
    book_id: str
    other_section_id: str
    other_book_id: str
    foreign_section_id: str
    foreign_book_id: str
    writer_id: str
    writer_credential_id: str
    writer_token: str
    reader_token: str
    hidden_token: str
    operator_token: str


def _add_caller(
    repository: AuthRepository,
    *,
    caller_id: str,
    library_id: str,
    kind: CallerKind,
    credential_id: str,
) -> str:
    caller = repository.add_caller(
        NewCaller(
            id=caller_id,
            library_id=library_id,
            kind=kind,
            name=f"Synthetic {kind.value} {caller_id[0]}",
            created_at=ISSUED_TIME,
            updated_at=ISSUED_TIME,
        )
    )
    return (
        CredentialIssuer(
            repository,
            id_factory=lambda: credential_id,
            clock=lambda: ISSUED_TIME,
        )
        .issue(caller, expires_at=EXPIRY_TIME)
        .value
    )


@pytest.fixture
def archive_api(tmp_path: Path) -> Iterator[ArchiveApiFixture]:
    engine = build_engine(f"sqlite:///{(tmp_path / 'archive-routes.db').as_posix()}")
    Page.metadata.create_all(engine)
    try:
        identifiers = iter(("1" * 32, "2" * 32, "3" * 32))
        with immediate_transaction(engine) as connection:
            structure = LibrarySeedService(
                LibraryRepository(connection),
                id_factory=lambda: next(identifiers),
                clock=lambda: 1_000_000,
            ).seed(
                LibraryStructureSeed(
                    library_name="Synthetic HTTP Library",
                    section_name="Synthetic Archive Section",
                    book_name="Synthetic Archive Book",
                )
            )
            foreign_identifiers = iter(("8" * 32, "9" * 32, "0" * 32))
            foreign_structure = LibrarySeedService(
                LibraryRepository(connection),
                id_factory=lambda: next(foreign_identifiers),
                clock=lambda: 1_000_001,
            ).seed(
                LibraryStructureSeed(
                    library_name="Foreign Synthetic HTTP Library",
                    section_name="Foreign Synthetic Archive Section",
                    book_name="Foreign Synthetic Archive Book",
                )
            )
            other_section_id = "4" * 32
            other_book_id = "5" * 32
            library = LibraryRepository(connection)
            library.add_section(
                NewSection(
                    id=other_section_id,
                    library_id=structure.library.id,
                    name="Synthetic Hidden Section",
                    created_at=1_000_000,
                    updated_at=1_000_000,
                )
            )
            library.add_book(
                NewBook(
                    id=other_book_id,
                    library_id=structure.library.id,
                    section_id=other_section_id,
                    name="Synthetic Hidden Book",
                    created_at=1_000_000,
                    updated_at=1_000_000,
                )
            )
            auth = AuthRepository(connection)
            writer_token = _add_caller(
                auth,
                caller_id="a" * 32,
                library_id=structure.library.id,
                kind=CallerKind.AGENT,
                credential_id="b" * 32,
            )
            reader_token = _add_caller(
                auth,
                caller_id="c" * 32,
                library_id=structure.library.id,
                kind=CallerKind.AGENT,
                credential_id="d" * 32,
            )
            hidden_token = _add_caller(
                auth,
                caller_id="e" * 32,
                library_id=structure.library.id,
                kind=CallerKind.AGENT,
                credential_id="f" * 32,
            )
            operator_token = _add_caller(
                auth,
                caller_id="6" * 32,
                library_id=structure.library.id,
                kind=CallerKind.OPERATOR,
                credential_id="7" * 32,
            )
            for caller_id, section_id, action in (
                ("a" * 32, structure.section.id, SectionAction.ARCHIVE_WRITE),
                ("a" * 32, other_section_id, SectionAction.ARCHIVE_WRITE),
                ("c" * 32, structure.section.id, SectionAction.PAGE_READ),
                ("e" * 32, other_section_id, SectionAction.PAGE_READ),
            ):
                auth.add_grant(
                    NewSectionGrant(
                        library_id=structure.library.id,
                        caller_id=caller_id,
                        section_id=section_id,
                        action=action,
                        created_at=OPERATION_TIME - 1_000_000,
                    )
                )
        yield ArchiveApiFixture(
            engine=engine,
            library_id=structure.library.id,
            section_id=structure.section.id,
            book_id=structure.book.id,
            other_section_id=other_section_id,
            other_book_id=other_book_id,
            foreign_section_id=foreign_structure.section.id,
            foreign_book_id=foreign_structure.book.id,
            writer_id="a" * 32,
            writer_credential_id="b" * 32,
            writer_token=writer_token,
            reader_token=reader_token,
            hidden_token=hidden_token,
            operator_token=operator_token,
        )
    finally:
        engine.dispose()


def _build_app(
    fixture: ArchiveApiFixture,
    *,
    request_id_factory: Callable[[], str] = lambda: REQUEST_ID,
    service_factory: ArchiveServiceFactory | None = None,
) -> FastAPI:
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_api_exception_handlers(application)
    application.add_middleware(
        RequestIDMiddleware,
        request_id_factory=request_id_factory,
    )
    application.include_router(
        create_archive_router(
            fixture.engine,
            clock=lambda: OPERATION_TIME,
            service_factory=service_factory,
        )
    )
    return application


def _authorization(token: str) -> tuple[str, str]:
    return ("Authorization", f"Bearer {token}")


def _multipart(
    metadata: object = CREATE_METADATA,
    content: bytes = CONTENT,
    *,
    boundary: str = "synthetic-boundary",
    metadata_content_type: str = "application/json",
    content_content_type: str = "text/markdown;charset=utf-8",
    order: Sequence[str] = ("metadata", "content"),
    filename: str | None = None,
) -> tuple[str, bytes]:
    metadata_bytes = (
        metadata
        if isinstance(metadata, bytes)
        else json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
    )
    values = {"metadata": metadata_bytes, "content": content}
    types = {"metadata": metadata_content_type, "content": content_content_type}
    chunks: list[bytes] = []
    for name in order:
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None and name == "content":
            disposition += f'; filename="{filename}"'
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                f"{disposition}\r\n".encode(),
                f"Content-Type: {types[name]}\r\n\r\n".encode(),
                values[name],
                b"\r\n",
            )
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def _create_path(fixture: ArchiveApiFixture, *, section_id: str | None = None) -> str:
    return f"/api/v1/sections/{section_id or fixture.section_id}/books/{fixture.book_id}/pages"


def _create(
    client: TestClient,
    fixture: ArchiveApiFixture,
    *,
    key: str = "synthetic-create-key",
    token: str | None = None,
    metadata: object = CREATE_METADATA,
    content: bytes = CONTENT,
    path: str | None = None,
    extra_headers: Sequence[tuple[str, str]] = (),
) -> Any:
    media_type, body = _multipart(metadata, content)
    headers = [
        _authorization(token or fixture.writer_token),
        ("Idempotency-Key", key),
        ("Content-Type", media_type),
        *extra_headers,
    ]
    return client.post(path or _create_path(fixture), headers=headers, content=body)


def _problem(response: Any, status: int, code: str) -> ProblemDetails:
    assert response.status_code == status
    assert response.headers["Content-Type"].startswith("application/problem+json")
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    problem = ProblemDetails.model_validate(response.json())
    assert problem.status == status
    assert problem.code == code
    assert problem.request_id == response.headers[REQUEST_ID_HEADER]
    return problem


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            name: connection.execute(select(func.count()).select_from(model)).scalar_one()
            for name, model in (
                ("pages", Page),
                ("revisions", Revision),
                ("page_sources", PageSource),
                ("audit_events", AuditEvent),
                ("idempotency_records", IdempotencyRecord),
            )
        }


def _seed_hidden_archive(fixture: ArchiveApiFixture) -> tuple[str, str]:
    with immediate_transaction(fixture.engine) as connection:
        result = ArchiveService(
            connection,
            clock=lambda: OPERATION_TIME,
        ).create_archive(
            fixture.writer_token,
            CreateArchiveCommand(
                library_id=fixture.library_id,
                section_id=fixture.other_section_id,
                book_id=fixture.other_book_id,
                title="Synthetic Hidden Archive",
                occurred_at=parse_occurrence_time("2026-08-13T10:01:00.123456Z").utc_microseconds,
                content_md=b"# Synthetic hidden archive\n",
                source=ArchiveSourceInput(kind="synthetic hidden"),
                request_id=f"req_{'8' * 32}",
            ),
            ArchiveIdempotencyKey(key_digest=digest_idempotency_key("seed-hidden-archive-key")),
        )
        assert isinstance(result, ArchiveMutationSuccess)
    with immediate_transaction(fixture.engine) as connection:
        assert AuthRepository(connection).remove_grant(
            fixture.library_id,
            fixture.writer_id,
            fixture.other_section_id,
            SectionAction.ARCHIVE_WRITE,
        )
    return result.page.page_id, result.response.response_etag


def _client_module(monkeypatch: pytest.MonkeyPatch, name: str) -> ModuleType:
    source = Path(__file__).resolve().parents[2] / "clients" / "python" / "src" / "patchouli_client"
    package = ModuleType("patchouli_client")
    package.__dict__["__path__"] = [str(source)]
    monkeypatch.setitem(sys.modules, "patchouli_client", package)
    loaded: ModuleType | None = None
    for module_name in ("errors", "models", "multipart"):
        loaded = importlib.import_module(f"patchouli_client.{module_name}")
        if module_name == name:
            return loaded
    raise AssertionError("Unknown synthetic client module.")


def test_router_inventory_is_exact_and_app_remains_unmodified() -> None:
    engine = build_engine("sqlite://")
    try:
        router = create_archive_router(engine)
    finally:
        engine.dispose()
    assert [(cast(Any, route).path, cast(Any, route).methods) for route in router.routes] == [
        (
            "/api/v1/sections/{section_id}/books/{book_id}/pages",
            {"POST"},
        ),
        (
            "/api/v1/sections/{section_id}/pages/{page_id}/revisions",
            {"POST"},
        ),
    ]
    app_source = (Path(__file__).resolve().parents[2] / "src/patchouli_lib/app.py").read_text()
    assert "archive_routes" not in app_source
    assert "create_archive_router" not in app_source


def test_typed_client_multipart_create_and_response_parity(
    archive_api: ArchiveApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _client_module(monkeypatch, "models")
    multipart = _client_module(monkeypatch, "multipart")
    metadata = models.ArchiveCreateMetadata(
        title="Synthetic Archive",
        occurred_at=datetime(2026, 8, 13, 10, 0, 0, 123456, tzinfo=UTC),
        source=models.SourceInput(kind="synthetic", locator="urn:synthetic:archive"),
    )
    content = models.MarkdownContent(CONTENT)
    request_body = multipart.build_archive_multipart(
        metadata.to_wire(),
        content,
        boundary="client-synthetic-boundary",
    )
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "typed-client-key"),
                ("Content-Type", request_body.media_type),
            ],
            content=request_body.body,
        )

    assert response.status_code == 201
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    assert response.headers[REQUEST_ID_HEADER] == REQUEST_ID
    assert "Idempotency-Replayed" not in response.headers
    assert response.headers["Location"].startswith(
        f"/api/v1/sections/{archive_api.section_id}/pages/"
    )
    assert response.headers["ETag"].startswith('"page-v1-')
    parsed = models.PageDocument.from_dict(response.json())
    parsed.require_current_revision()
    assert parsed.page.book_id == archive_api.book_id
    assert parsed.revision.content == CONTENT.decode()
    assert parsed.citation.href.endswith("/revisions/1")


@pytest.mark.parametrize(
    ("token", "status", "code"),
    [
        (None, 401, "authentication_required"),
        ("invalid", 401, "invalid_token"),
    ],
)
def test_missing_and_invalid_authentication_are_stable_and_secret_safe(
    archive_api: ArchiveApiFixture,
    token: str | None,
    status: int,
    code: str,
) -> None:
    media_type, body = _multipart()
    headers = [
        ("Idempotency-Key", "auth-failure-key"),
        ("Content-Type", media_type),
    ]
    if token is not None:
        headers.append(_authorization(token))
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = client.post(_create_path(archive_api), headers=headers, content=body)

    problem = _problem(response, status, code)
    if status == 401:
        assert response.headers["WWW-Authenticate"] in {
            "Bearer",
            'Bearer error="invalid_token"',
        }
    rendered = response.text + repr(problem)
    assert archive_api.writer_token not in rendered
    assert "auth-failure-key" not in rendered
    assert "urn:synthetic:archive" not in rendered
    assert CONTENT.decode() not in rendered


@pytest.mark.parametrize(
    ("token", "section_id", "status", "code"),
    [
        ("operator", "primary", 403, "insufficient_scope"),
        ("reader", "primary", 403, "insufficient_scope"),
        ("hidden", "primary", 404, "resource_not_found"),
    ],
)
def test_operator_visible_without_write_and_hidden_section_are_distinct(
    archive_api: ArchiveApiFixture,
    token: str,
    section_id: str,
    status: int,
    code: str,
) -> None:
    selected = {
        "operator": archive_api.operator_token,
        "reader": archive_api.reader_token,
        "hidden": archive_api.hidden_token,
    }[token]
    target = archive_api.section_id if section_id == "primary" else archive_api.other_section_id
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = _create(
            client,
            archive_api,
            key=f"{token}-authorization-key",
            token=selected,
            path=_create_path(archive_api, section_id=target),
        )
    _problem(response, status, code)
    assert _counts(archive_api.engine) == {
        "pages": 0,
        "revisions": 0,
        "page_sources": 0,
        "audit_events": 0,
        "idempotency_records": 0,
    }


def test_wrong_book_section_and_page_are_hidden_as_not_found(
    archive_api: ArchiveApiFixture,
) -> None:
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        wrong_book = _create(
            client,
            archive_api,
            key="wrong-book-key",
            path=(f"/api/v1/sections/{archive_api.section_id}/books/{'8' * 32}/pages"),
        )
        wrong_section = _create(
            client,
            archive_api,
            key="wrong-section-key",
            path=_create_path(archive_api, section_id=archive_api.other_section_id),
        )
        wrong_library = _create(
            client,
            archive_api,
            key="wrong-library-key",
            path=(
                f"/api/v1/sections/{archive_api.foreign_section_id}/books/"
                f"{archive_api.foreign_book_id}/pages"
            ),
        )
        created = _create(client, archive_api, key="wrong-page-fixture-key")
        page_id = created.json()["page"]["page_id"]
        media_type, body = _multipart(
            {"source": {"kind": "synthetic revision"}},
            b"# Revised\n",
        )
        wrong_page = client.post(
            (
                f"/api/v1/sections/{archive_api.section_id}/pages/"
                "20260813t100000123z-missing-synthetic-page/revisions"
            ),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "wrong-page-key"),
                ("If-Match", created.headers["ETag"]),
                ("Content-Type", media_type),
            ],
            content=body,
        )
        page_wrong_section = client.post(
            (f"/api/v1/sections/{archive_api.other_section_id}/pages/{page_id}/revisions"),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "page-wrong-section-key"),
                ("If-Match", created.headers["ETag"]),
                ("Content-Type", media_type),
            ],
            content=body,
        )
    assert created.status_code == 201
    for response in (
        wrong_book,
        wrong_section,
        wrong_library,
        wrong_page,
        page_wrong_section,
    ):
        _problem(response, 404, "resource_not_found")


def test_create_hidden_cross_section_book_matches_absent_without_mutation(
    archive_api: ArchiveApiFixture,
) -> None:
    with immediate_transaction(archive_api.engine) as connection:
        assert AuthRepository(connection).remove_grant(
            archive_api.library_id,
            archive_api.writer_id,
            archive_api.other_section_id,
            SectionAction.ARCHIVE_WRITE,
        )
    before = _counts(archive_api.engine)
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        hidden = _create(
            client,
            archive_api,
            key="hidden-cross-section-book-key",
            path=(
                f"/api/v1/sections/{archive_api.section_id}/books/{archive_api.other_book_id}/pages"
            ),
        )
        absent = _create(
            client,
            archive_api,
            key="absent-book-comparison-key",
            path=(f"/api/v1/sections/{archive_api.section_id}/books/{'d' * 32}/pages"),
        )
    for response in (hidden, absent):
        _problem(response, 404, "resource_not_found")
    assert hidden.content == absent.content
    assert _counts(archive_api.engine) == before


def test_revise_hidden_cross_section_page_matches_absent_without_mutation(
    archive_api: ArchiveApiFixture,
) -> None:
    hidden_page_id, hidden_etag = _seed_hidden_archive(archive_api)
    before = _counts(archive_api.engine)
    media_type, body = _multipart(
        {"source": {"kind": "synthetic hidden revision"}},
        b"# Hidden revision attempt\n",
    )

    def revise_headers(key: str) -> list[tuple[str, str]]:
        return [
            _authorization(archive_api.writer_token),
            ("Idempotency-Key", key),
            ("If-Match", hidden_etag),
            ("Content-Type", media_type),
        ]

    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        hidden = client.post(
            (f"/api/v1/sections/{archive_api.section_id}/pages/{hidden_page_id}/revisions"),
            headers=revise_headers("hidden-cross-section-page-key"),
            content=body,
        )
        absent = client.post(
            (
                f"/api/v1/sections/{archive_api.section_id}/pages/"
                "20260813t100100123z-absent-synthetic-page/revisions"
            ),
            headers=revise_headers("absent-page-comparison-key"),
            content=body,
        )
    for response in (hidden, absent):
        _problem(response, 404, "resource_not_found")
    assert hidden.content == absent.content
    assert _counts(archive_api.engine) == before


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ((), 422, "request_validation_failed"),
        ((("Idempotency-Key", "second"),), 422, "request_validation_failed"),
        ((("If-Match", '"page-v1-' + "0" * 64 + '"'),), 422, "request_validation_failed"),
    ],
)
def test_create_header_contract(
    archive_api: ArchiveApiFixture,
    headers: Sequence[tuple[str, str]],
    status: int,
    code: str,
) -> None:
    media_type, body = _multipart()
    request_headers = [
        _authorization(archive_api.writer_token),
        ("Content-Type", media_type),
        *headers,
    ]
    if headers != ():
        request_headers.insert(1, ("Idempotency-Key", "first"))
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = client.post(
            _create_path(archive_api),
            headers=request_headers,
            content=body,
        )
    _problem(response, status, code)


@pytest.mark.parametrize(
    "key",
    ["", " leading", "trailing ", "two words", "line\nfeed", "x" * 257],
)
def test_idempotency_key_rejects_empty_ows_control_overlength_and_non_ascii(
    archive_api: ArchiveApiFixture,
    key: str,
) -> None:
    media_type, body = _multipart()
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", key),
                ("Content-Type", media_type),
            ],
            content=body,
        )
    _problem(response, 422, "request_validation_failed")


def test_idempotency_key_rejects_non_ascii_raw_header(
    archive_api: ArchiveApiFixture,
) -> None:
    media_type, body = _multipart()
    headers = httpx2.Headers(
        [
            (b"Authorization", f"Bearer {archive_api.writer_token}".encode()),
            (b"Idempotency-Key", b"\xff"),
            (b"Content-Type", media_type.encode()),
        ]
    )
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = client.post(_create_path(archive_api), headers=headers, content=body)
    _problem(response, 422, "request_validation_failed")


@pytest.mark.parametrize(
    ("metadata", "content", "status", "code"),
    [
        (b"{", CONTENT, 422, "request_validation_failed"),
        (
            b'{"title":"a","title":"b","occurred_at":"2026-08-13T10:00:00Z","source":{"kind":"s"}}',
            CONTENT,
            422,
            "request_validation_failed",
        ),
        (
            b'{"title":"a","occurred_at":NaN,"source":{"kind":"s"}}',
            CONTENT,
            422,
            "request_validation_failed",
        ),
        ({"title": "Missing fields"}, CONTENT, 422, "request_validation_failed"),
        ({**CREATE_METADATA, "unknown": True}, CONTENT, 422, "request_validation_failed"),
        (CREATE_METADATA, b"", 422, "request_validation_failed"),
        (CREATE_METADATA, b"nul\x00body", 422, "request_validation_failed"),
        (CREATE_METADATA, b"\xff", 422, "request_validation_failed"),
    ],
)
def test_malformed_metadata_and_content_are_validation_failures(
    archive_api: ArchiveApiFixture,
    metadata: object,
    content: bytes,
    status: int,
    code: str,
) -> None:
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = _create(
            client,
            archive_api,
            key="invalid-body-key",
            metadata=metadata,
            content=content,
        )
    _problem(response, status, code)
    assert _counts(archive_api.engine)["pages"] == 0


def test_metadata_integer_beyond_python_digit_limit_is_safe_422(
    archive_api: ArchiveApiFixture,
) -> None:
    oversized_integer = "9" * 5_000
    metadata = (
        b'{"title":'
        + oversized_integer.encode()
        + b',"occurred_at":"2026-08-13T10:00:00Z","source":{"kind":"synthetic"}}'
    )
    assert len(metadata) < MAX_ARCHIVE_METADATA_BYTES
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = _create(
            client,
            archive_api,
            key="oversized-json-integer-key",
            metadata=metadata,
        )
    _problem(response, 422, "request_validation_failed")
    assert oversized_integer not in response.text
    assert "oversized-json-integer-key" not in response.text
    assert _counts(archive_api.engine)["pages"] == 0


@pytest.mark.parametrize(
    ("top_type", "metadata_type", "content_type"),
    [
        ("application/json", "application/json", "text/markdown;charset=utf-8"),
        (None, "text/plain", "text/markdown;charset=utf-8"),
        (None, "application/json", "text/plain;charset=utf-8"),
        (None, "application/json;charset=latin-1", "text/markdown;charset=utf-8"),
        (None, "application/json", "text/markdown"),
    ],
)
def test_top_and_part_media_types_are_strict(
    archive_api: ArchiveApiFixture,
    top_type: str | None,
    metadata_type: str,
    content_type: str,
) -> None:
    media_type, body = _multipart(
        metadata_content_type=metadata_type,
        content_content_type=content_type,
    )
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "media-key"),
                ("Content-Type", top_type or media_type),
            ],
            content=body,
        )
    _problem(response, 415, "unsupported_media_type")


@pytest.mark.parametrize(
    "content_type",
    [
        ("multipart/form-data; boundary=synthetic-boundary; boundary=synthetic-boundary"),
        "multipart/form-data; boundary=synthetic-boundary; boundary=other-boundary",
        "multipart/form-data; boundary=other-boundary; boundary=synthetic-boundary",
        "multipart/form-data; BOUNDARY=synthetic-boundary; boundary=synthetic-boundary",
    ],
)
def test_duplicate_top_level_boundary_parameters_are_415(
    archive_api: ArchiveApiFixture,
    content_type: str,
) -> None:
    _, body = _multipart()
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "duplicate-boundary-key"),
                ("Content-Type", content_type),
            ],
            content=body,
        )
    _problem(response, 415, "unsupported_media_type")


@pytest.mark.parametrize(
    "disposition",
    [
        'form-data; name="metadata"; name="metadata"',
        'form-data; name="metadata"; name="content"',
        'form-data; name="content"; name="metadata"',
        'form-data; NAME="metadata"; filename="safe;name=ignored"; name="metadata"',
    ],
)
def test_duplicate_disposition_name_parameters_are_422(
    archive_api: ArchiveApiFixture,
    disposition: str,
) -> None:
    media_type, body = _multipart()
    body = body.replace(
        b'Content-Disposition: form-data; name="metadata"',
        f"Content-Disposition: {disposition}".encode(),
        1,
    )
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "duplicate-disposition-name-key"),
                ("Content-Type", media_type),
            ],
            content=body,
        )
    _problem(response, 422, "request_validation_failed")


@pytest.mark.parametrize(
    "metadata_content_type",
    [
        "application/json;charset=utf-8;charset=utf-8",
        "application/json;charset=latin-1;charset=utf-8",
        "application/json;charset=utf-8;charset=latin-1",
        "application/json;CHARSET=utf-8;charset=utf-8",
    ],
)
def test_duplicate_part_charset_parameters_are_415(
    archive_api: ArchiveApiFixture,
    metadata_content_type: str,
) -> None:
    media_type, body = _multipart(metadata_content_type=metadata_content_type)
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "duplicate-part-charset-key"),
                ("Content-Type", media_type),
            ],
            content=body,
        )
    _problem(response, 415, "unsupported_media_type")


def test_quoted_mime_separators_and_escapes_are_not_false_duplicates(
    archive_api: ArchiveApiFixture,
) -> None:
    boundary = 'quoted;"boundary'
    _, body = _multipart(
        boundary=boundary,
        metadata_content_type='application/json;charset="utf-8"',
    )
    body = body.replace(
        b'Content-Disposition: form-data; name="content"',
        (
            b'Content-Disposition: form-data; name="content"; '
            b'filename="ignored;name=\\"synthetic.md"'
        ),
        1,
    )
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "quoted-mime-parameter-key"),
                ("Content-Type", 'multipart/form-data; boundary="quoted;\\"boundary"'),
            ],
            content=body,
        )
    assert response.status_code == 201
    assert "ignored" not in response.text


@pytest.mark.parametrize("order", [("metadata",), ("content",), ("metadata", "metadata")])
def test_missing_and_duplicate_multipart_parts_are_rejected(
    archive_api: ArchiveApiFixture,
    order: Sequence[str],
) -> None:
    media_type, body = _multipart(order=order)
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "part-cardinality-key"),
                ("Content-Type", media_type),
            ],
            content=body,
        )
    _problem(response, 422, "request_validation_failed")


def test_extra_part_and_duplicate_part_headers_are_rejected(
    archive_api: ArchiveApiFixture,
) -> None:
    boundary = "extra-part-boundary"
    media_type, body = _multipart(boundary=boundary)
    closing = f"--{boundary}--\r\n".encode()
    extra = b"".join(
        (
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="extra"\r\n',
            b"Content-Type: application/json\r\n\r\n{}\r\n",
            closing,
        )
    )
    extra_body = body.removesuffix(closing) + extra
    duplicate_header_body = body.replace(
        b"Content-Type: application/json\r\n\r\n",
        b"Content-Type: application/json\r\nContent-Type: application/json\r\n\r\n",
        1,
    )
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        extra_response = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "extra-part-key"),
                ("Content-Type", media_type),
            ],
            content=extra_body,
        )
        header_response = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "duplicate-part-header-key"),
                ("Content-Type", media_type),
            ],
            content=duplicate_header_body,
        )
    _problem(extra_response, 422, "request_validation_failed")
    _problem(header_response, 422, "request_validation_failed")


def test_multipart_order_filename_and_json_whitespace_do_not_fingerprint(
    archive_api: ArchiveApiFixture,
) -> None:
    media_type, first = _multipart(
        json.dumps(CREATE_METADATA, indent=2).encode(),
        order=("content", "metadata"),
        filename="ignored-private-name.md",
    )
    _, retry = _multipart(CREATE_METADATA, boundary="different-boundary")
    headers = [
        _authorization(archive_api.writer_token),
        ("Idempotency-Key", "presentation-neutral-key"),
        ("Content-Type", media_type),
    ]
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        created = client.post(_create_path(archive_api), headers=headers, content=first)
        replayed = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "presentation-neutral-key"),
                ("Content-Type", "multipart/form-data; boundary=different-boundary"),
            ],
            content=retry,
        )

    assert created.status_code == replayed.status_code == 201
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert created.content == replayed.content
    assert "ignored-private-name" not in replayed.text


def test_stream_counters_enforce_metadata_content_and_total_limits(
    archive_api: ArchiveApiFixture,
) -> None:
    cases = [
        _multipart(b"{" + b" " * MAX_ARCHIVE_METADATA_BYTES),
        _multipart(CREATE_METADATA, b"a" * (MAX_MARKDOWN_BYTES + 1)),
    ]
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        for index, (media_type, body) in enumerate(cases):
            response = client.post(
                _create_path(archive_api),
                headers=[
                    _authorization(archive_api.writer_token),
                    ("Idempotency-Key", f"over-limit-{index}"),
                    ("Content-Type", media_type),
                ],
                content=(body[offset : offset + 997] for offset in range(0, len(body), 997)),
            )
            _problem(response, 413, "content_too_large")

        media_type, valid_body = _multipart()
        hinted = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "hinted-over-limit"),
                ("Content-Type", media_type),
                ("Content-Length", str(MAX_ARCHIVE_MULTIPART_BYTES + 1)),
            ],
            content=valid_body,
        )
    _problem(hinted, 413, "content_too_large")


def test_exact_content_limit_succeeds_and_total_stream_limit_is_authoritative(
    archive_api: ArchiveApiFixture,
) -> None:
    exact_content = b"a" * MAX_MARKDOWN_BYTES
    media_type, exact_body = _multipart(CREATE_METADATA, exact_content)
    _, ordinary_body = _multipart(boundary="total-limit-boundary")
    over_total = ordinary_body + b"x" * MAX_ARCHIVE_MULTIPART_BYTES
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        exact = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "exact-content-limit-key"),
                ("Content-Type", media_type),
            ],
            content=(
                exact_body[offset : offset + 8_191] for offset in range(0, len(exact_body), 8_191)
            ),
        )
        total = client.post(
            _create_path(archive_api),
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "total-stream-limit-key"),
                ("Content-Type", "multipart/form-data; boundary=total-limit-boundary"),
                ("Content-Length", "1"),
            ],
            content=(
                over_total[offset : offset + 4_093] for offset in range(0, len(over_total), 4_093)
            ),
        )
    assert exact.status_code == 201
    assert exact.json()["revision"]["content"] == exact_content.decode()
    _problem(total, 413, "content_too_large")


def test_same_key_replay_uses_current_request_id_and_mismatch_is_409(
    archive_api: ArchiveApiFixture,
) -> None:
    request_ids = iter((f"req_{'1' * 32}", f"req_{'2' * 32}", f"req_{'3' * 32}"))
    with TestClient(
        _build_app(archive_api, request_id_factory=lambda: next(request_ids)),
        raise_server_exceptions=False,
    ) as client:
        created = _create(client, archive_api, key="replay-key")
        replayed = _create(client, archive_api, key="replay-key")
        conflict = _create(
            client,
            archive_api,
            key="replay-key",
            content=b"# Changed exact bytes\n",
        )

    assert created.status_code == replayed.status_code == 201
    assert "Idempotency-Replayed" not in created.headers
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert created.content == replayed.content
    assert created.headers[REQUEST_ID_HEADER] == f"req_{'1' * 32}"
    assert replayed.headers[REQUEST_ID_HEADER] == f"req_{'2' * 32}"
    _problem(conflict, 409, "idempotency_mismatch")
    assert _counts(archive_api.engine) == {
        "pages": 1,
        "revisions": 1,
        "page_sources": 1,
        "audit_events": 1,
        "idempotency_records": 1,
    }


def test_same_key_metadata_source_conflicts_but_wrong_route_stays_hidden(
    archive_api: ArchiveApiFixture,
) -> None:
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        created = _create(client, archive_api, key="semantic-mismatch-key")
        changed_title = _create(
            client,
            archive_api,
            key="semantic-mismatch-key",
            metadata={**CREATE_METADATA, "title": "Changed Synthetic Title"},
        )
        changed_source = _create(
            client,
            archive_api,
            key="semantic-mismatch-key",
            metadata={
                **CREATE_METADATA,
                "source": {"kind": "different synthetic source"},
            },
        )
        changed_route = _create(
            client,
            archive_api,
            key="semantic-mismatch-key",
            path=_create_path(archive_api, section_id=archive_api.other_section_id),
        )
    assert created.status_code == 201
    for response in (changed_title, changed_source):
        _problem(response, 409, "idempotency_mismatch")
    _problem(changed_route, 404, "resource_not_found")
    assert _counts(archive_api.engine)["pages"] == 1


def test_revision_requires_strong_if_match_and_replays_old_etag_after_advance(
    archive_api: ArchiveApiFixture,
) -> None:
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        created = _create(client, archive_api, key="revision-create-key")
        page_id = created.json()["page"]["page_id"]
        path = f"/api/v1/sections/{archive_api.section_id}/pages/{page_id}/revisions"
        media_type, body = _multipart(
            {"source": {"kind": "synthetic revision"}},
            b"# Revision two\n",
        )
        base_headers = [
            _authorization(archive_api.writer_token),
            ("Idempotency-Key", "revision-key"),
            ("Content-Type", media_type),
        ]
        missing = client.post(path, headers=base_headers, content=body)
        malformed = [
            client.post(
                path,
                headers=[*base_headers, *values],
                content=body,
            )
            for values in (
                (("If-Match", "W/" + created.headers["ETag"]),),
                (("If-Match", "*"),),
                (("If-Match", f'{created.headers["ETag"]}, "other"'),),
                (
                    ("If-Match", created.headers["ETag"]),
                    ("If-Match", created.headers["ETag"]),
                ),
            )
        ]
        revised = client.post(
            path,
            headers=[*base_headers, ("If-Match", created.headers["ETag"])],
            content=body,
        )
        stale = client.post(
            path,
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "stale-new-key"),
                ("Content-Type", media_type),
                ("If-Match", created.headers["ETag"]),
            ],
            content=body,
        )
        _, third_body = _multipart(
            {"source": {"kind": "synthetic revision three"}},
            b"# Revision three\n",
        )
        advanced = client.post(
            path,
            headers=[
                _authorization(archive_api.writer_token),
                ("Idempotency-Key", "revision-three-key"),
                ("Content-Type", media_type),
                ("If-Match", revised.headers["ETag"]),
            ],
            content=third_body,
        )
        replayed = client.post(
            path,
            headers=[*base_headers, ("If-Match", created.headers["ETag"])],
            content=body,
        )
        if_match_mismatch = client.post(
            path,
            headers=[
                *base_headers,
                ("If-Match", '"page-v1-' + "f" * 64 + '"'),
            ],
            content=body,
        )

    _problem(missing, 428, "precondition_required")
    for response in malformed:
        _problem(response, 422, "request_validation_failed")
    assert revised.status_code == advanced.status_code == replayed.status_code == 201
    assert revised.headers["Location"] == revised.json()["citation"]["href"]
    assert revised.headers["Location"].endswith("/revisions/2")
    _problem(stale, 412, "revision_conflict")
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert replayed.headers["ETag"] == revised.headers["ETag"]
    assert replayed.headers["ETag"] != advanced.headers["ETag"]
    _problem(if_match_mismatch, 409, "idempotency_mismatch")


@pytest.mark.parametrize("mutation", ["revoke", "disable", "remove"])
def test_transaction_internal_revocation_disable_and_grant_removal_deny(
    archive_api: ArchiveApiFixture,
    mutation: str,
) -> None:
    def factory(connection: Connection) -> ArchiveService:
        repository = AuthRepository(connection)
        if mutation == "revoke":
            credential = repository.get_credential(
                archive_api.library_id,
                archive_api.writer_id,
                archive_api.writer_credential_id,
            )
            assert credential is not None
            repository.revoke_credential(credential, revoked_at=OPERATION_TIME)
        elif mutation == "disable":
            repository.disable_caller(
                archive_api.library_id,
                archive_api.writer_id,
                disabled_at=OPERATION_TIME,
            )
        else:
            assert repository.remove_grant(
                archive_api.library_id,
                archive_api.writer_id,
                archive_api.section_id,
                SectionAction.ARCHIVE_WRITE,
            )
        return ArchiveService(connection, clock=lambda: OPERATION_TIME)

    with TestClient(
        _build_app(archive_api, service_factory=factory),
        raise_server_exceptions=False,
    ) as client:
        response = _create(
            client,
            archive_api,
            key=f"state-change-{mutation}",
        )
    expected = "invalid_token" if mutation in {"revoke", "disable"} else "insufficient_scope"
    _problem(response, 401 if mutation in {"revoke", "disable"} else 403, expected)
    with archive_api.engine.connect() as connection:
        repository = AuthRepository(connection)
        caller = repository.get_caller(archive_api.library_id, archive_api.writer_id)
        credential = repository.get_credential(
            archive_api.library_id,
            archive_api.writer_id,
            archive_api.writer_credential_id,
        )
        grant = repository.get_grant(
            archive_api.library_id,
            archive_api.writer_id,
            archive_api.section_id,
            SectionAction.ARCHIVE_WRITE,
        )
    assert caller is not None and caller.disabled_at is None
    assert credential is not None and credential.revoked_at is None
    assert grant is not None
    assert _counts(archive_api.engine)["pages"] == 0


def test_atomic_fault_rolls_back_and_unknown_text_is_redacted(
    archive_api: ArchiveApiFixture,
) -> None:
    private_failure = "synthetic-private-persistence-failure"

    class FailingArchiveService(ArchiveService):
        def create_archive(self, *args: Any, **kwargs: Any) -> Any:
            super().create_archive(*args, **kwargs)
            raise RuntimeError(private_failure)

    def factory(connection: Any) -> ArchiveService:
        return FailingArchiveService(connection, clock=lambda: OPERATION_TIME)

    with TestClient(
        _build_app(archive_api, service_factory=factory),
        raise_server_exceptions=False,
    ) as client:
        response = _create(client, archive_api, key="fault-key")

    _problem(response, 500, "internal_error")
    assert private_failure not in response.text
    assert archive_api.writer_token not in response.text
    assert "fault-key" not in response.text
    assert _counts(archive_api.engine) == {
        "pages": 0,
        "revisions": 0,
        "page_sources": 0,
        "audit_events": 0,
        "idempotency_records": 0,
    }


def test_last_used_commits_after_envelope_validation_and_coalesces(
    archive_api: ArchiveApiFixture,
) -> None:
    def last_used() -> int | None:
        with archive_api.engine.connect() as connection:
            return connection.execute(
                select(Credential.last_used_at).where(
                    Credential.id == archive_api.writer_credential_id
                )
            ).scalar_one()

    assert last_used() is None
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        rejected = _create(
            client,
            archive_api,
            key="pre-auth-validation-key",
            content=b"\xff",
        )
        touched_at = last_used()
        created = _create(client, archive_api, key="last-used-create-key")
        assert last_used() == touched_at
        replayed = _create(client, archive_api, key="last-used-create-key")
    _problem(rejected, 422, "request_validation_failed")
    assert created.status_code == replayed.status_code == 201
    assert touched_at == OPERATION_TIME
    assert last_used() == touched_at


def test_cancellation_while_streaming_stops_before_database_work(
    archive_api: ArchiveApiFixture,
) -> None:
    media_type, body = _multipart()

    async def scenario() -> None:
        async def interrupted_body() -> Any:
            yield body[:100]
            await anyio.sleep_forever()

        transport = httpx2.ASGITransport(app=_build_app(archive_api))
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            with anyio.move_on_after(0.05) as scope:
                await client.post(
                    _create_path(archive_api),
                    headers=[
                        _authorization(archive_api.writer_token),
                        ("Idempotency-Key", "cancel-before-database-key"),
                        ("Content-Type", media_type),
                    ],
                    content=interrupted_body(),
                )
            assert scope.cancel_called

    anyio.run(scenario)
    with archive_api.engine.connect() as connection:
        last_used = connection.execute(
            select(Credential.last_used_at).where(Credential.id == archive_api.writer_credential_id)
        ).scalar_one()
    assert last_used is None
    assert all(value == 0 for value in _counts(archive_api.engine).values())


def test_cancellation_during_worker_does_not_abandon_atomic_mutation(
    archive_api: ArchiveApiFixture,
) -> None:
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    class BlockingArchiveService(ArchiveService):
        def create_archive(self, *args: Any, **kwargs: Any) -> Any:
            result = super().create_archive(*args, **kwargs)
            started.set()
            assert release.wait(timeout=5)
            completed.set()
            return result

    def factory(connection: Connection) -> ArchiveService:
        return BlockingArchiveService(connection, clock=lambda: OPERATION_TIME)

    media_type, body = _multipart()

    async def scenario() -> None:
        transport = httpx2.ASGITransport(app=_build_app(archive_api, service_factory=factory))
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(
                    partial_async_post,
                    client,
                    _create_path(archive_api),
                    [
                        _authorization(archive_api.writer_token),
                        ("Idempotency-Key", "cancel-during-worker-key"),
                        ("Content-Type", media_type),
                    ],
                    body,
                )
                assert await anyio.to_thread.run_sync(started.wait, 5)
                timer = threading.Timer(0.05, release.set)
                timer.start()
                tasks.cancel_scope.cancel()
            timer.join(timeout=1)

    async def partial_async_post(
        client: httpx2.AsyncClient,
        path: str,
        headers: Sequence[tuple[str, str]],
        content: bytes,
    ) -> None:
        try:
            await client.post(path, headers=headers, content=content)
        except anyio.get_cancelled_exc_class():
            pass

    anyio.run(scenario)
    assert completed.is_set()
    assert _counts(archive_api.engine) == {
        "pages": 1,
        "revisions": 1,
        "page_sources": 1,
        "audit_events": 1,
        "idempotency_records": 1,
    }


def test_source_locator_null_and_omitted_share_one_replay_fingerprint(
    archive_api: ArchiveApiFixture,
) -> None:
    omitted = {
        "title": "Nullable Source",
        "occurred_at": CREATE_METADATA["occurred_at"],
        "source": {"kind": "synthetic"},
    }
    explicit = {
        **omitted,
        "source": {"kind": "synthetic", "locator": None},
    }
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        created = _create(
            client,
            archive_api,
            key="null-normalization-key",
            metadata=omitted,
        )
        replayed = _create(
            client,
            archive_api,
            key="null-normalization-key",
            metadata=explicit,
        )
    assert created.status_code == replayed.status_code == 201
    assert replayed.headers["Idempotency-Replayed"] == "true"


def test_success_persists_exact_content_without_secret_or_locator_in_audit(
    archive_api: ArchiveApiFixture,
) -> None:
    with TestClient(_build_app(archive_api), raise_server_exceptions=False) as client:
        response = _create(client, archive_api, key="redaction-key")
    assert response.status_code == 201
    with archive_api.engine.connect() as connection:
        revision = connection.execute(select(Revision.__table__)).mappings().one()
        audit = connection.execute(select(AuditEvent.__table__)).mappings().one()
        idempotency = connection.execute(select(IdempotencyRecord.__table__)).mappings().one()
    assert revision["content_md"] == CONTENT
    serialized = repr(dict(audit)) + repr(dict(idempotency)) + response.text
    assert archive_api.writer_token not in serialized
    assert "redaction-key" not in serialized
    assert "urn:synthetic:archive" not in serialized
    assert CONTENT.decode() not in repr(dict(audit))
    assert all(connection_value is not None for connection_value in revision.values())


def test_python_multipart_is_used_without_form_or_uploadfile() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "src/patchouli_lib/api/archive_routes.py"
    ).read_text()
    assert "MultipartParser" in source
    assert "request.stream()" in source
    assert "Request.form" not in source
    assert "request.form" not in source
    assert "UploadFile" not in source
    assert "form-data" in source
    assert str(MAX_ARCHIVE_METADATA_BYTES) not in source
    assert MAX_ARCHIVE_MULTIPART_BYTES > MAX_MARKDOWN_BYTES


def test_no_raw_secrets_are_retained_by_public_factory(
    archive_api: ArchiveApiFixture,
) -> None:
    router = create_archive_router(archive_api.engine, clock=lambda: OPERATION_TIME)
    rendered = repr(router) + repr(router.routes)
    assert archive_api.writer_token not in rendered
    assert archive_api.writer_token.rsplit(".", maxsplit=1)[1] not in rendered
    assert "synthetic-create-key" not in rendered
    assert "urn:synthetic:archive" not in rendered
    assert CONTENT.decode() not in rendered
    assert "patchouli_client" not in sys.modules or isinstance(
        sys.modules["patchouli_client"],
        ModuleType,
    )
