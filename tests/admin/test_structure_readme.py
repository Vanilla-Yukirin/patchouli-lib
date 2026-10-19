"""One HTTP journey for three distinct README editors; no browser matrix."""

from __future__ import annotations

import re
from html import escape

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from test_library_edit import _post, _setup_master
from test_library_edit import browser as browser

from patchouli_lib.admin.markdown_preview import render_readme
from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.auth.models import MasterAuditEvent
from patchouli_lib.library.models import Book, Library, Section


def _timestamp(client: TestClient, path: str) -> int:
    page = client.get(path)
    assert page.status_code == 200
    match = re.search(r'name="expected_updated_at" value="([0-9]+)"', page.text)
    assert match is not None
    return int(match.group(1))


def test_three_level_readme_create_edit_conflict_and_preserved_description(
    browser: tuple[TestClient, Engine],
) -> None:
    client, engine = browser
    csrf = _setup_master(client, engine)
    source = "  # How to use\n\n[Skill](/api/v1/agent/skill/manifest)\n<script>unsafe()</script>\n"
    path = "/admin/libraries"
    for suffix, name, description in (
        ("", "Library", "description"),
        ("/sections", "Section", "description"),
        ("/books", "Book", "summary"),
    ):
        created = _post(
            client,
            path + suffix,
            {"csrf_token": csrf, "name": name, description: "Short purpose", "readme": source},
        )
        assert created.status_code == 303
        path = created.headers["location"]
        page = client.get(path)
        assert "<h1>How to use</h1>" in page.text
        assert '<a href="/api/v1/agent/skill/manifest">Skill</a>' in page.text
        assert "<script>unsafe()" not in page.text
        assert escape(source) in page.text
        assert not re.search(r'<textarea[^>]*name="readme"[^>]*maxlength', page.text)
        original = _timestamp(client, path)
        updated = "\n## New instructions\n" + "说明" * 3_000 + "\n"
        fields = {"csrf_token": csrf, "expected_updated_at": str(original), "readme": updated}
        saved = _post(client, path + "/readme", fields)
        assert saved.status_code == 303
        assert saved.headers["location"] == path + "#readme-heading"
        stale = _post(client, path + "/readme", {**fields, "readme": "Unsaved draft"})
        assert stale.status_code == 409 and "Unsaved draft</textarea>" in stale.text
        assert "<details open>" in stale.text
        assert "<h2>New instructions</h2>" in stale.text
        assert "Short purpose" in stale.text
        assert _post(client, path + "/readme", {**fields, "csrf_token": "bad"}).status_code == 403
        assert (
            _post(
                client, path + "/readme", fields, origin="https://other.example.invalid"
            ).status_code
            == 403
        )
    with engine.connect() as connection:
        for model in (Library, Section, Book):
            assert connection.scalar(select(model.readme)) == updated
        assert connection.scalar(select(Library.description)) == "Short purpose"
        assert connection.scalar(select(Section.description)) == "Short purpose"
        assert connection.scalar(select(Book.summary)) == "Short purpose"
        events = list(
            connection.scalars(
                select(MasterAuditEvent.action).where(
                    MasterAuditEvent.action.like("%.readme.update")
                )
            )
        )
        assert set(events) == {
            "library.readme.update",
            "section.readme.update",
            "book.readme.update",
        }
        assert len(events) == 3
        library_id = connection.scalar(select(Library.id))
        section_id = connection.scalar(select(Section.id))
        book_id = connection.scalar(select(Book.id))
    assert library_id is not None and section_id is not None and book_id is not None
    read_model = AdminReadModel(engine)
    assert read_model.list_libraries()[0].readme == ""
    library_view = read_model.get_library(library_id)
    assert library_view is not None and library_view.library.readme == updated
    assert library_view.sections[0].readme == ""
    section_view = read_model.get_section(library_id, section_id)
    assert section_view is not None and section_view.section.readme == updated
    assert section_view.library.readme == "" and section_view.books[0].readme == ""
    book_view = read_model.get_book_page(library_id, section_id, book_id)
    assert book_view is not None and book_view.book.readme == updated
    assert book_view.library.readme == "" and book_view.section.readme == ""


def test_readme_renderer_blocks_network_images_and_unsafe_links() -> None:
    rendered = render_readme(
        "![remote](https://example.invalid/image.png)\n"
        "[bad](//example.invalid/) [bad](javascript:alert(1))\n"
        '<iframe src="https://example.invalid"></iframe>\n'
        "[docs](https://example.invalid/docs)"
    )
    assert "<img" not in rendered and "<iframe" not in rendered
    assert 'href="//example.invalid/' not in rendered and 'href="javascript:' not in rendered
    assert 'href="https://example.invalid/docs" rel="noreferrer noopener"' in rendered
