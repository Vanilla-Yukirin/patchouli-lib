"""Synthetic real-schema snapshots for the unindexed current-Page projection."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from sqlalchemy import Engine

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.search.current_page_projection import (
    ProjectionTransactionRequiredError,
    load_current_page_projection,
)
from patchouli_lib.search.ngram import MAX_INPUT_BYTES
from patchouli_lib.search.projection_v2 import project_page_text
from patchouli_lib.tags.repository import TagRepository

CLASSIFICATION_VERSION = "synthetic-explicit-classification-v1"


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    database_url = f"sqlite:///{(tmp_path / 'search-projection.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "head")
    value = build_engine(database_url)
    try:
        yield value
    finally:
        value.dispose()


def _seed_legacy_page(engine: Engine) -> tuple[str, str, bytes, str, int]:
    library_id, section_id, book_id = seed_library_structure(engine)
    page, _, _, _, _ = values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
        title="合成页面",
        content_md="鸢尾旧稿独有\n".encode(),
    )
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, values)
    return library_id, section_id, page.page_uid, page.page_id, page.occurred_at


def test_legacy_current_revision_uses_verified_files_tags_and_declared_time(
    engine: Engine,
) -> None:
    library_id, section_id, page_uid, page_id, occurred_at = _seed_legacy_page(engine)
    with immediate_transaction(engine) as connection:
        tags = TagRepository(connection)
        tags.add_tag(library_id=library_id, tag_id="a" * 32, name="合成标签", created_at=0)
        tags.attach_page(library_id=library_id, page_uid=page_uid, tag_id="a" * 32, created_at=0)

    with engine.connect() as connection:
        with pytest.raises(ProjectionTransactionRequiredError):
            load_current_page_projection(
                connection,
                library_id=library_id,
                page_uid=page_uid,
                file_classification={"content.md": "text"},
                classification_version=CLASSIFICATION_VERSION,
            )
        connection.exec_driver_sql("BEGIN")
        projected = load_current_page_projection(
            connection,
            library_id=library_id,
            page_uid=page_uid,
            file_classification={"content.md": "text"},
            classification_version=CLASSIFICATION_VERSION,
        )
        assert (
            load_current_page_projection(
                connection,
                library_id="f" * 32,
                page_uid=page_uid,
                file_classification={"content.md": "text"},
                classification_version=CLASSIFICATION_VERSION,
            )
            is None
        )
        connection.rollback()

    assert projected is not None
    assert (projected.library_id, projected.section_id, projected.page_id) == (
        library_id,
        section_id,
        page_id,
    )
    assert projected.occurred_at == occurred_at
    assert projected.revision_number == 1
    assert [(tag.library_id, tag.tag_id, tag.display_name) for tag in projected.tags] == [
        (library_id, "a" * 32, "合成标签")
    ]
    assert [(item.name, item.indexing_kind) for item in projected.files] == [("content.md", "text")]
    assert projected.terms == project_page_text(
        title="合成页面", body=None, text_files=(("content.md", "鸢尾旧稿独有\n".encode()),)
    )


def test_only_current_revision_is_projected_and_opaque_bytes_are_excluded(
    engine: Engine,
) -> None:
    library_id, _section_id, page_uid, page_id, occurred_at = _seed_legacy_page(engine)
    text = "夜航新稿全文\n".encode()
    binary = "不应索引的秘密".encode()
    manifest = build_file_manifest((("notes.md", text), ("sealed.bin", binary)))
    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        page = repository.get_page(library_id, page_id)
        assert page is not None
        repository.add_file_set_revision(
            page,
            revision_id=f"rev_{'b' * 32}",
            created_at=3_000_000,
            manifest=manifest,
        )
        assert (
            repository.advance_file_set_current_revision(
                page, revision_id=f"rev_{'b' * 32}", updated_at=3_000_000
            )
            is not None
        )

    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        projected = load_current_page_projection(
            connection,
            library_id=library_id,
            page_uid=page_uid,
            file_classification={"notes.md": "text", "sealed.bin": "opaque"},
            classification_version=CLASSIFICATION_VERSION,
        )
        connection.rollback()

    assert projected is not None
    assert projected.revision_id == f"rev_{'b' * 32}"
    assert projected.revision_number == 2
    assert projected.occurred_at == occurred_at
    assert projected.snapshot_sha256 == manifest.snapshot_sha256
    assert projected.terms == project_page_text(
        title="合成页面",
        body=None,
        text_files=(("notes.md", text),),
        opaque_file_names=("sealed.bin",),
    )
    assert [item.name for item in projected.files] == ["notes.md", "sealed.bin"]
    assert all(
        item.content_sha256 == entry.content_sha256
        for item, entry in zip(projected.files, manifest.files, strict=True)
    )


def test_missing_extra_or_invalid_file_labels_fail_closed(engine: Engine) -> None:
    library_id, _section_id, page_uid, _page_id, _occurred_at = _seed_legacy_page(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        for labels in ({}, {"content.md": "text", "extra.txt": "opaque"}, {"content.md": "skip"}):
            with pytest.raises(ValueError, match="indexing kind"):
                load_current_page_projection(
                    connection,
                    library_id=library_id,
                    page_uid=page_uid,
                    file_classification=labels,  # type: ignore[arg-type]
                    classification_version=CLASSIFICATION_VERSION,
                )
        connection.rollback()


def test_invalid_declared_utf8_fails_but_opaque_file_never_exposes_bytes(engine: Engine) -> None:
    library_id, _section_id, page_uid, page_id, _occurred_at = _seed_legacy_page(engine)
    manifest = build_file_manifest((("opaque.dat", b"\xff\xfesecret"),))
    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        page = repository.get_page(library_id, page_id)
        assert page is not None
        repository.add_file_set_revision(
            page, revision_id=f"rev_{'c' * 32}", created_at=3_000_000, manifest=manifest
        )
        assert (
            repository.advance_file_set_current_revision(
                page, revision_id=f"rev_{'c' * 32}", updated_at=3_000_000
            )
            is not None
        )
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        with pytest.raises(ValueError, match="valid UTF-8"):
            load_current_page_projection(
                connection,
                library_id=library_id,
                page_uid=page_uid,
                file_classification={"opaque.dat": "text"},
                classification_version=CLASSIFICATION_VERSION,
            )
        projected = load_current_page_projection(
            connection,
            library_id=library_id,
            page_uid=page_uid,
            file_classification={"opaque.dat": "opaque"},
            classification_version=CLASSIFICATION_VERSION,
        )
        connection.rollback()
    assert projected is not None
    assert projected.terms == project_page_text(
        title="合成页面", body=None, opaque_file_names=("opaque.dat",)
    )


def test_oversized_declared_text_fails_without_partial_projection(engine: Engine) -> None:
    library_id, _section_id, page_uid, page_id, _occurred_at = _seed_legacy_page(engine)
    large_text = b"x" * (MAX_INPUT_BYTES + 1)
    manifest = build_file_manifest((("large.txt", large_text),))
    with immediate_transaction(engine) as connection:
        repository = ContentRepository(connection)
        page = repository.get_page(library_id, page_id)
        assert page is not None
        repository.add_file_set_revision(
            page, revision_id=f"rev_{'9' * 32}", created_at=3_000_000, manifest=manifest
        )
        assert (
            repository.advance_file_set_current_revision(
                page, revision_id=f"rev_{'9' * 32}", updated_at=3_000_000
            )
            is not None
        )
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        with pytest.raises(ValueError, match="byte limit"):
            load_current_page_projection(
                connection,
                library_id=library_id,
                page_uid=page_uid,
                file_classification={"large.txt": "text"},
                classification_version=CLASSIFICATION_VERSION,
            )
        connection.rollback()


def test_deleted_page_and_wrong_library_return_no_projection(engine: Engine) -> None:
    library_id, _section_id, page_uid, page_id, _occurred_at = _seed_legacy_page(engine)
    with immediate_transaction(engine) as connection:
        AuthRepository(connection).add_caller(
            NewCaller(
                id="d" * 32,
                library_id=library_id,
                kind=CallerKind.AGENT,
                name="Synthetic actor",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        repository = ContentRepository(connection)
        page = repository.get_page(library_id, page_id)
        assert page is not None
        deleted, _ = repository.transition_page_lifecycle(
            page,
            action="delete",
            actor_caller_id="d" * 32,
            actor_home_library_id=library_id,
            request_id=f"req_{'e' * 32}",
            changed_at=3_000_000,
        )
        assert deleted.deleted_at is not None
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        for target_library in (library_id, "f" * 32):
            assert (
                load_current_page_projection(
                    connection,
                    library_id=target_library,
                    page_uid=page_uid,
                    file_classification={"content.md": "text"},
                    classification_version=CLASSIFICATION_VERSION,
                )
                is None
            )
        connection.rollback()
