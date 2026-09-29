from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
import pytest
from conftest import protected_headers

from patchouli_client import (
    ApiLimits,
    BearerToken,
    FileSetCreateMetadata,
    FileSetFile,
    FileSetFileSummary,
    FileSetManifest,
    FileSetRevisionMetadata,
    FileSetSource,
    IdempotencyKey,
    PatchouliClient,
    ProblemError,
    ProtocolError,
)
from patchouli_client.multipart import build_file_set_multipart

TOKEN = BearerToken("cred_synthetic_123")
KEY = IdempotencyKey("synthetic-operation-123")
ETAG = '"page-v2-' + "a" * 64 + '"'
PAGE = "20260929t120000000z-synthetic"
REVISION = "rev_0123456789abcdef0123456789abcdef"


def manifest(
    files: tuple[tuple[str, bytes], ...] = (("content.md", b"# Example"),),
) -> dict[str, object]:
    entries = [
        {
            "filename": name,
            "size_bytes": len(content),
            "content_sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, content in sorted(files)
    ]
    digest = hashlib.sha256(b"patchouli-page-file-snapshot-v1\x00")
    digest.update(len(entries).to_bytes(8, "big"))
    for entry in entries:
        name_bytes = str(entry["filename"]).encode()
        digest.update(len(name_bytes).to_bytes(8, "big"))
        digest.update(name_bytes)
        size = entry["size_bytes"]
        assert isinstance(size, int)
        digest.update(size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(str(entry["content_sha256"])))
    return {
        "page_id": PAGE,
        "revision_id": REVISION,
        "revision_number": 1,
        "snapshot_sha256": digest.hexdigest(),
        "files": entries,
    }


def client(handler: httpx.MockTransport | None = None) -> PatchouliClient:
    return PatchouliClient(
        "https://patchouli.example.invalid",
        http_transport=handler,
    )


def streamed_response(status: int, headers: dict[str, str], body: bytes) -> httpx.Response:
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


@pytest.mark.parametrize(
    "files",
    [
        (FileSetFile("content.md", b"# Example"),),
        (FileSetFile("content.md", b"# Example"), FileSetFile("figure.png", b"\x00\xff")),
        (FileSetFile("figure.png", b"\x00\xff"),),
    ],
)
def test_create_uses_one_multipart_operation_for_every_file_count(
    files: tuple[FileSetFile, ...],
) -> None:
    expected = manifest(tuple((file.filename, file.body) for file in files))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/libraries/lib_1/sections/sec_1/books/book_1/pages"
        assert request.headers["Idempotency-Key"] == "synthetic-operation-123"
        assert "If-Match" not in request.headers
        body = request.content
        assert body.count(b'name="metadata"') == 1
        assert body.count(b'name="file"; filename="') == len(files)
        assert b'name="content"' not in body
        assert body.index(b'name="metadata"') < body.index(b'name="file"')
        assert body.count(b"Content-Type: application/json") == 1
        for file in files:
            assert b'filename="' + file.filename.encode() + b'"' in body
            assert file.body in body
        location = f"/api/v1/libraries/lib_1/sections/sec_1/pages/{PAGE}/revisions/{REVISION}/files"
        return httpx.Response(
            201,
            headers=protected_headers(ETag=ETAG, Location=location),
            json={
                **expected,
                "section_id": "sec_1",
                "book_id": "book_1",
                "occurred_at": "2026-09-29T12:00:00.000000Z",
                "occurrence_defaulted": False,
            },
        )

    with client(httpx.MockTransport(handler)) as api:
        result = api.create_file_set(
            "lib_1",
            "sec_1",
            "book_1",
            FileSetCreateMetadata(
                "Example",
                FileSetSource("manual", captured_at=123),
                datetime(2026, 9, 29, 12, tzinfo=UTC),
            ),
            files,
            token=TOKEN,
            idempotency_key=KEY,
        )
    assert result.value.manifest.snapshot_sha256 == expected["snapshot_sha256"]
    assert result.metadata.etag == ETAG


def test_revise_current_exact_manifest_and_download() -> None:
    expected = manifest((("content.md", b"# Example"), ("figure.png", b"\x00\xff")))
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.method == "POST":
            assert request.headers["If-Match"] == ETAG
            assert request.content.count(b'name="file"; filename="') == 2
            return httpx.Response(
                200,
                headers=protected_headers(ETag=ETAG),
                json={**expected, "changed": False, "section_id": "sec_1"},
            )
        if request.url.path.endswith("/figure.png"):
            return streamed_response(
                200,
                {
                    **protected_headers(),
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": (
                        "attachment; filename=\"download\"; filename*=UTF-8''"
                        + quote("figure.png", safe="")
                    ),
                    "X-Content-Type-Options": "nosniff",
                },
                b"\x00\xff",
            )
        if request.url.path.endswith("/files"):
            return httpx.Response(200, headers=protected_headers(), json=expected)
        return httpx.Response(200, headers=protected_headers(ETag=ETAG), json=expected)

    with client(httpx.MockTransport(handler)) as api:
        revision = api.revise_file_set(
            "lib_1",
            "sec_1",
            PAGE,
            FileSetRevisionMetadata(FileSetSource("manual")),
            (FileSetFile("content.md", b"# Example"), FileSetFile("figure.png", b"\x00\xff")),
            token=TOKEN,
            idempotency_key=KEY,
            if_match=ETAG,
        )
        current = api.get_current_file_set("lib_1", "sec_1", PAGE, token=TOKEN)
        exact = api.get_file_set_manifest("lib_1", "sec_1", PAGE, REVISION, token=TOKEN)
        content = api.download_file_set_file(
            "lib_1", "sec_1", PAGE, REVISION, exact.value.files[1], token=TOKEN
        )
    assert revision.value.changed is False
    assert current.metadata.etag == ETAG
    assert exact.value == current.value
    assert content.value == b"\x00\xff"
    assert seen[2].endswith(f"/revisions/{REVISION}/files")


