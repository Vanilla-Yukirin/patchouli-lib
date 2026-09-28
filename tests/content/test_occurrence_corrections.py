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
    CorrectArchiveOccurrenceCommand,
    PageOccurrenceCorrectionCommand,
    PageRecord,
)
from patchouli_lib.content.service import (
    ArchivePreconditionFailedError,
    ArchiveService,
    legacy_page_current_etag,
    page_current_etag,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import OriginalResponse, ReplayResponse
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

    stale_revision = AppendArchiveRevisionCommand(
        library_id=archive_scope.library_id,
        section_id=archive_scope.section_id,
        page_id=page_id,
        expected_etag=created.response.response_etag,
        source=ArchiveSourceInput(kind="synthetic"),
        content_md=b"# Corrected time\n",
        request_id="req_" + "9" * 32,
    )
    with (
        immediate_transaction(content_engine) as connection,
        pytest.raises(ArchivePreconditionFailedError),
    ):
        ArchiveService(connection, clock=lambda: OPERATION_TIME + 150).append_revision(
            archive_scope.token.value,
            stale_revision,
            _key("stale-after-correction"),
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
        current_etag = page_current_etag(
            page.page_uid,
            page.current_revision_id,
            page.current_revision_number,
            page.occurred_at,
            page.updated_at,
        )
        assert current_etag != created.response.response_etag
        append = ArchiveService(
            connection,
            clock=lambda: OPERATION_TIME + 150,
            revision_id_factory=lambda: "rev_" + "7" * 32,
            id_factory=lambda: "8" * 32,
        ).append_revision(
            archive_scope.token.value,
            stale_revision.model_copy(update={"expected_etag": current_etag}),
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


def test_authorized_correction_replay_and_backup_graph(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    tmp_path: Path,
) -> None:
    created = _initial_archive(content_engine, archive_scope)
    command = CorrectArchiveOccurrenceCommand(
        library_id=archive_scope.library_id,
        section_id=archive_scope.section_id,
        page_id=created.page.page_id,
        expected_etag=created.response.response_etag,
        occurred_at=created.page.occurred_at + 3_000_000,
        request_id="req_" + "f" * 32,
    )
    with immediate_transaction(content_engine) as connection:
        result = ArchiveService(
            connection,
            clock=lambda: OPERATION_TIME + 50,
            id_factory=lambda: "6" * 32,
        ).correct_occurrence(archive_scope.token.value, command, _key("correct-occurrence"))
        assert isinstance(result, OriginalResponse)
        assert result.response_status == 200
        assert result.response_location == (
            f"/api/v1/sections/{archive_scope.section_id}/pages/{created.page.page_id}"
        )
    with immediate_transaction(content_engine) as connection:
        replay = ArchiveService(connection, clock=lambda: OPERATION_TIME + 100).correct_occurrence(
            archive_scope.token.value, command, _key("correct-occurrence")
        )
        assert isinstance(replay, ReplayResponse)
        assert replay.response_body == result.response_body
        assert replay.response_etag == result.response_etag
        appended = ArchiveService(
            connection,
            clock=lambda: OPERATION_TIME + 150,
            id_factory=lambda: "7" * 32,
            revision_id_factory=lambda: "rev_" + "8" * 32,
        ).append_revision(
            archive_scope.token.value,
            AppendArchiveRevisionCommand(
                library_id=archive_scope.library_id,
                section_id=archive_scope.section_id,
                page_id=created.page.page_id,
                expected_etag=result.response_etag,
                source=ArchiveSourceInput(kind="synthetic"),
                content_md=b"# Later revision\n",
                request_id="req_" + "e" * 32,
            ),
            _key("append-after-correct-api"),
        )
        assert isinstance(appended, ArchiveMutationSuccess)
        assert appended.page.current_revision_number == 2
    with immediate_transaction(content_engine) as connection:
        later_replay = ArchiveService(
            connection, clock=lambda: OPERATION_TIME + 200
        ).correct_occurrence(archive_scope.token.value, command, _key("correct-occurrence"))
        assert isinstance(later_replay, ReplayResponse)
        assert later_replay.response_body == result.response_body
        assert later_replay.response_etag == result.response_etag
    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Revision)) == 2
        assert connection.scalar(select(func.count()).select_from(PageOccurrenceCorrection)) == 1
    validated = _portable_copy(content_engine, tmp_path / "correction-replay.db")
    assert validate_database(validated).schema_revision == "20260929_0011"


def test_backup_rejects_patch_replay_that_is_not_its_correction(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    tmp_path: Path,
) -> None:
    created = _initial_archive(content_engine, archive_scope)
    command = CorrectArchiveOccurrenceCommand(
        library_id=archive_scope.library_id,
        section_id=archive_scope.section_id,
        page_id=created.page.page_id,
        expected_etag=created.response.response_etag,
        occurred_at=created.page.occurred_at + 3_000_000,
        request_id="req_" + "f" * 32,
    )
    with immediate_transaction(content_engine) as connection:
        result = ArchiveService(
            connection,
            clock=lambda: OPERATION_TIME + 50,
            id_factory=lambda: "6" * 32,
        ).correct_occurrence(archive_scope.token.value, command, _key("correct-occurrence"))
        assert isinstance(result, OriginalResponse)

    for field, replacement in (
        ("response_etag", created.response.response_etag),
        ("original_request_timestamp", canonical_utc_wire(OPERATION_TIME + 51)),
        ("response_status", 201),
    ):
        database = _portable_copy(content_engine, tmp_path / f"patch-{field}.db")
        with sqlite3.connect(database) as connection:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'trg_idempotency_records_immutable_update'"
            ).fetchone()
            assert trigger is not None and isinstance(trigger[0], str)
            connection.execute("DROP TRIGGER trg_idempotency_records_immutable_update")
            connection.execute(
                f"UPDATE idempotency_records SET {field} = ? WHERE method = 'PATCH'",
                (replacement,),
            )
            connection.execute(trigger[0])
            connection.commit()
        with pytest.raises(BackupDatabaseError):
            validate_database(database)


