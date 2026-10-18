"""Explicit browser confirmation and durable retries for historical file-group restore."""

from __future__ import annotations

from html import escape

from patchouli_lib.admin.pages import AdminLocale, _browser_document
from patchouli_lib.admin.read_model import PageView


def revision_restore_page(
    csrf_token: str,
    *,
    library_id: str,
    section_id: str,
    book_id: str,
    page_id: str,
    revision_number: int,
    view: PageView | None,
    locale: AdminLocale,
) -> str:
    def text(chinese: str, english: str) -> str:
        return escape(chinese if locale == "zh-CN" else english)

    base = f"/admin/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages/{page_id}"
    path = f"{base}/revisions/{revision_number}/restore"
    heading = text("恢复历史版本", "Restore a historical revision")
    help_text = text(
        "将所选版本的全部文件恢复为当前内容，旧版本仍完整保留。内容相同则不新增版本。"
        "不会更改文档标题、Tag、声明时间或归属，也不是数据库恢复。",
        "Restore the selected revision's complete file set as current content. "
        "History is preserved; identical content creates no revision. "
        "This does not change title, Tags, declared time or location, or restore a database.",
    )
    body = f'<section class="card"><p class="section-help">{help_text}</p>'
    body += f"<p>{text('所选版本', 'Selected revision')}: {revision_number}</p>"
    if view is None:
        body += (
            '<p role="alert">'
            + text(
                "目标目前不可新写。仅可重试本标签页保存的原操作，不会恢复已删除文档。",
                "The target is unavailable for new writes. Only this tab's saved operation "
                "can be retried; a deleted document will not leave Trash.",
            )
            + "</p>"
        )
    else:
        body += (
            f"<h2>{escape(view.page.title)}</h2>"
            f'<p class="meta">{text("当前版本", "Current revision")}: '
            f"{view.page.revision_number}</p>"
            '<ul class="item-list">'
            + "".join(
                f"<li><code>{escape(item.name)}</code> · {item.size_bytes} B</li>"
                for item in view.files
            )
            + "</ul>"
        )
    etag = "" if view is None else view.current_etag
    confirm = text(
        "确认将此版本的整组文件恢复为当前内容。", "Confirm restoring this complete file set."
    )
    body += (
        f'<form id="revision-restore" method="post" action="{escape(path, quote=True)}" '
        f'data-available="{"yes" if view is not None else "no"}" '
        f'data-lang="{locale}" autocomplete="off">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}">'
        f'<input type="hidden" name="expected_etag" value="{escape(etag, quote=True)}">'
        '<label><input type="checkbox" name="confirm_restore" value="yes" required> '
        f"{confirm}</label>"
        '<button id="restore-submit" type="submit" disabled>'
        f"{text('确认恢复', 'Confirm restore')}</button> "
        '<button id="restore-new-operation" type="button" disabled>'
        f"{text('放弃原重试，重新读取当前状态', 'Discard retry and reload current state')}</button>"
        '</form><p id="restore-status" role="status" aria-live="polite"></p>'
        '<div id="restore-result"></div><noscript><p>'
        f"{text('恢复需要启用本网站脚本。', "Restore requires this site's script.")}"
        '</p></noscript><p class="meta">'
        + text(
            "结果未知时保留原操作键、所选版本和原版本条件。刷新后仍可重试；"
            "改变操作前请先检查上次结果。提交时服务器才核验完整文件字节。",
            "An unknown result retains the key, selected revision and original ETag. "
            "Refresh can retry it; check the previous result before changing the operation. "
            "The server verifies complete file bytes on submission.",
        )
        + f'</p><p><a href="{escape(base, quote=True)}">'
        f"{text('返回当前文档', 'Back to current document')}</a></p></section>"
    )
    return _browser_document(
        csrf_token, locale, heading, path, body, script_src="/admin/revision-restore.js"
    )