@pytest.mark.parametrize(
    ("override", "headers", "error"),
    [
        ({"section_id": "sec_wrong"}, {}, "scope"),
        ({"page_id": "page_wrong"}, {}, "Location"),
        ({"snapshot_sha256": "b" * 64}, {}, "snapshot digest"),
        ({}, {"Location": "https://evil.invalid/file"}, "canonical relative"),
        ({}, {"ETag": "W/" + ETAG}, "strong Page ETag"),
    ],
)
def test_create_rejects_mismatched_or_unsafe_response(
    override: dict[str, object], headers: dict[str, str], error: str
) -> None:
    payload = {
        **manifest(),
        "section_id": "sec_1",
        "book_id": "book_1",
        "occurred_at": "2026-09-29T12:00:00.000000Z",
        "occurrence_defaulted": True,
        **override,
    }
    response_headers = {
        **protected_headers(),
        "ETag": ETAG,
        "Location": (
            f"/api/v1/libraries/lib_1/sections/sec_1/pages/{PAGE}/revisions/{REVISION}/files"
        ),
        **headers,
    }
    with (
        client(
            httpx.MockTransport(
                lambda _: httpx.Response(201, headers=response_headers, json=payload)
            )
        ) as api,
        pytest.raises(ProtocolError, match=error),
    ):
        api.create_file_set(
            "lib_1",
            "sec_1",
            "book_1",
            FileSetCreateMetadata("Example", FileSetSource("manual")),
            (FileSetFile("content.md", b"# Example"),),
            token=TOKEN,
            idempotency_key=KEY,
        )


def test_download_rejects_unsafe_headers_and_corrupt_bytes() -> None:
    summary = FileSetFileSummary("figure.png", 2, hashlib.sha256(b"\x00\xff").hexdigest())
    headers = {
        **protected_headers(),
        "Content-Type": "application/octet-stream",
        "Content-Disposition": "attachment; filename=\"download\"; filename*=UTF-8''figure.png",
        "X-Content-Type-Options": "nosniff",
    }
    for changed_headers, body in [
        ({"Content-Disposition": "inline"}, b"\x00\xff"),
        ({"X-Content-Type-Options": ""}, b"\x00\xff"),
        ({}, b"\x00\x00"),
    ]:

        def handler(
            _request: httpx.Request,
            changed_headers: dict[str, str] = changed_headers,
            body: bytes = body,
        ) -> httpx.Response:
            return streamed_response(200, {**headers, **changed_headers}, body)

        with client(httpx.MockTransport(handler)) as api, pytest.raises(ProtocolError):
            api.download_file_set_file("lib_1", "sec_1", PAGE, REVISION, summary, token=TOKEN)


