"""Version presentation and title extraction without duplicate browser journeys."""

import json
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from patchouli_lib.content.file_set_service import FileSetRevisionService
from patchouli_lib.content.presentation import extract_title, read_presentation
from patchouli_lib.database import immediate_transaction

from .test_file_set_service import REVISION_TWO, _seed, _source


def test_shared_title_vectors() -> None:
    vectors = json.loads(
        (Path(__file__).parents[1] / "vectors/revision-titles.json").read_text("utf-8")
    )
    for vector in vectors:
        assert extract_title(vector["filename"], vector["content"].encode()) == vector["title"], (
            vector["name"]
        )


def test_primary_file_only_change_is_a_revision_and_is_immutable(content_engine: Engine) -> None:
    library_id, page_id, page_uid, legacy, etag = _seed(content_engine)
    with immediate_transaction(content_engine) as connection:
        old = read_presentation(connection, library_id, page_uid, f"rev_{'3' * 32}")
        assert old.primary_file is None and old.display_title == "无标题"
        result = FileSetRevisionService(connection).append_existing_page(
            library_id=library_id,
            page_id=page_id,
            expected_etag=etag,
            files=(("content.md", legacy),),
            revision_id=REVISION_TWO,
            revision_at=3_000_000,
            source=_source(library_id, page_uid, REVISION_TWO, 2),
            primary_file="content.md",
        )
        assert result.changed and result.page.current_revision_number == 2
        presentation = read_presentation(connection, library_id, page_uid, REVISION_TWO)
        assert presentation.primary_file == "content.md"
        assert presentation.display_title == extract_title("content.md", legacy)
    with content_engine.begin() as connection, pytest.raises(IntegrityError):
        connection.exec_driver_sql("UPDATE revision_presentations SET display_title='changed'")