def test_historical_v1_replay_survives_later_occurrence_correction(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    tmp_path: Path,
) -> None:
    created = _initial_archive(content_engine, archive_scope)
    original_occurrence = created.page.occurred_at
    legacy_etag = legacy_page_current_etag(
        created.page.page_uid,
        created.revision.revision_id,
        created.revision.revision_number,
    )
    # Convert only this test fixture's immutable replay header to the format
    # written by older releases; its body and original request stay unchanged.
    with content_engine.begin() as connection:
        trigger_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'trg_idempotency_records_immutable_update'"
        ).scalar_one()
        connection.exec_driver_sql("DROP TRIGGER trg_idempotency_records_immutable_update")
        updated = connection.exec_driver_sql(
            "UPDATE idempotency_records SET response_etag = ? WHERE response_etag = ?",
            (legacy_etag, created.response.response_etag),
        )
        assert updated.rowcount == 1
        connection.exec_driver_sql(trigger_sql)

    corrected_occurrence = original_occurrence + 25_000_000
    _correct(
        content_engine,
        archive_scope,
        created.page.page_id,
        old=original_occurrence,
        new=corrected_occurrence,
        at=OPERATION_TIME + 100,
    )
    with immediate_transaction(content_engine) as connection:
        replay = _service(connection).create_archive(
            archive_scope.token.value,
            _create_command(archive_scope),
            _key(),
        )
        assert isinstance(replay, ArchiveMutationReplay)
        assert replay.response.response_etag == legacy_etag
        assert replay.body.page.occurred_at == canonical_utc_wire(original_occurrence)
        page = ContentRepository(connection).get_page(
            archive_scope.library_id, created.page.page_id
        )
        assert page is not None and page.occurred_at == corrected_occurrence
        assert connection.scalar(select(func.count()).select_from(Revision)) == 1
        assert connection.scalar(select(func.count()).select_from(PageOccurrenceCorrection)) == 1
    assert validate_database(
        _portable_copy(content_engine, tmp_path / "historical-v1-corrected.db")
    ).schema_revision == ("20260929_0011")


