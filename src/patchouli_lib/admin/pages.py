from __future__ import annotations

from html import escape
from typing import Literal

from patchouli_lib.admin.service import DeliveredCredential
from patchouli_lib.auth.schemas import SectionAction

AdminLocale = Literal["en", "zh-CN"]

STYLESHEET = """
:root {
  color-scheme: light;
  font-family: Inter, ui-sans-serif, system-ui, sans-serif;
  background: #f3f5f1;
  color: #1d2a21;
}
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; }
a { color: #355f43; }
header {
  background: #183c2a;
  color: #fff;
  padding: 1rem max(1rem, calc((100% - 70rem) / 2));
}
header a { color: #fff; text-decoration: none; }
nav { display: flex; flex-wrap: wrap; gap: .8rem; align-items: center; }
main { max-width: 70rem; margin: 0 auto; padding: 2rem 1rem 4rem; }
.narrow { max-width: 30rem; }
.grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr)); }
.card {
  background: #fff;
  border: 1px solid #d6ddd5;
  border-radius: .7rem;
  box-shadow: 0 .25rem 1rem rgb(24 60 42 / 8%);
  padding: 1.2rem;
}
.nav-actions { display: flex; gap: .8rem; align-items: center; margin-left: auto; }
.language-switch { display: inline-flex; gap: .4rem; align-items: center; white-space: nowrap; }
.language-switch a[aria-current="page"] { font-weight: 750; text-decoration: none; }
.login-tools { display: flex; justify-content: flex-end; }
label { display: block; font-weight: 650; margin-top: .8rem; }
input, textarea, select {
  border: 1px solid #87988b;
  border-radius: .35rem;
  display: block;
  font: inherit;
  margin-top: .25rem;
  padding: .55rem;
  width: 100%;
}
fieldset { border: 1px solid #d6ddd5; margin-top: .8rem; }
fieldset label { display: flex; gap: .5rem; font-weight: 400; }
fieldset input { width: auto; }
button {
  background: #2f6845;
  border: 0;
  border-radius: .35rem;
  color: #fff;
  cursor: pointer;
  font: inherit;
  font-weight: 700;
  margin-top: 1rem;
  padding: .6rem .9rem;
}
.secondary { background: #587063; margin-top: 0; }
.notice { border-left: .3rem solid #c57d14; padding: .7rem 1rem; background: #fff7e8; }
.error { border-left-color: #a92f2f; background: #fff0f0; }
.secret {
  display: block;
  overflow-wrap: anywhere;
  padding: 1rem;
  background: #17231c;
  color: #dff6e6;
  user-select: all;
}
pre { overflow-x: auto; background: #17231c; color: #dff6e6; padding: 1rem; }
dt { font-weight: 700; }
dd { margin: 0 0 .7rem; overflow-wrap: anywhere; }
small { color: #526259; }
@media (max-width: 40rem) {
  .nav-actions { width: 100%; margin-left: 0; justify-content: space-between; }
}
""".strip()


