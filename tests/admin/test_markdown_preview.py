from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import quote

import pytest

from patchouli_lib.admin.markdown_preview import (
    MAX_MARKDOWN_PREVIEW_BYTES,
    MarkdownPreviewUnavailableError,
    render_markdown_preview,
)


class _Tags(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _render(
    source: str | bytes,
    *,
    names: tuple[str, ...] = ("content.md", "image.png", "notes.json"),
    revision: str = "revision-a",
    image_url: Callable[[str], str | None] | None = None,
) -> str:
    return render_markdown_preview(
        source.encode() if isinstance(source, str) else source,
        filenames=names,
        image_url=image_url or (lambda name: f"/preview/{revision}/{quote(name, safe='')}"),
        download_url=lambda name: f"/download/{revision}/{quote(name, safe='')}",
    )


def test_renders_classic_markdown_without_html_ids_or_autolinking() -> None:
    html = _render(
        "# Title\n\n**strong**\n\n- item\n\n```html\n<script>x</script>\n```\n\n"
        "| A | B |\n| --- | --- |\n| one | two |\n\nhttps://example.test\n"
        "<script>alert(1)</script><img src=x onerror=alert(1)>"
    )
    tags = _Tags(html).tags
    assert "<h1>Title</h1>" in html
    assert "<strong>strong</strong>" in html
    assert "<li>item</li>" in html
    assert "<table>" in html
    assert "&lt;script&gt;" in html
    assert not any(tag in {"script", "img", "a"} for tag, _ in tags)
    assert not any("id" in attrs for _, attrs in tags)


def test_chinese_and_encoded_flat_names_resolve_in_exact_revision() -> None:
    names = ("说明.md", "图 片#1.png", "数据%2F.json", "café.png")
    source = (
        "![中文](./%E5%9B%BE%20%E7%89%87%231.png) "
        "[数据](%E6%95%B0%E6%8D%AE%252F.json) ![accent](cafe%CC%81.png)"
    )
    first = _Tags(_render(source, names=names, revision="first")).tags
    second = _Tags(_render(source, names=names, revision="second")).tags
    assert first[1] == (
        "img",
        {
            "src": "/preview/first/" + quote("图 片#1.png", safe=""),
            "alt": "中文",
            "loading": "lazy",
            "decoding": "async",
            "referrerpolicy": "no-referrer",
        },
    )
    assert any(
        attrs.get("href") == "/download/first/" + quote(names[2], safe="") for _, attrs in first
    )
    assert any(
        attrs.get("src") == "/preview/first/" + quote(names[3], safe="") for _, attrs in first
    )
    assert all("/first/" not in str(attrs) for _, attrs in second)
    assert any("/second/" in str(attrs) for _, attrs in second)


@pytest.mark.parametrize(
    "destination",
    [
        "https://example.test/image.png",
        "//example.test/image.png",
        "data:image/png;base64,aGVsbG8=",
        "data:image/svg+xml,%3Csvg%3E",
        "file:///image.png",
        "javascript:alert(1)",
        "../image.png",
        "%2e%2e%2fimage.png",
        "sub/image.png",
        "%2Fimage.png",
        "%5Cimage.png",
        "sub%2Fimage.png",
        "././image.png",
        "missing.png",
        "image.png?secret=1",
        "image.png#fragment",
        "drawing.svg",
        "drawing.SVGZ",
        "image%00.png",
        "%FF.png",
    ],
)
def test_images_cannot_escape_the_flat_verified_snapshot(destination: str) -> None:
    calls: list[str] = []

    def resolver(name: str) -> str:
        calls.append(name)
        return "/should-not-be-called"

    html = _render(
        f"![safe alt]({destination})",
        names=("image.png", "drawing.svg", "drawing.SVGZ"),
        image_url=resolver,
    )
    assert not any(tag == "img" for tag, _ in _Tags(html).tags)
    assert calls == []


def test_missing_preview_leaves_text_and_local_file_links_use_download_resolver() -> None:
    html = _render("![alt](image.png) [notes](notes.json)", image_url=lambda _: None)
    tags = _Tags(html).tags
    assert not any(tag == "img" for tag, _ in tags)
    assert '<span class="unavailable-image">alt</span>' in html
    assert any(attrs.get("href") == "/download/revision-a/notes.json" for _, attrs in tags)


def test_https_links_are_explicit_and_attributes_are_escaped() -> None:
    html = _render(
        '[site](https://example.test/?a=1&b=2 "quoted & <title>") ![<&](image.png "image & title")'
    )
    tags = _Tags(html).tags
    anchors = [attrs for tag, attrs in tags if tag == "a"]
    assert anchors[0]["rel"] == "noreferrer noopener"
    assert anchors[0]["href"] == "https://example.test/?a=1&b=2"
    assert anchors[0]["title"] == "quoted & <title>"
    assert any(attrs.get("title") == "image & title" for tag, attrs in tags if tag == "img")
    assert all(set(attrs) <= {"href", "title", "rel"} for attrs in anchors)
    assert all("onerror" not in attrs for _, attrs in tags)


@pytest.mark.parametrize(
    "destination",
    [
        "javascript:alert(1)",
        "data:text/html,attack",
        "file:///secret",
        "http://example.test",
        "https://user:pass@example.test",
        "//example.test",
        "../content.md",
        "missing.json",
    ],
)
def test_unsafe_links_never_render_active_hrefs(destination: str) -> None:
    html = _render(f"[text]({destination}) **after**")
    assert not any(tag == "a" for tag, _ in _Tags(html).tags)
    assert "<strong>after</strong>" in html


@pytest.mark.parametrize(
    "content",
    [b"\xff", b"before\x00after", b"x" * (MAX_MARKDOWN_PREVIEW_BYTES + 1)],
    ids=["invalid-utf8", "embedded-nul", "over-byte-budget"],
)
def test_rejects_malformed_or_over_budget_markdown(content: bytes) -> None:
    with pytest.raises(MarkdownPreviewUnavailableError):
        _render(content)


def test_exact_markdown_byte_budget_is_allowed() -> None:
    assert _render(b"x" * MAX_MARKDOWN_PREVIEW_BYTES).startswith("<p>")


@pytest.mark.parametrize(
    "names",
    [(), ("../image.png",), ("IMAGE.PNG", "image.png"), ("cafe\u0301.png",), ("x.png",) * 65],
)
def test_requires_bounded_canonical_unambiguous_names(names: tuple[str, ...]) -> None:
    with pytest.raises(MarkdownPreviewUnavailableError, match="Invalid preview file list"):
        _render("text", names=names)


@pytest.mark.parametrize(
    "url",
    [
        "https://outside.test",
        "//outside.test",
        "javascript:bad",
        "/\\outside.test",
        "/path\nnext",
        "/path with space",
    ],
)
def test_rejects_non_relative_or_ambiguous_resolver_outputs(url: str) -> None:
    with pytest.raises(MarkdownPreviewUnavailableError, match="Invalid preview URL"):
        _render("![alt](image.png)", image_url=lambda _: url)


def test_resolver_url_quotes_do_not_add_attributes() -> None:
    url = '/preview/x?name="&value=<bad>'
    html = _render("![alt](image.png)", image_url=lambda _: url)
    images = [attrs for tag, attrs in _Tags(html).tags if tag == "img"]
    assert images[0]["src"] == url
    assert set(images[0]) == {"src", "alt", "loading", "decoding", "referrerpolicy"}
