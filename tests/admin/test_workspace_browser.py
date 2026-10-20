"""One real SPA journey covers reading, split editing, rename review and narrow layout."""

from __future__ import annotations

import base64
import json
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from browser_helpers import (
    _available_port,
    _browser_executable,
    _browser_stderr_signals,
    _DevTools,
    _page_target,
    _target_locations,
)
from sqlalchemy import Engine

from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.app import create_app
from patchouli_lib.config import Settings
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.file_set_create_core import FileSetPageCreateCore
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveSourceInput
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.library_skills.service import LibrarySkillService
from patchouli_lib.tags.repository import TagRepository

TOKEN = "synthetic browser workspace master token"
SOURCE = (
    "# Workspace title\n\nRead this document. [Attachment](note.txt)\n"
    "\n## Details\n\nOriginal text.\n"
)
ATTACHMENT = b"\xef\xbb\xbfExact attachment bytes.\n"
OTHER_SOURCE = "# Other document\n\nContent belonging only to B.\n"


@dataclass(frozen=True)
class Browser:
    tools: _DevTools
    engine: Engine
    origin: str
    library: str
    page: str
    other_page: str
    skill: str
    skill_revision: str


def wait(tools: _DevTools, expression: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if tools.evaluate(expression):
            return
        time.sleep(0.05)
    visible = tools.evaluate("document.body.innerText.slice(0, 1200)")
    pytest.fail(f"The synthetic SPA did not reach the expected state: {visible!r}")


def click(tools: _DevTools, expression: str) -> None:
    sampled = tools.command(
        "Runtime.evaluate",
        {
            "expression": f"""(async () => {{
              const deadline = performance.now() + 15000;
              let previous = null, stable = 0, observed = {{candidate: 'missing'}};
              const describe = element => element ? {{
                tag: element.tagName,
                label: (element.getAttribute('aria-label') ||
                  element.textContent || '').slice(0, 80)
              }} : null;
              while (performance.now() < deadline) {{
                const element = ({expression});
                if (element?.isConnected) {{
                  element.scrollIntoView({{block:'center', inline:'nearest', behavior:'instant'}});
                  const r = element.getBoundingClientRect(), style = getComputedStyle(element);
                  const width = document.documentElement.clientWidth;
                  const height = document.documentElement.clientHeight;
                  const left = Math.max(0, r.left), right = Math.min(width, r.right);
                  const top = Math.max(0, r.top), bottom = Math.min(height, r.bottom);
                  const x = (left + right) / 2, y = (top + bottom) / 2;
                  const hit = document.elementFromPoint(x, y);
                  const disabled = element.matches(':disabled') ||
                    element.closest('[inert]') !== null ||
                    element.getAttribute('aria-disabled') === 'true';
                  const actionable = right > left && bottom > top && !disabled &&
                    style.visibility === 'visible' && style.display !== 'none' &&
                    Number(style.opacity) > 0 && !!hit && element.contains(hit);
                  observed = {{candidate: describe(element), hit: describe(hit), disabled,
                    actionable, bounds: [r.x, r.y, r.width, r.height], point: {{x, y}},
                    viewport: {{width, height}}}};
                  const bounds = [r.x, r.y, r.width, r.height];
                  const unchanged = previous?.element === element &&
                    bounds.every((value, index) => Math.abs(value - previous.bounds[index]) < 0.25);
                  stable = actionable && unchanged ? stable + 1 : 0;
                  if (stable >= 2) return {{x, y}};
                  previous = {{element, bounds}};
                }} else {{
                  stable = 0;
                  previous = null;
                  observed = {{candidate: 'missing'}};
                }}
                await new Promise(requestAnimationFrame);
              }}
              return {{error: 'target_not_actionable', observed}};
            }})()""",
            "awaitPromise": True,
            "returnByValue": True,
        },
    )
    point = sampled.get("result", {}).get("value")
    assert isinstance(point, dict) and "x" in point and "y" in point, point
    for kind in ("mousePressed", "mouseReleased"):
        tools.command(
            "Input.dispatchMouseEvent", {"type": kind, "button": "left", "clickCount": 1, **point}
        )


def button(text: str) -> str:
    return (
        "Array.from(document.querySelectorAll('button')).find(b => "
        f"b.textContent.trim() === {json.dumps(text)} && b.getBoundingClientRect().width)"
    )


def screenshot(tools: _DevTools, path: Path) -> None:
    result = tools.command(
        "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}
    )
    path.write_bytes(base64.b64decode(result["data"]))


def edit_source(tools: _DevTools, source: str) -> None:
    click(tools, "document.querySelector('.source-pane .cm-content')")
    for kind in ("keyDown", "keyUp"):
        tools.command(
            "Input.dispatchKeyEvent",
            {"type": kind, "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 2},
        )
    tools.command("Input.insertText", {"text": source})


def select_page(tools: _DevTools, title: str) -> None:
    click(tools, "document.querySelector('.page-heading .mobile-menu')")
    wait(tools, "document.querySelector('[role=dialog] .tree-pages button') !== null")
    click(
        tools,
        "Array.from(document.querySelectorAll('[role=dialog] .tree-pages button'))"
        f".find(b=>b.textContent.trim()==={json.dumps(title)})",
    )


@pytest.fixture
def workspace_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Browser]:
    executable = _browser_executable()
    database_url = f"sqlite:///{(tmp_path / 'workspace-browser.sqlite').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config("alembic.ini"), "head")
    app = create_app(
        Settings.model_validate(
            {
                "environment": "test",
                "database_url": database_url,
                "admin_session_signing_secret": "s" * 32,
                "admin_allow_private_http": True,
                "retrieval_cursor_signing_secret": "r" * 32,
            }
        )
    )
    engine = app.state.engine
    with immediate_transaction(engine) as connection:
        structure = LibrarySeedService(LibraryRepository(connection)).seed(
            LibraryStructureSeed(
                library_name="Browser Library", section_name="Projects", book_name="Workspace"
            )
        )
        MasterTokenRepository(connection).initialize_from_local_cli(TOKEN, now=1_000_000)
        TagRepository(connection).add_tag(
            library_id=structure.library.id,
            tag_id="a" * 32,
            name="Browser tag",
            created_at=1_000_000,
        )
        page = FileSetPageCreateCore(connection).create_page(
            book=structure.book,
            title="Workspace title",
            occurred_at=1_000_000,
            operation_at=2_000_000,
            manifest=build_file_manifest((("entry.md", SOURCE.encode()), ("note.txt", ATTACHMENT))),
            source=ArchiveSourceInput(kind="synthetic_browser"),
            primary_file="entry.md",
        )
        other = FileSetPageCreateCore(connection).create_page(
            book=structure.book,
            title="Other document",
            occurred_at=1_000_001,
            operation_at=2_000_001,
            manifest=build_file_manifest((("entry.md", OTHER_SOURCE.encode()),)),
            source=ArchiveSourceInput(kind="synthetic_browser"),
            primary_file="entry.md",
        )
        skills = LibrarySkillService(connection, authorize=lambda *_: None, clock=lambda: 3_000_000)
        skill = skills.create(
            structure.library.id,
            name="browser-skill",
            description="Synthetic browser package",
            files=(("SKILL.md", b"# Historical Skill\n"),),
        ).skill
        LibrarySkillService(connection, authorize=lambda *_: None, clock=lambda: 4_000_000).revise(
            structure.library.id,
            str(skill["id"]),
            name="browser-skill",
            description="Synthetic browser package",
            files=(("SKILL.md", b"# Current Skill\n"),),
            expected_etag=str(skill["etag"]),
        )
    port = _available_port()
    origin = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = Thread(target=server.run, daemon=True)
    process: subprocess.Popen[bytes] | None = None
    tools: _DevTools | None = None
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        debug_port = _available_port()
        stderr_path = tmp_path / "browser-stderr.log"
        with stderr_path.open("wb") as stderr:
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
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
                    f"--remote-debugging-port={debug_port}",
                    f"--user-data-dir={tmp_path / 'browser-profile'}",
                    origin + "/admin",
                ],
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
        target = _page_target(debug_port, origin, process, stderr_path)
        tools = _DevTools(str(target["webSocketDebuggerUrl"]))
        tools.command("Runtime.enable")
        tools.command("Page.enable")
        tools.command(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
        )
        tools.command(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "localStorage.setItem('patchouli.locale','en');"},
        )
        tools.command("Page.navigate", {"url": origin + "/admin"})
        wait(tools, "document.querySelector('input[name=token]') !== null")
        tools.evaluate(
            f"document.querySelector('input[name=token]').value={json.dumps(TOKEN)}; "
            "document.querySelector('.auth-card').requestSubmit(); true"
        )
        wait(
            tools,
            "document.querySelector('main') !== null && "
            "document.querySelector('.auth-card') === null",
        )
        yield Browser(
            tools,
            engine,
            origin,
            structure.library.id,
            page.page_id,
            other.page_id,
            str(skill["id"]),
            str(skill["revision_id"]),
        )
    finally:
        if tools is not None:
            tools.close()
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
        engine.dispose()


