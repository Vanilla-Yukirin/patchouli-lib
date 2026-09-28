"""Stable Page IDs and immutable declared-time correction history."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import Engine, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from patchouli_lib.backup import BackupDatabaseError, validate_database
from patchouli_lib.content.models import (
    Page,
    PageOccurrenceCorrection,
    PageOccurrenceCorrectionGuard,
    Revision,
)
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    AppendArchiveRevisionCommand,
    ArchiveMutationReplay,
    ArchiveMutationSuccess,
    ArchiveSourceInput,
    PageOccurrenceCorrectionCommand,
    PageRecord,
)
from patchouli_lib.content.service import ArchiveService
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import canonical_utc_wire

from .conftest import OPERATION_TIME, ArchiveScope
from .test_service import _create_command, _key, _service


def _initial_archive(engine: Engine, scope: ArchiveScope) -> ArchiveMutationSuccess:
    with immediate_transaction(engine) as connection:
        result = _service(connection).create_archive(
            scope.token.value, _create_command(scope), _key()
        )
        assert isinstance(result, ArchiveMutationSuccess)
        return result


def _correct(
    engine: Engine,
    scope: ArchiveScope,
    page_id: str,
    *,
    old: int,
    new: int,
    at: int,
) -> None:
    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        page = repository.get_page(scope.library_id, page_id)
        assert page is not None
        updated, correction = repository.correct_occurrence(
            page,
            PageOccurrenceCorrectionCommand(
                library_id=scope.library_id,
                page_uid=page.page_uid,
                old_occurred_at=old,
                new_occurred_at=new,
                actor_caller_id=scope.caller_id,
                corrected_at=at,
            ),
        )
        assert updated.page_id == page_id
        assert updated.occurred_at == new
        assert correction.at_revision_number == page.current_revision_number
        assert correction.actor_caller_id == scope.caller_id


def _portable_copy(engine: Engine, destination: Path) -> Path:
    raw = engine.raw_connection()
    try:
        source = raw.driver_connection
        assert isinstance(source, sqlite3.Connection)
        with sqlite3.connect(destination) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
    finally:
        raw.close()
    return destination


def test_correction_keeps_id_and_history_and_old_replays_valid(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    tmp_path: Path,
) -> None:
    created = _initial_archive(content_engine, archive_scope)
    original = created.page.occurred_at
    page_id = created.page.page_id
    first_new = original + 25_000_000
    _correct(
        content_engine,
        archive_scope,
        page_id,
        old=original,
        new=first_new,
        at=OPERATION_TIME + 100,
    )

    with immediate_transaction(content_engine) as connection:
        replay = _service(connection).create_archive(
            archive_scope.token.value, _create_command(archive_scope), _key()
        )
        assert isinstance(replay, ArchiveMutationReplay)
        assert replay.body.page.occurred_at == canonical_utc_wire(original)
        page = ContentRepository(connection).get_page(archive_scope.library_id, page_id)
        assert page is not None
        assert page.occurred_at == first_new
        append = ArchiveService(
            connection,
            clock=lambda: OPERATION_TIME + 150,
            revision_id_factory=lambda: "rev_" + "7" * 32,
            id_factory=lambda: "8" * 32,
        ).append_revision(
            archive_scope.token.value,
            AppendArchiveRevisionCommand(
                library_id=archive_scope.library_id,
                section_id=archive_scope.section_id,
                page_id=page_id,
                expected_etag=created.response.response_etag,
                source=ArchiveSourceInput(kind="synthetic"),
                content_md=b"# Corrected time\n",
                request_id="req_" + "9" * 32,
            ),
            _key("append-after-correction"),
        )
        assert isinstance(append, ArchiveMutationSuccess)
        assert append.page.occurred_at == first_new

    second_new = first_new - 5_000_000
    _correct(
        content_engine,
        archive_scope,
        page_id,
        old=first_new,
        new=second_new,
        at=OPERATION_TIME + 200,
    )
    with content_engine.connect() as connection:
        stored_page = connection.execute(select(Page.__table__)).mappings().one()
        assert stored_page["page_id"] == page_id
        assert stored_page["id_timestamp_micros"] == created.page.id_timestamp_micros
        assert PageRecord.model_validate(dict(stored_page)).occurred_at == second_new
        assert connection.scalar(select(func.count()).select_from(Revision)) == 2
        assert (
            connection.scalar(select(func.count()).select_from(PageOccurrenceCorrectionGuard)) == 0
        )
        corrections = (
            connection.execute(
                select(PageOccurrenceCorrection.__table__).order_by(
                    PageOccurrenceCorrection.sequence
                )
            )
            .mappings()
            .all()
        )
        assert [
            (item["sequence"], item["old_occurred_at"], item["new_occurred_at"])
            for item in corrections
        ] == [
            (1, original, first_new),
            (2, first_new, second_new),
        ]
        assert [item["at_revision_number"] for item in corrections] == [1, 2]
    assert validate_database(
        _portable_copy(content_engine, tmp_path / "corrected.db")
    ).schema_revision == ("20260929_0011")


def test_direct_update_and_incomplete_guard_cannot_commit(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    created = _initial_archive(content_engine, archive_scope)
    page = created.page
    with (
        pytest.raises(IntegrityError, match="guard"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(
            update(Page)
            .where(Page.library_id == page.library_id, Page.page_uid == page.page_uid)
            .values(occurred_at=page.occurred_at + 1, updated_at=OPERATION_TIME + 1)
        )
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(
            insert(PageOccurrenceCorrectionGuard),
            {
                "library_id": page.library_id,
                "page_uid": page.page_uid,
                "sequence": 1,
                "old_occurred_at": page.occurred_at,
                "new_occurred_at": page.occurred_at + 1,
                "actor_caller_id": archive_scope.caller_id,
                "corrected_at": OPERATION_TIME + 1,
            },
        )
    with content_engine.connect() as connection:
        assert connection.scalar(select(Page.occurred_at)) == page.occurred_at
        assert connection.scalar(select(func.count()).select_from(PageOccurrenceCorrection)) == 0


def test_correction_log_cannot_be_rewritten(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    created = _initial_archive(content_engine, archive_scope)
    _correct(
        content_engine,
        archive_scope,
        created.page.page_id,
        old=created.page.occurred_at,
        new=created.page.occurred_at + 1,
        at=OPERATION_TIME + 1,
    )
    with (
        pytest.raises(IntegrityError, match="immutable"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(update(PageOccurrenceCorrection).values(new_occurred_at=0))
    with (
        pytest.raises(IntegrityError, match="backwards"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.execute(update(Page).values(updated_at=OPERATION_TIME))


def test_initial_identity_is_still_checked_but_stored_correction_can_diverge() -> None:
    with pytest.raises(ValidationError):
        PageOccurrenceCorrectionCommand(
            library_id="1" * 32,
            page_uid=b"1" * 16,
            old_occurred_at=0,
            new_occurred_at=0,
            actor_caller_id="2" * 32,
            corrected_at=1,
        )


def test_backup_rejects_tampered_chain_and_downgrade_refuses_loss(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    tmp_path: Path,
) -> None:
    created = _initial_archive(content_engine, archive_scope)
    _correct(
        content_engine,
        archive_scope,
        created.page.page_id,
        old=created.page.occurred_at,
        new=created.page.occurred_at + 25_000_000,
        at=OPERATION_TIME + 1,
    )
    with pytest.raises(RuntimeError, match="Cannot discard recorded"):
        command.downgrade(Config("alembic.ini"), "20260929_0010")
    with content_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "20260929_0011"
        )

    database = _portable_copy(content_engine, tmp_path / "tampered-correction.db")
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'trigger' "
            "AND name = 'trg_page_occurrence_corrections_no_update'"
        ).fetchone()
        assert row is not None and isinstance(row[0], str)
        connection.execute("DROP TRIGGER trg_page_occurrence_corrections_no_update")
        connection.execute(
            "UPDATE page_occurrence_corrections SET old_occurred_at = old_occurred_at + 2"
        )
        connection.execute(row[0])
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


@pytest.mark.parametrize("append_first", [False, True])
def test_backup_rejects_correction_before_page_or_revision_creation(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    tmp_path: Path,
    append_first: bool,
) -> None:
    created = _initial_archive(content_engine, archive_scope)
    if append_first:
        with immediate_transaction(content_engine) as connection:
            result = ArchiveService(
                connection,
                clock=lambda: OPERATION_TIME + 100,
                revision_id_factory=lambda: "rev_" + "7" * 32,
                id_factory=lambda: "8" * 32,
            ).append_revision(
                archive_scope.token.value,
                AppendArchiveRevisionCommand(
                    library_id=archive_scope.library_id,
                    section_id=archive_scope.section_id,
                    page_id=created.page.page_id,
                    expected_etag=created.response.response_etag,
                    source=ArchiveSourceInput(kind="synthetic"),
                    content_md=b"# Later revision\n",
                    request_id="req_" + "9" * 32,
                ),
                _key("append-before-correction"),
            )
            assert isinstance(result, ArchiveMutationSuccess)
    _correct(
        content_engine,
        archive_scope,
        created.page.page_id,
        old=created.page.occurred_at,
        new=created.page.occurred_at + 5_000_000,
        at=OPERATION_TIME + 200,
    )
    tampered_at = OPERATION_TIME + 50 if append_first else OPERATION_TIME - 1
    assert tampered_at >= 0
    database = _portable_copy(content_engine, tmp_path / "tampered-correction-time.db")
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'trigger' "
            "AND name = 'trg_page_occurrence_corrections_no_update'"
        ).fetchone()
        assert row is not None and isinstance(row[0], str)
        connection.execute("DROP TRIGGER trg_page_occurrence_corrections_no_update")
        connection.execute(
            "UPDATE page_occurrence_corrections SET corrected_at = ?", (tampered_at,)
        )
        connection.execute(row[0])
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database)
