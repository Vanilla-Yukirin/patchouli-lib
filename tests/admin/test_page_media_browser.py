"""Real-browser acceptance for master-only, revision-bound Page media previews."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

from PIL import Image
from test_file_set_browser import (
    _Browser,
    _download,
    _navigate,
    _select_files,
    _submit,
    _upload_form,
    _wait,
)

pytest_plugins = ("test_file_set_browser",)


def _png(color: str) -> bytes:
    with Image.new("RGB", (4, 3), color) as image, BytesIO() as output:
        image.save(output, format="PNG")
        return output.getvalue()


def _files(
    tmp_path: Path,
    directory_name: str,
    *,
    heading: str,
    color: str,
    second_markdown: bool,
) -> tuple[tuple[Path, ...], bytes]:
    directory = tmp_path / directory_name
    directory.mkdir()
    content = f"# {heading}\n\n![Synthetic image](./photo.png)\n".encode()
    markdown = directory / "content.md"
    markdown.write_bytes(content)
    image = directory / "photo.png"
    image.write_bytes(_png(color))
    paths = [markdown, image]
    if second_markdown:
        alternate = directory / "second.md"
        alternate.write_bytes(
            b"# Second document\n\n<script>window.syntheticPreviewExecuted = true</script>\n"
        )
        paths.append(alternate)
    return tuple(paths), content


def _open_safe_preview(browser: _Browser, revision_path: str) -> str:
    _navigate(
        browser,
        revision_path,
        'document.querySelector(".revision-files a[download]") !== null',
    )
    preview_path = browser.tools.evaluate(
        "Array.from(document.querySelectorAll('a.button'))"
        ".find(link => link.textContent.trim() === 'Safe Markdown preview')"
        "?.getAttribute('href')"
    )
    assert isinstance(preview_path, str)
    page_and_revision = revision_path.rsplit("/pages/", 1)[-1]
    library_path = browser.book_path.split("/sections/", 1)[0]
    assert preview_path == f"{library_path}/pages/{page_and_revision}"
    assert browser.tools.evaluate(
        f"""(() => {{
          const link = Array.from(document.querySelectorAll('a.button'))
            .find(item => item.getAttribute('href') === {json.dumps(preview_path)});
          if (!link) return false;
          link.click();
          return true;
        }})()"""
    )
    _wait(
        browser.tools,
        f"location.pathname === {json.dumps(preview_path)} && "
        'document.querySelector(".rendered-markdown h1") !== null',
        description="the clicked master preview",
    )
    return preview_path


def _loaded_image(browser: _Browser, revision_number: int) -> None:
    assert browser.tools.evaluate(
        "(() => { const image = document.querySelector('.rendered-markdown img'); "
        "if (!image) return false; image.scrollIntoView(); return true; })()"
    )
    _wait(
        browser.tools,
        "document.querySelector('.rendered-markdown img')?.naturalWidth > 0",
        description="the same-version local image decode",
    )
    assert browser.tools.evaluate(
        "document.querySelector('.rendered-markdown img').getAttribute('src')"
    ).endswith(f"/revisions/{revision_number}/preview-images/photo.png")


def test_browser_uploads_three_files_and_selects_safe_preview(
    file_set_browser: _Browser, tmp_path: Path
) -> None:
    browser = file_set_browser
    files, content = _files(
        tmp_path, "original", heading="Original document", color="red", second_markdown=True
    )
    _upload_form(browser, browser.book_path + "/new-page", create=True)
    _select_files(browser, files)
    revision = _submit(browser, expected_status="Files saved.")

    preview = _open_safe_preview(browser, revision)
    assert (
        browser.tools.evaluate("document.querySelector('.rendered-markdown h1').textContent")
        == "Original document"
    )
    _loaded_image(browser, 1)
    assert browser.tools.evaluate(
        """(() => {
          const select = document.querySelector('#preview-file');
          if (!select || !Array.from(select.options).some(item => item.value === 'second.md')) {
            return false;
          }
          select.value = 'second.md';
          select.form.requestSubmit();
          return true;
        })()"""
    )
    _wait(
        browser.tools,
        f"location.pathname === {json.dumps(preview)} && "
        'new URLSearchParams(location.search).get("preview_file") === "second.md" && '
        'document.querySelector(".rendered-markdown h1")?.textContent === "Second document"',
        description="the selected second Markdown preview",
    )
    assert browser.tools.evaluate("window.syntheticPreviewExecuted") is None
    assert browser.tools.evaluate("document.querySelector('.rendered-markdown script')") is None
    assert "<script>window.syntheticPreviewExecuted" in browser.tools.evaluate(
        "document.querySelector('.rendered-markdown').textContent"
    )
    _download(browser, revision + "/files/content.md", content)


def test_browser_historical_preview_stays_bound_to_original_revision(
    file_set_browser: _Browser, tmp_path: Path
) -> None:
    browser = file_set_browser
    original_files, original = _files(
        tmp_path, "before", heading="Before revision", color="red", second_markdown=True
    )
    _upload_form(browser, browser.book_path + "/new-page", create=True)
    _select_files(browser, original_files)
    first = _submit(browser, expected_status="Files saved.")
    page_path = first.rsplit("/revisions/", 1)[0]

    revised_files, revised = _files(
        tmp_path, "after", heading="After revision", color="blue", second_markdown=False
    )
    _upload_form(browser, page_path + "/files/edit", create=False)
    _select_files(browser, revised_files)
    second = _submit(browser, expected_status="Files saved.")
    assert second == page_path + "/revisions/2"

    historical = _open_safe_preview(browser, first)
    assert (
        browser.tools.evaluate("document.querySelector('.rendered-markdown h1').textContent")
        == "Before revision"
    )
    _loaded_image(browser, 1)
    assert browser.tools.evaluate(
        "Array.from(document.querySelector('#preview-file').options, item => item.value)"
    ) == ["content.md", "second.md"]

    current = _open_safe_preview(browser, second)
    assert current != historical
    assert (
        browser.tools.evaluate("document.querySelector('.rendered-markdown h1').textContent")
        == "After revision"
    )
    _loaded_image(browser, 2)
    assert browser.tools.evaluate(
        "Array.from(document.querySelector('#preview-file').options, item => item.value)"
    ) == ["content.md"]
    _download(browser, first + "/files/content.md", original)
    _download(browser, second + "/files/content.md", revised)