def test_spa_document_edit_rename_review_and_narrow_directory(
    workspace_browser: Browser, tmp_path: Path
) -> None:
    browser = workspace_browser
    tools = browser.tools
    page_path = f"/admin/libraries/{browser.library}/pages/{browser.page}"
    tools.command("Page.navigate", {"url": browser.origin + "/admin/tags"})
    wait(tools, "document.querySelector('.tag-row input[type=checkbox]') !== null")
    click(tools, "document.querySelector('.tag-row input[type=checkbox]')")
    assert tools.evaluate(f"!({button('Save selected names')}).disabled")
    click(tools, button("Delete selected"))
    wait(tools, "document.querySelector('[role=dialog]') !== null")
    assert tools.evaluate(
        "document.querySelector('[role=dialog]').textContent.includes('Delete 1 tags?')"
    )
    click(tools, "document.querySelector('[role=dialog] button[aria-label=Close]')")
    wait(tools, "document.querySelector('[role=dialog]') === null")
    click(tools, "document.querySelector('.filter-bar input')")
    tools.command("Input.insertText", {"text": "no matching tag"})
    wait(tools, "document.querySelector('.tag-row') === null")
    assert tools.evaluate(f"({button('Save selected names')}).disabled")
    assert tools.evaluate(f"({button('Delete selected')}).disabled")
    assert tools.evaluate(f"({button('Select current results')}).disabled")
    assert tools.evaluate("document.querySelector('.panel h2').textContent") == "Tags · 0"
    for kind in ("keyDown", "keyUp"):
        tools.command(
            "Input.dispatchKeyEvent",
            {"type": kind, "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 2},
        )
    for kind in ("keyDown", "keyUp"):
        tools.command(
            "Input.dispatchKeyEvent",
            {"type": kind, "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
        )
    wait(tools, "document.querySelector('.tag-row input[type=checkbox]') !== null")
    assert not tools.evaluate("document.querySelector('.tag-row input[type=checkbox]').checked")
    assert tools.evaluate(f"({button('Save selected names')}).disabled")
    assert tools.evaluate(f"({button('Delete selected')}).disabled")
    tools.command("Page.navigate", {"url": f"{browser.origin}/admin/libraries/{browser.library}"})
    wait(tools, f"Boolean({button('New section')})")
    click(tools, button("New section"))
    wait(tools, "document.querySelector('[role=dialog] .cm-content') !== null")
    edit_source(tools, "# Discarded section rules\n")
    wait(
        tools,
        "document.querySelector('[role=dialog] .cm-content')"
        "?.textContent.includes('Discarded section rules')",
    )
    click(tools, "document.querySelector('[role=dialog] button[aria-label=Close]')")
    wait(tools, "document.querySelector('[role=dialog]') === null")
    click(tools, button("New section"))
    wait(tools, "document.querySelector('[role=dialog] .cm-content') !== null")
    assert tools.evaluate("document.querySelector('[role=dialog] .cm-content').textContent") == ""
    click(tools, "document.querySelector('[role=dialog] button[aria-label=Close]')")
    wait(tools, "document.querySelector('[role=dialog]') === null")
    tools.command("Page.navigate", {"url": browser.origin + page_path})
    wait(
        tools,
        "document.querySelector('.reading-pane .prose')"
        "?.textContent.includes('Read this document.')",
    )
    assert (
        tools.evaluate("document.querySelector('.reading-pane .prose h1')?.textContent")
        == "Workspace title"
    )
    assert tools.evaluate(
        "document.querySelector('.reading-pane .prose a')?.getAttribute('href')"
    ).startswith("/admin/api/")
    original_attachment_href = tools.evaluate(
        "document.querySelector('.reading-pane .prose a').getAttribute('href')"
    )
    screenshot(tools, tmp_path / "desktop-reading.png")
    for exact_path, title in (
        (page_path + "/revisions/1", "Workspace title"),
        (
            f"/admin/libraries/{browser.library}/skills/{browser.skill}"
            f"/revisions/{browser.skill_revision}",
            "Historical Skill",
        ),
    ):
        tools.command("Page.navigate", {"url": browser.origin + exact_path})
        wait(
            tools,
            "document.querySelector('.reading-pane .prose h1')?.textContent"
            f" === {json.dumps(title)}",
        )
        assert tools.evaluate(f"({button('Edit')}) === undefined")
        assert tools.evaluate("document.querySelector('.source-pane') === null")
        for tab in ("Files", "Properties"):
            click(
                tools,
                "Array.from(document.querySelectorAll('[role=tab]'))"
                f".find(b=>b.textContent.trim()==={json.dumps(tab)})",
            )
            assert tools.evaluate(f"({button('Rename')}) === undefined")
            assert tools.evaluate(f"({button('Save')}) === undefined")
            assert tools.evaluate(
                "document.querySelector('[role=tabpanel][data-state=active] "
                "input:not([type=file]), "
                "[role=tabpanel][data-state=active] textarea') === null"
            )
            if tab == "Properties" and title == "Historical Skill":
                assert (
                    tools.evaluate(
                        "document.querySelector('[role=tabpanel][data-state=active] time')"
                        "?.getAttribute('datetime')"
                    )
                    == "1970-01-01T00:00:03.000Z"
                )
        click(
            tools,
            "Array.from(document.querySelectorAll('[role=tab]'))"
            ".find(b=>b.textContent.trim()==='History')",
        )
        wait(tools, f"Boolean({button('Restore')})")
        click(tools, button("Back to latest"))
        wait(tools, f"Boolean({button('Edit')})")
        assert "/revisions/" not in tools.evaluate("location.pathname")
    tools.command("Page.navigate", {"url": browser.origin + page_path})
    wait(
        tools,
        "document.querySelector('.reading-pane .prose')?.textContent.includes('Original text.')",
    )
    tools.evaluate("window.confirm=()=>true; true")
    click(tools, button("Edit"))
    wait(
        tools,
        "document.querySelector('.source-pane .cm-content')"
        "?.textContent.includes('Original text.')",
    )
    edit_source(tools, "# Discarded draft\n\nThis must not survive Cancel.\n")
    wait(
        tools,
        "document.querySelector('.preview-pane .prose')?.textContent.includes('Discarded draft')",
    )
    click(tools, button("Cancel"))
    wait(
        tools,
        "document.querySelector('.reading-pane .prose')?.textContent.includes('Original text.')",
    )
    click(tools, button("Edit"))
    wait(
        tools,
        "document.querySelector('.source-pane .cm-content')"
        "?.textContent.includes('Original text.')",
    )
    assert not tools.evaluate(
        "document.querySelector('.source-pane .cm-content').textContent.includes('Discarded draft')"
    )
    edit_source(tools, "# Unsaved A draft\n\nNever save A into B.\n")
    wait(
        tools,
        "document.querySelector('.preview-pane .prose')?.textContent.includes('Unsaved A draft')",
    )
    tools.command(
        "Emulation.setDeviceMetricsOverride",
        {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": False},
    )
    select_page(tools, "Other document")
    wait(
        tools,
        "document.querySelector('.reading-pane .prose')"
        "?.textContent.includes('Content belonging only to B.')",
    )
    assert tools.evaluate("location.pathname").endswith(browser.other_page)
    click(tools, button("Edit"))
    wait(
        tools,
        "document.querySelector('.source-pane .cm-content')"
        "?.textContent.includes('Content belonging only to B.')",
    )
    assert not tools.evaluate(
        "document.querySelector('.source-pane .cm-content').textContent.includes('Unsaved A draft')"
    )
    edit_source(tools, OTHER_SOURCE + "\nSaved only to B.\n")
    wait(
        tools,
        "document.querySelector('.preview-pane .prose')?.textContent.includes('Saved only to B.')",
    )
    click(tools, button("Save"))
    wait(tools, "document.querySelector('[role=status]')?.textContent.includes('Saved')")
    with browser.engine.connect() as connection:
        repository = ContentRepository(connection)
        original = repository.get_page(browser.library, browser.page)
        other = repository.get_page(browser.library, browser.other_page)
        assert original is not None and original.current_revision_number == 1
        assert other is not None and other.current_revision_number == 2
        other_files = repository.get_current_file_manifest(other)
        assert other_files.files[0].content == (OTHER_SOURCE + "\nSaved only to B.\n").encode()
    select_page(tools, "Workspace title")
    wait(
        tools,
        "document.querySelector('.reading-pane .prose')?.textContent.includes('Original text.')",
    )
    tools.command(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
    )
    tools.evaluate("""(() => {
      window.workspaceEdits = [];
      const originalFetch = window.fetch.bind(window);
      window.fetch = (input, options) => {
        if (options?.method === 'PUT' && options.body instanceof FormData) {
          const metadata = JSON.parse(options.body.get('metadata'));
          window.workspaceEdits.push({
            edits: metadata.edits,
            files: options.body.getAll('files').map(file => file.name)
          });
        }
        const response = originalFetch(input, options);
        if (window.workspaceHoldEntry && String(input).includes('/texts/entry.md?')) {
          window.workspaceHoldEntry = false;
          return response.then(async value => {
            const bytes = await value.arrayBuffer();
            return new Promise(resolve => {
              window.workspaceReleaseEntry = () => resolve(new Response(bytes, {
                status: value.status, headers: value.headers
              }));
            });
          });
        }
        return response;
      };
      return true;
    })()""")
    click(tools, button("Edit"))
    wait(
        tools,
        "document.querySelector('.source-pane .cm-content') !== null && "
        "document.querySelector('.preview-pane .prose') !== null",
    )
    click(
        tools,
        "Array.from(document.querySelectorAll('.directory button'))"
        ".find(b=>b.querySelector('span')?.textContent==='note.txt')",
    )
    wait(
        tools,
        "document.querySelector('.source-pane .cm-content')"
        "?.textContent.includes('Exact attachment bytes.')",
    )
    tools.evaluate("window.workspaceHoldEntry=true; window.workspaceReleaseEntry=null; true")
    click(
        tools,
        "Array.from(document.querySelectorAll('.directory button'))"
        ".find(b=>b.querySelector('span')?.textContent==='entry.md')",
    )
    wait(
        tools,
        "document.querySelector('.source-pane .cm-content')"
        "?.textContent.includes('Original text.')",
    )
    revised = SOURCE.replace("Workspace title", "Updated workspace").replace(
        "Original text.", "Edited in the browser."
    )
    edit_source(tools, revised)
    wait(
        tools,
        "document.querySelector('.preview-pane .prose')"
        "?.textContent.includes('Edited in the browser.')",
    )
    wait(tools, "typeof window.workspaceReleaseEntry === 'function'")
    after_delayed_read = tools.command(
        "Runtime.evaluate",
        {
            "expression": """(async () => {
              window.workspaceReleaseEntry();
              await new Promise(requestAnimationFrame);
              await new Promise(requestAnimationFrame);
              return document.querySelector('.preview-pane .prose').textContent;
            })()""",
            "awaitPromise": True,
            "returnByValue": True,
        },
    )
    assert "Edited in the browser." in after_delayed_read["result"]["value"]
    screenshot(tools, tmp_path / "desktop-editor.png")
    click(
        tools,
        "Array.from(document.querySelectorAll('[role=tab]')).find(b=>b.textContent.trim()==='Files')",
    )
    wait(tools, "document.querySelector('.file-row') !== null")
    tools.evaluate("window.prompt=()=> 'renamed-note.txt'; true")
    click(
        tools,
        "Array.from(Array.from(document.querySelectorAll('.file-row'))"
        ".find(r=>r.querySelector('strong')?.textContent==='note.txt')"
        "?.querySelectorAll('button')||[]).find(b=>b.textContent.trim()==='Rename')",
    )
    wait(
        tools,
        "document.querySelector('[role=dialog]')"
        "?.textContent.includes('Update attachment references')",
    )
    assert tools.evaluate("document.querySelectorAll('.reference-row').length") == 1
    screenshot(tools, tmp_path / "desktop-reference-review.png")
    click(tools, button("Apply to editor"))
    wait(tools, "document.querySelector('[role=dialog]') === null")
    click(
        tools,
        "Array.from(document.querySelectorAll('[role=tab]'))"
        ".find(b=>b.textContent.trim()==='Content')",
    )
    wait(tools, "document.querySelector('.preview-pane .prose a') !== null")
    assert (
        tools.evaluate("document.querySelector('.preview-pane .prose a').getAttribute('href')")
        == original_attachment_href
    )
    assert (
        tools.evaluate("document.querySelector('.source-pane .cm-content').textContent").find(
            "renamed-note.txt"
        )
        >= 0
    )
    click(tools, button("Save"))
    wait(tools, "document.querySelector('[role=status]')?.textContent.includes('Saved')")
    wait(
        tools,
        "document.querySelector('.reading-pane .prose h1')?.textContent === 'Updated workspace'",
    )
    with browser.engine.connect() as connection:
        page = ContentRepository(connection).get_page(browser.library, browser.page)
        assert page is not None and page.current_revision_number == 2
        manifest = ContentRepository(connection).get_current_file_manifest(page)
        files = {entry.name: entry.content for entry in manifest.files}
    assert files["renamed-note.txt"] == ATTACHMENT
    assert b"[Attachment](renamed-note.txt)" in files["entry.md"]
    assert b"Edited in the browser." in files["entry.md"]
    assert "note.txt" not in files
    assert tools.evaluate("window.workspaceEdits") == [{"edits": [], "files": ["entry.md"]}]
    tools.command("Page.navigate", {"url": browser.origin + page_path})
    wait(
        tools,
        "document.querySelector('.reading-pane .prose h1')?.textContent === 'Updated workspace'",
    )
    tools.command(
        "Emulation.setDeviceMetricsOverride",
        {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": False},
    )
    wait(tools, "innerWidth <= 390")
    tools.evaluate("""(() => {
      window.workspaceStyleViolations = 0;
      window.addEventListener('securitypolicyviolation', event => {
        if (event.effectiveDirective.startsWith('style-src')) {
          window.workspaceStyleViolations += 1;
        }
      });
      return true;
    })()""")
    click(tools, "document.querySelector('.page-heading .mobile-menu')")
    wait(
        tools, "document.querySelector('[role=dialog]')?.textContent.includes('Contents and files')"
    )
    assert tools.evaluate(
        "document.querySelector('[role=dialog]').textContent.includes('renamed-note.txt')"
    )
    screenshot(tools, tmp_path / "narrow-directory.png")
    dimensions = tools.evaluate("""(() => {
      const root = document.documentElement;
      const dialog = document.querySelector('[role=dialog]');
      const rect = dialog.getBoundingClientRect();
      const nonce = document.querySelector('meta[name="csp-nonce"]')?.content;
      const lockStyle = Array.from(document.querySelectorAll('style'))
        .find(style => style.textContent.includes('body[data-scroll-locked]'));
      let lockStylesheetActive = false;
      try { lockStylesheetActive = !!lockStyle?.sheet?.cssRules.length; } catch {}
      return {
        width: innerWidth, clientWidth: root.clientWidth, scrollWidth: root.scrollWidth,
        clientHeight: root.clientHeight, scrollHeight: root.scrollHeight,
        bodyLocked: document.body.hasAttribute('data-scroll-locked'),
        bodyOverflowY: getComputedStyle(document.body).overflowY,
        dialog: {
          left: rect.left, right: rect.right,
          clientWidth: dialog.clientWidth, scrollWidth: dialog.scrollWidth,
          clientHeight: dialog.clientHeight, scrollHeight: dialog.scrollHeight
        },
        noncePresent: !!nonce,
        lockStylePresent: !!lockStyle,
        lockStyleNonceMatches: !!nonce && lockStyle?.nonce === nonce,
        lockStylesheetActive,
        styleViolations: window.workspaceStyleViolations,
        overflow: Array.from(document.querySelectorAll('body *')).filter(element => {
          const bounds = element.getBoundingClientRect();
          return bounds.width > 0 && bounds.right > root.clientWidth + 1;
        }).slice(0,12).map(element => ({
          tag: element.tagName, classes: element.className,
          right: element.getBoundingClientRect().right
        }))
      };
    })()""")
    print("Narrow drawer diagnostics:", dimensions)
    assert dimensions["scrollWidth"] <= dimensions["clientWidth"] + 1, dimensions
    assert dimensions["dialog"]["scrollWidth"] <= dimensions["dialog"]["clientWidth"] + 1
    assert dimensions["dialog"]["right"] <= dimensions["clientWidth"] + 1, dimensions
    assert dimensions["bodyLocked"] and dimensions["bodyOverflowY"] == "hidden", dimensions
    assert dimensions["noncePresent"] and dimensions["lockStylePresent"], dimensions
    assert dimensions["lockStyleNonceMatches"] and dimensions["lockStylesheetActive"], dimensions
    assert dimensions["styleViolations"] == 0, dimensions

    # A valid large source remains editable; quotes/backslashes must not inflate
    # JSON metadata beyond its separate limit. Its BOM survives the file upload.
    tools.command("Page.navigate", {"url": browser.origin + page_path})
    wait(
        tools,
        "document.querySelector('.reading-pane .prose h1')?.textContent === 'Updated workspace'",
    )
    tools.command(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
    )
    click(tools, button("Edit"))
    wait(tools, "document.querySelector('.source-pane .cm-content') !== null")
    large_source = "\ufeff# Large browser title\n\n" + '"\\' * 550_000 + "\n"
    assert len(json.dumps({"text": large_source}).encode()) > 2 * 1024 * 1024
    edit_source(tools, large_source)
    wait(tools, "document.querySelector('.preview-pane .prose pre') !== null")
    click(tools, button("Save"))
    wait(tools, "document.querySelector('[role=status]')?.textContent.includes('Saved')")
    with browser.engine.connect() as connection:
        repository = ContentRepository(connection)
        large_page = repository.get_page(browser.library, browser.page)
        assert large_page is not None and large_page.current_revision_number == 3
        large_files = {
            entry.name: entry.content
            for entry in repository.get_current_file_manifest(large_page).files
        }
        assert large_files["entry.md"] == large_source.encode()
        assert large_files["renamed-note.txt"] == ATTACHMENT
    tools.command("Page.navigate", {"url": browser.origin + page_path})
    wait(
        tools,
        "document.querySelector('.reading-pane .prose pre')"
        "?.textContent.includes('Large browser title')",
    )
    click(tools, button("Edit"))
    wait(
        tools,
        "document.querySelector('.source-pane .cm-content')"
        "?.textContent.includes('Large browser title')",
    )
    click(tools, button("Save"))
    wait(tools, "document.querySelector('[role=status]')?.textContent.includes('no new revision')")
    with browser.engine.connect() as connection:
        unchanged_page = ContentRepository(connection).get_page(browser.library, browser.page)
        assert unchanged_page is not None and unchanged_page.current_revision_number == 3

    click(tools, button("Edit"))
    wait(tools, "document.querySelector('.source-pane .cm-content') !== null")
    click(
        tools,
        "Array.from(document.querySelectorAll('[role=tab]'))"
        ".find(b=>b.textContent.trim()==='Files')",
    )
    tools.command("Page.setInterceptFileChooserDialog", {"enabled": True})
    replaced_entry_href = tools.evaluate(
        "Array.from(document.querySelectorAll('.file-row'))"
        ".find(r=>r.querySelector('strong')?.textContent==='entry.md')"
        ".querySelector('a[download]').getAttribute('href')"
    )
    click(
        tools,
        "Array.from(Array.from(document.querySelectorAll('.file-row'))"
        ".find(r=>r.querySelector('strong')?.textContent==='entry.md')"
        "?.querySelectorAll('button')||[]).find(b=>b.textContent.trim()==='Replace')",
    )
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"\xff\x00\x01")
    document = tools.command("DOM.getDocument")
    node = tools.command(
        "DOM.querySelector",
        {
            "nodeId": document["root"]["nodeId"],
            "selector": "input[type=file]:not([multiple])",
        },
    )
    tools.command("DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": [str(replacement)]})
    click(
        tools,
        "Array.from(document.querySelectorAll('[role=tab]'))"
        ".find(b=>b.textContent.trim()==='Content')",
    )
    wait(
        tools,
        "document.querySelector('.reading-pane')?.textContent.includes('cannot be previewed')",
    )
    assert not tools.evaluate(
        "Array.from(document.querySelectorAll('.reading-pane [href]'))"
        f".some(element=>element.getAttribute('href')==={json.dumps(replaced_entry_href)})"
    )
    assert tools.evaluate("document.querySelector('.source-pane') === null")
    assert tools.evaluate(f"({button('Set as entry')}) === undefined")
    click(tools, button("Save"))
    wait(tools, "document.querySelector('[role=status]')?.textContent.includes('Saved')")
    with browser.engine.connect() as connection:
        repository = ContentRepository(connection)
        binary_page = repository.get_page(browser.library, browser.page)
        assert binary_page is not None and binary_page.current_revision_number == 4
        replaced_files = repository.get_current_file_manifest(binary_page)
        assert {entry.name: entry.content for entry in replaced_files.files} == {
            "entry.md": replacement.read_bytes(),
            "renamed-note.txt": ATTACHMENT,
        }
    tools.command("Network.clearBrowserCookies")
    click(tools, button("Manage"))
    wait(tools, "document.querySelector('[role=menuitem]') !== null")
    click(
        tools,
        "Array.from(document.querySelectorAll('[role=menuitem]'))"
        ".find(item=>item.textContent.trim()==='Requests')",
    )
    wait(tools, "document.querySelector('.auth-card input[name=token]') !== null")
    assert tools.evaluate("document.querySelector('.auth-card input[name=token]').value") == ""
    assert tools.evaluate("document.querySelector('.auth-card [role=alert]').textContent") == (
        "Your session has ended. Unsaved input has been cleared. "
        "Sign in again and check any operation whose outcome was not confirmed."
    )
    assert tools.evaluate("document.querySelector('main') === null")


def test_browser_failure_diagnostics_redact_local_details(tmp_path: Path) -> None:
    stderr_path = tmp_path / "browser-stderr.log"
    stderr_path.write_text(
        "DevTools listening on ws://127.0.0.1:12345/devtools/browser/example\n"
        "Profile /tmp/synthetic private/profile Authorization: Bearer synthetic-secret\n",
        encoding="utf-8",
    )

    diagnostic = _browser_stderr_signals(stderr_path)
    assert diagnostic == "devtools-listening"
    assert "synthetic-private" not in diagnostic
    assert "synthetic private" not in diagnostic
    assert "synthetic-secret" not in diagnostic
    assert "Bearer" not in diagnostic
    assert _target_locations(
        [{"url": "http://127.0.0.1:8765/private/secret?token=synthetic-secret"}],
        "http://127.0.0.1:8765",
    ) == ["<other local path>"]
