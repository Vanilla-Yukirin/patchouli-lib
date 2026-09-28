"""Pure flat-file snapshot validation, without API or database assumptions."""

import hashlib
import unicodedata
from dataclasses import replace

import pytest

from patchouli_lib.content import file_manifest
from patchouli_lib.content.file_manifest import (
    FileManifest,
    ManifestFile,
    build_file_manifest,
    normalize_file_name,
)


def test_manifest_normalizes_sorts_and_preserves_exact_bytes() -> None:
    first = ("Cafe\u0301.md", b"# title\r\n")
    second = ("图片.png", b"\x00\xff\x80")
    manifest = build_file_manifest(item for item in (second, first))

    assert [entry.name for entry in manifest.files] == ["Caf\u00e9.md", "图片.png"]
    assert [entry.content for entry in manifest.files] == [first[1], second[1]]
    assert [entry.content_sha256 for entry in manifest.files] == [
        hashlib.sha256(first[1]).digest(),
        hashlib.sha256(second[1]).digest(),
    ]
    assert manifest.total_size_bytes == len(first[1]) + len(second[1])
    assert build_file_manifest((first, second)).snapshot_sha256 == manifest.snapshot_sha256
    assert b"# title" not in repr(manifest).encode()
    assert b"# title" not in repr(manifest.files[0]).encode()


def test_snapshot_digest_has_unambiguous_framing_and_changes_with_file_state() -> None:
    original = build_file_manifest((("a", b"bc"), ("ab", b"c")))

    assert len(original.snapshot_sha256) == 32
    assert original.snapshot_sha256 != hashlib.sha256(b"a" + b"bc" + b"ab" + b"c").digest()
    assert build_file_manifest((("a", b"bc"), ("ab", b"c"))).snapshot_sha256 == (
        original.snapshot_sha256
    )
    assert build_file_manifest((("a", b"b"), ("ab", b"cc"))).snapshot_sha256 != (
        original.snapshot_sha256
    )
    assert build_file_manifest((("a", b"bc"), ("ac", b"c"))).snapshot_sha256 != (
        original.snapshot_sha256
    )
    assert build_file_manifest((("a", b"bc"),)).snapshot_sha256 != original.snapshot_sha256
    assert build_file_manifest((("a", b"bc"), ("ab", b""))).snapshot_sha256 != (
        original.snapshot_sha256
    )


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "../escape",
        "nested/file.md",
        "nested\\file.md",
        "C:\\file.md",
        " leading.md",
        "trailing.md ",
        "trailing.",
        "space .md",
        "a\x00b",
        "a\nb",
        "a\x7fb",
        "a\u202eb",
        "a\u2028b",
        "a\u2029b",
        "a?b",
        "a*b",
        "a|b",
        "a<b",
        'a"b',
        "CON",
        "con.txt",
        "CON .txt",
        "NUL.json",
        "COM1.md",
        "lpt9.txt",
        "COM\u00b9.txt",
        "CONOUT$.txt",
    ],
)
def test_rejects_unsafe_flat_file_names(name: str) -> None:
    with pytest.raises(ValueError):
        normalize_file_name(name)


def test_raw_name_limit_runs_before_unicode_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = "e" + "\u0301" * file_manifest.MAX_RAW_FILENAME_CHARS

    def unexpected_normalization(_form: str, _value: str) -> str:
        raise AssertionError("oversized name reached Unicode normalization")

    with monkeypatch.context() as scope:
        scope.setattr(unicodedata, "normalize", unexpected_normalization)
        with pytest.raises(ValueError, match="too long"):
            normalize_file_name(raw)


def test_rejects_normalized_and_case_insensitive_collisions() -> None:
    for pairs in (
        (("Caf\u00e9.md", b"one"), ("Cafe\u0301.md", b"two")),
        (("Readme.md", b"one"), ("README.md", b"two")),
        (("Stra\u00dfe.md", b"one"), ("STRASSE.md", b"two")),
    ):
        with pytest.raises(ValueError, match="duplicate or ambiguous"):
            build_file_manifest(pairs)


def test_empty_content_is_allowed_but_empty_manifest_is_not() -> None:
    assert build_file_manifest((("empty.bin", b""),)).total_size_bytes == 0
    with pytest.raises(ValueError, match="at least one"):
        build_file_manifest(())


def test_direct_file_construction_and_replace_revalidate_content_metadata() -> None:
    entry = build_file_manifest((("valid.md", b"content"),)).files[0]
    assert (
        ManifestFile(
            name=entry.name,
            content=entry.content,
            content_size_bytes=entry.content_size_bytes,
            content_sha256=entry.content_sha256,
        )
        == entry
    )

    with pytest.raises(ValueError, match="normalized"):
        replace(entry, name="Cafe\u0301.md")
    with pytest.raises(ValueError, match="size"):
        replace(entry, content_size_bytes=1)
    with pytest.raises(ValueError, match="digest"):
        replace(entry, content_sha256=b"\x00" * 32)
    with pytest.raises(ValueError, match="digest"):
        replace(entry, content=b"changed")
    with pytest.raises(TypeError, match="exact bytes"):
        replace(entry, content=bytearray(b"content"))  # type: ignore[arg-type]


def test_direct_manifest_construction_and_replace_revalidate_snapshot() -> None:
    manifest = build_file_manifest((("valid.md", b"content"),))
    assert (
        FileManifest(
            files=manifest.files,
            total_size_bytes=manifest.total_size_bytes,
            snapshot_sha256=manifest.snapshot_sha256,
        )
        == manifest
    )

    with pytest.raises(ValueError, match="size"):
        replace(manifest, total_size_bytes=1)
    with pytest.raises(ValueError, match="digest"):
        replace(manifest, snapshot_sha256=b"\x00" * 32)
    with pytest.raises(ValueError, match="digest"):
        replace(manifest, files=(replace(manifest.files[0], name="renamed.md"),))
    with pytest.raises(ValueError, match="duplicate"):
        replace(manifest, files=(manifest.files[0], manifest.files[0]))
    with pytest.raises(ValueError, match="bounded"):
        replace(manifest, files=())


def test_bounded_file_count_name_length_and_total_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_manifest, "MAX_FILES_PER_PAGE", 2)
    monkeypatch.setattr(file_manifest, "MAX_FILENAME_BYTES", 5)
    monkeypatch.setattr(file_manifest, "MAX_FILE_BYTES", 4)
    monkeypatch.setattr(file_manifest, "MAX_PAGE_BYTES", 5)

    assert build_file_manifest((("é.md", b"1234"), ("b", b"5"))).total_size_bytes == 5
    with pytest.raises(ValueError, match="too many"):
        build_file_manifest((("a", b""), ("b", b""), ("c", b"")))
    with pytest.raises(ValueError, match="too long"):
        build_file_manifest((("abcdef", b""),))
    with pytest.raises(ValueError, match="configured limit"):
        build_file_manifest((("a", b"12345"),))
    with pytest.raises(ValueError, match="configured limit"):
        build_file_manifest((("a", b"1234"), ("b", b"56")))


@pytest.mark.parametrize(
    "files",
    [
        "not-pairs",
        (("valid.md", bytearray(b"mutable")),),
        ((b"bytes.md", b"content"),),
        (("missing-content.md",),),
    ],
)
def test_requires_exact_pair_and_bytes_types(files: object) -> None:
    with pytest.raises(TypeError):
        build_file_manifest(files)  # type: ignore[arg-type]
