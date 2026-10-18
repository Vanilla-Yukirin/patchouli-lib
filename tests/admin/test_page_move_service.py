"""Movement preserves immutable content and replays the original successful request."""

from __future__ import annotations

from dataclasses import replace
from time import time

import pytest
from content.conftest import OPERATION_TIME
from content.conftest import content_engine as content_engine
from content.page_move_helpers import _command, _target
from content.test_master_revision_restore_service import _TOKEN, _counts, _etag, _key, _page, _story
from sqlalchemy import Engine, func, select

from patchouli_lib.admin.contracts import MasterDeletePageFormInput
from patchouli_lib.admin.file_set_service import MasterFileSetService
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.move_receipts import MasterMoveReceiptRow
from patchouli_lib.admin.page_move_service import (
    MasterPageMoveConflictError,
    MasterPageMoveNotFoundError,
    MasterPageMoveService,
)
from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.admin.revision_restore_service import MasterRevisionRestoreService
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.admin.session import AdminSessionCodec
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.page_move_models import PageMoveEvent, PageMoveGuard
from patchouli_lib.database import immediate_transaction
from patchouli_lib.search.index_v2 import rebuild_search_index
from patchouli_lib.search.query_v2 import SearchQueryV2Wire
from patchouli_lib.search.service_v2 import search_pages_for_master


def _move_counts(engine: Engine) -> tuple[int, ...]:
    with engine.connect() as connection:
        return tuple(
            connection.scalar(select(func.count()).select_from(model)) or 0
            for model in (PageMoveEvent, PageMoveGuard, MasterMoveReceiptRow)
        )


@pytest.mark.parametrize("kind", ["legacy", "markdown", "mixed", "binary"])
@pytest.mark.parametrize("cross_section", [False, True])
def test_move_preserves_identity_content_and_exact_replay(
    content_engine: Engine, kind: str, cross_section: bool
) -> None:
    story = _story(content_engine, kind)
    before = _page(content_engine, story.command.library_id, story.command.page_id)
    command = _command(before, _target(content_engine, before, cross_section=cross_section))
    counts = _counts(content_engine)
    # A backwards wall clock must still advance the Page's logical clock.
    moves = MasterPageMoveService(content_engine, clock=lambda: OPERATION_TIME)
    first = moves.move_page(command, _key("move"), master_session=story.session)
    after = _page(content_engine, before.library_id, before.page_id)
    expected = before.model_copy(
        update={
            "section_id": command.target_section_id,
            "book_id": command.target_book_id,
            "updated_at": before.updated_at + 1,
        }
    )
    assert after == expected
    assert _counts(content_engine) == (*counts[:3], counts[3] + 1, counts[4])
    assert _move_counts(content_engine) == (1, 0, 1)
    assert first.receipt.changed == 1 and not first.replayed
    replay = moves.move_page(command, _key("move"), master_session=story.session)
    assert replay.receipt == first.receipt and replay.replayed
    assert _move_counts(content_engine) == (1, 0, 1)
    activities = AdminReadModel(content_engine).recent_content_activity()
    movements = [item for item in activities if item.action == "content.page.move"]
    assert len(movements) == 1
    assert movements[0].page_id == before.page_id
    assert movements[0].revision_number == before.current_revision_number
    assert all(
        (item.section_id, item.book_id) == (command.target_section_id, command.target_book_id)
        for item in activities
        if item.page_id == before.page_id
    )


