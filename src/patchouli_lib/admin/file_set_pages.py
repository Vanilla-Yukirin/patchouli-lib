"""Standard browser forms for master-owned complete file-set writes."""

from __future__ import annotations

from html import escape

from patchouli_lib.admin.pages import AdminLocale, _browser_document
from patchouli_lib.admin.read_model import BookView, PageView


def file_set_upload_page(
    csrf_token: str,
    *,
    library_id: str,
    section_id: str,
    book_id: str,
    page_id: str | None,
    book: BookView | None = None,
    page: PageView | None = None,
    locale: AdminLocale = "en",
) -> str:
    def text(chinese: str, english: str) -> str:
        return escape(chinese if locale == "zh-CN" else english)

    base = f"/admin/libraries/{library_id}/sections/{section_id}/books/{book_id}"
    create = page_id is None
    target = base + "/pages" if create else f"{base}/pages/{page_id}/file-revisions"
    path = base + "/new-page" if create else f"{base}/pages/{page_id}/files/edit"
    available = book is not None if create else page is not None
    title = text("新建文档", "Create a document") if create else text("更新文件", "Update files")
    etag = "" if page is None else page.current_etag
    intro = text(
        "每份文档可含同层多个文件，不支持子文件夹。一次提交保存完整的一组文件。",
        "A document can contain several flat files, without subfolders. "
        "Each submission saves one complete file set.",
    )
    body = f'<section class="card"><p class="section-help">{intro}</p>'
    if not create:
        help_text = text(
            "更新会整组替换：本次未选入的文件不会出现在新版本中，"
            "旧版本仍完整保留。正文相同则不新增版本。",
            "Updates replace the whole set: omitted files leave the new version, "
            "but remain in history. Identical content does not create a revision.",
        )
        body += f'<p class="section-help">{help_text}</p>'
    if not available:
        unavailable = text(
            "目标目前不可新写。若上次提交的结果未知，可重新选择原文件，"
            "重试本标签页保存的原操作；不会恢复已删除文档。",
            "The target is unavailable for new writes. If a previous result is unknown, "
            "reselect the original files and retry this tab's saved operation. "
            "This does not restore a deleted document.",
        )
        body += f'<p role="alert">{unavailable}</p>'
    if page is not None:
        body += (
            f"<h2>{escape(page.page.title)}</h2>"
            f'<p class="meta">{text("当前版本", "Current revision")}: '
            f"{page.page.revision_number}</p>"
            '<ul class="item-list">'
            + "".join(
                f"<li><code>{escape(item.name)}</code> · {item.size_bytes} B</li>"
                for item in page.files
            )
            + "</ul>"
        )
    body += (
        f'<form id="file-set-upload" method="post" enctype="multipart/form-data" '
        f'action="{escape(target, quote=True)}" '
        f'data-operation="{"create" if create else "revise"}" '
        f'data-available="{"yes" if available else "no"}" '
        f'data-lang="{locale}" autocomplete="off">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}">'
        f'<input type="hidden" name="expected_etag" value="{escape(etag, quote=True)}">'
    )
    if create:
        time_label = text("文档发生时间（UTC，可不填）", "Declared time (UTC, optional)")
        time_help = text(
            "填写带 Z 的 UTC 时间；未填会使用服务器接收时间并提示。",
            "Use a UTC timestamp ending in Z; "
            "omitted time uses server receipt time with a warning.",
        )
        body += (
            f'<label for="upload-title">{text("文档标题", "Document title")}</label>'
            '<input id="upload-title" name="title" type="text" required>'
            f'<label for="upload-time">{time_label}</label>'
            '<input id="upload-time" name="occurred_at" type="text" '
            'placeholder="2026-01-01T12:00:00Z">'
            f'<p class="meta">{time_help}</p>'
        )
    size_help = text(
        "最多 64 个文件，单文件 16 MiB，整组 64 MiB。文件不会发送给第三方。",
        "Up to 64 files, 16 MiB per file, 64 MiB per set. Files are not sent to third parties.",
    )
    no_script = text(
        "上传需要启用本网站的脚本；未启用时不能提交。",
        "Uploads need this site's script; submission is disabled without it.",
    )
    retry_help = text(
        "提交结果未知时保留原操作键。刷新后需重新选择原文件；改标题、时间或文件请明确开始新操作。",
        "An unknown result keeps the original operation key. After refresh, "
        "reselect the original files; explicitly start a new operation "
        "before changing metadata or files.",
    )
    body += (
        f'<label for="upload-files">{text("本版本的全部文件", "All files for this revision")}'
        "</label>"
        '<input id="upload-files" name="files" type="file" multiple required>'
        f'<p class="meta">{size_help}</p>'
        '<button id="upload-submit" type="submit" disabled>'
        f"{text('保存文件', 'Save files')}</button> "
        '<button id="upload-new-operation" type="button" disabled>'
        f"{text('放弃原重试，开始新操作', 'Discard retry and start a new operation')}</button>"
        '</form><p id="upload-status" role="status" aria-live="polite"></p>'
        '<div id="upload-result"></div>'
        f'<noscript><p role="alert">{no_script}</p></noscript>'
        f'<p class="meta">{retry_help}</p>'
        f'<p><a href="{escape(base, quote=True)}">{text("返回书籍", "Back to Book")}</a></p>'
        "</section>"
    )
    return _browser_document(
        csrf_token,
        locale,
        title,
        path,
        body,
        script_src="/admin/file-set-upload.js",
    )