_ZH_CN: dict[str, str] = {
    "A required form field is missing.": "缺少必填字段。",
    "Administration": "管理面板",
    "Administration password": "管理密码",
    "Administration sign in": "管理面板登录",
    "Agent caller ID": "Agent 调用方 ID",
    "Agent credential created": "Agent 凭据已创建",
    "Agent credential ID": "Agent 凭据 ID",
    "Agent credential revoked": "Agent 凭据已撤销",
    "Agent description": "Agent 说明",
    "Agent instructions": "Agent 使用说明",
    "Agent name": "Agent 名称",
    "Book name": "书籍名称",
    "Book summary": "书籍摘要",
    "Caller ID": "调用方 ID",
    "Check the submitted fields and try again.": "请检查填写内容后重试。",
    "Create Agent credential": "创建 Agent 凭据",
    "Credential ID": "凭据 ID",
    "Credential lifetime in seconds": "凭据有效期（秒）",
    "Current operator credential": "当前管理员凭据",
    "Exact Section permissions": "分区权限（精确范围）",
    "Guide": "指南",
    "Initialize": "初始化",
    "Initialize the library": "初始化知识库",
    "Invalid password.": "密码不正确。",
    "Language": "语言",
    "Library ID": "知识库 ID",
    "Library initialized": "知识库已初始化",
    "Library name": "知识库名称",
    "MCP setup": "MCP 配置",
    "Only URL-encoded forms are accepted.": "只接受 URL 编码的表单。",
    "Operator credential recovered": "管理员凭据已恢复",
    "Operator description": "管理员说明",
    "Operator guide": "管理员指南",
    "Operator name": "管理员名称",
    "PatchouliLib administration": "PatchouliLib 管理面板",
    "Provision an Agent": "创建 Agent 凭据",
    "Recover credential": "恢复凭据",
    "Recover the operator": "恢复管理员凭据",
    "Request origin was rejected.": "请求来源被拒绝。",
    "Return to administration": "返回管理面板",
    "Revoke an Agent credential": "撤销 Agent 凭据",
    "Revoke credential": "撤销凭据",
    "Section description": "分区说明",
    "Section name": "分区名称",
    "Sign in": "登录",
    "Sign in again.": "请重新登录。",
    "Sign out": "退出登录",
    "The action completed.": "操作已完成。",
    "The action conflicts with current local state.": "操作与当前本地状态冲突。",
    "The action could not be completed.": "无法完成此操作。",
    "The Agent credential is no longer active.": "Agent 凭据已失效。",
    "The form expired or failed its safety check.": "表单已过期或未通过安全检查。",
    "The operator credential was rejected.": "管理员凭据被拒绝。",
    "The requested local resource was not found.": "找不到请求的本地资源。",
    "The submitted form contains a duplicate field.": "提交的表单包含重复字段。",
    "The submitted form contains an unknown field.": "提交的表单包含未知字段。",
    "The submitted form is invalid.": "提交的表单无效。",
    "The submitted form is too large.": "提交的表单过大。",
}


def localize(locale: AdminLocale, text: str) -> str:
    if locale == "zh-CN":
        return _ZH_CN.get(text, text)
    return text


def login_page(*, locale: AdminLocale = "en", message: str | None = None) -> str:
    notice = "" if message is None else _notice(localize(locale, message), error=True)
    description = (
        "此面板用于管理本地应用状态，不能部署镜像、执行主机命令或控制 Docker。"
        if locale == "zh-CN"
        else (
            "This console manages local application state. It cannot deploy images, "
            "run host commands, or control Docker."
        )
    )
    content = f"""
<main class="narrow">
  <section class="card">
    <div class="login-tools">{_language_switch(locale, "/admin/login")}</div>
    <h1>{localize(locale, "PatchouliLib administration")}</h1>
    <p>{description}</p>
    {notice}
    <form method="post" action="/admin/login" autocomplete="off">
      <label for="password">{localize(locale, "Administration password")}</label>
      <input id="password" name="password" type="password"
        minlength="12" maxlength="1024" autocomplete="current-password" required>
      <button type="submit">{localize(locale, "Sign in")}</button>
    </form>
  </section>
</main>
"""
    return _document(localize(locale, "Administration sign in"), content, locale)