def test_noop_success_stays_noop_after_later_move_and_delete(content_engine: Engine) -> None:
    story = _story(content_engine)
    before = _page(content_engine, story.command.library_id, story.command.page_id)
    target = _target(content_engine, before)
    moves = MasterPageMoveService(content_engine, clock=lambda: OPERATION_TIME + 20)
    noop = _command(before, (before.section_id, before.book_id))
    original = moves.move_page(noop, _key("noop"), master_session=story.session)
    assert original.receipt.changed == 0
    assert _move_counts(content_engine) == (0, 0, 1)
    command = _command(before, target)
    moved = moves.move_page(command, _key("move"), master_session=story.session)
    current = _page(content_engine, before.library_id, before.page_id)
    AdminActionService(content_engine, clock=lambda: OPERATION_TIME + 30).delete_page_as_master(
        current.library_id,
        current.section_id,
        current.book_id,
        current.page_id,
        MasterDeletePageFormInput(expected_etag=_etag(current), confirm_delete="yes"),
        master_session=story.session,
    )
    counts = _counts(content_engine)
    for request, key, receipt in (
        (noop, "noop", original.receipt),
        (command, "move", moved.receipt),
    ):
        replay = moves.move_page(request, _key(key), master_session=story.session)
        assert replay.replayed and replay.receipt == receipt
    deleted = _page(content_engine, before.library_id, before.page_id)
    assert deleted.deleted_at is not None and (deleted.section_id, deleted.book_id) == target
    assert _counts(content_engine) == counts
    assert _move_counts(content_engine) == (1, 0, 2)


def test_move_failure_and_expired_replay_leave_state_unchanged(content_engine: Engine) -> None:
    story = _story(content_engine)
    before = _page(content_engine, story.command.library_id, story.command.page_id)
    command = _command(before, _target(content_engine, before))
    moves = MasterPageMoveService(content_engine, clock=lambda: OPERATION_TIME + 20)
    with pytest.raises(MasterPageMoveNotFoundError):
        moves.move_page(
            command.model_copy(update={"target_book_id": "f" * 32}),
            _key("invalid"),
            master_session=story.session,
        )
    assert _move_counts(content_engine) == (0, 0, 0)
    moves.move_page(command, _key("move"), master_session=story.session)
    with pytest.raises(MasterPageMoveConflictError):
        moves.move_page(
            command.model_copy(update={"target_book_id": "f" * 32}),
            _key("move"),
            master_session=story.session,
        )
    with pytest.raises(AuthenticationError):
        moves.move_page(
            command,
            _key("move"),
            master_session=replace(story.session, expires_at=int(time()) - 1),
        )
    current = _page(content_engine, before.library_id, before.page_id)
    with pytest.raises(FileSetPreconditionFailedError):
        moves.move_page(
            _command(current, (before.section_id, before.book_id)).model_copy(
                update={"expected_etag": command.expected_etag}
            ),
            _key("stale"),
            master_session=story.session,
        )
    assert _move_counts(content_engine) == (1, 0, 1)
    assert _page(content_engine, before.library_id, before.page_id) == current


