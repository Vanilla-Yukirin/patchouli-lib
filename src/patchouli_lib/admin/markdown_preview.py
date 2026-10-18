"""Bounded Markdown rendering; no storage reads or authorization decisions.

The caller must authorize and verify a complete Revision snapshot first. Both
the filename collection and URL resolvers must refer to that exact snapshot,
not the Page's mutable current Revision. A resolver returns a same-origin,
root-relative URL, or None when a file has no permitted preview/download.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Collection, Sequence
from html import escape
from typing import cast
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict

from patchouli_lib.content.file_manifest import (
    MAX_FILES_PER_PAGE,
    MAX_RAW_FILENAME_CHARS,
    normalize_file_name,
)

MAX_MARKDOWN_PREVIEW_BYTES = 1024 * 1024
type PreviewURLResolver = Callable[[str], str | None]


class MarkdownPreviewUnavailableError(ValueError):
    """Rendering is unavailable; the original download remains independent."""


def _filenames(values: Collection[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)) or not 1 <= len(values) <= MAX_FILES_PER_PAGE:
        raise MarkdownPreviewUnavailableError("Invalid preview file list.")
    names: set[str] = set()
    keys: set[str] = set()
    try:
        for value in values:
            name = normalize_file_name(value)
            key = unicodedata.normalize("NFC", name.casefold())
            if value != name or key in keys:
                raise ValueError
            names.add(name)
            keys.add(key)
    except (TypeError, ValueError, UnicodeError):
        raise MarkdownPreviewUnavailableError("Invalid preview file list.") from None
    return frozenset(names)


def _has_controls(value: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value)


def _text_attr(token: Token, name: str) -> str | None:
    value = token.attrGet(name)
    return str(value) if value is not None else None


def _sibling_name(destination: str, names: frozenset[str]) -> str | None:
    if len(destination) > 4 * MAX_RAW_FILENAME_CHARS or _has_controls(destination):
        return None
    try:
        parts = urlsplit(destination)
        if parts.scheme or parts.netloc or parts.query or parts.fragment:
            return None
        path = parts.path
        if path.startswith("./"):
            path = path[2:]
        # Decode exactly once, after splitting URI delimiters. Encoded slashes
        # and parent paths still fail the shared flat-file naming validator.
        name = normalize_file_name(unquote(path, encoding="utf-8", errors="strict"))
    except (TypeError, ValueError, UnicodeError):
        return None
    return name if name in names else None


def _resolved_url(resolver: PreviewURLResolver, name: str) -> str | None:
    value = resolver(name)
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or _has_controls(value)
        or any(char.isspace() for char in value)
    ):
        raise MarkdownPreviewUnavailableError("Invalid preview URL resolver result.")
    try:
        parts = urlsplit(value)
        if parts.scheme or parts.netloc:
            raise ValueError
    except ValueError:
        raise MarkdownPreviewUnavailableError("Invalid preview URL resolver result.") from None
    return value


def _external_https(destination: str) -> bool:
    if _has_controls(destination) or "\\" in destination:
        return False
    try:
        parts = urlsplit(destination)
        return bool(
            parts.scheme == "https"
            and parts.hostname
            and parts.username is None
            and parts.password is None
            and not any(char.isspace() for char in parts.netloc)
        )
    except ValueError:
        return False


def render_markdown_preview(
    content: bytes,
    *,
    filenames: Collection[str],
    image_url: PreviewURLResolver,
    download_url: PreviewURLResolver,
) -> str:
    """Render UTF-8 Markdown with only exact-snapshot sibling file references.

    This is not a MIME detector: image_url must only expose decoded/re-encoded
    PNG/JPEG previews. Remote images, SVG, data URLs and subdirectories never
    reach that resolver. Unsafe/unavailable references become plain text.
    Oversized or malformed Markdown raises a safe error without echoing bytes.
    """

    if type(content) is not bytes or len(content) > MAX_MARKDOWN_PREVIEW_BYTES:
        raise MarkdownPreviewUnavailableError("Markdown preview exceeds its byte budget.")
    try:
        source = content.decode("utf-8", errors="strict")
        if "\x00" in source:
            raise ValueError
    except (UnicodeError, ValueError):
        raise MarkdownPreviewUnavailableError(
            "Markdown preview requires valid UTF-8 text."
        ) from None
    names = _filenames(filenames)
    # Per-call parser/rules prevent resolver or link-stack state crossing callers.
    parser = MarkdownIt("js-default", {"html": False, "linkify": False, "typographer": False})
    parser.enable("table")
    # The default factory above is RendererHTML; MarkdownIt's general protocol
    # also permits non-HTML renderers, which this bounded helper does not use.
    renderer = cast(RendererHTML, parser.renderer)
    link_stack: list[bool] = []

    def image_rule(
        rule_renderer: RendererHTML,
        tokens: Sequence[Token],
        index: int,
        options: OptionsDict,
        env: EnvType,
    ) -> str:
        token = tokens[index]
        alt = escape(rule_renderer.renderInlineAsText(token.children, options, env), quote=True)
        name = _sibling_name(_text_attr(token, "src") or "", names)
        url = None
        if name is not None and not name.casefold().endswith((".svg", ".svgz")):
            url = _resolved_url(image_url, name)
        if url is None:
            return f'<span class="unavailable-image">{alt}</span>'
        title = _text_attr(token, "title")
        title_attr = f' title="{escape(title, quote=True)}"' if title is not None else ""
        return (
            f'<img src="{escape(url, quote=True)}" alt="{alt}"{title_attr}'
            ' loading="lazy" decoding="async" referrerpolicy="no-referrer">'
        )

    def link_open_rule(
        _renderer: RendererHTML,
        tokens: Sequence[Token],
        index: int,
        _options: OptionsDict,
        _env: EnvType,
    ) -> str:
        token = tokens[index]
        destination = _text_attr(token, "href") or ""
        name = _sibling_name(destination, names)
        url = _resolved_url(download_url, name) if name is not None else None
        external = url is None and name is None and _external_https(destination)
        if external:
            url = destination
        link_stack.append(url is not None)
        if url is None:
            return "<span>"
        title = _text_attr(token, "title")
        title_attr = f' title="{escape(title, quote=True)}"' if title is not None else ""
        rel = ' rel="noreferrer noopener"' if external else ""
        return f'<a href="{escape(url, quote=True)}"{title_attr}{rel}>'

    def link_close_rule(
        _renderer: RendererHTML,
        _tokens: Sequence[Token],
        _index: int,
        _options: OptionsDict,
        _env: EnvType,
    ) -> str:
        return "</a>" if link_stack.pop() else "</span>"

    parser.add_render_rule("image", image_rule)
    parser.add_render_rule("link_open", link_open_rule)
    parser.add_render_rule("link_close", link_close_rule)
    env: EnvType = {}
    return renderer.render(parser.parse(source, env), parser.options, env)
