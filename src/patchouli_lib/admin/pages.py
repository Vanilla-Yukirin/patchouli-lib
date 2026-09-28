from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Literal

from patchouli_lib.admin.read_model import (
    BookView,
    CallerView,
    ContentActivityItem,
    LibraryItem,
    LibraryView,
    PageView,
    SectionView,
)
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
.section-help {
  color: #68766d;
  font-size: .9rem;
  line-height: 1.45;
  margin: -.25rem 0 .8rem;
}
.field-help {
  color: #68766d;
  display: block;
  font-size: .82rem;
  font-weight: 400;
  line-height: 1.4;
  margin-top: .3rem;
}
.admin-shell {
  display: grid;
  grid-template-columns: 13rem minmax(0, 1fr);
  max-width: 90rem;
  margin: 0 auto;
}
.admin-shell main { min-width: 0; max-width: none; margin: 0; }
.side-nav { padding: 2rem 1rem; border-right: 1px solid #d6ddd5; }
.side-nav a { display: block; padding: .6rem .75rem; border-radius: .4rem; text-decoration: none; }
.side-nav a[aria-current="page"] { background: #dce9de; font-weight: 700; }
.breadcrumb {
  display: flex;
  gap: .5rem;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 1rem;
}
.item-list { list-style: none; padding: 0; display: grid; gap: .8rem; }
.item-list li { background: #fff; border: 1px solid #d6ddd5; border-radius: .6rem; padding: 1rem; }
.item-list a { font-weight: 700; }
.item-list p { margin-bottom: 0; overflow-wrap: anywhere; }
.meta { color: #526259; font-size: .9rem; }
.markdown-preview { white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 40rem) {
  .nav-actions { width: 100%; margin-left: 0; justify-content: space-between; }
  .admin-shell { display: block; }
  .side-nav {
    display: flex;
    gap: .3rem;
    padding: .5rem 1rem;
    overflow-x: auto;
    border-right: 0;
    border-bottom: 1px solid #d6ddd5;
  }
  .side-nav a { white-space: nowrap; }
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
    "Finish setup": "完成首次设置",
    "First-time setup": "首次设置",
    "Initial operator token lifetime in seconds": "管理员令牌有效期（秒）",
    "Invalid password.": "密码不正确。",
    "Language": "语言",
    "Library ID": "知识库 ID",
    "Library initialized": "知识库已初始化",
    "Library name": "知识库名称",
    "Libraries": "知识库",
    "Home": "主页",
    "Sections": "分区",
    "Books": "书籍",
    "Pages": "页面",
    "Created": "创建时间",
    "Content activity": "内容近况",
    "Created a page": "创建了页面",
    "Revised a page": "更新了页面",
    "No content activity yet.": "暂无内容活动。",
    "Page no longer available": "页面目前不可预览",
    "Identity details": "身份详情",
    "Identity kind": "身份类型",
    "Identity description": "身份说明",
    "Identity disabled": "身份已停用",
    "Identity active": "身份有效",
    "Status": "状态",
    "Back to activity": "返回近况",
    "Occurred": "发生时间",
    "Current revision": "当前版本",
    "Page type": "页面类型",
    "Current Markdown": "当前 Markdown 正文",
    "Markdown body": "Markdown 正文",
    "Version history": "版本历史",
    "Version": "版本",
    "Recorded": "记录时间",
    "Back to current version": "返回当前版本",
    "No libraries yet.": "暂无知识库。",
    "No sections yet.": "暂无分区。",
    "No books yet.": "暂无书籍。",
    "No pages yet.": "暂无页面。",
    "Page count": "页面数",
    "The requested item was not found.": "找不到请求的内容。",
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
    (
        "A large, durable category and the boundary used for Agent permissions, "
        "such as Personal, Projects, or Research."
    ): ("一个长期使用的大分类，也是 Agent 权限的边界，例如“个人资料”“项目”或“研究”。"),
    (
        "A stable name used to identify this administrator. Audit records are linked "
        "to this identity. It is not the administration sign-in name."
    ): ("用于标识这个管理员，操作记录会关联到这个身份；它不是网页登录账号。"),
    (
        "Complete this when setting up a Library for the first time. It creates the "
        "starting structure and issues a temporary operator token. Do not use it to "
        "edit an existing Library; submit it again only when you deliberately want "
        "another Library."
    ): (
        "首次设置一个知识库时填写：创建基础结构，并生成一份临时管理员令牌。"
        "它不能修改已有知识库；只有明确要新建另一个知识库时才再次填写。"
    ),
    (
        "How long the first operator token remains valid. This is not a Library "
        "lifetime. 3600 seconds is one hour."
    ): ("首次生成的管理员令牌可以使用多久；不是知识库有效期。3600 秒就是 1 小时。"),
    "Optional. Briefly explain what belongs in this Section.": (
        "可选。简单说明这个分区收录什么内容。"
    ),
    "Optional. Briefly explain what this Book contains.": ("可选。简单说明这本书收录什么内容。"),
    "Optional. Describe this administrator's purpose, such as Primary local administrator.": (
        "可选。说明这个管理员的用途，例如“主要本地管理员”。"
    ),
    "The first topic container inside this Section. You can start with Inbox or General.": (
        "这个分区里的第一组内容，可以先用“收件箱”或“综合资料”。"
    ),
    "The name of this whole knowledge space. One Library is usually enough for personal use.": (
        "整个知识空间的名称。个人使用通常一个知识库就够了。"
    ),
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
    activities: tuple[ContentActivityItem, ...] | None = None,
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
        "Complete this when setting up a Library for the first time. It creates the "
        "starting structure and issues a temporary operator token. Do not use it to edit "
        "an existing Library; submit it again only when you deliberately want another "
        "Library."
    )
    initialize_fields = "".join(
        (
            _text(
                "library_name",
                "Library name",
                locale,
                help_text=(
                    "The name of this whole knowledge space. One Library is usually "
                    "enough for personal use."
                ),
            ),
            _text(
                "section_name",
                "Section name",
                locale,
                help_text=(
                    "A large, durable category and the boundary used for Agent "
                    "permissions, such as Personal, Projects, or Research."
                ),
            ),
            _textarea(
                "section_description",
                "Section description",
                locale,
                help_text="Optional. Briefly explain what belongs in this Section.",
            ),
            _text(
                "book_name",
                "Book name",
                locale,
                help_text=(
                    "The first topic container inside this Section. You can start with "
                    "Inbox or General."
                ),
            ),
            _textarea(
                "book_summary",
                "Book summary",
                locale,
                help_text="Optional. Briefly explain what this Book contains.",
            ),
            _text(
                "operator_name",
                "Operator name",
                locale,
                help_text=(
                    "A stable name used to identify this administrator. Audit records "
                    "are linked to this identity. It is not the administration sign-in "
                    "name."
                ),
            ),
            _textarea(
                "operator_description",
                "Operator description",
                locale,
                help_text=(
                    "Optional. Describe this administrator's purpose, such as Primary "
                    "local administrator."
                ),
            ),
            _number(
                "credential_ttl_seconds",
                "Initial operator token lifetime in seconds",
                3600,
                locale,
                help_text=(
                    "How long the first operator token remains valid. This is not a "
                    "Library lifetime. 3600 seconds is one hour."
                ),
            ),
        )
    )
    recover_description = (
        "撤销当前有效的管理员凭据，并签发一个替代凭据。"
        if locale == "zh-CN"
        else "Revokes active operator credentials and issues one replacement."
    )
    content = f"""
{_header(csrf, locale)}
<div class="admin-shell">
{_sidebar(locale, current="home")}
<main>
  <h1>{localize(locale, "Administration")}</h1>
  {"" if activities is None else _content_activity_timeline(activities, locale)}
  <p class="notice">{credential_notice}</p>
  {notice}
  <div class="grid">
    <section class="card">
      <h2>{localize(locale, "First-time setup")}</h2>
      <p class="section-help">{localize(locale, initialize_description)}</p>
      <form method="post" action="/admin/bootstrap" autocomplete="off">
        {_csrf(csrf)}
        {initialize_fields}
        <button type="submit">{localize(locale, "Finish setup")}</button>
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
</div>
"""
    return _document(localize(locale, "Administration"), content, locale)


def _content_activity_timeline(
    activities: tuple[ContentActivityItem, ...], locale: AdminLocale
) -> str:
    entries: list[str] = []
    for item in activities:
        actor_path = (
            f"/admin/libraries/{escape(item.library_id, quote=True)}/callers/"
            f"{escape(item.actor_id, quote=True)}"
        )
        actor = f'<a href="{actor_path}">{escape(item.actor_name)}</a>'
        action = "Created a page" if item.action == "content.archive.create" else "Revised a page"
        if (
            item.page_id is not None
            and item.section_id is not None
            and item.book_id is not None
            and item.revision_number is not None
        ):
            page_path = (
                f"/admin/libraries/{escape(item.library_id, quote=True)}"
                f"/sections/{escape(item.section_id, quote=True)}"
                f"/books/{escape(item.book_id, quote=True)}"
                f"/pages/{escape(item.page_id, quote=True)}"
                f"/revisions/{item.revision_number}"
            )
            page = f'<a href="{page_path}">{escape(item.page_title or "")}</a>'
        else:
            page = escape(item.page_title or localize(locale, "Page no longer available"))
        entries.append(
            f"<li>{actor} {localize(locale, action)} {page}"
            f'<p class="meta">{_relative_time(item.occurred_at, locale)}</p></li>'
        )
    body = (
        f'<ul class="item-list">{"".join(entries)}</ul>'
        if entries
        else f"<p>{localize(locale, 'No content activity yet.')}</p>"
    )
    return f'<section class="card"><h2>{localize(locale, "Content activity")}</h2>{body}</section>'


def caller_page(csrf_token: str, view: CallerView, *, locale: AdminLocale = "en") -> str:
    status = "Identity disabled" if view.disabled_at is not None else "Identity active"
    body = (
        "<dl>"
        f"<dt>{localize(locale, 'Identity kind')}</dt><dd>{escape(view.kind)}</dd>"
        f"<dt>{localize(locale, 'Identity description')}</dt>"
        f"<dd>{escape(view.description)}</dd>"
        f"<dt>{localize(locale, 'Status')}</dt><dd>{localize(locale, status)}</dd>"
        "</dl>"
        f'<p><a href="/admin">{localize(locale, "Back to activity")}</a></p>'
    )
    return _browser_document(
        csrf_token,
        locale,
        view.name,
        f"/admin/libraries/{escape(view.library_id, quote=True)}/callers/"
        f"{escape(view.id, quote=True)}",
        body,
        crumbs=((localize(locale, "Libraries"), "/admin/libraries"),),
    )


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
<div class="admin-shell">
{_sidebar(locale, current="home")}
<main>
  <section class="card">
    <h1>{escape(localized_heading)}</h1>
    {_notice(localize(locale, message))}
    <p><a href="/admin">{localize(locale, "Return to administration")}</a></p>
  </section>
</main>
</div>
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
        f"{_header(csrf, locale, switch_path=switch_path)}"
        f'<div class="admin-shell">{_sidebar(locale, current=page)}'
        f'<main><section class="card"><h1>{localized_title}</h1>{body}</section></main></div>'
    )
    return _document(localized_title, content, locale)


def libraries_page(
    csrf_token: str,
    libraries: tuple[LibraryItem, ...],
    *,
    locale: AdminLocale = "en",
) -> str:
    cards = "".join(
        '<li><a href="/admin/libraries/'
        f'{escape(item.id, quote=True)}">{escape(item.name)}</a>'
        f'<p class="meta">{localize(locale, "Created")}: {_time(item.created_at)} · '
        f"{localize(locale, 'Page count')}: {item.page_count}</p></li>"
        for item in libraries
    )
    body = (
        f'<ul class="item-list">{cards}</ul>'
        if libraries
        else f'<p class="card">{localize(locale, "No libraries yet.")}</p>'
    )
    return _browser_document(
        csrf_token, locale, localize(locale, "Libraries"), "/admin/libraries", body
    )


def library_page(
    csrf_token: str,
    view: LibraryView,
    *,
    locale: AdminLocale = "en",
) -> str:
    base = f"/admin/libraries/{escape(view.library.id, quote=True)}"
    cards = "".join(
        f'<li><a href="{base}/sections/{escape(item.id, quote=True)}">'
        f"{escape(item.name)}</a><p>{escape(item.description)}</p></li>"
        for item in view.sections
    )
    body = (
        f'<p class="meta">{localize(locale, "Created")}: {_time(view.library.created_at)} · '
        f"{localize(locale, 'Page count')}: {view.library.page_count}</p>"
        f"<h2>{localize(locale, 'Sections')}</h2>"
        + (
            f'<ul class="item-list">{cards}</ul>'
            if view.sections
            else f'<p class="card">{localize(locale, "No sections yet.")}</p>'
        )
    )
    return _browser_document(
        csrf_token,
        locale,
        view.library.name,
        base,
        body,
        crumbs=((localize(locale, "Libraries"), "/admin/libraries"),),
    )


def section_page(
    csrf_token: str,
    view: SectionView,
    *,
    locale: AdminLocale = "en",
) -> str:
    library_path = f"/admin/libraries/{escape(view.library.id, quote=True)}"
    base = f"{library_path}/sections/{escape(view.section.id, quote=True)}"
    cards = "".join(
        f'<li><a href="{base}/books/{escape(item.id, quote=True)}">'
        f"{escape(item.name)}</a><p>{escape(item.summary)}</p></li>"
        for item in view.books
    )
    body = f"<p>{escape(view.section.description)}</p><h2>{localize(locale, 'Books')}</h2>" + (
        f'<ul class="item-list">{cards}</ul>'
        if view.books
        else f'<p class="card">{localize(locale, "No books yet.")}</p>'
    )
    return _browser_document(
        csrf_token,
        locale,
        view.section.name,
        base,
        body,
        crumbs=(
            (localize(locale, "Libraries"), "/admin/libraries"),
            (view.library.name, library_path),
        ),
    )


def book_page(
    csrf_token: str,
    view: BookView,
    *,
    locale: AdminLocale = "en",
) -> str:
    library_path = f"/admin/libraries/{escape(view.library.id, quote=True)}"
    section_path = f"{library_path}/sections/{escape(view.section.id, quote=True)}"
    base = f"{section_path}/books/{escape(view.book.id, quote=True)}"
    cards = "".join(
        f'<li><a href="{base}/pages/{escape(item.id, quote=True)}">'
        f'{escape(item.title)}</a><p class="meta">'
        f"{localize(locale, 'Occurred')}: {_time(item.occurred_at)} · "
        f"{localize(locale, 'Current revision')}: {item.revision_number}</p></li>"
        for item in view.pages
    )
    body = f"<p>{escape(view.book.summary)}</p><h2>{localize(locale, 'Pages')}</h2>" + (
        f'<ul class="item-list">{cards}</ul>'
        if view.pages
        else f'<p class="card">{localize(locale, "No pages yet.")}</p>'
    )
    return _browser_document(
        csrf_token,
        locale,
        view.book.name,
        base,
        body,
        crumbs=(
            (localize(locale, "Libraries"), "/admin/libraries"),
            (view.library.name, library_path),
            (view.section.name, section_path),
        ),
    )


def page_preview_page(
    csrf_token: str,
    view: PageView,
    *,
    locale: AdminLocale = "en",
) -> str:
    library_path = f"/admin/libraries/{escape(view.library.id, quote=True)}"
    section_path = f"{library_path}/sections/{escape(view.section.id, quote=True)}"
    book_path = f"{section_path}/books/{escape(view.book.id, quote=True)}"
    base = f"{book_path}/pages/{escape(view.page.id, quote=True)}"
    history = "".join(
        f'<li><a href="{base}/revisions/{item.number}">'
        f"{localize(locale, 'Version')} {item.number}</a>"
        f'<p class="meta">{localize(locale, "Recorded")}: {_time(item.created_at)}</p></li>'
        for item in view.revisions
    )
    current = view.selected_revision_number == view.page.revision_number
    heading = "Current Markdown" if current else "Markdown body"
    body = (
        f'<p class="meta">{localize(locale, "Page type")}: {escape(view.page.page_type)} · '
        f"{localize(locale, 'Occurred')}: {_time(view.page.occurred_at)} · "
        f"{localize(locale, 'Current revision')}: {view.page.revision_number}</p>"
        f'<p class="meta">{localize(locale, "Version")}: '
        f"{view.selected_revision_number} · "
        f"{localize(locale, 'Recorded')}: {_time(view.selected_revision_created_at)}</p>"
        + (
            ""
            if current
            else f'<p><a href="{base}">{localize(locale, "Back to current version")}</a></p>'
        )
        + f"<h2>{localize(locale, heading)}</h2>"
        f'<pre class="markdown-preview">{escape(view.markdown)}</pre>'
        f"<h2>{localize(locale, 'Version history')}</h2>"
        f'<ul class="item-list">{history}</ul>'
    )
    return _browser_document(
        csrf_token,
        locale,
        view.page.title,
        base,
        body,
        crumbs=(
            (localize(locale, "Libraries"), "/admin/libraries"),
            (view.library.name, library_path),
            (view.section.name, section_path),
            (view.book.name, book_path),
        ),
    )


def browser_not_found_page(csrf_token: str, *, locale: AdminLocale = "en") -> str:
    return _browser_document(
        csrf_token,
        locale,
        localize(locale, "The requested item was not found."),
        "/admin/libraries",
        "",
    )


def _browser_document(
    csrf_token: str,
    locale: AdminLocale,
    title: str,
    path: str,
    body: str,
    *,
    crumbs: tuple[tuple[str, str], ...] = (),
) -> str:
    links = "".join(
        f'<a href="{escape(href, quote=True)}">{escape(label)}</a><span aria-hidden="true">/</span>'
        for label, href in crumbs
    )
    heading = escape(title)
    content = (
        f"{_header(escape(csrf_token, quote=True), locale, switch_path=path)}"
        '<div class="admin-shell">'
        f"{_sidebar(locale, current='libraries')}"
        f'<main><div class="breadcrumb">{links}<span>{heading}</span></div>'
        f"<h1>{heading}</h1>{body}</main></div>"
    )
    return _document(title, content, locale)


def _sidebar(locale: AdminLocale, *, current: str) -> str:
    entries = (
        ("home", "Home", "/admin"),
        ("libraries", "Libraries", "/admin/libraries"),
        ("guide", "Guide", "/admin/guide"),
        ("agent", "Agent", "/admin/agent"),
        ("mcp", "MCP", "/admin/mcp"),
    )
    links = "".join(
        f'<a href="{path}"'
        + (' aria-current="page"' if key == current else "")
        + f">{escape(localize(locale, label))}</a>"
        for key, label, path in entries
    )
    return (
        f'<aside class="side-nav" aria-label="{localize(locale, "Administration")}">{links}</aside>'
    )


def _time(timestamp_micros: int) -> str:
    try:
        value = datetime.fromtimestamp(timestamp_micros / 1_000_000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        # SQLite accepts timestamps beyond datetime's supported year range.
        # Keep the read-only browser usable without inventing a calendar date.
        return f"{timestamp_micros} µs (UTC)"
    iso = value.isoformat(timespec="seconds")
    return f'<time datetime="{iso}">{value:%Y-%m-%d %H:%M} UTC</time>'


def _relative_time(timestamp_micros: int, locale: AdminLocale) -> str:
    try:
        value = datetime.fromtimestamp(timestamp_micros / 1_000_000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return _time(timestamp_micros)
    seconds = max(0, int((datetime.now(UTC) - value).total_seconds()))
    if seconds < 60:
        count, unit = seconds, ("秒前", "seconds ago")
    elif seconds < 3600:
        count, unit = seconds // 60, ("分钟前", "minutes ago")
    elif seconds < 86_400:
        count, unit = seconds // 3600, ("小时前", "hours ago")
    else:
        count, unit = seconds // 86_400, ("天前", "days ago")
    relative = f"{count}{unit[0]}" if locale == "zh-CN" else f"{count} {unit[1]}"
    absolute = value.strftime("%Y-%m-%d %H:%M:%S UTC")
    return f'<time datetime="{value.isoformat()}" title="{absolute}">{relative}</time>'


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
    <a href="/admin/libraries">{localize(locale, "Libraries")}</a>
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


def _text(
    name: str,
    label: str,
    locale: AdminLocale,
    *,
    help_text: str | None = None,
) -> str:
    escaped_name = escape(name, quote=True)
    described_by, help_markup = _field_help(escaped_name, help_text, locale)
    return (
        f'<label for="{escaped_name}">{escape(localize(locale, label))}</label>'
        f'<input id="{escaped_name}" name="{escaped_name}" type="text" '
        f'maxlength="200"{described_by} required>{help_markup}'
    )


def _secret(name: str, label: str, locale: AdminLocale) -> str:
    escaped_name = escape(name, quote=True)
    return (
        f'<label for="{escaped_name}">{escape(localize(locale, label))}</label>'
        f'<input id="{escaped_name}" name="{escaped_name}" type="password" '
        'maxlength="256" autocomplete="off" spellcheck="false" required>'
    )


def _textarea(
    name: str,
    label: str,
    locale: AdminLocale,
    *,
    help_text: str | None = None,
) -> str:
    escaped_name = escape(name, quote=True)
    described_by, help_markup = _field_help(escaped_name, help_text, locale)
    return (
        f'<label for="{escaped_name}">{escape(localize(locale, label))}</label>'
        f'<textarea id="{escaped_name}" name="{escaped_name}" '
        f'maxlength="4000" rows="3"{described_by}></textarea>{help_markup}'
    )


def _number(
    name: str,
    label: str,
    value: int,
    locale: AdminLocale,
    *,
    help_text: str | None = None,
) -> str:
    escaped_name = escape(name, quote=True)
    described_by, help_markup = _field_help(escaped_name, help_text, locale)
    return (
        f'<label for="{escaped_name}">{escape(localize(locale, label))}</label>'
        f'<input id="{escaped_name}" name="{escaped_name}" type="number" '
        f'min="1" value="{value}"{described_by} required>{help_markup}'
    )


def _field_help(
    escaped_name: str,
    help_text: str | None,
    locale: AdminLocale,
) -> tuple[str, str]:
    if help_text is None:
        return "", ""
    help_id = f"{escaped_name}-help"
    described_by = f' aria-describedby="{help_id}"'
    help_markup = (
        f'<small class="field-help" id="{help_id}">{escape(localize(locale, help_text))}</small>'
    )
    return described_by, help_markup


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