def test_failed_audit_rolls_back_page_move(
    content_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    story = _story(content_engine)
    before = _page(content_engine, story.command.library_id, story.command.page_id)
    command = _command(before, _target(content_engine, before))
    counts = _counts(content_engine)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Synthetic audit failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", fail)
    with pytest.raises(RuntimeError, match="Synthetic audit failure"):
        MasterPageMoveService(content_engine).move_page(
            command, _key("fail"), master_session=story.session
        )
    assert _page(content_engine, before.library_id, before.page_id) == before
    assert _counts(content_engine) == counts and _move_counts(content_engine) == (0, 0, 0)


def test_move_updates_search_location_and_index_failure_rolls_back_everything(
    content_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    story = _story(content_engine)
    before = _page(content_engine, story.command.library_id, story.command.page_id)
    target = _target(content_engine, before)
    command = _command(before, target)
    codec = AdminSessionCodec(b"synthetic movement session material", ttl_seconds=600)
    cookie, session = codec.issue_master(
        story.session.identity_id, story.session.session_generation
    )
    rebuild_search_index(content_engine)
    query = SearchQueryV2Wire(keywords=["current-marker"]).to_query()
    original = search_pages_for_master(content_engine, cookie, codec, query)
    assert len(original.items) == 1 and original.items[0].page_id == before.page_id
    counts = _counts(content_engine)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Synthetic movement index failure")

    with monkeypatch.context() as isolated:
        isolated.setattr("patchouli_lib.search.index_v2.flush_dirty_since", fail)
        with pytest.raises(RuntimeError, match="Synthetic movement index failure"):
            MasterPageMoveService(content_engine).move_page(
                command, _key("indexed-move"), master_session=session
            )
    assert _page(content_engine, before.library_id, before.page_id) == before
    assert _counts(content_engine) == counts and _move_counts(content_engine) == (0, 0, 0)
    assert search_pages_for_master(content_engine, cookie, codec, query) == original
    MasterPageMoveService(content_engine).move_page(
        command, _key("indexed-move"), master_session=session
    )
    moved = search_pages_for_master(content_engine, cookie, codec, query)
    assert len(moved.items) == 1
    assert (moved.items[0].section_id, moved.items[0].book_id) == target
    assert (
        replace(moved.items[0], section_id=before.section_id, book_id=before.book_id)
        == (original.items[0])
    )


def test_historical_restore_success_replays_after_move(content_engine: Engine) -> None:
    story = _story(content_engine)
    restores = MasterRevisionRestoreService(content_engine, clock=lambda: OPERATION_TIME + 20)
    restored = restores.restore_revision(
        story.command, _key("restore"), master_session=story.session
    )
    before = _page(content_engine, story.command.library_id, story.command.page_id)
    target = _target(content_engine, before)
    MasterPageMoveService(content_engine, clock=lambda: OPERATION_TIME + 30).move_page(
        _command(before, target), _key("move"), master_session=story.session
    )
    counts = _counts(content_engine)
    replay = restores.restore_revision(story.command, _key("restore"), master_session=story.session)
    assert replay.replayed and replay.receipt == restored.receipt
    assert replay.manifest == restored.manifest
    assert _counts(content_engine) == counts


def test_move_round_trip_then_revision_preserves_old_success_but_rotation_denies_old_session(
    content_engine: Engine,
) -> None:
    story = _story(content_engine)
    initial = _page(content_engine, story.command.library_id, story.command.page_id)
    target = _target(content_engine, initial)
    moves = MasterPageMoveService(content_engine, clock=lambda: OPERATION_TIME + 20)
    outward = _command(initial, target)
    first = moves.move_page(outward, _key("outward"), master_session=story.session)
    moved = _page(content_engine, initial.library_id, initial.page_id)
    stable = AdminReadModel(content_engine).get_page_by_id(
        initial.library_id, initial.page_id, 1, authorize=lambda connection: True
    )
    assert stable is not None
    assert (stable.section.id, stable.book.id) == target
    moves.move_page(
        _command(moved, (initial.section_id, initial.book_id)),
        _key("return"),
        master_session=story.session,
    )
    returned = _page(content_engine, initial.library_id, initial.page_id)
    MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME + 30).revise_page(
        story.append((("after.md", b"# After round trip\n"),), _etag(returned)),
        initial.book_id,
        _key("after-round-trip"),
        master_session=story.session,
    )
    current = _page(content_engine, initial.library_id, initial.page_id)
    counts = _counts(content_engine)
    replay = moves.move_page(outward, _key("outward"), master_session=story.session)
    assert replay.replayed and replay.receipt == first.receipt
    assert _page(content_engine, initial.library_id, initial.page_id) == current
    assert _counts(content_engine) == counts
    with immediate_transaction(content_engine) as connection:
        rotated = MasterTokenRepository(connection).rotate(
            _TOKEN, "synthetic rotated movement master token", now=OPERATION_TIME + 40
        )
        assert rotated is not None
    with pytest.raises(AuthenticationError):
        moves.move_page(outward, _key("outward"), master_session=story.session)
    renewed = replace(story.session, session_generation=rotated.session_generation)
    assert (
        moves.move_page(outward, _key("outward"), master_session=renewed).receipt == first.receipt
    )
    assert _move_counts(content_engine) == (2, 0, 2)
