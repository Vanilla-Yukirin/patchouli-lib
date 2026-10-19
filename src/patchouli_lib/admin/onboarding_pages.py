"""Master-only, short-lived onboarding ticket presentation."""

from datetime import UTC, datetime
from html import escape

from patchouli_lib.admin.pages import AdminLocale

TICKET_SCRIPT = """
(() => {
  const form = document.getElementById('ticket-form');
  if (!form) return;
  const drawer = document.getElementById('ticket-drawer');
  const result = document.getElementById('ticket-result');
  const submit = form.querySelector('button');
  let expiryTimer;
  let expiryDeadline;
  function updateExpiry() {
    const expiry = result.querySelector('[data-ticket-expires]');
    if (!expiry) return;
    const seconds = Math.max(0, Math.ceil((expiryDeadline - performance.now()) / 1000));
    const remaining = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    expiry.textContent = seconds ? remaining
      : form.dataset.expired;
    result.querySelectorAll('[data-ticket-copy]').forEach(button => button.disabled = !seconds);
    if (!seconds) clearInterval(expiryTimer);
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submit.disabled) return;
    clearInterval(expiryTimer);
    submit.disabled = true;
    result.textContent = form.dataset.loading;
    drawer.showModal();
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: new URLSearchParams(new FormData(form)),
        credentials: 'same-origin', redirect: 'error'
      });
      result.innerHTML = await response.text();
      if (response.ok) {
        const expiry = result.querySelector('[data-ticket-expires]');
        expiryDeadline = performance.now() + Number(expiry.dataset.remainingMs);
        updateExpiry();
        expiryTimer = setInterval(updateExpiry, 1000);
      }
    } catch {
      result.textContent = form.dataset.failed;
    } finally {
      submit.disabled = false;
    }
  });
  drawer.querySelector('[data-ticket-close]').addEventListener('click', () => drawer.close());
  drawer.addEventListener('click', async event => {
    const button = event.target.closest('[data-ticket-copy]');
    if (!button || button.disabled) return;
    const output = document.getElementById(button.dataset.ticketCopy);
    const status = document.getElementById('ticket-copy-status');
    try {
      await navigator.clipboard.writeText(output.value);
      status.textContent = button.dataset.copied;
    } catch {
      const details = output.closest('details');
      if (details) details.open = true;
      output.focus();
      output.select();
      status.textContent = button.dataset.manual;
    }
  });
  window.addEventListener('pagehide', () => {
    clearInterval(expiryTimer);
    result.replaceChildren();
    drawer.close();
  });
})();
"""


def onboarding_ticket_page(
    csrf_token: str,
    *,
    locale: AdminLocale = "en",
    code: str | None = None,
    expires_at: int | None = None,
    origin: str = "",
    message: str | None = None,
) -> str:
    chinese = locale == "zh-CN"
    warning = (
        "5 分钟内使用，仅可兑换一次。仅交给信任的 Agent：它可兑换所有当前和未来知识库"
        "的永久读写凭据。长期 Token 不要发到聊天。"
        if chinese
        else "Use within 5 minutes, once only. Give it only to a trusted Agent: it can redeem "
        "permanent read/write access to all current and future Libraries. "
        "Keep the Token out of chat."
    )
    body = f'<section><p class="section-help">{warning}</p>'
    if message:
        body += f'<p role="alert">{escape(message)}</p>'
    if code is not None:
        assert expires_at is not None
        expiry = datetime.fromtimestamp(expires_at / 1_000_000, UTC).isoformat()
        remaining_ms = max(0, (expires_at - int(datetime.now(UTC).timestamp() * 1_000_000)) // 1000)
        prompt = (
            f"请接入 PatchouliLib：{origin}\n接入小票：{code}\n"
            f"到期时间：{expiry}（只能兑换一次）。\n"
            "先读取同一服务的 /connect 公开接入契约。"
            "先询问我凭据名称、说明、有效期、独立读取与写入范围，以及本机专用环境变量名。"
            "使用 /api/v1/onboarding/libraries 查询可选知识库。"
            "先确认保存位置和变量名，没有同名已有值或配置冲突；未经我确认不覆盖。"
            "确认后写本地脚本调用 /api/v1/onboarding/redeem，直接将返回 Token 保存到"
            "本机环境变量或私有配置，不打印响应或 Token，不把长期 Token 传入模型上下文。"
            "写入后从刚保存的位置重新读取，在脚本内存中核对值一致，再调用 "
            "/api/v1/auth/whoami 核对身份与权限。"
            "然后携带 Token 下载 /api/v1/agent/skill/manifest，按清单下载全部文件，"
            "逐项核对 SHA-256 和字节数后安装。"
            "仅在我授权的本地 Skill、memory 或 AGENTS.md 中记录变量名和用途，不记录 Token。"
            "若超时或响应丢失，不盲目重复兑换；请我检查凭据管理页面。"
            if chinese
            else f"Connect to PatchouliLib: {origin}\nOnboarding ticket: {code}\n"
            f"Expires: {expiry} (one redemption only).\n"
            "Read the public /connect onboarding contract on this same service first. "
            "First ask me for the credential name, description, lifetime, independent read/write "
            "scopes and a dedicated local environment-variable name. Query "
            "/api/v1/onboarding/libraries for available Libraries. Confirm the storage location "
            "and check for existing variables/config conflicts; do not overwrite without my "
            "confirmation. After confirmation, write a "
            "local script to call /api/v1/onboarding/redeem and save its returned Token directly "
            "to a local environment variable or private config. Do not print the response or "
            "Token or send it into model context. Read back from the saved location and verify "
            "the value matches in script memory, then call /api/v1/auth/whoami to verify identity "
            "and access. Download /api/v1/agent/skill/manifest with the Token, download every "
            "listed file and check its SHA-256 and byte count before installation. Record only "
            "the variable name and purpose in local Skill, memory or AGENTS.md files I have "
            "authorized. If the response is lost or times "
            "out, do not blindly retry: ask me to inspect credential management."
        )
        copied = "已复制" if chinese else "Copied"
        manual = "请手动复制选中的文本" if chinese else "Copy the selected text manually"
        body += (
            f"<p>{'剩余时间' if chinese else 'Time left'}: "
            f'<time data-ticket-expires data-remaining-ms="{remaining_ms}" '
            f'datetime="{expiry}" title="{expiry}">5:00</time></p>'
        )
        for key, value, label in (
            ("ticket-code", code, "复制小票" if chinese else "Copy ticket"),
            (
                "ticket-prompt",
                prompt,
                "复制接入指令" if chinese else "Copy onboarding instructions",
            ),
        ):
            if key == "ticket-prompt":
                body += "<details><summary>" + ("指令预览" if chinese else "Preview instructions")
                body += "</summary>"
            body += (
                f'<label for="{key}">{label}</label>'
                f'<textarea id="{key}" readonly data-ticket-secret spellcheck="false">'
                f"{escape(value)}</textarea>"
            )
            if key == "ticket-prompt":
                body += "</details>"
            body += (
                f'<button type="button" data-ticket-copy="{key}" '
                f'data-copied="{copied}" data-manual="{manual}">{label}</button>'
            )
        body += '<p id="ticket-copy-status" role="status" aria-live="polite"></p>'
    return body + "</section>"
