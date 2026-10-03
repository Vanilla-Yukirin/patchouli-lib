"""Real file-picker/FormData writes against an isolated migrated loopback app."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import quote, unquote
from uuid import uuid4

import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, func, select
from test_browser_login import _available_port, _browser_executable, _DevTools, _page_target

from patchouli_lib.admin.file_set_receipts import MasterFileSetReceiptRow
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.app import create_app
from patchouli_lib.auth.models import MasterAuditEvent
from patchouli_lib.config import Settings
from patchouli_lib.content.models import Page, PageSource, Revision
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService

_MASTER_TOKEN = "synthetic master token for isolated file picker tests"
_ORIGINAL_MD = b"# Synthetic original\n"
_REVISED_MD = b"# Synthetic revised\n"
_BINARY = b"\x00\xff\x81\x00synthetic binary\r\n"
_DECLARED_TIME = "2026-08-13T10:00:00.123456Z"


@dataclass(frozen=True)
class _Browser:
    tools: _DevTools
    engine: Engine
    origin: str
    book_path: str
    download_root: Path


def _wait(tools: _DevTools, expression: str, *, description: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if tools.evaluate(expression):
                return
        except TimeoutError:
            pass
        time.sleep(0.05)
    pytest.fail(f"The isolated browser did not finish {description}.")


def _navigate(browser: _Browser, path: str, ready: str) -> None:
    assert path.startswith("/admin") and not path.startswith("//")
    browser.tools.command("Page.navigate", {"url": browser.origin + path})
    _wait(
        browser.tools,
        f"location.pathname === {json.dumps(path.split('?', 1)[0])} && ({ready})",
        description="the local page navigation",
    )


@pytest.fixture
def file_set_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[_Browser]:
    context = getattr(request, "param", None)
    assert context in (None, "insecure-http")
    hostname = "synthetic.invalid" if context == "insecure-http" else "127.0.0.1"
    executable = _browser_executable()
    database_url = f"sqlite:///{(tmp_path / 'file-set-browser.sqlite').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    application = create_app(
        Settings.model_validate(
            {
                "environment": "production",
                "database_url": database_url,
                "admin_allow_private_http": True,
                "retrieval_cursor_signing_secret": "r" * 32,
                "admin_session_signing_secret": "s" * 32,
                "admin_session_ttl_seconds": 600,
            }
        )
    )
    engine = application.state.engine
    ids = iter(("1" * 32, "2" * 32, "3" * 32))
    with immediate_transaction(engine) as connection:
        structure = LibrarySeedService(
            LibraryRepository(connection), id_factory=lambda: next(ids), clock=lambda: 1_000_000
        ).seed(
            LibraryStructureSeed(
                library_name="Synthetic Library",
                section_name="Synthetic Section",
                book_name="Synthetic Book",
            )
        )
        MasterTokenRepository(connection).initialize_from_local_cli(_MASTER_TOKEN, now=1_000_000)
    book_path = (
        f"/admin/libraries/{structure.library.id}/sections/{structure.section.id}"
        f"/books/{structure.book.id}"
    )
    port = _available_port()
    origin = f"http://{hostname}:{port}"
    server = uvicorn.Server(
        uvicorn.Config(
            application, host="127.0.0.1", port=port, log_level="error", access_log=False
        )
    )
    thread = Thread(target=server.run, daemon=True)
    process: subprocess.Popen[bytes] | None = None
    tools: _DevTools | None = None
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "The isolated loopback application did not start."
        debug_port = _available_port()
        stderr_path = tmp_path / "browser-stderr.log"
        with stderr_path.open("wb") as stderr_output:
            process = subprocess.Popen(
                [
                    executable,
                    "--headless=new",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-sync",
                    "--no-first-run",
                    "--no-sandbox",
                    "--no-proxy-server",
                    "--host-resolver-rules=MAP synthetic.invalid 127.0.0.1, "
                    "MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
                    f"--remote-debugging-port={debug_port}",
                    f"--user-data-dir={tmp_path / 'browser-profile'}",
                    f"{origin}/admin/login?lang=en",
                ],
                stdout=subprocess.DEVNULL,
                stderr=stderr_output,
            )
        target = _page_target(debug_port, origin, process, stderr_path)
        tools = _DevTools(str(target["webSocketDebuggerUrl"]))
        tools.command("Network.enable")
        tools.command("Runtime.enable")
        _wait(
            tools,
            'document.querySelector("input[name=password]") !== null',
            description="the master login form",
        )
        assert tools.evaluate(
            f"""(() => {{
              document.querySelector('input[name="password"]').value = {json.dumps(_MASTER_TOKEN)};
              document.querySelector('form').requestSubmit();
              return true;
            }})()"""
        )
        _wait(
            tools,
            'location.pathname === "/admin" && document.querySelector("h1") !== null',
            description="the master login",
        )
        download_root = tmp_path / "browser-downloads"
        download_root.mkdir()
        yield _Browser(tools, engine, origin, book_path, download_root)
    finally:
        if tools is not None:
            tools.close()
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
        server.should_exit = True
        thread.join(timeout=10)
        engine.dispose()
        assert not thread.is_alive(), "The isolated loopback application did not stop."


def _upload_form(browser: _Browser, path: str, *, create: bool) -> None:
    _navigate(
        browser,
        path,
        'document.querySelector("#upload-files") !== null && '
        'document.querySelector("#upload-submit")?.disabled === false',
    )
    if create:
        assert browser.tools.evaluate(
            f"""(() => {{
              document.querySelector('#upload-title').value = 'Synthetic browser document';
              document.querySelector('#upload-time').value = {json.dumps(_DECLARED_TIME)};
              return true;
            }})()"""
        )


def _select_files(browser: _Browser, files: tuple[Path, ...]) -> None:
    document = browser.tools.command("DOM.getDocument")
    selected = browser.tools.command(
        "DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": "#upload-files"}
    )
    assert selected["nodeId"] != 0
    browser.tools.command(
        "DOM.setFileInputFiles",
        {"nodeId": selected["nodeId"], "files": [str(path.resolve()) for path in files]},
    )
    assert browser.tools.evaluate(
        "Array.from(document.querySelector('#upload-files').files, file => file.name)"
    ) == [path.name for path in files]


def _submit(browser: _Browser, *, expected_status: str) -> str:
    assert browser.tools.evaluate(
        """(() => {
          const button = document.querySelector('#upload-submit');
          if (!button || button.disabled) return false;
          button.click();
          return true;
        })()"""
    )
    _wait(
        browser.tools,
        f"document.querySelector('#upload-status')?.textContent === {json.dumps(expected_status)}",
        description="the file-picker submission",
    )
    link = browser.tools.evaluate(
        "document.querySelector('#upload-result a')?.getAttribute('href')"
    )
    assert isinstance(link, str) and link.startswith(browser.book_path + "/pages/")
    requests = [
        entry["params"]["request"]
        for entry in browser.tools.events
        if entry.get("method") == "Network.requestWillBeSent"
        and entry.get("params", {}).get("request", {}).get("method") == "POST"
        and entry.get("params", {})
        .get("request", {})
        .get("url", "")
        .startswith(browser.origin + browser.book_path + "/pages")
    ]
    assert requests, "The real browser did not issue an upload request."
    headers = {key.casefold(): value for key, value in requests[-1]["headers"].items()}
    assert headers["content-type"].startswith("multipart/form-data; boundary=")
    return link


def _download(browser: _Browser, path: str, expected: bytes) -> None:
    assert path.startswith(browser.book_path + "/pages/")
    revision = path.rsplit("/files/", 1)[0]
    _navigate(browser, revision, 'document.querySelector(".revision-files a[download]") !== null')
    directory = browser.download_root / uuid4().hex
    directory.mkdir()
    browser.tools.command(
        "Browser.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": str(directory.resolve())},
    )
    assert browser.tools.evaluate(
        f"""(() => {{
          const link = Array.from(document.querySelectorAll('.revision-files a[download]'))
            .find(item => item.getAttribute('href') === {json.dumps(path)});
          if (!link) return false;
          link.click();
          return true;
        }})()"""
    )
    downloaded = directory / unquote(path.rsplit("/", 1)[-1])
    deadline = time.monotonic() + 10
    while not downloaded.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert downloaded.is_file(), "The exact-revision link did not complete its local download."
    assert downloaded.read_bytes() == expected
    assert list(directory.iterdir()) == [downloaded]


def _assert_counts(browser: _Browser, *, revisions: int, receipts: int) -> None:
    with browser.engine.connect() as connection:
        counts = tuple(
            connection.scalar(select(func.count()).select_from(model))
            for model in (Page, Revision, PageSource, MasterAuditEvent, MasterFileSetReceiptRow)
        )
    assert counts == (1, revisions, revisions, revisions, receipts)
    activities = AdminReadModel(browser.engine).recent_content_activity()
    assert len(activities) == revisions
    assert {item.revision_number for item in activities} == set(range(1, revisions + 1))
    assert sum(item.action == "content.page.file_set.create" for item in activities) == 1
    assert (
        sum(item.action == "content.page.file_set.revise" for item in activities) == revisions - 1
    )


def _local_files(tmp_path: Path, *, revised: bool = False) -> tuple[Path, Path]:
    directory = tmp_path / ("revised" if revised else "original")
    directory.mkdir()
    markdown = directory / "content.md"
    binary = directory / "payload.bin"
    markdown.write_bytes(_REVISED_MD if revised else _ORIGINAL_MD)
    binary.write_bytes(_BINARY)
    return markdown, binary


def test_real_file_picker_creates_downloads_revises_and_keeps_noop_history(
    file_set_browser: _Browser, tmp_path: Path
) -> None:
    browser = file_set_browser
    original_files = _local_files(tmp_path)
    _upload_form(browser, browser.book_path + "/new-page", create=True)
    _select_files(browser, original_files)
    first = _submit(browser, expected_status="Files saved.")
    assert first.endswith("/revisions/1")
    _assert_counts(browser, revisions=1, receipts=1)
    _navigate(browser, first, 'document.querySelector(".revision-files a[download]") !== null')
    expected_links = [first + "/files/" + quote(path.name, safe="") for path in original_files]
    assert browser.tools.evaluate(
        "Array.from(document.querySelectorAll('.revision-files a[download]'), "
        "link => link.getAttribute('href')).sort()"
    ) == sorted(expected_links)
    for link, content in zip(expected_links, (_ORIGINAL_MD, _BINARY), strict=True):
        _download(browser, link, content)

    page_path = first.rsplit("/revisions/", 1)[0]
    revised_files = _local_files(tmp_path, revised=True)
    _upload_form(browser, page_path + "/files/edit", create=False)
    previous_etag = browser.tools.evaluate(
        "document.querySelector('input[name=expected_etag]').value"
    )
    _select_files(browser, revised_files)
    second = _submit(browser, expected_status="Files saved.")
    assert second == page_path + "/revisions/2"
    _assert_counts(browser, revisions=2, receipts=2)
    assert browser.tools.evaluate("document.querySelector('#upload-submit').disabled") is True
    assert (
        browser.tools.evaluate("document.querySelector('#upload-new-operation').disabled") is True
    )
    assert (
        browser.tools.evaluate("document.querySelector('#upload-reload').getAttribute('href')")
        == page_path + "/files/edit"
    )
    assert browser.tools.evaluate("document.querySelector('#upload-reload').textContent") == (
        "Reload current revision to update again"
    )
    assert browser.tools.evaluate(
        "(() => { document.querySelector('#upload-reload').click(); return true; })()"
    )
    _wait(
        browser.tools,
        "document.querySelector('#upload-submit')?.disabled === false && "
        "document.querySelector('input[name=expected_etag]')?.value !== "
        + json.dumps(previous_etag),
        description="the clickable current-revision reload",
    )
    assert browser.tools.evaluate("location.pathname") == page_path + "/files/edit"
    assert browser.tools.evaluate("document.querySelector('#upload-files').files.length") == 0
    _download(browser, first + "/files/content.md", _ORIGINAL_MD)
    _download(browser, second + "/files/content.md", _REVISED_MD)
    _download(browser, second + "/files/payload.bin", _BINARY)

    _upload_form(browser, page_path + "/files/edit", create=False)
    _select_files(browser, tuple(reversed(revised_files)))
    unchanged = _submit(browser, expected_status="Identical files; no new revision.")
    assert unchanged == second
    _assert_counts(browser, revisions=2, receipts=3)
    _navigate(browser, "/admin", "document.querySelector('h1') !== null")
    for revision in (first, second):
        assert (
            browser.tools.evaluate(
                f"document.querySelectorAll('a[href={json.dumps(revision)}]').length"
            )
            == 1
        )


@pytest.mark.parametrize("file_set_browser", ["insecure-http"], indirect=True)
def test_insecure_http_creates_two_documents_with_explicit_new_operation(
    file_set_browser: _Browser, tmp_path: Path
) -> None:
    browser = file_set_browser
    assert browser.tools.evaluate("location.hostname") == "synthetic.invalid"
    assert browser.tools.evaluate("location.protocol") == "http:"
    assert browser.tools.evaluate("window.isSecureContext") is False
    files = _local_files(tmp_path)
    _upload_form(browser, browser.book_path + "/new-page", create=True)
    _select_files(browser, files)
    first = _submit(browser, expected_status="Files saved.")
    assert first.endswith("/revisions/1")
    _assert_counts(browser, revisions=1, receipts=1)
    assert browser.tools.evaluate("document.querySelector('#upload-submit').disabled") is True
    assert (
        browser.tools.evaluate("document.querySelector('#upload-new-operation').disabled") is False
    )
    assert browser.tools.evaluate(
        "(() => { document.querySelector('#upload-new-operation').click(); return true; })()"
    )
    _wait(
        browser.tools,
        "document.querySelector('#upload-status')?.textContent === "
        "'New operation started; check metadata and files.' && "
        "document.querySelector('#upload-submit')?.disabled === false",
        description="the explicit second creation operation",
    )
    assert browser.tools.evaluate("document.querySelector('#upload-result').childElementCount") == 0
    assert browser.tools.evaluate("document.querySelector('#upload-files').files.length") == 0
    assert browser.tools.evaluate("document.querySelector('#upload-title').readOnly") is False
    assert browser.tools.evaluate(
        "(() => { document.querySelector('#upload-title').value = "
        "'Second synthetic browser document'; return true; })()"
    )
    _select_files(browser, tuple(reversed(files)))
    second = _submit(browser, expected_status="Files saved.")
    assert second.endswith("/revisions/1") and second != first
    assert browser.tools.evaluate("window.isSecureContext") is False
    assert browser.tools.evaluate("document.querySelector('#upload-submit').disabled") is True
    assert (
        browser.tools.evaluate("document.querySelector('#upload-new-operation').disabled") is False
    )

    requests = [
        entry["params"]["request"]
        for entry in browser.tools.events
        if entry.get("method") == "Network.requestWillBeSent"
        and entry.get("params", {}).get("request", {}).get("method") == "POST"
        and entry.get("params", {}).get("request", {}).get("url")
        == browser.origin + browser.book_path + "/pages"
    ]
    keys = [
        {key.casefold(): value for key, value in item["headers"].items()}["idempotency-key"]
        for item in requests
    ]
    assert len(keys) == 2 and len(set(keys)) == 2
    assert all(len(key) == 32 and set(key) <= set("0123456789abcdef") for key in keys)
    with browser.engine.connect() as connection:
        counts = tuple(
            connection.scalar(select(func.count()).select_from(model))
            for model in (Page, Revision, PageSource, MasterAuditEvent, MasterFileSetReceiptRow)
        )
        digests = tuple(connection.scalars(select(MasterFileSetReceiptRow.key_digest)))
        titles = set(connection.scalars(select(Page.title)))
    assert counts == (2, 2, 2, 2, 2)
    assert len(set(digests)) == 2
    assert titles == {"Synthetic browser document", "Second synthetic browser document"}
    activities = AdminReadModel(browser.engine).recent_content_activity()
    assert len(activities) == 2
    assert all(item.action == "content.page.file_set.create" for item in activities)
    assert all(item.revision_number == 1 for item in activities)
    _download(browser, second + "/files/content.md", _ORIGINAL_MD)
    _download(browser, second + "/files/payload.bin", _BINARY)


@pytest.mark.parametrize("operation", ["create", "revise"])
def test_real_form_retry_survives_refresh_after_committed_response_loss(
    file_set_browser: _Browser, tmp_path: Path, operation: str
) -> None:
    browser = file_set_browser
    files = _local_files(tmp_path)
    _upload_form(browser, browser.book_path + "/new-page", create=True)
    if operation == "revise":
        _select_files(browser, files)
        first = _submit(browser, expected_status="Files saved.")
        page_path = first.rsplit("/revisions/", 1)[0]
        files = _local_files(tmp_path, revised=True)
        _upload_form(browser, page_path + "/files/edit", create=False)
    target = browser.tools.evaluate(
        "document.querySelector('#file-set-upload').getAttribute('action')"
    )
    storage_key = "patchouli-file-set-v1:" + target
    _select_files(browser, files)
    assert browser.tools.evaluate(
        """(() => {
          const originalFetch = window.fetch.bind(window);
          window.fetch = async (...args) => {
            const response = await originalFetch(...args);
            if (args[1]?.method === 'POST') {
              window.syntheticLostResponse = {
                status: response.status, success: await response.clone().json()
              };
              throw new Error('synthetic committed response loss');
            }
            return response;
          };
          document.querySelector('#upload-submit').click();
          return true;
        })()"""
    )
    _wait(
        browser.tools,
        "document.querySelector('#upload-status')?.textContent.startsWith('Unconfirmed result.')",
        description="the committed but unconfirmed upload",
    )
    lost: Any = browser.tools.evaluate("window.syntheticLostResponse")
    assert isinstance(lost, dict) and 200 <= lost["status"] < 300
    assert lost["success"]["changed"] is True and lost["success"]["replayed"] is False
    revision_count = 1 if operation == "create" else 2
    _assert_counts(browser, revisions=revision_count, receipts=revision_count)
    pending = browser.tools.evaluate(f"sessionStorage.getItem({json.dumps(storage_key)})")
    assert isinstance(pending, str)
    stored = json.loads(pending)
    assert stored["operation"] == operation

    browser.tools.command("Page.reload")
    _wait(
        browser.tools,
        "document.querySelector('#upload-status')?.textContent === "
        "'Original operation retained. Reselect the original files to retry.' && "
        "document.querySelector('#upload-submit')?.disabled === false",
        description="the restored retry record",
    )
    assert browser.tools.evaluate(f"sessionStorage.getItem({json.dumps(storage_key)})") == pending
    assert browser.tools.evaluate("document.querySelector('#upload-files').files.length") == 0
    if operation == "create":
        assert browser.tools.evaluate("document.querySelector('#upload-title').value") == (
            "Synthetic browser document"
        )
        assert (
            browser.tools.evaluate("document.querySelector('#upload-time').value") == _DECLARED_TIME
        )
        assert browser.tools.evaluate("document.querySelector('#upload-title').readOnly") is True
    else:
        assert stored["etag"] != browser.tools.evaluate(
            "document.querySelector('input[name=expected_etag]').value"
        )
    _select_files(browser, tuple(reversed(files)))
    exact = _submit(
        browser,
        expected_status="Original success returned; it may not be the current revision.",
    )
    assert exact == lost["success"]["revision_url"]
    assert exact.endswith(f"/revisions/{revision_count}")
    assert browser.tools.evaluate(f"sessionStorage.getItem({json.dumps(storage_key)})") is None
    _assert_counts(browser, revisions=revision_count, receipts=revision_count)
    _download(
        browser, exact + "/files/content.md", _ORIGINAL_MD if operation == "create" else _REVISED_MD
    )
    _download(browser, exact + "/files/payload.bin", _BINARY)
