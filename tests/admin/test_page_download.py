"""Exact admin file reads over migrated, sealed synthetic Page Revisions."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from time import time

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from sqlalchemy import Connection, Engine

from patchouli_lib.admin.contracts import MasterDeletePageFormInput
from patchouli_lib.admin.file_download import (
    AdminFileDownloadPersistenceError,
    AdminFileDownloadService,
)
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.content.file_set_service import FileSetRevisionService
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import NewPageSource
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.retrieval.repository import RetrievalRepository, StoredRevisionFile

_ROOT = Path(__file__).resolve().parents[2]
_LEGACY = b"# Synthetic original\n"
_MIXED = b"# Synthetic mixed\n<script>not inline</script>\n"
_BINARY = b"\x00\xff\x01<script>never rendered</script>"


@dataclass(frozen=True, slots=True)
class DownloadFixture:
    engine: Engine
    library_id: str
    section_id: str
    book_id: str
    page_id: str

    @property
    def service(self) -> AdminFileDownloadService:
        return AdminFileDownloadService(self.engine)

    def get(
        self,
        number: int,
        filename: str,
        *,
        authorize: Callable[[Connection], bool] | None = None,
    ) -> bytes | None:
        callback = authorize if authorize is not None else lambda _connection: True
        result = self.service.get_file(
            self.library_id,
            self.section_id,
            self.book_id,
            self.page_id,
            number,
            filename,
            authorize=callback,
        )
        return None if result is None else result.content


@pytest.fixture
def download_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DownloadFixture]:
    database_url = f"sqlite:///{(tmp_path / 'admin-download.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(_ROOT / "alembic.ini")), "head")
    engine = build_engine(database_url)
    library_id, section_id, book_id = seed_library_structure(engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
        content_md=_LEGACY,
    )
    page, _, _, _, original_source = values
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, values)
    fixture = DownloadFixture(engine, library_id, section_id, book_id, page.page_id)

    def append(number: int, files: tuple[tuple[str, bytes], ...]) -> None:
        revision_id = "rev_" + str(number + 1) * 32
        revision_at = (number + 1) * 1_000_000
        source: NewPageSource = original_source.model_copy(
            update={
                "source_id": str(number + 4) * 32,
                "revision_id": revision_id,
                "revision_number": number,
                "created_at": revision_at,
            }
        )
        with immediate_transaction(engine) as connection:
            current = ContentRepository(connection).get_page(library_id, page.page_id)
            assert current is not None
            FileSetRevisionService(connection).append_existing_page(
                library_id=library_id,
                page_id=page.page_id,
                expected_etag=page_current_etag(
                    current.page_uid,
                    current.current_revision_id,
                    current.current_revision_number,
                    current.occurred_at,
                    current.updated_at,
                ),
                files=files,
                revision_id=revision_id,
                revision_at=revision_at,
                source=source,
            )

    append(2, (("content.md", _MIXED), ("payload.bin", _BINARY)))
    append(3, (("payload.bin", _BINARY),))
    try:
        yield fixture
    finally:
        engine.dispose()


def test_current_and_historical_downloads_return_exact_raw_bytes(
    download_fixture: DownloadFixture,
) -> None:
    fixture = download_fixture
    assert fixture.get(1, "content.md") == _LEGACY
    assert fixture.get(2, "content.md") == _MIXED
    assert fixture.get(2, "payload.bin") == _BINARY
    assert fixture.get(3, "payload.bin") == _BINARY
    assert fixture.get(3, "content.md") is None


def test_authentication_precedes_path_inspection_and_uses_real_read_transaction(
    download_fixture: DownloadFixture,
) -> None:
    fixture = download_fixture
    checked: list[bool] = []

    def reject(connection: Connection) -> bool:
        raw = connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
        checked.append(raw.in_transaction)
        return False

    with pytest.raises(AuthenticationError):
        fixture.service.get_file(
            "invalid-library",
            fixture.section_id,
            fixture.book_id,
            fixture.page_id,
            0,
            "../invalid",
            authorize=reject,
        )
    assert checked == [True]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("library_id", "f" * 32),
        ("section_id", "f" * 32),
        ("book_id", "f" * 32),
        ("page_id", "not-a-page"),
        ("revision_number", 0),
        ("revision_number", 4),
        ("revision_number", 1 << 63),
        ("filename", "../content.md"),
        ("filename", "CON"),
        ("filename", "missing.md"),
    ],
)
def test_wrong_path_version_and_file_are_not_found(
    download_fixture: DownloadFixture, field: str, value: object
) -> None:
    fixture = download_fixture
    arguments = {
        "library_id": fixture.library_id,
        "section_id": fixture.section_id,
        "book_id": fixture.book_id,
        "page_id": fixture.page_id,
        "revision_number": 2,
        "filename": "payload.bin",
    }
    arguments[field] = value
    assert fixture.service.get_file(**arguments, authorize=lambda _connection: True) is None  # type: ignore[arg-type]


def test_wrong_existing_book_and_deleted_page_are_not_found(
    download_fixture: DownloadFixture,
) -> None:
    fixture = download_fixture
    _, _, other_book_id = seed_library_structure(fixture.engine, prefix="4", label="Second")
    assert (
        fixture.service.get_file(
            fixture.library_id,
            fixture.section_id,
            other_book_id,
            fixture.page_id,
            2,
            "payload.bin",
            authorize=lambda _connection: True,
        )
        is None
    )
    with fixture.engine.connect() as connection:
        page = ContentRepository(connection).get_page(fixture.library_id, fixture.page_id)
        assert page is not None
        expected_etag = page_current_etag(
            page.page_uid,
            page.current_revision_id,
            page.current_revision_number,
            page.occurred_at,
            page.updated_at,
        )
    with immediate_transaction(fixture.engine) as connection:
        state = MasterTokenRepository(connection).initialize_from_local_cli(
            "synthetic master token for download deletion", now=1_000
        )
    session = MasterAdminSession(
        expires_at=int(time()) + 600,
        csrf_token="s" * 32,
        identity_id=state.identity_id,
        session_generation=state.session_generation,
    )
    AdminActionService(fixture.engine, clock=lambda: 5_000_000).delete_page_as_master(
        fixture.library_id,
        fixture.section_id,
        fixture.book_id,
        fixture.page_id,
        MasterDeletePageFormInput(expected_etag=expected_etag, confirm_delete="yes"),
        master_session=session,
    )
    assert fixture.get(1, "content.md") is None
    assert fixture.get(2, "payload.bin") is None


def test_corrupt_other_file_prevents_target_download(
    download_fixture: DownloadFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = RetrievalRepository.list_revision_files

    def corrupt_other_file(
        repository: RetrievalRepository,
        library_id: str,
        page_uid: bytes,
        revision_id: str,
        revision_number: int,
    ) -> tuple[StoredRevisionFile, ...]:
        stored = original(repository, library_id, page_uid, revision_id, revision_number)
        if revision_number != 2:
            return stored
        return tuple(
            StoredRevisionFile(
                item.name,
                item.content,
                item.size_bytes,
                b"x" * 32 if item.name == "content.md" else item.content_sha256,
            )
            for item in stored
        )

    monkeypatch.setattr(RetrievalRepository, "list_revision_files", corrupt_other_file)
    with pytest.raises(AdminFileDownloadPersistenceError):
        download_fixture.get(2, "payload.bin")