def test_file_set_limits_optional_and_separate_from_legacy_limit() -> None:
    legacy = {
        "max_content_bytes": 2 * 1024 * 1024,
        "default_page_size": 20,
        "max_page_size": 100,
        "max_query_bytes": 4096,
    }
    assert ApiLimits.from_dict(legacy).file_set is None
    limits = ApiLimits.from_dict(
        {
            **legacy,
            "file_set": {
                "max_file_bytes": 16 * 1024 * 1024,
                "max_page_bytes": 64 * 1024 * 1024,
                "max_files_per_page": 64,
            },
        }
    )
    assert limits.file_set is not None
    assert limits.file_set.max_file_bytes == 16 * 1024 * 1024
    assert limits.max_content_bytes == 2 * 1024 * 1024


def test_file_set_upload_rejects_ambiguous_names_and_keeps_empty_binary() -> None:
    with pytest.raises(ValueError, match="ambiguous duplicate"):
        build_file_set_multipart(
            {"source": {"kind": "manual"}},
            (FileSetFile("A.txt", b"a"), FileSetFile("a.TXT", b"b")),
        )
    body = build_file_set_multipart(
        {"source": {"kind": "manual"}},
        (FileSetFile("empty.bin", b""),),
        boundary="synthetic-boundary",
    ).body
    assert b'filename="empty.bin"\r\n\r\n\r\n--synthetic-boundary--' in body


@pytest.mark.parametrize("name", ["../x", "CON.txt", "trailing. ", "a\x00b", "a\\b"])
def test_file_set_input_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError):
        FileSetFile(name, b"payload")


def test_file_set_input_validates_source_and_metadata() -> None:
    for kind in ("", " manual", "manual ", "manual\x00origin"):
        with pytest.raises(ValueError):
            FileSetSource(kind)
    with pytest.raises(ValueError):
        FileSetSource("manual", locator="")
    with pytest.raises(ValueError):
        FileSetSource("manual", captured_at=True)
    with pytest.raises(ValueError):
        FileSetCreateMetadata(" ", FileSetSource("manual"))
    with pytest.raises(ValueError):
        FileSetCreateMetadata("Example", FileSetSource("manual"), datetime(2026, 9, 29))
    with pytest.raises(ValueError):
        FileSetRevisionMetadata(source=None)  # type: ignore[arg-type]


def test_multipart_rejects_invalid_boundary_empty_files_and_oversized_metadata() -> None:
    for boundary in ("a;b", "bad\r\nheader", "x" * 71):
        with pytest.raises(ValueError, match="boundary"):
            build_file_set_multipart({}, (FileSetFile("x.txt", b"x"),), boundary=boundary)
    with pytest.raises(ValueError, match="typed files"):
        build_file_set_multipart({}, ())
    with pytest.raises(ValueError, match="metadata"):
        build_file_set_multipart({"title": "x" * (64 * 1024)}, (FileSetFile("x.txt", b"x"),))


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"page_id": ""}, "identifiers"),
        ({"revision_number": 0}, "identifiers"),
        ({"files": []}, "identifiers"),
        ({"snapshot_sha256": "A" * 64}, "SHA-256"),
        ({"unexpected": 1}, "expected fields"),
    ],
)
def test_manifest_rejects_invalid_top_level_fields(change: dict[str, object], error: str) -> None:
    with pytest.raises(ProtocolError, match=error):
        FileSetManifest.from_dict({**manifest(), **change})


@pytest.mark.parametrize(
    ("entry_change", "error"),
    [
        ({"filename": "../unsafe"}, "unsafe filename"),
        ({"size_bytes": -1}, "file size"),
        ({"content_sha256": "bad"}, "SHA-256"),
        ({"unexpected": 1}, "expected fields"),
    ],
)
def test_manifest_rejects_invalid_file_entries(entry_change: dict[str, object], error: str) -> None:
    value = manifest()
    file = value["files"]
    assert isinstance(file, list)
    first = file[0]
    assert isinstance(first, dict)
    value["files"] = [{**first, **entry_change}]
    with pytest.raises(ProtocolError, match=error):
        FileSetManifest.from_dict(value)


