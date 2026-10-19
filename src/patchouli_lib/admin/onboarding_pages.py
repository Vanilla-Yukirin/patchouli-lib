"""Master-only, short-lived onboarding ticket presentation."""

from datetime import UTC, datetime
from html import escape

from patchouli_lib.admin.pages import AdminLocale, _browser_document, _csrf

TICKET_SCRIPT = """
document.querySelectorAll('[data-ticket-copy]').forEach(button => {
  button.addEventListener('click', async () => {
    const output = document.getElementById(button.dataset.ticketCopy);
    const status = document.getElementById('ticket-copy-status');
    try {
      await navigator.clipboard.writeText(output.value);
      status.textContent = button.dataset.copied;
    } catch {
      output.focus();
      output.select();
      status.textContent = button.dataset.manual;
    }
  });
});
window.addEventListener('pagehide', () => {
  document.querySelectorAll('[data-ticket-secret]').forEach(output => {
    output.value = '';
    output.textContent = '';
  });
});
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
    title = "接入小票" if chinese else "Onboarding ticket"
    warning = (
        "小票是临时签发凭证：16 位随机字符，5 分钟有效，只能兑换一次。"
        "持有小票的人可自行选择凭据名称、说明、有效期与读写权限，"
        "包括永不过期及所有当前和未来知识库的全部读写权限。"
        "请仅交给你信任的 Agent；小票可放入对话，长期 Token 不要放入对话。"
        if chinese
        else "A ticket is temporary credential-issuing authority: 16 random characters, "
        "valid for 5 minutes and redeemable once. Its holder chooses the credential name, "
        "description, lifetime and read/write permissions, including never-expiring access "
        "to all current and future Libraries. Give it only to an Agent you trust. The ticket "
        "may be sent in chat; the long-lived Token must stay local."
    )
    body = f'<section class="card"><p class="notice">{warning}</p>'
    if message:
        body += f'<p role="alert">{escape(message)}</p>'
    if code is None:
        label = "立即生成小票" if chinese else "Generate ticket now"
        body += (
            '<form method="post" action="/admin/onboarding-tickets" autocomplete="off">'
            + _csrf(escape(csrf_token, quote=True))
            + f'<button type="submit">{label}</button></form>'
        )
    else:
        assert expires_at is not None
        expiry = datetime.fromtimestamp(expires_at / 1_000_000, UTC).isoformat()
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
        body += f"<p>{'到期时间' if chinese else 'Expires'}: {escape(expiry)}</p>"
        for key, value, label in (
            ("ticket-code", code, "复制小票" if chinese else "Copy ticket"),
            (
                "ticket-prompt",
                prompt,
                "复制接入指令" if chinese else "Copy onboarding instructions",
            ),
        ):
            body += (
                f'<label for="{key}">{label}</label>'
                f'<textarea id="{key}" readonly data-ticket-secret spellcheck="false">'
                f'{escape(value)}</textarea><button type="button" data-ticket-copy="{key}" '
                f'data-copied="{copied}" data-manual="{manual}">{label}</button>'
            )
        body += '<p id="ticket-copy-status" role="status" aria-live="polite"></p>'
    body += '</section><a class="action-link" href="/admin/agents">'
    body += "返回凭据管理" if chinese else "Back to credential management"
    body += "</a>"
    return _browser_document(
        csrf_token,
        locale,
        title,
        "/admin/onboarding-tickets",
        body,
        script_src="/admin/onboarding-tickets.js" if code else None,
        current_section="agents",
    )