FILE_SET_UPLOAD_SCRIPT = r"""
"use strict";
(() => {
  const form = document.getElementById("file-set-upload");
  if (!form) return;
  const chinese = form.dataset.lang === "zh-CN";
  const t = (zh, en) => chinese ? zh : en;
  const status = document.getElementById("upload-status");
  const result = document.getElementById("upload-result");
  const submit = document.getElementById("upload-submit");
  const reset = document.getElementById("upload-new-operation");
  const files = form.elements.namedItem("files");
  const title = form.elements.namedItem("title");
  const time = form.elements.namedItem("occurred_at");
  const csrf = form.elements.namedItem("csrf_token").value;
  const currentEtag = form.elements.namedItem("expected_etag").value;
  const target = new URL(form.getAttribute("action"), location.origin);
  const storageKey = "patchouli-file-set-v1:" + target.pathname;
  let pending = null;
  let sending = false;
  let storageWorks = true;
  let completed = false;
  const message = text => { status.textContent = text; };
  const validPending = value => value && value.version === 1 &&
    value.target === target.pathname && value.operation === form.dataset.operation &&
    typeof value.key === "string" && /^[0-9a-f]{32}$/.test(value.key) &&
    typeof value.metadata === "string" && value.metadata.length <= 65536 &&
    typeof value.etag === "string" && value.etag.length <= 100 &&
    Array.isArray(value.files) && value.files.length >= 1 && value.files.length <= 64 &&
    value.files.every(item => item && typeof item.name === "string" &&
      typeof item.size === "number" && Number.isSafeInteger(item.size) && item.size >= 0);
  const refreshControls = () => {
    submit.disabled = sending || !storageWorks || completed ||
      (!pending && form.dataset.available !== "yes");
    reset.disabled = sending || !storageWorks ||
      (completed && form.dataset.operation === "revise");
    if (title) title.readOnly = Boolean(pending);
    if (time) time.readOnly = Boolean(pending);
  };
  try {
    const saved = sessionStorage.getItem(storageKey);
    if (saved) {
      const value = JSON.parse(saved);
      if (!validPending(value)) throw new Error("invalid retry");
      const metadata = JSON.parse(value.metadata);
      if (value.operation === "create") {
        if (!metadata || typeof metadata.title !== "string" ||
            !(metadata.occurred_at === null || typeof metadata.occurred_at === "string")) {
          throw new Error("invalid retry metadata");
        }
        title.value = metadata.title;
        time.value = metadata.occurred_at || "";
      }
      pending = value;
      message(t("已保留原操作，请重新选择原文件后重试。",
        "Original operation retained. Reselect the original files to retry."));
    }
    // Fail closed if the browser cannot durably keep an unknown-result key.
    const probe = storageKey + ":probe";
    sessionStorage.setItem(probe, "1");
    sessionStorage.removeItem(probe);
  } catch (_) {
    storageWorks = false;
    message(t("无法读取或保存重试记录。请允许网站会话存储后重试。",
      "Cannot read or store the retry record. Allow site session storage before uploading."));
  }
  refreshControls();
  reset.addEventListener("click", () => {
    if (pending && !window.confirm(t(
      "上次操作可能已经成功。请先检查文档列表。确定放弃重试并开始新操作？",
      "The previous operation may have succeeded. Check the document list. Discard retry?"
    ))) return;
    try { sessionStorage.removeItem(storageKey); }
    catch (_) { storageWorks = false; refreshControls(); return; }
    pending = null;
    completed = false;
    result.replaceChildren();
    files.value = "";
    message(t("已开始新操作；请确认标题、时间和文件。",
      "New operation started; check metadata and files."));
    refreshControls();
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (sending || !storageWorks || submit.disabled || !form.reportValidity()) return;
    const selected = Array.from(files.files);
    if (!selected.length || selected.length > 64 || selected.some(file => file.size > 16777216) ||
        selected.reduce((sum, file) => sum + file.size, 0) > 67108864) {
      message(t("请检查文件数量和大小。", "Check file count and sizes.")); return;
    }
    const descriptors = selected.map(file => ({name: file.name, size: file.size}));
    descriptors.sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : a.size - b.size);
    if (pending && JSON.stringify(pending.files) !== JSON.stringify(descriptors)) {
      message(t("重选同名、同大小的原文件；服务器还会校验全部字节。更改请开始新操作。",
        "Reselect original names and sizes; the server checks bytes. " +
        "New edits need a new operation."));
      return;
    }
    if (!pending) {
      const bytes = new Uint8Array(16);
      crypto.getRandomValues(bytes);
      pending = {
        version: 1, target: target.pathname, operation: form.dataset.operation,
        key: Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join(""),
        metadata: JSON.stringify(form.dataset.operation === "create" ?
          {title: title.value, occurred_at: time.value.trim() || null} : {}),
        etag: currentEtag, files: descriptors
      };
      try { sessionStorage.setItem(storageKey, JSON.stringify(pending)); }
      catch (_) {
        pending = null; storageWorks = false; refreshControls();
        message(t("未能保存重试记录，未发出上传请求。",
          "Retry record could not be saved; no upload was sent.")); return;
      }
    }
    sending = true; refreshControls();
    result.replaceChildren();
    message(t("正在保存，请稍候……", "Saving, please wait…"));
    const body = new FormData();
    body.append("metadata", pending.metadata);
    selected.forEach(file => body.append("file", file, file.name));
    const headers = {"X-CSRF-Token": csrf, "Idempotency-Key": pending.key};
    if (pending.operation === "revise") headers["If-Match"] = pending.etag;
    try {
      const response = await fetch(target.pathname, {
        method: "POST", headers, body, credentials: "same-origin", redirect: "error"
      });
      const data = await response.json();
      if (!response.ok) {
        message(typeof data.message === "string" ? data.message :
          t("提交失败。请检查状态后重试原操作。",
            "Submission failed. Check state, then retry the original operation."));
        return;
      }
      if (typeof data.revision_url !== "string" || !Number.isSafeInteger(data.revision_number) ||
          typeof data.changed !== "boolean" || typeof data.replayed !== "boolean") {
        throw new Error("invalid result");
      }
      const link = new URL(data.revision_url, location.origin);
      if (link.origin !== location.origin || !link.pathname.startsWith("/admin/libraries/")) {
        throw new Error("invalid link");
      }
      // Do not erase the key before a validated, exact-version success was received.
      sessionStorage.removeItem(storageKey);
      pending = null;
      const anchor = document.createElement("a");
      anchor.href = link.pathname;
      anchor.textContent = t("查看本次准确版本", "View this exact revision") +
        " " + data.revision_number;
      result.append(anchor);
      message(data.replayed ? t("已返回原提交结果（可能不是当前版本）。",
        "Original success returned; it may not be the current revision.") :
        data.changed ? t("文件已保存。", "Files saved.") :
          t("文件完全相同，未新增版本。", "Identical files; no new revision."));
      if (Array.isArray(data.warnings) && data.warnings.length) {
        const warning = document.createElement("p");
        warning.textContent = t("未声明时间，本次使用服务器接收时间。",
          "No declared time; server receipt time was used.");
        result.append(warning);
      }
      // Explicitly reset creation, or reload a revision form's now-stale ETag.
      completed = true;
      if (form.dataset.operation === "revise") {
        const reload = document.createElement("a");
        reload.id = "upload-reload";
        reload.href = location.pathname;
        reload.textContent = t("重新读取当前版本，继续更新",
          "Reload current revision to update again");
        result.append(document.createElement("br"), reload);
      }
    } catch (_) {
      message(t("尚未确认结果。保留原文件并重试；刷新后原操作键仍保留在本标签页。",
        "Unconfirmed result. Keep files and retry; this tab retains the key after refresh."));
    } finally { sending = false; refreshControls(); }
  });
})();
""".strip()
