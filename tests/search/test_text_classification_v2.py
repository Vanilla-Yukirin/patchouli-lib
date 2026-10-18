"""Pure classification checks for the proposed complete Page text policy."""

from __future__ import annotations

import pytest

from patchouli_lib.content.file_manifest import MAX_FILE_BYTES, MAX_PAGE_BYTES, build_file_manifest
from patchouli_lib.search.text_classification_v2 import (
    TEXT_CLASSIFICATION_VERSION,
    TextClassificationError,
    classify_page_manifest,
    indexing_kind_for_name,
)


@pytest.mark.parametrize(
    "name",
    [
        "note.md",
        "note.MARKDOWN",
        "note.txt",
        "note.json",
        "note.csv",
        "note.tsv",
        "note.yaml",
        "note.yml",
        "note.py",
        "note.sh",
        "Cafe\u0301.MD",
    ],
)
def test_allowlisted_nfc_name_extensions_declare_text(name: str) -> None:
    assert indexing_kind_for_name(name) == "text"


@pytest.mark.parametrize(
    "name", ["note.md.bin", "note.pdf", "note.html", "note", "md", "note.ｍｄ"]
)
def test_other_names_remain_opaque(name: str) -> None:
    assert indexing_kind_for_name(name) == "opaque"


def test_invalid_filename_does_not_receive_a_classification() -> None:
    with pytest.raises(ValueError):
        indexing_kind_for_name("../note.md")


def test_mixed_page_preserves_complete_utf8_text_and_never_decodes_opaque_bytes() -> None:
    manifest = build_file_manifest(
        (
            ("notes.MD", b"\xef\xbb\xbfalpha\r\nbeta"),
            ("secret.bin", b"secret\xff\x00"),
            ("table.csv", "甲,乙\n".encode()),
        )
    )

    result = classify_page_manifest(manifest, storage_format="file_set_v1")

    assert result.version == TEXT_CLASSIFICATION_VERSION
    assert result.storage_format == "file_set_v1"
    assert [(entry.name, entry.indexing_kind) for entry in result.files] == [
        ("notes.MD", "text"),
        ("secret.bin", "opaque"),
        ("table.csv", "text"),
    ]
    assert result.files[0].text == "\ufeffalpha\r\nbeta"
    assert result.files[1].text is None
    assert result.files[2].text == "甲,乙\n"
    assert result.total_text_bytes == len(manifest.files[0].content) + len(
        manifest.files[2].content
    )
    assert manifest.files[0].content == b"\xef\xbb\xbfalpha\r\nbeta"
    assert "alpha" not in repr(result.files[0])


def test_legacy_content_md_is_text_and_other_legacy_shape_fails() -> None:
    legacy = build_file_manifest((("content.md", b"# old\n"),))
    result = classify_page_manifest(legacy, storage_format="legacy_markdown")
    assert result.files[0].indexing_kind == "text"
    assert result.files[0].text == "# old\n"

    with pytest.raises(ValueError, match="legacy Markdown"):
        classify_page_manifest(
            build_file_manifest((("other.md", b"# old\n"),)), storage_format="legacy_markdown"
        )


@pytest.mark.parametrize(
    ("content", "reason"),
    [(b"\xff", "invalid_utf8"), (b"a\x00b", "contains_nul")],
)
def test_declared_text_fails_explicitly_without_payload_in_error(
    content: bytes, reason: str
) -> None:
    manifest = build_file_manifest((("note.txt", content),))
    with pytest.raises(TextClassificationError) as failure:
        classify_page_manifest(manifest, storage_format="file_set_v1")
    assert failure.value.filename == "note.txt"
    assert failure.value.reason == reason
    assert "note.txt" not in str(failure.value)
    assert repr(content) not in str(failure.value)


def test_opaque_suffix_keeps_even_utf8_and_binary_payloads_name_only() -> None:
    manifest = build_file_manifest(
        (("readme.dat", b"searchable-looking text"), ("raw.bin", b"\xff"))
    )
    result = classify_page_manifest(manifest, storage_format="file_set_v1")
    assert all(entry.indexing_kind == "opaque" and entry.text is None for entry in result.files)
    assert result.total_text_bytes == 0


def test_full_storage_limits_are_not_reduced_to_experimental_two_mib() -> None:
    content = b"a" * MAX_FILE_BYTES
    manifest = build_file_manifest(tuple((f"part-{number}.txt", content) for number in range(4)))
    assert manifest.total_size_bytes == MAX_PAGE_BYTES

    result = classify_page_manifest(manifest, storage_format="file_set_v1")

    assert result.total_text_bytes == MAX_PAGE_BYTES
    assert all(
        entry.text is not None and len(entry.text) == MAX_FILE_BYTES for entry in result.files
    )


def test_invalid_storage_format_is_rejected() -> None:
    manifest = build_file_manifest((("content.md", b"test"),))
    with pytest.raises(ValueError, match="storage format"):
        classify_page_manifest(manifest, storage_format="other")  # type: ignore[arg-type]