def test_same_microsecond_correction_and_revision_keep_strict_clock_and_etag(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    tmp_path: Path,
) -> None:
    created = _initial_archive(content_engine, archive_scope)
    first_occurrence = created.page.occurred_at + 1
    _correct(
        content_engine,
        archive_scope,
        created.page.page_id,
        old=created.page.occurred_at,
        new=first_occurrence,
        at=OPERATION_TIME,
    )
    with immediate_transaction(content_engine) as connection:
        current = ContentRepository(connection).get_page(
            archive_scope.library_id, created.page.page_id
        )
        assert current is not None
        assert current.updated_at == created.page.updated_at + 1
        corrected_etag = page_current_etag(
            current.page_uid,
            current.current_revision_id,
            current.current_revision_number,
            current.occurred_at,
            current.updated_at,
        )
        assert corrected_etag != created.response.response_etag
        revised = ArchiveService(
            connection,
            clock=lambda: OPERATION_TIME,
        ).append_revision(
            archive_scope.token.value,
            AppendArchiveRevisionCommand(
                library_id=archive_scope.library_id,
                section_id=archive_scope.section_id,
                page_id=created.page.page_id,
                expected_etag=corrected_etag,
                source=ArchiveSourceInput(kind="synthetic"),
                content_md=b"# Same-tick revision\n",
                request_id="req_" + "a" * 32,
            ),
            _key("same-tick-revision"),
        )
        assert isinstance(revised, ArchiveMutationSuccess)
        assert revised.page.updated_at == current.updated_at + 1
        assert revised.revision.created_at == revised.page.updated_at

    second_occurrence = first_occurrence + 1
    _correct(
        content_engine,
        archive_scope,
        created.page.page_id,
        old=first_occurrence,
        new=second_occurrence,
        at=OPERATION_TIME,
    )
    with content_engine.connect() as connection:
        stored = ContentRepository(connection).get_page(
            archive_scope.library_id, created.page.page_id
        )
        assert stored is not None
        assert stored.updated_at == revised.page.updated_at + 1
        assert (
            page_current_etag(
                stored.page_uid,
                stored.current_revision_id,
                stored.current_revision_number,
                stored.occurred_at,
                stored.updated_at,
            )
            != revised.response.response_etag
        )
    portable = _portable_copy(content_engine, tmp_path / "same-microsecond.db")
    assert validate_database(portable).schema_revision == ("20260929_0011")
    with sqlite3.connect(portable) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'trg_idempotency_records_immutable_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER trg_idempotency_records_immutable_update")
        connection.execute(trigger_sql)
    assert validate_database(portable).schema_revision == ("20260929_0011")
    with sqlite3.connect(portable) as connection:
        connection.execute("DROP TRIGGER trg_idempotency_records_immutable_update")
        connection.execute(
            "UPDATE idempotency_records SET response_etag = ? WHERE response_etag = ?",
            (created.response.response_etag, revised.response.response_etag),
        )
        connection.execute(trigger_sql)
    with pytest.raises(BackupDatabaseError):
        validate_database(portable)


def test_backup_rejects_correction_timestamp_at_or_after_next_revision(
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
        new=created.page.occurred_at + 1,
        at=OPERATION_TIME + 1,
    )
    with immediate_transaction(content_engine) as connection:
        page = ContentRepository(connection).get_page(
            archive_scope.library_id, created.page.page_id
        )
        assert page is not None
        revised = ArchiveService(
            connection,
            clock=lambda: OPERATION_TIME + 2,
            revision_id_factory=lambda: "rev_" + "7" * 32,
            id_factory=lambda: "8" * 32,
        ).append_revision(
            archive_scope.token.value,
            AppendArchiveRevisionCommand(
                library_id=archive_scope.library_id,
                section_id=archive_scope.section_id,
                page_id=page.page_id,
                expected_etag=page_current_etag(
                    page.page_uid,
                    page.current_revision_id,
                    page.current_revision_number,
                    page.occurred_at,
                    page.updated_at,
                ),
                source=ArchiveSourceInput(kind="synthetic"),
                content_md=b"# Later revision\n",
                request_id="req_" + "d" * 32,
            ),
            _key("revision-after-correction"),
        )
        assert isinstance(revised, ArchiveMutationSuccess)

    database = _portable_copy(content_engine, tmp_path / "correction-after-revision.db")
    assert validate_database(database).schema_revision == "20260929_0011"
    with sqlite3.connect(database) as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'trigger' "
            "AND name = 'trg_page_occurrence_corrections_no_update'"
        ).fetchone()
        assert trigger is not None and isinstance(trigger[0], str)
        connection.execute("DROP TRIGGER trg_page_occurrence_corrections_no_update")
        connection.execute(
            "UPDATE page_occurrence_corrections SET corrected_at = ?",
            (revised.revision.created_at,),
        )
        connection.execute(trigger[0])
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


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
