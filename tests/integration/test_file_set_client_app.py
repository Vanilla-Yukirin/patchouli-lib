"""Typed-client file-set round trips against the real application, without a network."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from time import time_ns

import anyio
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from patchouli_client import (
    BearerToken,
    FileSetCreateMetadata,
    FileSetFile,
    FileSetManifest,
    FileSetRevisionMetadata,
    FileSetSource,
    IdempotencyKey,
    PatchouliClient,
    ProblemError,
)
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

ROOT = Path(__file__).resolve().parents[2]
SOURCE = FileSetSource("synthetic")


class _SyncASGITransport(httpx.BaseTransport):
    """Bridge the synchronous typed client to HTTPX's asynchronous ASGI transport."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def dispatch() -> tuple[int, list[tuple[bytes, bytes]], bytes]:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self._app),
                base_url="https://synthetic.invalid",
            ) as client:
                response = await client.request(
                    request.method,
                    str(request.url),
                    headers=request.headers,
                    content=request.read(),
                )
                return response.status_code, response.headers.raw, response.content

        status, headers, body = anyio.run(dispatch)
        # Leave the returned stream unconsumed for the typed client's bounded download path.
        return httpx.Response(
            status, headers=headers, stream=httpx.ByteStream(body), request=request
        )


@pytest.fixture
def file_set_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[PatchouliClient, str, str, str, BearerToken]]:
    database_url = f"sqlite:///{(tmp_path / 'file-set-client-app.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    app = create_app(Settings.model_validate({"environment": "test", "database_url": database_url}))
    engine: Engine = app.state.engine
    now = time_ns() // 1_000
    try:
        with immediate_transaction(engine) as connection:
            identifiers = iter(("1" * 32, "2" * 32, "3" * 32))
            structure = LibrarySeedService(
                LibraryRepository(connection),
                id_factory=lambda: next(identifiers),
                clock=lambda: now,
            ).seed(
                LibraryStructureSeed(
                    library_name="Synthetic integrated library",
                    section_name="Synthetic integrated section",
                    book_name="Synthetic integrated book",
                )
            )
            auth = AuthRepository(connection)
            caller = auth.add_caller(
                NewCaller(
                    id="4" * 32,
                    library_id=structure.library.id,
                    kind=CallerKind.AGENT,
                    name="Synthetic integrated caller",
                    created_at=now,
                    updated_at=now,
                )
            )
            token = CredentialIssuer(auth, id_factory=lambda: "5" * 32, clock=lambda: now).issue(
                caller, expires_at=now + 3_600_000_000
            )
            for action in (SectionAction.ARCHIVE_WRITE, SectionAction.PAGE_READ):
                auth.add_grant(
                    NewSectionGrant(
                        library_id=structure.library.id,
                        caller_id=caller.id,
                        section_id=structure.section.id,
                        action=action,
                        created_at=now,
                    )
                )
        with PatchouliClient(
            "https://synthetic.invalid", http_transport=_SyncASGITransport(app)
        ) as client:
            yield (
                client,
                structure.library.id,
                structure.section.id,
                structure.book.id,
                BearerToken(token.value),
            )
    finally:
        engine.dispose()


def _assert_exact_files(
    client: PatchouliClient,
    library_id: str,
    section_id: str,
    token: BearerToken,
    manifest: FileSetManifest,
    expected: tuple[FileSetFile, ...],
) -> None:
    exact = client.get_file_set_manifest(
        library_id, section_id, manifest.page_id, manifest.revision_id, token=token
    ).value
    assert exact == manifest
    expected_by_name = {file.filename: file.body for file in expected}
    assert [(item.filename, item.size_bytes, item.content_sha256) for item in exact.files] == [
        (name, len(body), hashlib.sha256(body).hexdigest())
        for name, body in sorted(expected_by_name.items(), key=lambda item: item[0].encode())
    ]
    for summary in exact.files:
        download = client.download_file_set_file(
            library_id,
            section_id,
            manifest.page_id,
            manifest.revision_id,
            summary,
            token=token,
        )
        assert download.value == expected_by_name[summary.filename]