def test_manifest_rejects_unsorted_files_and_incorrect_snapshot_digest() -> None:
    value = manifest((("a.txt", b"a"), ("b.txt", b"b")))
    files = value["files"]
    assert isinstance(files, list)
    value["files"] = list(reversed(files))
    with pytest.raises(ProtocolError, match="ordered"):
        FileSetManifest.from_dict(value)
    value = manifest()
    value["snapshot_sha256"] = "b" * 64
    with pytest.raises(ProtocolError, match="snapshot digest"):
        FileSetManifest.from_dict(value)


def test_current_and_exact_reads_reject_wrong_ids_and_missing_etag() -> None:
    for operation in ("current", "exact"):
        for wrong in (True, False):
            payload = manifest()
            if wrong:
                payload["page_id"] = "different-page"
            headers = protected_headers(ETag=ETAG) if not wrong else protected_headers()

            def handler(
                _request: httpx.Request,
                headers: dict[str, str] = headers,
                payload: dict[str, object] = payload,
            ) -> httpx.Response:
                return httpx.Response(200, headers=headers, json=payload)

            with client(httpx.MockTransport(handler)) as api:
                if operation == "current":
                    if wrong:
                        with pytest.raises(ProtocolError, match="requested Page"):
                            api.get_current_file_set("lib_1", "sec_1", PAGE, token=TOKEN)
                    else:
                        # The exact route has no ETag requirement; current does.
                        assert (
                            api.get_current_file_set(
                                "lib_1", "sec_1", PAGE, token=TOKEN
                            ).metadata.etag
                            == ETAG
                        )
                elif wrong:
                    with pytest.raises(ProtocolError, match="requested Revision"):
                        api.get_file_set_manifest("lib_1", "sec_1", PAGE, REVISION, token=TOKEN)
                else:
                    assert (
                        api.get_file_set_manifest(
                            "lib_1", "sec_1", PAGE, REVISION, token=TOKEN
                        ).value.revision_id
                        == REVISION
                    )

    with (
        client(
            httpx.MockTransport(
                lambda _: httpx.Response(200, headers=protected_headers(), json=manifest())
            )
        ) as api,
        pytest.raises(ProtocolError, match="strong Page ETag"),
    ):
        api.get_current_file_set("lib_1", "sec_1", PAGE, token=TOKEN)


def test_revise_rejects_wrong_response_identity_and_weak_precondition() -> None:
    with (
        client(httpx.MockTransport(lambda _: httpx.Response(200))) as api,
        pytest.raises(ProtocolError, match="strong Page ETag"),
    ):
        api.revise_file_set(
            "lib_1",
            "sec_1",
            PAGE,
            FileSetRevisionMetadata(FileSetSource("manual")),
            (FileSetFile("content.md", b"# Example"),),
            token=TOKEN,
            idempotency_key=KEY,
            if_match="W/" + ETAG,
        )
    payload = {**manifest(), "changed": True, "section_id": "other"}
    with (
        client(
            httpx.MockTransport(
                lambda _: httpx.Response(200, headers=protected_headers(ETag=ETAG), json=payload)
            )
        ) as api,
        pytest.raises(ProtocolError, match="requested Page"),
    ):
        api.revise_file_set(
            "lib_1",
            "sec_1",
            PAGE,
            FileSetRevisionMetadata(FileSetSource("manual")),
            (FileSetFile("content.md", b"# Example"),),
            token=TOKEN,
            idempotency_key=KEY,
            if_match=ETAG,
        )


def test_binary_error_uses_problem_details() -> None:
    file = FileSetFileSummary("content.md", 9, hashlib.sha256(b"# Example").hexdigest())
    problem = {
        "type": "about:blank",
        "title": "Not found",
        "status": 404,
        "detail": "The resource does not exist.",
        "code": "not_found",
        "request_id": "req_synthetic",
    }
    with (
        client(
            httpx.MockTransport(
                lambda _: streamed_response(
                    404,
                    {**protected_headers(), "Content-Type": "application/problem+json"},
                    json.dumps(problem).encode(),
                )
            )
        ) as api,
        pytest.raises(ProblemError),
    ):
        api.download_file_set_file("lib_1", "sec_1", PAGE, REVISION, file, token=TOKEN)