REVISION_RESTORE_SCRIPT = r"""
"use strict";
(() => {
  const form = document.getElementById("revision-restore");
  if (!form) return;
  const t = (zh, en) => form.dataset.lang === "zh-CN" ? zh : en;
  const status = document.getElementById("restore-status");
  const result = document.getElementById("restore-result");
  const submit = document.getElementById("restore-submit");
  const reset = document.getElementById("restore-new-operation");
  const target = new URL(form.getAttribute("action"), location.origin);
  const csrf = form.elements.namedItem("csrf_token").value;
  const currentEtag = form.elements.namedItem("expected_etag").value;
  const storageKey = "patchouli-revision-restore-v1:" + target.pathname;
  const message = value => { status.textContent = value; };
  let pending = null, sending = false, completed = false, storageWorks = true;
  const controls = () => {
    submit.disabled = sending || completed || !storageWorks ||
      (!pending && form.dataset.available !== "yes");
    reset.disabled = sending || completed || !storageWorks;
  };
  try {
    const saved = sessionStorage.getItem(storageKey);
    if (saved) {
      const value = JSON.parse(saved);
      if (!value || value.version !== 1 || value.target !== target.pathname ||
          typeof value.key !== "string" || !/^[0-9a-f]{32}$/.test(value.key) ||
          typeof value.etag !== "string" || !/^"page-v[12]-[0-9a-f]{64}"$/.test(value.etag)) {
        throw new Error("invalid retry");
      }
      pending = value;
      message(t("已保留原恢复操作，请确认后重试。",
        "Original restore retained. Confirm to retry."));
    }
    const probe = storageKey + ":probe";
    sessionStorage.setItem(probe, "1");
    sessionStorage.removeItem(probe);
  } catch (_) {
    storageWorks = false;
    message(t("无法保存重试记录，请允许网站会话存储。",
      "Allow site session storage to keep retries."));
  }
  controls();
  reset.addEventListener("click", () => {
    if (pending && !window.confirm(t("上次可能已成功。检查结果后，确定放弃原重试？",
      "The previous restore may have succeeded. Check its result. Discard retry?"))) return;
    try { sessionStorage.removeItem(storageKey); location.reload(); }
    catch (_) { storageWorks = false; controls(); }
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (sending || submit.disabled || !form.reportValidity()) return;
    if (!pending) {
      const bytes = new Uint8Array(16);
      crypto.getRandomValues(bytes);
      pending = {version: 1, target: target.pathname, etag: currentEtag,
        key: Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("")};
      try { sessionStorage.setItem(storageKey, JSON.stringify(pending)); }
      catch (_) {
        pending = null; storageWorks = false; controls();
        message(t("未能保存重试记录，未发出请求。",
          "Retry not saved; no request was sent.")); return;
      }
    }
    sending = true; controls(); result.replaceChildren();
    message(t("正在恢复……", "Restoring…"));
    try {
      const response = await fetch(target.pathname, {
        method: "POST", credentials: "same-origin", redirect: "error",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf,
          "Idempotency-Key": pending.key, "If-Match": pending.etag},
        body: JSON.stringify({confirm_restore: "yes"})
      });
      const data = await response.json();
      if (!response.ok) {
        message(typeof data.message === "string" ? data.message :
          t("恢复失败，请检查状态后重试。",
            "Restore failed. Check state before retrying.")); return;
      }
      if (typeof data.revision_url !== "string" || !Number.isSafeInteger(data.revision_number) ||
          typeof data.changed !== "boolean" || typeof data.replayed !== "boolean") {
        throw new Error("invalid result");
      }
      const link = new URL(data.revision_url, location.origin);
      if (link.origin !== location.origin || !link.pathname.startsWith("/admin/libraries/")) {
        throw new Error("invalid link");
      }
      sessionStorage.removeItem(storageKey); pending = null; completed = true;
      const anchor = document.createElement("a");
      anchor.href = link.pathname;
      anchor.textContent = t("查看本次准确版本", "View this exact revision") +
        " " + data.revision_number;
      result.append(anchor);
      message(data.replayed ? t("已返回原恢复结果（可能不是当前版本）。",
        "Original restore returned; it may not be current.") : data.changed ?
        t("已恢复文件，并生成新版本。", "Files restored in a new revision.") :
        t("文件完全相同，未新增版本。", "Identical files; no new revision."));
      const reload = document.createElement("a");
      reload.href = location.pathname;
      reload.textContent = t("重新读取当前状态", "Reload current state");
      result.append(document.createElement("br"), reload);
    } catch (_) {
      message(t("尚未确认结果。原操作已保留，可刷新后重试。",
        "Unconfirmed result. The original restore is retained; refresh to retry."));
    } finally { sending = false; controls(); }
  });
})();
""".strip()