def dashboard_page(
    csrf_token: str,
    *,
    locale: AdminLocale = "en",
    message: str | None = None,
) -> str:
    csrf = escape(csrf_token, quote=True)
    notice = "" if message is None else _notice(localize(locale, message), error=True)
    grants = "".join(
        (
            '<label><input type="checkbox" name="grants" '
            f'value="{escape(action.value, quote=True)}"> '
            f"{escape(action.value)}</label>"
        )
        for action in SectionAction
    )
    credential_notice = (
        "新凭据只会显示一次。请勿将其放入 URL、截图、日志或聊天。下方输入的"
        "管理员凭据仅用于一次请求，不会写入浏览器会话。"
        if locale == "zh-CN"
        else (
            "New credentials are displayed once. Keep them out of URLs, screenshots, logs, "
            "and chat. Operator credentials entered below are used for one request and are "
            "not placed in the browser session."
        )
    )
    initialize_description = (
        "创建第一个知识库、分区、书籍和本地管理员。"
        if locale == "zh-CN"
        else "Creates the first Library, Section, Book, and local operator."
    )
    recover_description = (
        "撤销当前有效的管理员凭据，并签发一个替代凭据。"
        if locale == "zh-CN"
        else "Revokes active operator credentials and issues one replacement."
    )
    content = f"""
{_header(csrf, locale)}
<main>
  <h1>{localize(locale, "Administration")}</h1>
  <p class="notice">{credential_notice}</p>
  {notice}
  <div class="grid">
    <section class="card">
      <h2>{localize(locale, "Initialize the library")}</h2>
      <p>{initialize_description}</p>
      <form method="post" action="/admin/bootstrap" autocomplete="off">
        {_csrf(csrf)}
        {_text("library_name", "Library name", locale)}
        {_text("section_name", "Section name", locale)}
        {_textarea("section_description", "Section description", locale)}
        {_text("book_name", "Book name", locale)}
        {_textarea("book_summary", "Book summary", locale)}
        {_text("operator_name", "Operator name", locale)}
        {_textarea("operator_description", "Operator description", locale)}
        {_number("credential_ttl_seconds", "Credential lifetime in seconds", 3600, locale)}
        <button type="submit">{localize(locale, "Initialize")}</button>
      </form>
    </section>
    <section class="card">
      <h2>{localize(locale, "Recover the operator")}</h2>
      <p>{recover_description}</p>
      <form method="post" action="/admin/recover" autocomplete="off">
        {_csrf(csrf)}
        {_text("library_name", "Library name", locale)}
        {_number("credential_ttl_seconds", "Credential lifetime in seconds", 3600, locale)}
        <button type="submit">{localize(locale, "Recover credential")}</button>
      </form>
    </section>
    <section class="card">
      <h2>{localize(locale, "Provision an Agent")}</h2>
      <form method="post" action="/admin/agents/provision" autocomplete="off">
        {_csrf(csrf)}
        {_secret("operator_token", "Current operator credential", locale)}
        {_text("library_name", "Library name", locale)}
        {_text("section_name", "Section name", locale)}
        {_text("agent_name", "Agent name", locale)}
        {_textarea("agent_description", "Agent description", locale)}
        {_number("credential_ttl_seconds", "Credential lifetime in seconds", 3600, locale)}
        <fieldset>
          <legend>{localize(locale, "Exact Section permissions")}</legend>{grants}
        </fieldset>
        <button type="submit">{localize(locale, "Create Agent credential")}</button>
      </form>
    </section>
    <section class="card">
      <h2>{localize(locale, "Revoke an Agent credential")}</h2>
      <form method="post" action="/admin/agents/revoke" autocomplete="off">
        {_csrf(csrf)}
        {_secret("operator_token", "Current operator credential", locale)}
        {_text("library_name", "Library name", locale)}
        {_text("caller_id", "Agent caller ID", locale)}
        {_text("credential_id", "Agent credential ID", locale)}
        <button type="submit">{localize(locale, "Revoke credential")}</button>
      </form>
    </section>
  </div>
</main>
"""
    return _document(localize(locale, "Administration"), content, locale)


def credential_page(
    csrf_token: str,
    *,
    heading: str,
    result: DeliveredCredential,
    locale: AdminLocale = "en",
) -> str:
    csrf = escape(csrf_token, quote=True)
    localized_heading = localize(locale, heading)
    credential_notice = (
        "此值仅在本次响应中显示。离开本页前，请将它存入认可的秘密存储。"
        if locale == "zh-CN"
        else (
            "This value is shown only in this response. Store it in an approved secret "
            "store before leaving this page."
        )
    )
    content = f"""
{_header(csrf, locale, switch_path=None)}
<main class="narrow">
  <section class="card">
    <h1>{escape(localized_heading)}</h1>
    <p class="notice">{credential_notice}</p>
    <code class="secret">{escape(result.value)}</code>
    <dl>
      <dt>{localize(locale, "Library ID")}</dt><dd>{escape(result.library_id)}</dd>
      <dt>{localize(locale, "Caller ID")}</dt><dd>{escape(result.caller_id)}</dd>
      <dt>{localize(locale, "Credential ID")}</dt><dd>{escape(result.credential_id)}</dd>
    </dl>
    <p><a href="/admin">{localize(locale, "Return to administration")}</a></p>
  </section>
</main>
"""
    return _document(localized_heading, content, locale)