def test_file_set_source_wire_carries_locator_and_microseconds() -> None:
    source = FileSetSource("manual", locator="synthetic:document", captured_at=123)
    assert source.to_wire() == {
        "kind": "manual",
        "locator": "synthetic:document",
        "captured_at": 123,
    }
    with pytest.raises(ValueError, match="bounded bytes"):
        FileSetFile("content.md", "not bytes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source must be"):
        FileSetCreateMetadata("Title", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="datetime"):
        FileSetCreateMetadata("Title", source, "2026-09-29")  # type: ignore[arg-type]


def test_file_set_response_rejects_noncanonical_name_and_excessive_total_size() -> None:
    entry = {
        "filename": "e\u0301.txt",
        "size_bytes": 0,
        "content_sha256": hashlib.sha256(b"").hexdigest(),
    }
    with pytest.raises(ProtocolError, match="unsafe filename"):
        FileSetFileSummary.from_dict(entry)
    huge_files = [
        {"filename": f"{index}.bin", "size_bytes": 16 * 1024 * 1024, "content_sha256": "a" * 64}
        for index in range(5)
    ]
    with pytest.raises(ProtocolError, match="page size limit"):
        FileSetManifest.from_dict({**manifest(), "files": huge_files})


def test_file_set_response_rejects_wrong_create_and_revision_shapes() -> None:
    from patchouli_client import FileSetCreateResult, FileSetRevisionResult

    create = {
        **manifest(),
        "section_id": "sec_1",
        "book_id": "book_1",
        "occurred_at": "2026-09-29T12:00:00.000000Z",
        "occurrence_defaulted": False,
    }
    cases: list[tuple[dict[str, object], str]] = [
        ({"revision_number": 2}, "first Revision"),
        ({"book_id": ""}, "scope identifier"),
        ({"extra": 1}, "expected fields"),
    ]
    for change, error in cases:
        with pytest.raises(ProtocolError, match=error):
            FileSetCreateResult.from_dict({**create, **change})
    with pytest.raises(ProtocolError, match="Section identifier"):
        FileSetRevisionResult.from_dict({**manifest(), "changed": True, "section_id": ""})


def test_revise_rejects_unexpected_location() -> None:
    payload = {**manifest(), "changed": True, "section_id": "sec_1"}
    with (
        client(
            httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers=protected_headers(ETag=ETAG, Location="/api/v1/unexpected"),
                    json=payload,
                )
            )
        ) as api,
        pytest.raises(ProtocolError, match="unexpectedly contained Location"),
    ):
        api.revise_file_set(
            "lib_1",
            "sec_1",
            PAGE,
            FileSetRevisionMetadata(FileSetSource("manual")),
            (FileSetFile("content.md", b"# Example"),),
            token=TOKEN,
            idempotency_key=KEY,
            if_match=ETAG,
        )


@pytest.mark.parametrize(
    ("status", "changed", "error"),
    [
        (204, {}, "status"),
        (200, {"Content-Type": "text/html"}, "application/octet-stream"),
        (200, {"Content-Disposition": ""}, "attachment disposition"),
    ],
)
def test_download_rejects_wrong_status_or_media(
    status: int, changed: dict[str, str], error: str
) -> None:
    file = FileSetFileSummary("content.md", 9, hashlib.sha256(b"# Example").hexdigest())
    headers = {
        **protected_headers(),
        "Content-Type": "application/octet-stream",
        "Content-Disposition": "attachment; filename=\"download\"; filename*=UTF-8''content.md",
        "X-Content-Type-Options": "nosniff",
        **changed,
    }
    with (
        client(
            httpx.MockTransport(lambda _: streamed_response(status, headers, b"# Example"))
        ) as api,
        pytest.raises(ProtocolError, match=error),
    ):
        api.download_file_set_file("lib_1", "sec_1", PAGE, REVISION, file, token=TOKEN)


def test_download_requires_one_content_disposition_header() -> None:
    file = FileSetFileSummary("content.md", 9, hashlib.sha256(b"# Example").hexdigest())
    with (
        client(
            httpx.MockTransport(
                lambda _: streamed_response(
                    200,
                    {
                        **protected_headers(),
                        "Content-Type": "application/octet-stream",
                        "X-Content-Type-Options": "nosniff",
                    },
                    b"# Example",
                )
            )
        ) as api,
        pytest.raises(ProtocolError, match="one Content-Disposition"),
    ):
        api.download_file_set_file("lib_1", "sec_1", PAGE, REVISION, file, token=TOKEN)
