"""Rebuildable, file-name-based text classification for a search-v2 candidate.

This pure module neither trusts an upload's MIME label nor inspects opaque
payloads. A text suffix declares that the *entire* file must be valid UTF-8
without NUL; it is never silently downgraded to a name-only file. Callers
must supply a verified, complete Page manifest and decide whether a failed
classification rejects a new write or keeps search unavailable during rebuild.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from patchouli_lib.content.file_manifest import FileManifest, normalize_file_name

# Bump this whenever the suffix set or decoding rule changes; old projections
# must then be rebuilt rather than mixed with the new classification.
TEXT_CLASSIFICATION_VERSION = "nfc-name-ascii-extension-utf8-strict-no-nul-v1"
TEXT_FILE_EXTENSIONS = frozenset(
    {"md", "markdown", "txt", "json", "csv", "tsv", "yaml", "yml", "py", "sh"}
)

FileStorageFormat = Literal["legacy_markdown", "file_set_v1"]
FileIndexingKind = Literal["text", "opaque"]
TextClassificationFailure = Literal["invalid_utf8", "contains_nul"]


class TextClassificationError(ValueError):
    """A declared text file cannot be indexed in full.

    ``filename`` is available to an authorized caller for a precise response,
    but exception text deliberately contains neither the name nor file bytes.
    """

    def __init__(self, filename: str, reason: TextClassificationFailure) -> None:
        self.filename = filename
        self.reason = reason
        detail = {
            "invalid_utf8": "Declared Page text must be valid UTF-8.",
            "contains_nul": "Declared Page text must not contain NUL.",
        }[reason]
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ClassifiedFile:
    """One verified source file and its complete decoded text, if eligible."""

    name: str
    indexing_kind: FileIndexingKind
    size_bytes: int
    text: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class PageTextClassification:
    """A complete Page's versioned classification in manifest order."""

    version: str
    storage_format: FileStorageFormat
    files: tuple[ClassifiedFile, ...]
    total_text_bytes: int


def indexing_kind_for_name(name: str) -> FileIndexingKind:
    """Classify a safe NFC filename by its ASCII extension, never by MIME."""

    normalized = normalize_file_name(name)
    _stem, separator, extension = normalized.rpartition(".")
    if separator and extension.isascii() and extension.lower() in TEXT_FILE_EXTENSIONS:
        return "text"
    return "opaque"


def classify_page_manifest(
    manifest: FileManifest, *, storage_format: FileStorageFormat
) -> PageTextClassification:
    """Classify every file without imposing the experimental 2 MiB budget.

    The manifest's storage limits are 16 MiB per file and 64 MiB per Page.
    Decoding uses strict UTF-8: BOM and CRLF remain in the returned text, and
    the authoritative file bytes are not modified. Opaque bytes are untouched.
    """

    if type(manifest) is not FileManifest:
        raise TypeError("A verified Page file manifest is required.")
    if storage_format not in ("legacy_markdown", "file_set_v1"):
        raise ValueError("Unsupported Page file storage format.")
    if storage_format == "legacy_markdown" and (
        len(manifest.files) != 1 or manifest.files[0].name != "content.md"
    ):
        raise ValueError("A legacy Markdown snapshot must contain only content.md.")

    classified: list[ClassifiedFile] = []
    total_text_bytes = 0
    for entry in manifest.files:
        kind = "text" if storage_format == "legacy_markdown" else indexing_kind_for_name(entry.name)
        decoded: str | None = None
        if kind == "text":
            if b"\x00" in entry.content:
                raise TextClassificationError(entry.name, "contains_nul")
            try:
                decoded = entry.content.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise TextClassificationError(entry.name, "invalid_utf8") from None
            total_text_bytes += entry.content_size_bytes
        classified.append(
            ClassifiedFile(
                name=entry.name,
                indexing_kind=kind,
                size_bytes=entry.content_size_bytes,
                text=decoded,
            )
        )
    return PageTextClassification(
        version=TEXT_CLASSIFICATION_VERSION,
        storage_format=storage_format,
        files=tuple(classified),
        total_text_bytes=total_text_bytes,
    )


__all__ = [
    "ClassifiedFile",
    "FileIndexingKind",
    "FileStorageFormat",
    "PageTextClassification",
    "TEXT_CLASSIFICATION_VERSION",
    "TEXT_FILE_EXTENSIONS",
    "TextClassificationError",
    "TextClassificationFailure",
    "classify_page_manifest",
    "indexing_kind_for_name",
]