@pytest.mark.parametrize(
    "first_files",
    [
        (FileSetFile("content.md", b"# Synthetic page\n"),),
        (
            FileSetFile("figure.png", b"\x89PNG\r\n\x1a\n\x00\xff"),
            FileSetFile("content.md", b"![figure](figure.png)\n"),
        ),
        (FileSetFile("only.bin", b"\x00\xff\x10\x80"),),
    ],
    ids=["single-markdown", "mixed-files", "binary-only"],
)
def test_typed_client_create_read_revise_and_preserve_history(
    file_set_app: tuple[PatchouliClient, str, str, str, BearerToken],
    first_files: tuple[FileSetFile, ...],
) -> None:
    client, library_id, section_id, book_id, token = file_set_app
    created = client.create_file_set(
        library_id,
        section_id,
        book_id,
        FileSetCreateMetadata("Synthetic page", SOURCE, datetime(2026, 9, 29, 12, tzinfo=UTC)),
        first_files,
        token=token,
        idempotency_key=IdempotencyKey("synthetic-create"),
    )
    first = created.value.manifest
    assert first.revision_number == 1
    assert created.metadata.etag is not None
    _assert_exact_files(client, library_id, section_id, token, first, first_files)
    current = client.get_current_file_set(library_id, section_id, first.page_id, token=token)
    assert current.value == first
    assert current.metadata.etag == created.metadata.etag

    second_files = (FileSetFile("replacement.txt", b"new revision\x00bytes"),)
    revised = client.revise_file_set(
        library_id,
        section_id,
        first.page_id,
        FileSetRevisionMetadata(SOURCE),
        second_files,
        token=token,
        idempotency_key=IdempotencyKey("synthetic-revise"),
        if_match=current.metadata.etag,
    )
    second = revised.value.manifest
    assert revised.value.changed is True
    assert second.page_id == first.page_id
    assert second.revision_id != first.revision_id
    assert second.revision_number == 2
    assert second.snapshot_sha256 != first.snapshot_sha256
    assert revised.metadata.etag != created.metadata.etag
    assert (
        client.get_current_file_set(library_id, section_id, first.page_id, token=token).value
        == second
    )
    _assert_exact_files(client, library_id, section_id, token, second, second_files)
    _assert_exact_files(client, library_id, section_id, token, first, first_files)


def test_replay_keeps_original_result_and_stale_etag_cannot_mutate(
    file_set_app: tuple[PatchouliClient, str, str, str, BearerToken],
) -> None:
    client, library_id, section_id, book_id, token = file_set_app
    first_files = (FileSetFile("content.md", b"first"),)
    metadata = FileSetCreateMetadata("Synthetic page", SOURCE)
    key = IdempotencyKey("synthetic-replayed-create")
    created = client.create_file_set(
        library_id, section_id, book_id, metadata, first_files, token=token, idempotency_key=key
    )
    replayed = client.create_file_set(
        library_id, section_id, book_id, metadata, first_files, token=token, idempotency_key=key
    )
    assert replayed.value == created.value
    assert replayed.metadata.etag == created.metadata.etag
    assert replayed.metadata.idempotency_replayed is True

    page_id = created.value.manifest.page_id
    etag = created.metadata.etag
    assert etag is not None
    second_files = (FileSetFile("content.md", b"second"),)
    revision_metadata = FileSetRevisionMetadata(SOURCE)
    revised = client.revise_file_set(
        library_id,
        section_id,
        page_id,
        revision_metadata,
        second_files,
        token=token,
        idempotency_key=IdempotencyKey("synthetic-replayed-revise"),
        if_match=etag,
    )
    replayed_revision = client.revise_file_set(
        library_id,
        section_id,
        page_id,
        revision_metadata,
        second_files,
        token=token,
        idempotency_key=IdempotencyKey("synthetic-replayed-revise"),
        if_match=etag,
    )
    assert replayed_revision.value == revised.value
    assert replayed_revision.metadata.etag == revised.metadata.etag
    assert replayed_revision.metadata.idempotency_replayed is True

    with pytest.raises(ProblemError) as conflict:
        client.revise_file_set(
            library_id,
            section_id,
            page_id,
            revision_metadata,
            (FileSetFile("content.md", b"third"),),
            token=token,
            idempotency_key=IdempotencyKey("synthetic-stale-precondition"),
            if_match=etag,
        )
    assert conflict.value.problem.status == 412
    assert conflict.value.problem.code == "revision_conflict"
    current = client.get_current_file_set(library_id, section_id, page_id, token=token)
    assert current.value == revised.value.manifest
    assert current.metadata.etag == revised.metadata.etag
    _assert_exact_files(client, library_id, section_id, token, created.value.manifest, first_files)
