"""Real file-picker/FormData writes against an isolated migrated loopback app."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from urllib.parse import unquote
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