def action_result_page(
    csrf_token: str,
    *,
    heading: str,
    message: str,
    locale: AdminLocale = "en",
) -> str:
    csrf = escape(csrf_token, quote=True)
    localized_heading = localize(locale, heading)
    content = f"""
{_header(csrf, locale)}
<main class="narrow">
  <section class="card">
    <h1>{escape(localized_heading)}</h1>
    {_notice(localize(locale, message))}
    <p><a href="/admin">{localize(locale, "Return to administration")}</a></p>
  </section>
</main>
"""
    return _document(localized_heading, content, locale)


def guide_page(csrf_token: str, page: str, *, locale: AdminLocale = "en") -> str:
    csrf = escape(csrf_token, quote=True)
    if locale == "zh-CN":
        pages = {
            "guide": (
                "Operator guide",
                """
<p>只需执行一次<strong>初始化</strong>。请把返回的管理员凭据保存在浏览器之外。
恢复管理员凭据会使此前仍有效的管理员凭据失效。</p>
<p>每个 Agent 只能关联一个指定分区，并且只授予它真正需要的操作权限。
请记录返回的调用方 ID 和凭据 ID，以便之后撤销凭据。</p>
<p>此面板不能更新镜像、回滚、控制 Docker、恢复备份、执行 Shell 命令或部署。
这些操作仍需通过独立的本地管理员流程完成。</p>
""",
            ),
            "agent": (
                "Agent instructions",
                """
<p>安装独立发布的 Python 客户端，然后先检查服务端协议和当前有效身份：</p>
<pre>patchouli capabilities
patchouli whoami
patchouli sections list</pre>
<p>凭据不能通过命令行选项传入。请通过令牌标准输入、当前进程的
<code>PATCHOULI_TOKEN</code> 环境变量，或可选的操作系统秘密存储提供凭据。
绝不能把令牌放进提示词、配置档案、URL、已跟踪文件或 Shell 参数。</p>
<p>内置的 <code>patchouli-agent</code> Skill 包含完整的安全归档和精确引用流程。</p>
""",
            ),
            "mcp": (
                "MCP setup",
                """
<p>安装 <code>patchouli-client[mcp]</code>，配置相同的非秘密客户端档案，
并让 Agent 宿主调用 <code>patchouli-mcp</code> 可执行程序。</p>
<pre>executable: patchouli-mcp
arguments: none
transport: stdio</pre>
<p>适配器不会打开监听端口，也不会在 MCP 工具参数中接受凭据、服务地址、
幂等键、日志路径或本地文件路径。请通过客户端环境或操作系统秘密存储配置凭据。</p>
""",
            ),
        }
    else:
        pages = {
            "guide": (
                "Operator guide",
                """
<p>Use <strong>Initialize</strong> once. Save the returned operator credential
outside the browser. Recovery invalidates prior active operator credentials.</p>
<p>Provision each Agent for one named Section and only the actions it needs.
Record the returned caller and credential IDs so the credential can be revoked.</p>
<p>This console has no image update, rollback, Docker, backup restore, shell, or
deployment controls. Those remain separate local operator procedures.</p>
""",
            ),
            "agent": (
                "Agent instructions",
                """
<p>Install the independently packaged Python client and start by checking the
server contract and effective identity:</p>
<pre>patchouli capabilities
patchouli whoami
patchouli sections list</pre>
<p>Credentials have no command-line option. Supply them through token stdin,
the process-local <code>PATCHOULI_TOKEN</code> environment variable, or the
optional operating-system secret store. Never place a token in a prompt,
profile, URL, tracked file, or shell argument.</p>
<p>The bundled <code>patchouli-agent</code> Skill contains the complete safe
archive and exact-citation workflow.</p>
""",
            ),
            "mcp": (
                "MCP setup",
                """
<p>Install <code>patchouli-client[mcp]</code>, configure the same non-secret
client profile, and point the Agent host at the <code>patchouli-mcp</code>
executable.</p>
<pre>executable: patchouli-mcp
arguments: none
transport: stdio</pre>
<p>The adapter opens no listener and accepts no credential, endpoint,
idempotency key, journal path, or local file path in MCP tool arguments.
Configure credentials through the client environment or operating-system
secret store.</p>
""",
            ),
        }
    title, body = pages[page]
    localized_title = localize(locale, title)
    switch_path = {
        "guide": "/admin/guide",
        "agent": "/admin/agent",
        "mcp": "/admin/mcp",
    }[page]
    content = (
        f'{_header(csrf, locale, switch_path=switch_path)}<main><section class="card">'
        f"<h1>{localized_title}</h1>{body}</section></main>"
    )
    return _document(localized_title, content, locale)


