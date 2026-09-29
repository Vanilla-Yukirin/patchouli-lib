"""Synthetic wire tests for the unregistered unified file-set parser."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from patchouli_lib.api import file_set_multipart as upload
from patchouli_lib.api.errors import ApplicationProblem

BOUNDARY = b"synthetic-file-set-boundary"
METADATA = b'{"title":"Synthetic Page"}'


def _part(headers: Sequence[bytes], content: bytes) -> bytes:
    return b"--" + BOUNDARY + b"\r\n" + b"\r\n".join(headers) + b"\r\n\r\n" + content + b"\r\n"


def _body(
    files: Sequence[tuple[bytes, bytes, bytes | None]] = ((b"content.md", b"# Example\n", None),),
    *,
    metadata: bytes = METADATA,
    metadata_headers: Sequence[bytes] | None = None,
    extra_parts: Sequence[bytes] = (),
) -> bytes:
    headers = metadata_headers or (
        b'Content-Disposition: form-data; name="metadata"',
        b"Content-Type: application/json; charset=utf-8",
    )
    parts = [_part(headers, metadata)]
    for filename, content, media in files:
        file_headers = [
            b'Content-Disposition: form-data; name="file"; filename="' + filename + b'"'
        ]
        if media is not None:
            file_headers.append(b"Content-Type: " + media)
        parts.append(_part(file_headers, content))
    parts.extend(extra_parts)
    return b"".join(parts) + b"--" + BOUNDARY + b"--\r\n"


def _parse(
    body: bytes,
    *,
    content_type: bytes | None = None,
    headers: Sequence[tuple[bytes, bytes]] = (),
    chunk_size: int | None = None,
    include_content_length: bool = True,
) -> upload.ParsedFileSetUpload:
    raw_headers = [(b"content-type", content_type or b"multipart/form-data; boundary=" + BOUNDARY)]
    if include_content_length:
        raw_headers.append((b"content-length", str(len(body)).encode("ascii")))
    raw_headers.extend(headers)
    chunks = (
        [body]
        if chunk_size is None
        else [body[index : index + chunk_size] for index in range(0, len(body), chunk_size)]
    )
    # Keep the stream split exact without accidentally consuming a lookahead.
    remaining = iter(enumerate(chunks))

    async def receive_chunk() -> dict[str, object]:
        current = next(remaining, None)
        if current is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        index, chunk = current
        return {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}

    scope = {"type": "http", "method": "POST", "path": "/upload", "headers": raw_headers}
    return asyncio.run(upload.parse_file_set_multipart(Request(scope, receive_chunk)))


def _assert_problem(
    body: bytes,
    status: int,
    *,
    content_type: bytes | None = None,
    headers: Sequence[tuple[bytes, bytes]] = (),
    chunk_size: int | None = None,
) -> None:
    with pytest.raises(ApplicationProblem) as failure:
        _parse(body, content_type=content_type, headers=headers, chunk_size=chunk_size)
    assert failure.value.status_code == status


def test_single_markdown_is_one_file_set_and_preserves_exact_bytes() -> None:
    parsed = _parse(_body(), chunk_size=7)
    assert parsed.metadata == METADATA
    assert len(parsed.manifest.files) == 1
    assert parsed.manifest.files[0].name == "content.md"
    assert parsed.manifest.files[0].content == b"# Example\n"
    assert parsed.manifest.files[0].content_sha256 == hashlib.sha256(b"# Example\n").digest()


def test_mixed_binary_and_unicode_files_have_one_canonical_snapshot() -> None:
    files = (
        ("图像.png".encode(), b"\x89PNG\x00\xff", b"image/png"),
        (b"data.bin", b"\x00\xff", b"application/octet-stream"),
        (b"content.md", b"# Heading", b"text/markdown; charset=utf-8"),
    )
    parsed = _parse(_body(files), chunk_size=1)
    assert [file.name for file in parsed.manifest.files] == [
        "content.md",
        "data.bin",
        "图像.png",
    ]
    assert parsed.manifest.total_size_bytes == 17
    assert all(len(file.content_sha256) == 32 for file in parsed.manifest.files)
    assert len(parsed.manifest.snapshot_sha256) == 32


@pytest.mark.parametrize(
    "files",
    [
        (),
        ((b"../escape.md", b"x", None),),
        ((b"CON.md", b"x", None),),
        ((b"bad\\name.md", b"x", None),),
        ((b"a.md", b"x", None), (b"A.MD", b"y", None)),
        (("é.md".encode(), b"x", None), ("e\u0301.md".encode(), b"y", None)),
        ((b"bad\xff.md", b"x", None),),
    ],
)
def test_invalid_file_set_rejected_without_silently_dropping_content(
    files: Sequence[tuple[bytes, bytes, bytes | None]],
) -> None:
    _assert_problem(_body(files), 422)


def test_non_markdown_only_file_set_is_valid() -> None:
    parsed = _parse(_body(((b"slides.pptx", b"PK\x03\x04", None),)))
    assert [entry.name for entry in parsed.manifest.files] == ["slides.pptx"]


def test_stream_without_content_length_and_exact_file_count_limit() -> None:
    files = tuple((f"file-{index:02}.bin".encode(), bytes((index,)), None) for index in range(64))
    parsed = _parse(_body(files), include_content_length=False, chunk_size=31)
    assert len(parsed.manifest.files) == 64
    _assert_problem(_body((*files, (b"extra.bin", b"x", None))), 413)


def test_real_asgi_request_stream_uses_same_single_and_multi_file_parser() -> None:
    app = FastAPI()

    @app.post("/synthetic-upload")
    async def synthetic_upload(request: Request) -> dict[str, object]:
        parsed = await upload.parse_file_set_multipart(request)
        return {
            "metadata": parsed.metadata.decode("utf-8"),
            "names": [entry.name for entry in parsed.manifest.files],
        }

    files = ((b"content.md", b"# Example", None), (b"plot.png", b"\x89PNG", None))
    with TestClient(app) as client:
        response = client.post(
            "/synthetic-upload",
            content=_body(files),
            headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY.decode()}"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "metadata": METADATA.decode(),
        "names": ["content.md", "plot.png"],
    }


@pytest.mark.parametrize(
    "extra",
    [
        _part(
            (b'Content-Disposition: form-data; name="metadata"', b"Content-Type: application/json"),
            b"{}",
        ),
        _part((b'Content-Disposition: form-data; name="unknown"',), b"x"),
        _part((b'Content-Disposition: form-data; name="file"',), b"x"),
        _part((b"Content-Disposition: form-data; name=\"file\"; filename*=utf-8''other.md",), b"x"),
        _part(
            (
                b'Content-Disposition: form-data; name="file"; filename="first.md"; '
                b"filename*=utf-8''other.md",
            ),
            b"x",
        ),
        _part(
            (
                b'Content-Disposition: form-data; name="file"; filename="first.md"; '
                b'filename*0="other"',
            ),
            b"x",
        ),
    ],
)
def test_extra_or_ambiguous_parts_rejected(extra: bytes) -> None:
    _assert_problem(_body(extra_parts=(extra,)), 422)


def test_file_before_metadata_rejected() -> None:
    body = (
        _part((b'Content-Disposition: form-data; name="file"; filename="first.md"',), b"x")
        + _body()
    )
    _assert_problem(body, 422)


@pytest.mark.parametrize(
    "content_type,status",
    [
        (b"application/json", 415),
        (b"multipart/form-data; boundary=wrong", 422),
        (b"multipart/form-data; boundary=a; boundary=b", 415),
        (b"multipart/form-data; boundary=synthetic-file-set-boundary; BOUNDARY=other", 415),
    ],
)
def test_invalid_top_level_media_rejected(content_type: bytes, status: int) -> None:
    _assert_problem(_body(), status, content_type=content_type)


@pytest.mark.parametrize(
    "media",
    [
        b"multipart/mixed",
        b"text/plain; charset=latin-1",
        b"text/plain; charset=utf-8; charset=utf-8",
        b"not-a-type",
    ],
)
def test_unsupported_file_media_label_rejected(media: bytes) -> None:
    _assert_problem(_body(((b"file.bin", b"x", media),)), 415)


def test_metadata_media_label_and_duplicate_headers_rejected() -> None:
    _assert_problem(
        _body(
            metadata_headers=(
                b'Content-Disposition: form-data; name="metadata"',
                b"Content-Type: text/plain",
            )
        ),
        415,
    )
    _assert_problem(
        _body(
            metadata_headers=(
                b'Content-Disposition: form-data; name="metadata"',
                b"Content-Type: application/json",
                b"Content-Type: application/json",
            )
        ),
        422,
    )


def test_predeclared_and_streamed_body_ceiling_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _body()
    monkeypatch.setattr(upload, "MAX_FILE_SET_MULTIPART_BYTES", len(body) - 1)
    _assert_problem(body, 413)
    with pytest.raises(ApplicationProblem) as failure:
        _parse(body, headers=((b"content-length", b"garbage"),))
    assert failure.value.status_code == 422


def test_metadata_file_and_page_limits_are_enforced_during_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upload, "MAX_FILE_SET_METADATA_BYTES", 3)
    _assert_problem(_body(), 413)
    monkeypatch.setattr(upload, "MAX_FILE_SET_METADATA_BYTES", len(METADATA))
    monkeypatch.setattr(upload, "MAX_FILE_BYTES", 1)
    _assert_problem(_body(((b"a.md", b"ab", None),)), 413)
    monkeypatch.setattr(upload, "MAX_FILE_BYTES", 4)
    monkeypatch.setattr(upload, "MAX_PAGE_BYTES", 4)
    _assert_problem(_body(((b"a.md", b"abc", None), (b"b.md", b"de", None))), 413)


def test_file_count_limit_is_checked_before_body_buffering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upload, "MAX_FILES_PER_PAGE", 1)
    _assert_problem(_body(((b"a.md", b"a", None), (b"b.md", b"b", None))), 413)


def test_incomplete_multipart_fails_closed() -> None:
    _assert_problem(_body()[:-5], 422)
