from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import Engine, func, select

from patchouli_lib.content.models import Page, Revision
from patchouli_lib.content.schemas import (
    ArchiveIdempotencyKey,
    ArchiveMutationReplay,
    ArchiveMutationSuccess,
    ArchiveSourceInput,
    CreateArchiveCommand,
)
from patchouli_lib.content.service import ArchiveService
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.models import IdempotencyRecord
from patchouli_lib.idempotency.schemas import digest_idempotency_key

from .conftest import OPERATION_TIME, ArchiveScope


def test_two_file_backed_connections_serialize_same_key_to_one_success(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    first_holds_write_lock = Event()
    release_first = Event()
    second_attempted_begin = Event()
    command = CreateArchiveCommand(
        library_id=archive_scope.library_id,
        section_id=archive_scope.section_id,
        book_id=archive_scope.book_id,
        title="Concurrent Synthetic Archive",
        occurred_at=OPERATION_TIME - 1_000,
        source=ArchiveSourceInput(kind="synthetic"),
        content_md=b"# Concurrent synthetic content\n",
        request_id=f"req_{'a' * 32}",
    )
    key = ArchiveIdempotencyKey(key_digest=digest_idempotency_key("synthetic-concurrent-key"))

    def first_writer() -> ArchiveMutationSuccess:
        with immediate_transaction(content_engine) as connection:
            result = ArchiveService(
                connection,
                clock=lambda: OPERATION_TIME,
                page_uid_factory=lambda: bytes.fromhex("aa" * 16),
                revision_id_factory=lambda: f"rev_{'b' * 32}",
                id_factory=lambda: "c" * 32,
            ).create_archive(archive_scope.token.value, command, key)
            assert isinstance(result, ArchiveMutationSuccess)
            first_holds_write_lock.set()
            assert release_first.wait(timeout=5)
            return result

    def retrying_writer() -> ArchiveMutationReplay:
        assert first_holds_write_lock.wait(timeout=5)
        second_attempted_begin.set()
        with immediate_transaction(content_engine) as connection:
            result = ArchiveService(
                connection,
                clock=lambda: OPERATION_TIME,
                page_uid_factory=lambda: bytes.fromhex("dd" * 16),
                revision_id_factory=lambda: f"rev_{'e' * 32}",
                id_factory=lambda: "f" * 32,
            ).create_archive(archive_scope.token.value, command, key)
            assert isinstance(result, ArchiveMutationReplay)
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_writer)
        retry_future = executor.submit(retrying_writer)
        assert first_holds_write_lock.wait(timeout=5)
        assert second_attempted_begin.wait(timeout=5)
        assert not retry_future.done()
        release_first.set()
        created = first_future.result(timeout=10)
        replayed = retry_future.result(timeout=10)

    assert replayed.response.response_body == created.response.response_body
    assert replayed.response.original_request_id == created.response.original_request_id
    with content_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(Page)).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(Revision)).scalar_one() == 1
        assert (
            connection.execute(select(func.count()).select_from(IdempotencyRecord)).scalar_one()
            == 1
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