def _header(
    csrf_token: str,
    locale: AdminLocale,
    *,
    switch_path: str | None = "/admin",
) -> str:
    language_switch = "" if switch_path is None else _language_switch(locale, switch_path)
    return f"""
<header>
  <nav aria-label="{localize(locale, "Administration")}">
    <a href="/admin"><strong>PatchouliLib</strong></a>
    <a href="/admin/guide">{localize(locale, "Guide")}</a>
    <a href="/admin/agent">Agent</a>
    <a href="/admin/mcp">MCP</a>
    <div class="nav-actions">
      {language_switch}
      <form method="post" action="/admin/logout">
        {_csrf(csrf_token)}
        <button class="secondary" type="submit">{localize(locale, "Sign out")}</button>
      </form>
    </div>
  </nav>
</header>
"""


def _language_switch(locale: AdminLocale, path: str) -> str:
    english_current = ' aria-current="page"' if locale == "en" else ""
    chinese_current = ' aria-current="page"' if locale == "zh-CN" else ""
    escaped_path = escape(path, quote=True)
    return (
        f'<span class="language-switch" aria-label="{localize(locale, "Language")}">'
        f'<a href="{escaped_path}?lang=zh-CN" hreflang="zh-CN" '
        f'lang="zh-CN"{chinese_current}>中文</a>'
        '<span aria-hidden="true">/</span>'
        f'<a href="{escaped_path}?lang=en" hreflang="en" '
        f'lang="en"{english_current}>English</a>'
        "</span>"
    )


def _document(title: str, content: str, locale: AdminLocale) -> str:
    return f"""<!doctype html>
<html lang="{locale}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} · PatchouliLib</title>
  <link rel="stylesheet" href="/admin/style.css">
</head>
<body>{content}</body>
</html>
"""


def _notice(message: str, *, error: bool = False) -> str:
    classes = "notice error" if error else "notice"
    return f'<p class="{classes}" role="status">{escape(message)}</p>'


def _csrf(value: str) -> str:
    return f'<input type="hidden" name="csrf_token" value="{value}">'


def _text(name: str, label: str, locale: AdminLocale) -> str:
    escaped_name = escape(name, quote=True)
    return (
        f'<label for="{escaped_name}">{escape(localize(locale, label))}</label>'
        f'<input id="{escaped_name}" name="{escaped_name}" type="text" '
        'maxlength="200" required>'
    )


def _secret(name: str, label: str, locale: AdminLocale) -> str:
    escaped_name = escape(name, quote=True)
    return (
        f'<label for="{escaped_name}">{escape(localize(locale, label))}</label>'
        f'<input id="{escaped_name}" name="{escaped_name}" type="password" '
        'maxlength="256" autocomplete="off" spellcheck="false" required>'
    )


def _textarea(name: str, label: str, locale: AdminLocale) -> str:
    escaped_name = escape(name, quote=True)
    return (
        f'<label for="{escaped_name}">{escape(localize(locale, label))}</label>'
        f'<textarea id="{escaped_name}" name="{escaped_name}" '
        'maxlength="4000" rows="3"></textarea>'
    )


def _number(name: str, label: str, value: int, locale: AdminLocale) -> str:
    escaped_name = escape(name, quote=True)
    return (
        f'<label for="{escaped_name}">{escape(localize(locale, label))}</label>'
        f'<input id="{escaped_name}" name="{escaped_name}" type="number" '
        f'min="1" value="{value}" required>'
    )


__all__ = [
    "STYLESHEET",
    "AdminLocale",
    "action_result_page",
    "credential_page",
    "dashboard_page",
    "guide_page",
    "localize",
    "login_page",
]
