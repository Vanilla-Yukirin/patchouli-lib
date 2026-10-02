"""Native movement controls and same-tab recovery of the original operation."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from patchouli_lib.admin.pages import AdminLocale, _browser_document
from patchouli_lib.admin.read_model import PageView


@dataclass(frozen=True, slots=True)
class MoveDestination:
    section_id: str
    section_name: str
    book_id: str
    book_name: str


def page_move_page(
    csrf_token: str,
    *,
    library_id: str,
    section_id: str,
    book_id: str,
    page_id: str,
    view: PageView | None,
    destinations: tuple[MoveDestination, ...],
    locale: AdminLocale,
) -> str:
    def text(chinese: str, english: str) -> str:
        return escape(chinese if locale == "zh-CN" else english)

    base = f"/admin/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages/{page_id}"
    path = base + "/move"
    current_url = f"/admin/libraries/{library_id}/pages/{page_id}"
    heading = text("移动文档", "Move Page")
    body = (
        '<section class="card"><p class="section-help">'
        + text(
            "只调整同一知识库内的分区与书籍归属，不复制文件，不新增内容版本，"
            "标题、Tag、声明时间和全部历史保持不变。目标与当前归属一致时不会移动。",
            "Change Section and Book within this Library without copying files or creating "
            "a revision. Title, Tags, declared time and history stay intact. "
            "The current destination is a no-op.",
        )
        + "</p>"
    )
    if view is None:
        body += (
            '<p role="alert">'
            + text(
                "原路径目前不可新写。仅可重试本标签页保存的原操作，不会恢复已删除文档。",
                "This path is unavailable for new writes. Only this tab's saved operation can be "
                "retried; a deleted Page will not leave Trash.",
            )
            + "</p>"
        )
        destinations = ()
    else:
        body += (
            f"<h2>{escape(view.page.title)}</h2><p>"
            f"{text('当前归属', 'Current location')}: {escape(view.section.name)} / "
            f"{escape(view.book.name)}</p>"
        )
    sections = dict.fromkeys((item.section_id, item.section_name) for item in destinations)
    section_options = "".join(
        f'<option value="{escape(identity, quote=True)}"'
        + (" selected" if identity == section_id else "")
        + f">{escape(name)}</option>"
        for identity, name in sections
    )
    book_options = "".join(
        f'<option value="{escape(item.book_id, quote=True)}" '
        f'data-section="{escape(item.section_id, quote=True)}"'
        + (" selected" if item.book_id == book_id else "")
        + f">{escape(item.section_name)} / {escape(item.book_name)}</option>"
        for item in destinations
    )
    etag = "" if view is None else view.current_etag
    body += (
        f'<form id="page-move" method="post" action="{escape(path, quote=True)}" '
        f'data-current-url="{escape(current_url, quote=True)}" '
        f'data-available="{"yes" if view is not None else "no"}" data-lang="{locale}" '
        'autocomplete="off">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}">'
        f'<input type="hidden" name="expected_etag" value="{escape(etag, quote=True)}">'
        f'<label for="target_section_id">{text("目标分区", "Target Section")}</label>'
        '<select id="target_section_id" name="target_section_id" required>'
        f'<option value="">{text("选择分区", "Choose Section")}</option>{section_options}</select>'
        f'<label for="target_book_id">{text("目标书籍", "Target Book")}</label>'
        '<select id="target_book_id" name="target_book_id" required>'
        f'<option value="">{text("选择书籍", "Choose Book")}</option>{book_options}</select>'
        '<label><input type="checkbox" name="confirm_move" value="yes" required> '
        f"{text('确认移到所选分区和书籍。', 'Confirm moving to the selected Section and Book.')}"
        '</label><button id="move-submit" type="submit" disabled>'
        f"{text('确认移动', 'Confirm move')}</button> "
        '<button id="move-discard" type="button" disabled>'
        f"{text('放弃原重试，返回当前文档', 'Discard retry and view current Page')}</button></form>"
        '<p id="move-status" role="status" aria-live="polite"></p><div id="move-result"></div>'
        "<noscript><p>"
        f"{text('移动需要启用本网站脚本。', "Movement requires this site's script.")}"
        '</p></noscript><p class="meta">'
        + text(
            "结果未知时保留原源路径、目标、版本条件与操作键；刷新后仍可原样重试。"
            "原成功可能已不是当前位置，开始其他操作前请先检查结果。",
            "An unknown result retains the original source path, destination, ETag and key. "
            "Refresh retries the same request. An original success may no longer be the current "
            "location; check its result before starting another operation.",
        )
        + f'</p><p><a href="{escape(current_url, quote=True)}">'
        f"{text('查看当前文档', 'View current Page')}</a></p></section>"
    )
    return _browser_document(
        csrf_token, locale, heading, path, body, script_src="/admin/page-move.js"
    )


PAGE_MOVE_SCRIPT = r"""
"use strict";
(() => {
  const form = document.getElementById("page-move");
  if (!form) return;
  const t = (zh, en) => form.dataset.lang === "zh-CN" ? zh : en;
  const section = form.elements.namedItem("target_section_id");
  const book = form.elements.namedItem("target_book_id");
  const confirm = form.elements.namedItem("confirm_move");
  const submit = document.getElementById("move-submit");
  const discard = document.getElementById("move-discard");
  const status = document.getElementById("move-status");
  const result = document.getElementById("move-result");
  const target = new URL(form.getAttribute("action"), location.origin);
  const current = new URL(form.dataset.currentUrl, location.origin);
  const storageKey = "patchouli-page-move-v1:" + target.pathname;
  const csrf = form.elements.namedItem("csrf_token").value;
  const etag = form.elements.namedItem("expected_etag").value;
  let pending = null, sending = false, completed = false, storageWorks = true;
  const message = value => { status.textContent = value; };
  const filterBooks = () => {
    for (const option of book.options) {
      option.hidden = !!option.value && option.dataset.section !== section.value;
      option.disabled = option.hidden;
    }
    if (book.selectedOptions[0]?.disabled) book.value = "";
  };
  const controls = () => {
    const unavailable = !pending && form.dataset.available !== "yes";
    submit.disabled = sending || completed || !storageWorks || unavailable;
    discard.disabled = sending || completed || !storageWorks || !pending;
    section.disabled = book.disabled = sending || completed || !!pending || unavailable;
    confirm.disabled = sending || completed || !storageWorks || unavailable;
  };
  try {
    const saved = sessionStorage.getItem(storageKey);
    if (saved) {
      const value = JSON.parse(saved);
      if (!value || value.version !== 1 || value.target !== target.pathname ||
          typeof value.key !== "string" || !/^[0-9a-f]{32}$/.test(value.key) ||
          typeof value.etag !== "string" || !/^"page-v[12]-[0-9a-f]{64}"$/.test(value.etag) ||
          typeof value.section !== "string" || !/^[0-9a-f]{32}$/.test(value.section) ||
          typeof value.book !== "string" || !/^[0-9a-f]{32}$/.test(value.book)) {
        throw new Error("invalid retry");
      }
      pending = value;
      if (!Array.from(section.options).some(option => option.value === value.section))
        section.add(new Option(value.section, value.section));
      if (!Array.from(book.options).some(option => option.value === value.book)) {
        const option = new Option(value.book, value.book);
        option.dataset.section = value.section;
        book.add(option);
      }
      section.value = value.section;
      book.value = value.book;
      message(t("已保留原移动操作，请确认后原样重试。",
        "Original movement retained. Confirm to retry the same request."));
    }
    const probe = storageKey + ":probe";
    sessionStorage.setItem(probe, "1"); sessionStorage.removeItem(probe);
  } catch (_) {
    storageWorks = false;
    message(t("无法保存重试记录，请允许网站会话存储。",
      "Allow site session storage to keep retries."));
  }
  filterBooks(); controls();
  section.addEventListener("change", filterBooks);
  discard.addEventListener("click", () => {
    if (!window.confirm(t("上次可能已成功。检查结果后，确定放弃原重试？",
      "The previous movement may have succeeded. Check its result. Discard retry?"))) return;
    try { sessionStorage.removeItem(storageKey); location.href = current.pathname; }
    catch (_) { storageWorks = false; controls(); }
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (sending || submit.disabled || !form.reportValidity()) return;
    if (!pending) {
      const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
      pending = {version: 1, target: target.pathname, etag, section: section.value,
        book: book.value,
        key: Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("")};
      try { sessionStorage.setItem(storageKey, JSON.stringify(pending)); }
      catch (_) {
        pending = null; storageWorks = false; controls();
        message(t("未能保存重试记录，未发出请求。", "Retry not saved; no request was sent."));
        return;
      }
    }
    sending = true; controls(); result.replaceChildren();
    message(t("正在移动……", "Moving…"));
    try {
      const response = await fetch(target.pathname, {
        method: "POST", credentials: "same-origin", redirect: "error",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf,
          "Idempotency-Key": pending.key, "If-Match": pending.etag},
        body: JSON.stringify({target_section_id: pending.section, target_book_id: pending.book,
          confirm_move: "yes"})
      });
      const data = await response.json();
      if (!response.ok) {
        message(typeof data.message === "string" ? data.message :
          t("移动失败，请检查状态后重试。", "Movement failed. Check state before retrying."));
        return;
      }
      const expected = current.pathname + "/revisions/" + data.revision_number;
      if (data.page_url !== current.pathname || data.revision_url !== expected ||
          !Number.isSafeInteger(data.revision_number) || data.revision_number < 1 ||
          typeof data.changed !== "boolean" || typeof data.replayed !== "boolean")
        throw new Error("invalid result");
      sessionStorage.removeItem(storageKey); pending = null; completed = true;
      const pageLink = document.createElement("a");
      pageLink.href = current.pathname;
      pageLink.textContent = t("查看当前文档", "View current Page");
      const revisionLink = document.createElement("a");
      revisionLink.href = expected;
      revisionLink.textContent = t("查看本次准确版本", "View this exact revision");
      result.append(pageLink, document.createElement("br"), revisionLink);
      message(data.replayed ? t("已返回原移动结果（可能不是当前位置），未再次移动。",
        "Original movement returned; it may not be the current location. No move was repeated.") :
        data.changed ? t("已移动文档，内容版本保持不变。",
          "Page moved; its revision is unchanged.") :
        t("目标与当前归属一致，未移动文档。",
          "The destination is unchanged; no movement occurred."));
    } catch (_) {
      message(t("尚未确认结果。原操作已保留，可刷新后重试。",
        "Unconfirmed result. The original movement is retained; refresh to retry."));
    } finally { sending = false; controls(); }
  });
})();
""".strip()
