from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from typing import Literal
from urllib.parse import quote, urlencode

from patchouli_lib.admin.interface_guide import api_guide, mcp_guide, skill_guide
from patchouli_lib.admin.read_model import (
    BookView,
    CallerItem,
    CallerView,
    ContentActivityItem,
    CredentialItem,
    LibraryItem,
    LibraryView,
    PageView,
    RequestLogItem,
    SectionView,
    TagDirectoryView,
    TagItem,
    TagView,
    TrashDirectoryView,
    TrashPageView,
)
from patchouli_lib.admin.request_log_filters import RequestLogFilters
from patchouli_lib.admin.service import DeliveredCredential
from patchouli_lib.api.agent_skill_routes import SkillBundle
from patchouli_lib.auth.library_policy import LibraryAction, target_library_grants_digest
from patchouli_lib.auth.schemas import SectionAction
from patchouli_lib.identifiers import canonical_utc_wire
from patchouli_lib.search.service_v2 import SearchPageV2

AdminLocale = Literal["en", "zh-CN"]


@dataclass(frozen=True, slots=True)
class MarkdownPreview:
    filenames: tuple[str, ...]
    selected_filename: str | None
    html: str | None = field(repr=False)


_TAG_TOKEN_HELP = (
    "Enter this Library's Operator token for each Tag change. "
    "It is not saved in the browser session."
)
_MASTER_TAG_HELP = "Your master session can change Tags without another token."
_RESTORE_TOKEN_HELP = (
    "Enter this Library's Operator token for this restore. It is not saved in the browser session."
)
_GRANT_SCOPE_HELP = (
    "These are existing single-Library, Section-level grants; "
    "they are not cross-Library permissions."
)
_CREDENTIAL_META_HELP = (
    "Only a master session can reveal a newly issued, active Agent Token. "
    "Older values cannot be recovered."
)
_REQUEST_LOG_HELP = (
    "API request metadata is kept online for 30 days. "
    "Tokens, content and search terms are not recorded."
)

REVEAL_SCRIPT = """
document.querySelectorAll('.token-reveal').forEach((form) => {
  const output = form.querySelector('.token-output');
  const status = form.querySelector('[role="status"]');
  const show = form.querySelector('.token-show');
  const copy = form.querySelector('.token-copy');
  let visible = false;

  window.addEventListener('pagehide', () => {
    output.textContent = '';
    output.hidden = true;
    visible = false;
  });

  async function loadCurrentValue() {
    const response = await fetch(form.action, {
      method: 'POST',
      body: new URLSearchParams(new FormData(form)),
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    });
    if (!response.ok) throw new Error('reveal rejected');
    return response.text();
  }

  show.addEventListener('click', async () => {
    if (visible) {
      output.textContent = '';
      output.hidden = true;
      visible = false;
      show.textContent = form.dataset.showLabel;
      status.textContent = '';
      return;
    }
    try {
      const value = await loadCurrentValue();
      output.textContent = value;
      output.hidden = false;
      visible = true;
      show.textContent = form.dataset.hideLabel;
      status.textContent = '';
    } catch {
      output.textContent = '';
      output.hidden = true;
      visible = false;
      status.textContent = form.dataset.errorLabel;
    }
  });

  copy.addEventListener('click', async () => {
    let value;
    try {
      // Recheck authorization and credential activity even if a value was shown earlier.
      value = await loadCurrentValue();
    } catch {
      output.textContent = '';
      output.hidden = true;
      visible = false;
      show.textContent = form.dataset.showLabel;
      status.textContent = form.dataset.errorLabel;
      return;
    }
    try {
      await navigator.clipboard.writeText(value);
      status.textContent = form.dataset.copiedLabel;
    } catch {
      // Plain HTTP and some browsers cannot write to the clipboard. The
      // protected POST did succeed: keep the value available for manual copy.
      output.textContent = value;
      output.hidden = false;
      visible = true;
      show.textContent = form.dataset.hideLabel;
      status.textContent = form.dataset.manualCopyLabel;
    }
  });
});
""".strip()

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
header nav { display: flex; flex-wrap: wrap; gap: .8rem; align-items: center; }
main { max-width: 70rem; margin: 0 auto; padding: 2rem 1rem 4rem; }
.narrow { max-width: 30rem; }
.grid {
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 20rem), 1fr));
}
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
.token-output[hidden] { display: none; }
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
.library-grid { grid-template-columns: repeat(auto-fit, minmax(min(100%, 16rem), 1fr)); }
.library-grid li { min-height: 8rem; }
.meta { color: #526259; font-size: .9rem; }
.markdown-preview { white-space: pre-wrap; overflow-wrap: anywhere; }
.revision-files code { overflow-wrap: anywhere; }
.rendered-markdown { white-space: normal; overflow-wrap: anywhere; }
.rendered-markdown img { max-width: 100%; height: auto; }
.rendered-markdown pre { overflow-x: auto; }
.revision-history [aria-current="true"] { border-color: #2f6845; }
.revision-history .selected-marker { color: #355f43; font-weight: 700; }
.interface-list code { overflow-wrap: anywhere; }
details.card { margin-top: 1rem; }
details summary { cursor: pointer; font-weight: 700; }
.credential-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: .3rem 1rem;
  margin: 0 0 .6rem;
}
.credential-summary .meta, .credential-item code { overflow-wrap: anywhere; }
.credential-metadata { margin: .8rem 0 0; }
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
    "Create a document": "新建文档",
    "Update files": "更新文件",
    "A required form field is missing.": "缺少必填字段。",
    "Administration": "管理面板",
    "Administration sections": "管理栏目",
    "Administration password": "管理密码",
    "Administration sign in": "管理面板登录",
    "Agent caller ID": "Agent 调用方 ID",
    "Agent credential created": "Agent 凭据已创建",
    "Agent credential rotated": "Agent 凭据已轮换",
    "Agent credential ID": "Agent 凭据 ID",
    "Agent credential revoked": "Agent 凭据已撤销",
    "Agent description": "Agent 说明",
    "Agent instructions": "Agent 使用说明",
    "Agent name": "Agent 名称",
    "Edit Agent": "编辑 Agent",
    "Save Agent": "保存 Agent",
    "The Agent changed since this form was opened. Reload and try again.": (
        "打开表单后 Agent 已发生变化，请刷新后重试。"
    ),
    "An identity with that name already exists in this Library.": ("该知识库已有同名身份。"),
    "Book name": "书籍名称",
    "Book summary": "书籍摘要",
    "Edit Section": "编辑分区",
    "Save Section": "保存分区",
    "The Section changed since this form was opened. Reload and try again.": (
        "分区已发生变化，请刷新后重试。"
    ),
    "Edit Book": "编辑书籍",
    "Save Book": "保存书籍",
    "Edit Page title": "编辑页面标题",
    "Page title": "页面标题",
    "Save title": "保存标题",
    "The Page changed since this form was opened. Reload and try again.": (
        "打开表单后页面已发生变化，请刷新后重试。"
    ),
    "The Book changed since this form was opened. Reload and try again.": (
        "打开表单后书籍已发生变化，请刷新后重试。"
    ),
    "Breadcrumb": "当前位置",
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
    "Library description": "知识库简介",
    "Edit Library": "编辑知识库",
    "Save Library": "保存知识库",
    "The Library changed since this form was opened. Reload and try again.": (
        "知识库已发生变化，请刷新后重试。"
    ),
    "Optional. Briefly describe this knowledge space.": "选填。简要说明这个知识库的用途。",
    "Libraries": "知识库",
    "Home": "主页",
    "Setup and credentials": "初始化与凭据",
    "Return to setup": "返回初始化与凭据",
    "Manage setup and credentials.": "在这里管理初始结构和现有凭据。",
    "Sections": "分区",
    "Books": "书籍",
    "Pages": "页面",
    "Tags": "标签",
    "Trash": "回收站",
    "No deleted pages.": "暂无已删除页面。",
    "Deleted": "删除时间",
    "Next page": "下一页",
    "This view shows metadata only; page contents remain unavailable here.": (
        "此处仅显示元数据，不提供已删除页面的正文。"
    ),
    "Restore page": "恢复页面",
    "Move to trash": "移入回收站",
    "Confirm moving this Page and all its versions to Trash.": (
        "确认将整个页面及其全部版本移入回收站。"
    ),
    "Files and history are preserved. You can restore this Page from Trash.": (
        "文件和历史版本都会保留，可以在回收站恢复。"
    ),
    "The page has already been deleted.": "此页面已经在回收站中。",
    "Restore this page and its complete revision history.": "恢复此页面及其完整历史版本。",
    _RESTORE_TOKEN_HELP: ("本次恢复需输入此知识库的管理员令牌；令牌不会保存在浏览器会话中。"),
    "The page changed since this form was opened. Reload the trash detail and try again.": (
        "打开表单后页面已发生变化。请重新打开回收站详情后再试。"
    ),
    "The page has already been restored.": "此页面已经恢复。",
    "The restore request conflicts with a previous request.": "恢复请求与之前的请求冲突。",
    "The requested Archive page was not found.": "未找到指定的 Archive 页面。",
    "Tagged pages": "标记的页面",
    "No tags yet.": "暂无标签。",
    "No tagged pages yet.": "暂无已标记的页面。",
    "Create Tag": "创建标签",
    "Tag name": "标签名称",
    "Manage page tags": "管理页面标签",
    "Choose a tag": "选择标签",
    "Attach tag": "关联标签",
    "Remove tag": "移除标签",
    "Create a Tag in this Library first.": "请先在本知识库创建标签。",
    _TAG_TOKEN_HELP: "每次更改标签都需输入本知识库的管理员令牌；令牌不会保存在管理会话中。",
    _MASTER_TAG_HELP: "当前主 Token 会话可以更改标签，无需再次填写令牌。",
    "Tag created.": "标签已创建。",
    "Tag already exists; nothing changed.": "标签已存在，没有改动。",
    "Tag attached.": "标签已关联。",
    "Tag was already attached; nothing changed.": "标签原本就已关联，没有改动。",
    "Tag removed.": "标签已移除。",
    "Tag was not attached; nothing changed.": "标签原本就未关联，没有改动。",
    "The requested Tag or page was not found.": "未找到指定标签或页面。",
    "Created": "创建时间",
    "Content activity": "内容近况",
    "Content activity by this identity": "此身份的内容近况",
    "Older activity": "更早的内容活动",
    "API requests": "接口请求记录",
    "API requests by this identity": "此身份的接口请求",
    "Older requests": "更早的请求",
    "No API requests in the retained period.": "保留期内暂无接口请求记录。",
    "Filter requests": "筛选请求",
    "All routes": "全部路由",
    "All methods": "全部方法",
    "HTTP method": "HTTP 方法",
    "Status code or interrupted": "状态码或 interrupted",
    "From (inclusive, RFC 3339)": "开始时间（含，RFC 3339）",
    "Until (exclusive, RFC 3339)": "结束时间（不含，RFC 3339）",
    "Apply filters": "应用筛选",
    "Clear filters": "清除筛选",
    "Invalid request-log filters or cursor.": "请求记录筛选条件或续页游标无效。",
    "Start a new request-log search": "重新查询请求记录",
    "Request ID": "请求 ID",
    "Endpoint": "接口",
    "HTTP status": "HTTP 状态",
    "Duration": "耗时",
    "Interrupted": "已中断",
    "Unknown identity": "未知身份",
    _REQUEST_LOG_HELP: ("接口请求元数据在线保留 30 天，不记录 Token、正文或搜索词。"),
    "Administrator": "管理员",
    "Created a page": "创建了页面",
    "Revised a page": "更新了页面",
    "Changed a page title": "修改了页面标题",
    "Moved a page": "移动了页面",
    "Corrected a page's occurrence time": "更正了页面的发生时间",
    "Edit declared time": "修改文档发生时间",
    "Declared time (RFC3339)": "文档发生时间（带时区的标准时间）",
    "Save declared time": "保存发生时间",
    "Declared time saved.": "发生时间已保存；文件和历史版本未改动。",
    "Declared time is unchanged.": "发生时间相同，没有改动。",
    "Use a timestamp with a timezone, for example 2026-08-13T10:00:00Z. "
    "This changes search and browse dates, not files, history or Page ID.": (
        "填写带时区的时间，例如 2026-08-13T10:00:00Z。"
        "这会改变搜索与浏览使用的日期，不改文件、历史版本或页面 ID。"
    ),
    "Deleted a page": "删除了页面",
    "Restored a page": "恢复了页面",
    "Created a tag": "创建了标签",
    "Attached tag": "关联了标签",
    "Removed tag": "移除了标签",
    "to page": "到页面",
    "from page": "从页面",
    "Tag no longer available": "标签目前不可查看",
    "No content activity yet.": "暂无内容活动。",
    "Page no longer available": "页面目前不可预览",
    "Identity details": "身份详情",
    "Identities": "设备与身份",
    "Agent / device": "Agent／设备",
    "Operator": "管理员",
    "No identities yet.": "暂无身份。",
    "Credentials cannot be recovered.": "这里不显示凭据。现有凭据无法从校验值还原。",
    "Identity kind": "身份类型",
    "Identity description": "身份说明",
    "Identity disabled": "身份已停用",
    "Identity active": "身份有效",
    "Existing credentials": "现有凭据",
    "Credential metadata": "查看凭据元数据",
    "Credential active": "凭据有效",
    "Credential not yet active": "凭据尚未生效",
    "Credential expired": "凭据已过期",
    "Credential revoked": "凭据已撤销",
    "Credential rotated": "凭据已轮转",
    "Credential blocked by disabled identity": "身份已停用，凭据不可用",
    "Expires": "到期时间",
    "Last used": "最后使用时间",
    "Revoked": "撤销时间",
    "Rotated": "轮转时间",
    "Never used": "尚未使用",
    "Not revoked": "未撤销",
    "Not rotated": "未轮转",
    "No credentials for this identity.": "此身份暂无凭据。",
    "Current Section grants": "当前分区授权",
    "No Section grants for this identity.": "此身份暂无分区授权。",
    _GRANT_SCOPE_HELP: ("这里展示的是现有的单知识库、分区级授权，不是跨知识库权限。"),
    _CREDENTIAL_META_HELP: ("只有主 Token 会话可查看新签发且仍有效的 Agent Token；旧值无法还原。"),
    "Show Token": "显示 Token",
    "Hide Token": "隐藏 Token",
    "Copy Token": "复制 Token",
    "Token copied.": "Token 已复制。",
    "Clipboard unavailable. Select the displayed Token to copy it manually.": (
        "剪贴板不可用。请选中显示的 Token 手动复制。"
    ),
    "Token could not be revealed. Refresh this page and check its status.": (
        "无法显示 Token。请刷新页面并检查凭据状态。"
    ),
    "This old Token cannot be recovered.": "此旧 Token 无法还原。",
    "Status": "状态",
    "Back to activity": "返回近况",
    "Occurred": "发生时间",
    "Current revision": "当前版本",
    "Files in this version": "此版本的文件",
    "Download file": "下载文件",
    "Page type": "页面类型",
    "Current Markdown": "当前 Markdown 正文",
    "Markdown body": "Markdown 正文",
    "No safe Markdown preview is available for this version.": (
        "此版本没有可安全预览的 Markdown 正文。"
    ),
    "Version history": "版本历史",
    "Older revisions": "更早的版本",
    "Latest revisions": "最新的版本",
    "Version": "版本",
    "Viewing": "正在查看",
    "Recorded": "记录时间",
    "Back to current version": "返回当前版本",
    "No libraries yet.": "暂无知识库。",
    "Choose a Library to browse its Tags.": "选择知识库以浏览其标签。",
    "Choose a Library to browse its Trash.": "选择知识库以浏览其回收站。",
    "No sections yet.": "暂无分区。",
    "No books yet.": "暂无书籍。",
    "No pages yet.": "暂无页面。",
    "Search": "搜索",
    "Search current Pages": "搜索当前页面",
    "Search words": "搜索词",
    (
        "Separate multiple words with spaces. Without spaces, "
        "Chinese text is one literal search item."
    ): ("多个关键词用空格分隔；没有空格的中文内容会作为一个完整词项搜索。"),
    "All libraries": "所有知识库",
    "Filter by Library": "筛选知识库",
    "Any selected Tag": "任一所选标签",
    "Hold Ctrl or Command to select multiple Tags. No selection means no Tag filter. "
    "When filtering one Library, select its Tags only.": (
        "按住 Ctrl 或 Command 可多选标签；不选则不限标签。指定知识库时只能选该库的标签。"
    ),
    "Occurred from (UTC)": "声明时间起点（UTC）",
    "Occurred before (UTC)": "声明时间终点（UTC，不含）",
    "Time filters use the Page's declared occurrence time, not its upload time.": (
        "时间筛选依据页面声明的发生时间，而非上传时间。"
    ),
    "Run search": "开始搜索",
    "Search results": "搜索结果",
    "No matching Pages.": "没有匹配的页面。",
    "Search index is not ready. Rebuild it before searching.": (
        "搜索索引尚未就绪，请先重建索引再搜索。"
    ),
    "Choose a keyword, Tag, or time range before searching.": (
        "请先填写关键词、选择标签或填写时间范围。"
    ),
    "The search form is invalid. Check the selected fields and UTC times.": (
        "搜索表单无效，请检查筛选项与 UTC 时间。"
    ),
    "The selected search scope is unavailable.": "所选搜索范围不可用。",
    "Only the Master Token session can search here.": "此处仅允许主 Token 会话搜索。",
    "Create Library": "新建知识库",
    "Create Section": "新建分区",
    "Create Book": "新建书籍",
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
    "Revoke this Token": "撤销这枚 Token",
    "After revocation, this Token cannot be used or shown again.": (
        "撤销后，这枚 Token 不能再使用或显示。"
    ),
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


def login_page(
    *, locale: AdminLocale = "en", message: str | None = None, master_mode: bool = False
) -> str:
    notice = "" if message is None else _notice(localize(locale, message), error=True)
    label = (
        ("管理 Token" if locale == "zh-CN" else "Master Token")
        if master_mode
        else localize(locale, "Administration password")
    )
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
      <label for="password">{label}</label>
      <input id="password" name="password" type="password"
        maxlength="1024" autocomplete="current-password" required>
      <button type="submit">{localize(locale, "Sign in")}</button>
    </form>
  </section>
</main>
"""
    return _document(localize(locale, "Administration sign in"), content, locale)


def master_setup_page(
    csrf_token: str | None,
    *,
    require_proof: bool,
    locale: AdminLocale = "en",
    message: str | None = None,
) -> str:
    """First identity setup; never prefill or reflect any credential."""

    chinese = locale == "zh-CN"
    title = "首次设置主 Token" if chinese else "Set up your master Token"
    explanation = (
        "主 Token 用于登录和管理，不需要用户名。请使用密码管理器生成并保存至少 "
        "32 字节的随机值；服务器仅保存校验值。设置后旧网页登录密码立即失效，"
        "现有 Agent Token 不受影响。"
        if chinese
        else "Use your master Token to sign in and manage the service, without a username. "
        "Generate and save at least 32 random bytes with a password manager. The server "
        "stores only a verifier. Setup disables the legacy web password immediately, "
        "but does not change existing Agent Tokens."
    )
    notice = "" if message is None else _notice(message, error=True)
    form = ""
    if csrf_token is not None:
        proof = ""
        if require_proof:
            label = "一次性设置凭据" if chinese else "One-time setup proof"
            help_text = (
                "输入部署时配置的设置凭据。它不是主 Token，设置成功后不能再次使用。"
                if chinese
                else "Enter the setup proof configured by the operator. This is not your "
                "master Token and cannot be used again after setup succeeds."
            )
            proof = (
                f'<label for="setup_proof">{label}</label>'
                '<input id="setup_proof" name="setup_proof" type="password" '
                'maxlength="1024" autocomplete="off" required>'
                f'<small class="field-help">{help_text}</small>'
            )
        token_label = "新的主 Token" if chinese else "New master Token"
        confirm_label = "再次输入主 Token" if chinese else "Confirm master Token"
        button = "保存并进入管理面板" if chinese else "Save and open administration"
        form = f"""
    <form method="post" action="/admin/master/setup">
      {_csrf(escape(csrf_token, quote=True))}
      {proof}
      <label for="master_token">{token_label}</label>
      <input id="master_token" name="master_token" type="password"
        maxlength="1024" autocomplete="new-password" required>
      <label for="confirmation">{confirm_label}</label>
      <input id="confirmation" name="confirmation" type="password"
        maxlength="1024" autocomplete="new-password" required>
      <button type="submit">{button}</button>
    </form>"""
    back = "返回登录" if chinese else "Back to sign in"
    content = f"""
<main class="narrow">
  <section class="card">
    <div class="login-tools">{_language_switch(locale, "/admin/master/setup")}</div>
    <h1>{title}</h1>
    <p class="section-help">{explanation}</p>
    {notice}
    {form}
    <p><a href="/admin/login">{back}</a></p>
  </section>
</main>"""
    return _document(title, content, locale)


def operations_page(
    csrf_token: str,
    *,
    locale: AdminLocale = "en",
    message: str | None = None,
    master_mode: bool = False,
) -> str:
    csrf = escape(csrf_token, quote=True)
    notice = "" if message is None else _notice(localize(locale, message), error=True)
    if master_mode:
        explanation = (
            "旧版初始化、管理员恢复及 Agent 签发／撤销表单不适用于主 Token 登录。"
            "请在知识库页面创建结构，并在身份页面签发 Agent Token；"
            "Agent 详情页可再次显示或撤销指定 Token。"
            if locale == "zh-CN"
            else (
                "The legacy setup, operator recovery, and Agent provision/revoke forms "
                "do not apply to Master Token sign-in. You can create structure from "
                "Libraries, issue Agent Tokens from Identities, and reveal or revoke a "
                "specific Token from Agent details."
            )
        )
        content = f"""
{_header(csrf, locale, switch_path="/admin/setup")}
<div class="admin-shell">
{_sidebar(locale, current="setup")}
<main>
  <h1>{localize(locale, "Setup and credentials")}</h1>
  {notice}
  <section class="card">
    <p>{explanation}</p>
    <p><a href="/admin/libraries">{localize(locale, "Libraries")}</a> ·
    <a href="/admin/agents">{localize(locale, "Identities")}</a></p>
  </section>
</main>
</div>
"""
        return _document(localize(locale, "Setup and credentials"), content, locale)
    master_setup_link = (
        "首次设置主 Token，统一网页登录和管理操作"
        if locale == "zh-CN"
        else "Set up your master Token for unified sign-in and administration"
    )
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
            _textarea(
                "library_description",
                "Library description",
                locale,
                help_text="Optional. Briefly describe this knowledge space.",
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
{_header(csrf, locale, switch_path="/admin/setup")}
<div class="admin-shell">
{_sidebar(locale, current="setup")}
<main>
  <h1>{localize(locale, "Setup and credentials")}</h1>
  <p class="section-help">{localize(locale, "Manage setup and credentials.")}</p>
  <p class="notice">{credential_notice}</p>
  {notice}
  <p><a href="/admin/master/setup">{master_setup_link}</a></p>
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
    return _document(localize(locale, "Setup and credentials"), content, locale)


def dashboard_page(
    csrf_token: str,
    *,
    locale: AdminLocale = "en",
    activities: tuple[ContentActivityItem, ...] = (),
    activity_next_cursor: str | None = None,
    activity_before_cursor: str | None = None,
    show_request_logs: bool = False,
) -> str:
    csrf = escape(csrf_token, quote=True)
    switch_path = (
        "/admin"
        if activity_before_cursor is None
        else f"/admin?before={quote(activity_before_cursor, safe='')}"
    )
    timeline = _content_activity_timeline(
        activities, locale, next_cursor=activity_next_cursor, base_path="/admin"
    )
    request_logs_link = (
        f'<p><a href="/admin/requests">{localize(locale, "API requests")}</a></p>'
        if show_request_logs
        else ""
    )
    content = f"""
{_header(csrf, locale, switch_path=switch_path)}
<div class="admin-shell">
{_sidebar(locale, current="home")}
<main>
  <h1>{localize(locale, "Home")}</h1>
  {timeline}
  {request_logs_link}
</main>
</div>
"""
    return _document(localize(locale, "Home"), content, locale)


@dataclass(frozen=True)
class SearchFormValues:
    """POSTed search controls, kept separate from normalized search semantics."""

    keywords: str = ""
    library_ids: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    occurred_from: str = ""
    occurred_before: str = ""
    cursor: str | None = None


def _search_match_sources(item: SearchPageV2, locale: AdminLocale) -> str:
    labels = (
        {"title": "文档标题", "file_name": "文件名", "file_text": "文件正文"}
        if locale == "zh-CN"
        else {"title": "Page title", "file_name": "File name", "file_text": "File text"}
    )
    sources = "; ".join(
        escape(labels.get(source.kind, source.kind))
        + (f": <code>{escape(source.file_name)}</code>" if source.file_name is not None else "")
        for source in item.match_sources
    )
    label = "命中来源" if locale == "zh-CN" else "Match sources"
    return f'<p class="meta search-match-sources">{label}: {sources}</p>' if sources else ""


def search_page(
    csrf_token: str,
    libraries: tuple[tuple[LibraryItem, tuple[TagItem, ...]], ...],
    *,
    locale: AdminLocale = "en",
    results: tuple[SearchPageV2, ...] | None = None,
    message: str | None = None,
    form_values: SearchFormValues | None = None,
    next_cursor: str | None = None,
) -> str:
    """Render a server-side search form without placing terms or credentials in URLs."""

    if form_values is None:
        form_values = SearchFormValues()
    csrf = escape(csrf_token, quote=True)
    keyword_help = localize(
        locale,
        "Separate multiple words with spaces. Without spaces, "
        "Chinese text is one literal search item.",
    )
    tag_help = (
        "按 Ctrl 或 Command 可选择多个标签；不选择表示不限标签。"
        "指定知识库时只能选择所选知识库的标签。"
        if locale == "zh-CN"
        else "Hold Ctrl or Command to select multiple Tags. No selection means no Tag filter. "
        "When filtering Libraries, select their Tags only."
    )
    library_help = (
        "按 Ctrl 或 Command 可选择多个知识库；不选择表示搜索全部知识库。"
        if locale == "zh-CN"
        else "Hold Ctrl or Command to select multiple Libraries. "
        "No selection searches all libraries."
    )
    known_libraries = {library.id for library, _ in libraries}
    time_help = localize(
        locale, "Time filters use the Page's declared occurrence time, not its upload time."
    )
    library_options = "".join(
        f'<option value="{escape(library.id, quote=True)}"'
        + (" selected" if library.id in form_values.library_ids else "")
        + f">{escape(library.name)}</option>"
        for library, _ in libraries
    )
    unavailable_library = "不可用知识库" if locale == "zh-CN" else "Unavailable Library"
    library_options += "".join(
        f'<option value="{escape(identity, quote=True)}" selected>'
        f"{unavailable_library}: {escape(identity)}</option>"
        for identity in dict.fromkeys(form_values.library_ids)
        if identity not in known_libraries
    )
    known_tags = {f"{library.id}:{tag.id}" for library, tags in libraries for tag in tags}
    tag_options = "".join(
        '<optgroup label="'
        + escape(library.name, quote=True)
        + '">'
        + "".join(
            f'<option value="{escape(library.id, quote=True)}:{escape(tag.id, quote=True)}"'
            + (" selected" if f"{library.id}:{tag.id}" in form_values.tags else "")
            + f">{escape(tag.name)}</option>"
            for tag in tags
        )
        + "</optgroup>"
        for library, tags in libraries
        if tags
    )
    unavailable_tag = "不可用 Tag" if locale == "zh-CN" else "Unavailable Tag"
    tag_options += "".join(
        f'<option value="{escape(identity, quote=True)}" selected>'
        f"{unavailable_tag}: {escape(identity)}</option>"
        for identity in dict.fromkeys(form_values.tags)
        if identity not in known_tags
    )
    result_html = ""
    if results is not None:
        cards = "".join(
            '<li><a href="/admin/libraries/'
            f"{escape(item.library_id, quote=True)}/sections/"
            f"{escape(item.section_id, quote=True)}/books/"
            f"{escape(item.book_id, quote=True)}/pages/"
            f'{escape(item.page_id, quote=True)}">{escape(item.title)}</a>'
            f'<p class="meta">{localize(locale, "Occurred")}: {_time(item.occurred_at)}'
            f" · {localize(locale, 'Version')}: {item.revision_number}</p>"
            + _search_match_sources(item, locale)
            + (
                f'<p class="search-snippet">{escape(item.snippet.text)}</p>'
                if item.snippet is not None
                else ""
            )
            + "</li>"
            for item in results
        )
        result_html = (
            f"<section><h2>{localize(locale, 'Search results')}</h2>"
            + (
                f'<ul class="item-list">{cards}</ul>'
                if cards
                else f'<p class="card">{localize(locale, "No matching Pages.")}</p>'
            )
            + "</section>"
        )
        if next_cursor is not None:
            controls = [
                ("keywords", form_values.keywords),
                ("occurred_from", form_values.occurred_from),
                ("occurred_before", form_values.occurred_before),
                ("cursor", next_cursor),
                *(("library_id", value) for value in form_values.library_ids),
                *(("tags", value) for value in form_values.tags),
            ]
            hidden = "".join(
                f'<input type="hidden" name="{name}" value="{escape(value, quote=True)}">'
                for name, value in controls
            )
            label = "下一页" if locale == "zh-CN" else "Next page"
            result_html += (
                '<form method="post" action="/admin/search" class="search-next-page">'
                + _csrf(csrf)
                + hidden
                + f'<button type="submit">{label}</button></form>'
            )
    notice = "" if message is None else _notice(localize(locale, message), error=True)
    content = f"""
{_header(csrf, locale, switch_path="/admin/search")}
<div class="admin-shell">
{_sidebar(locale, current="search")}
<main>
  <h1>{localize(locale, "Search current Pages")}</h1>
  {notice}
  <form class="card" method="post" action="/admin/search" autocomplete="off">
    {_csrf(csrf)}
    <label for="keywords">{localize(locale, "Search words")}</label>
    <input id="keywords" name="keywords" type="text" maxlength="32768"
      value="{escape(form_values.keywords, quote=True)}">
    <small class="field-help">{keyword_help}</small>
    <label for="library_id">{localize(locale, "Filter by Library")}</label>
    <select id="library_id" name="library_id" multiple size="4">
      {library_options}
    </select>
    <small class="field-help">{library_help}</small>
    <label for="tags">{localize(locale, "Any selected Tag")}</label>
    <select id="tags" name="tags" multiple size="6">{tag_options}</select>
    <small class="field-help">{tag_help}</small>
    <label for="occurred_from">{localize(locale, "Occurred from (UTC)")}</label>
    <input id="occurred_from" name="occurred_from" type="datetime-local" step="1"
      value="{escape(form_values.occurred_from, quote=True)}">
    <label for="occurred_before">{localize(locale, "Occurred before (UTC)")}</label>
    <input id="occurred_before" name="occurred_before" type="datetime-local" step="1"
      value="{escape(form_values.occurred_before, quote=True)}">
    <small class="field-help">{time_help}</small>
    <button type="submit">{localize(locale, "Run search")}</button>
  </form>
  {result_html}
</main>
</div>
"""
    return _document(localize(locale, "Search"), content, locale)


def request_log_page(
    csrf_token: str,
    items: tuple[RequestLogItem, ...],
    *,
    filters: RequestLogFilters,
    locale: AdminLocale = "en",
    next_cursor: str | None = None,
    before_cursor: str | None = None,
    actor: tuple[str, str] | None = None,
    route_options: tuple[tuple[str, str], ...] = (),
) -> str:
    base_path = (
        "/admin/requests"
        if actor is None
        else f"/admin/libraries/{quote(actor[0], safe='')}/callers/"
        f"{quote(actor[1], safe='')}/requests"
    )
    filter_items = filters.query_items()
    switch_items = filter_items + (("before", before_cursor),) if before_cursor else filter_items
    switch_path = base_path + (f"?{urlencode(switch_items)}" if switch_items else "")
    available_routes = sorted(
        {route for _, route in route_options} | ({filters.route} if filters.route else set())
    )
    route_choices = [f'<option value="">{localize(locale, "All routes")}</option>']
    route_choices.extend(
        f'<option value="{escape(route, quote=True)}"'
        + (" selected" if route == filters.route else "")
        + f">{escape(route)}</option>"
        for route in available_routes
    )
    method_choices = [f'<option value="">{localize(locale, "All methods")}</option>']
    method_choices.extend(
        f'<option value="{method}"'
        + (" selected" if method == filters.method else "")
        + f">{method}</option>"
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "OTHER")
    )
    form = (
        f'<form method="get" action="{escape(base_path, quote=True)}" class="card">'
        f"<h2>{localize(locale, 'Filter requests')}</h2>"
        f'<label>{localize(locale, "Endpoint")} <select name="route">'
        f"{''.join(route_choices)}</select></label>"
        f"<label>{localize(locale, 'HTTP method')} "
        f'<select name="method">{"".join(method_choices)}</select></label>'
        f"<label>{localize(locale, 'Status code or interrupted')} "
        f'<input name="status" maxlength="11" value="{escape(filters.status or "", quote=True)}" '
        'placeholder="200 / interrupted"></label>'
        f"<label>{localize(locale, 'From (inclusive, RFC 3339)')} "
        f'<input name="since" maxlength="35" value="{escape(filters.since or "", quote=True)}" '
        'placeholder="2026-01-01T00:00:00Z"></label>'
        f"<label>{localize(locale, 'Until (exclusive, RFC 3339)')} "
        f'<input name="until" maxlength="35" value="{escape(filters.until or "", quote=True)}" '
        'placeholder="2026-02-01T00:00:00Z"></label>'
        f'<button type="submit">{localize(locale, "Apply filters")}</button> '
        f'<a href="{escape(base_path, quote=True)}">{localize(locale, "Clear filters")}</a>'
        "</form>"
    )
    rows: list[str] = []
    for item in items:
        if item.home_library_id is not None and item.caller_id is not None:
            identity_path = (
                f"/admin/libraries/{quote(item.home_library_id, safe='')}/callers/"
                f"{quote(item.caller_id, safe='')}"
            )
            identity = (
                f'<a href="{escape(identity_path, quote=True)}">'
                f"<code>{escape(item.caller_id)}</code></a>"
            )
        else:
            identity = localize(locale, "Unknown identity")
        status = (
            str(item.status_code)
            if item.completion == "completed" and item.status_code is not None
            else localize(locale, "Interrupted")
        )
        rows.append(
            "<li>"
            f"<code>{escape(item.method)} {escape(item.route_template)}</code>"
            f'<p class="meta">{_time(item.occurred_at)} · '
            f"{localize(locale, 'HTTP status')}: {escape(status)} · "
            f"{localize(locale, 'Duration')}: {item.duration_us / 1000:.1f} ms · "
            f"{identity}</p>"
            f'<p class="meta">{localize(locale, "Request ID")}: '
            f"<code>{escape(item.request_id)}</code></p>"
            "</li>"
        )
    body = (
        f'<ul class="item-list">{"".join(rows)}</ul>'
        if rows
        else f"<p>{localize(locale, 'No API requests in the retained period.')}</p>"
    )
    if next_cursor is not None:
        next_items = (*filter_items, ("before", next_cursor))
        body += (
            f'<nav aria-label="{localize(locale, "API requests")}">'
            f'<a href="{escape(base_path + "?" + urlencode(next_items), quote=True)}">'
            f"{localize(locale, 'Older requests')}</a></nav>"
        )
    title = "API requests" if actor is None else "API requests by this identity"
    return _browser_document(
        csrf_token,
        locale,
        localize(locale, title),
        switch_path,
        f'<p class="section-help">'
        f"{localize(locale, _REQUEST_LOG_HELP)}"
        f'</p>{form}<section class="card">{body}</section>',
        current_section="requests",
    )


def request_log_filter_error_page(
    csrf_token: str,
    *,
    locale: AdminLocale = "en",
    actor: tuple[str, str] | None = None,
) -> str:
    base_path = (
        "/admin/requests"
        if actor is None
        else f"/admin/libraries/{quote(actor[0], safe='')}/callers/"
        f"{quote(actor[1], safe='')}/requests"
    )
    return _browser_document(
        csrf_token,
        locale,
        localize(locale, "API requests"),
        base_path,
        '<section class="card"><p role="alert">'
        f"{localize(locale, 'Invalid request-log filters or cursor.')}</p>"
        f'<p><a href="{escape(base_path, quote=True)}">'
        f"{localize(locale, 'Start a new request-log search')}</a></p></section>",
        current_section="requests",
    )


def _content_activity_timeline(
    activities: tuple[ContentActivityItem, ...],
    locale: AdminLocale,
    *,
    title: str = "Content activity",
    next_cursor: str | None = None,
    base_path: str = "/admin",
) -> str:
    entries: list[str] = []
    for item in activities:
        if item.actor_home_library_id is None or item.actor_id is None:
            actor = escape(localize(locale, item.actor_name))
        else:
            actor_path = (
                f"/admin/libraries/{escape(item.actor_home_library_id, quote=True)}/callers/"
                f"{escape(item.actor_id, quote=True)}"
            )
            actor = f'<a href="{actor_path}">{escape(item.actor_name)}</a>'
        tag = escape(item.tag_name or localize(locale, "Tag no longer available"))
        if item.tag_id is not None:
            tag_path = (
                f"/admin/libraries/{escape(item.library_id, quote=True)}/tags/"
                f"{escape(item.tag_id, quote=True)}"
            )
            tag = f'<a href="{tag_path}">{tag}</a>'
        page = escape(item.page_title or localize(locale, "Page no longer available"))
        if item.page_id is not None and item.section_id is not None and item.book_id is not None:
            section_path = (
                f"/admin/libraries/{escape(item.library_id, quote=True)}"
                f"/sections/{escape(item.section_id, quote=True)}"
            )
            if item.page_deleted:
                page_path = f"{section_path}/trash/{escape(item.page_id, quote=True)}"
            else:
                page_path = (
                    section_path
                    + f"/books/{escape(item.book_id, quote=True)}"
                    + f"/pages/{escape(item.page_id, quote=True)}"
                )
                if item.revision_number is not None:
                    page_path += f"/revisions/{item.revision_number}"
            page = f'<a href="{page_path}">{page}</a>'
        if item.action == "tag.create":
            description = f"{localize(locale, 'Created a tag')} {tag}"
        elif item.action == "tag.page.attach":
            description = (
                f"{localize(locale, 'Attached tag')} {tag} {localize(locale, 'to page')} {page}"
            )
        elif item.action == "tag.page.detach":
            description = (
                f"{localize(locale, 'Removed tag')} {tag} {localize(locale, 'from page')} {page}"
            )
        else:
            action = {
                "content.archive.create": "Created a page",
                "content.archive.revise": "Revised a page",
                "content.page.file_set.create": "Created a page",
                "content.page.file_set.revise": "Revised a page",
                "content.archive.correct_occurrence": "Corrected a page's occurrence time",
                "content.page.occurrence.correct": "Corrected a page's occurrence time",
                "content.archive.delete": "Deleted a page",
                "content.archive.restore": "Restored a page",
                "content.page.title.edit": "Changed a page title",
                "content.page.move": "Moved a page",
            }[item.action]
            description = f"{localize(locale, action)} {page}"
        entries.append(
            f"<li>{actor} {description}"
            f'<p class="meta">{_relative_time(item.occurred_at, locale)}</p></li>'
        )
    body = (
        f'<ul class="item-list">{"".join(entries)}</ul>'
        if entries
        else f"<p>{localize(locale, 'No content activity yet.')}</p>"
    )
    older = (
        f'<nav aria-label="{localize(locale, title)}">'
        f'<a href="{escape(base_path, quote=True)}?before={escape(next_cursor, quote=True)}">'
        f"{localize(locale, 'Older activity')}</a></nav>"
        if next_cursor is not None
        else ""
    )
    return f'<section class="card"><h2>{localize(locale, title)}</h2>{body}{older}</section>'


def caller_page(
    csrf_token: str,
    view: CallerView,
    *,
    locale: AdminLocale = "en",
    allow_master_actions: bool = False,
    activities: tuple[ContentActivityItem, ...] = (),
    activity_next_cursor: str | None = None,
    activity_before_cursor: str | None = None,
    message: str | None = None,
) -> str:
    status = "Identity disabled" if view.disabled_at is not None else "Identity active"
    now_micros = int(datetime.now(UTC).timestamp() * 1_000_000)
    credentials = []
    for item in view.credentials:
        if item.rotated_at is not None:
            credential_status = "Credential rotated"
        elif item.revoked_at is not None:
            credential_status = "Credential revoked"
        elif view.disabled_at is not None:
            credential_status = "Credential blocked by disabled identity"
        elif item.expires_at <= now_micros:
            credential_status = "Credential expired"
        elif item.created_at > now_micros:
            credential_status = "Credential not yet active"
        else:
            credential_status = "Credential active"
        last_used = (
            _time(item.last_used_at)
            if item.last_used_at is not None
            else localize(locale, "Never used")
        )
        revoked = (
            _time(item.revoked_at)
            if item.revoked_at is not None
            else localize(locale, "Not revoked")
        )
        rotated = (
            _time(item.rotated_at)
            if item.rotated_at is not None
            else localize(locale, "Not rotated")
        )
        if item.library_grants_policy:
            empty_grants = "无读取或写入权限" if locale == "zh-CN" else "No read or write grants."
            action_labels = {"read": "读取", "write": "写入"} if locale == "zh-CN" else {}
            grant_entries = "".join(
                "<li>"
                f"{escape(grant.library_name)} (<code>{escape(grant.library_id)}</code>)"
                f" — {action_labels.get(grant.action, escape(grant.action))}</li>"
                for grant in item.library_grants
            )
            policy_summary = (
                f"<p>{'逐知识库授权' if locale == 'zh-CN' else 'Per-Library grants'}"
                "</p>"
                + (f"<ul>{grant_entries}</ul>" if grant_entries else f"<p>{empty_grants}</p>")
            )
        else:
            policy_summary = (
                "<p>旧版分区授权；不会自动获得整库权限。</p>"
                if locale == "zh-CN"
                else "<p>Legacy Section grants; no automatic full-Library access.</p>"
            )
        reveal = ""
        if (
            view.kind == "agent"
            and allow_master_actions
            and credential_status == "Credential active"
        ):
            if item.token_tail is None:
                reveal = (
                    f'<p class="meta">{localize(locale, "This old Token cannot be recovered.")}</p>'
                )
            else:
                path = (
                    f"/admin/libraries/{view.library_id}/callers/{view.id}"
                    f"/credentials/{item.id}/reveal"
                )
                attributes = " ".join(
                    f'data-{key}-label="{escape(localize(locale, label), quote=True)}"'
                    for key, label in (
                        ("show", "Show Token"),
                        ("hide", "Hide Token"),
                        (
                            "error",
                            "Token could not be revealed. Refresh this page and check its status.",
                        ),
                        ("copied", "Token copied."),
                        (
                            "manual-copy",
                            "Clipboard unavailable. Select the displayed Token "
                            "to copy it manually.",
                        ),
                    )
                )
                reveal = (
                    f'<form class="token-reveal" method="post" action="{escape(path, quote=True)}" '
                    f"{attributes}>{_csrf(escape(csrf_token, quote=True))}"
                    f"<code>plb1…{escape(item.token_tail)}</code> "
                    '<button class="token-show" type="button">'
                    f"{localize(locale, 'Show Token')}</button> "
                    '<button class="token-copy" type="button">'
                    f"{localize(locale, 'Copy Token')}</button>"
                    '<code class="secret token-output" hidden></code>'
                    '<span role="status" aria-live="polite"></span></form>'
                )
        revoke = ""
        if (
            view.kind == "agent"
            and allow_master_actions
            and item.revoked_at is None
            and item.rotated_at is None
        ):
            path = (
                f"/admin/libraries/{view.library_id}/callers/{view.id}/credentials/{item.id}/revoke"
            )
            warning = localize(
                locale, "After revocation, this Token cannot be used or shown again."
            )
            revoke = (
                f"<details><summary>{localize(locale, 'Revoke this Token')}</summary>"
                f"<p>{warning}</p>"
                f'<p class="revoke-target">{localize(locale, "Credential ID")}: '
                f"<code>{escape(item.id)}</code></p>"
                f'<form method="post" action="{escape(path, quote=True)}">'
                f"{_csrf(escape(csrf_token, quote=True))}"
                f'<button type="submit">{localize(locale, "Revoke credential")}</button>'
                "</form></details>"
            )
        rotate = ""
        if (
            view.kind == "agent"
            and allow_master_actions
            and credential_status == "Credential active"
            and item.library_grants_policy
        ):
            path = (
                f"/admin/libraries/{view.library_id}/callers/{view.id}/credentials/{item.id}/rotate"
            )
            rotate_label = "轮换此 Token" if locale == "zh-CN" else "Rotate this Token"
            rotate_button = "确认轮换" if locale == "zh-CN" else "Rotate credential"
            ttl_field = _number(
                "credential_ttl_seconds", "Credential lifetime in seconds", 31_536_000, locale
            )
            rotate = (
                f"<details><summary>{rotate_label}</summary>"
                f"<p>{
                    '旧 Token 将立即失效，新 Token 保留上面列出的逐知识库授权。'
                    if locale == 'zh-CN'
                    else 'The old Token stops working immediately; '
                    'the new Token keeps exactly the per-Library grants listed above.'
                }</p>"
                f"<p><code>{escape(item.id)}</code></p>"
                f'<form method="post" action="{escape(path, quote=True)}">'
                f"{_csrf(escape(csrf_token, quote=True))}"
                f"{ttl_field}"
                f'<button type="submit">{rotate_button}</button>'
                "</form></details>"
            )
        edit_grants = ""
        if (
            view.kind == "agent"
            and allow_master_actions
            and credential_status == "Credential active"
            and item.library_grants_policy
        ):
            path = (
                f"/admin/libraries/{view.library_id}/callers/{view.id}/credentials/{item.id}/grants"
            )
            label = "编辑此 Token 的授权" if locale == "zh-CN" else "Edit this Token's grants"
            edit_grants = f'<p><a href="{escape(path, quote=True)}">{label}</a></p>'
        credentials.append(
            '<li class="credential-item">'
            '<p class="credential-summary">'
            f"<strong>{localize(locale, credential_status)}</strong>"
            f'<span class="meta">{localize(locale, "Expires")} {_time(item.expires_at)}</span>'
            "</p>"
            f"<details><summary>{localize(locale, 'Credential metadata')}</summary>"
            '<dl class="credential-metadata">'
            f"<dt>{localize(locale, 'Credential ID')}</dt><dd><code>{escape(item.id)}</code></dd>"
            f"<dt>{localize(locale, 'Created')}</dt><dd>{_time(item.created_at)}</dd>"
            f"<dt>{localize(locale, 'Last used')}</dt>"
            f"<dd>{last_used}</dd>"
            f"<dt>{localize(locale, 'Revoked')}</dt>"
            f"<dd>{revoked}</dd>"
            f"<dt>{localize(locale, 'Rotated')}</dt>"
            f"<dd>{rotated}</dd>"
            f"</dl>{policy_summary}</details>{reveal}{edit_grants}{rotate}{revoke}</li>"
        )
    credential_list = (
        f'<ul class="item-list">{"".join(credentials)}</ul>'
        if credentials
        else f"<p>{localize(locale, 'No credentials for this identity.')}</p>"
    )
    grants = "".join(
        "<li>"
        f"<strong>{escape(item.section_name)}</strong> "
        f"(<code>{escape(item.section_id)}</code>) — <code>{escape(item.action)}</code>"
        "</li>"
        for item in view.section_grants
    )
    grant_list = (
        f'<ul class="item-list">{grants}</ul>'
        if grants
        else f"<p>{localize(locale, 'No Section grants for this identity.')}</p>"
    )
    activity_path = (
        f"/admin/libraries/{escape(view.library_id, quote=True)}/callers/"
        f"{escape(view.id, quote=True)}"
    )
    switch_path = (
        activity_path
        if activity_before_cursor is None
        else f"{activity_path}?before={quote(activity_before_cursor, safe='')}"
    )
    identity_activity = _content_activity_timeline(
        activities,
        locale,
        title="Content activity by this identity",
        next_cursor=activity_next_cursor,
        base_path=activity_path,
    )
    body = (
        '<dl class="card">'
        f"<dt>{localize(locale, 'Identity kind')}</dt><dd>{escape(view.kind)}</dd>"
        f"<dt>{localize(locale, 'Identity description')}</dt>"
        f"<dd>{escape(view.description)}</dd>"
        f"<dt>{localize(locale, 'Status')}</dt><dd>{localize(locale, status)}</dd>"
        "</dl>"
        f'<section class="card"><h2>{localize(locale, "Existing credentials")}</h2>'
        f'<p class="section-help">{localize(locale, _CREDENTIAL_META_HELP)}</p>'
        f"{credential_list}</section>"
        f'<section class="card"><h2>{localize(locale, "Current Section grants")}</h2>'
        f'<p class="section-help">{localize(locale, _GRANT_SCOPE_HELP)}</p>'
        f"{grant_list}</section>"
        f"{identity_activity}"
        f'<p><a href="/admin">{localize(locale, "Back to activity")}</a></p>'
    )
    if allow_master_actions:
        body += (
            f'<p><a href="{escape(activity_path, quote=True)}/requests">'
            f"{localize(locale, 'API requests by this identity')}</a></p>"
        )
    if allow_master_actions and view.kind == "agent":
        path = f"/admin/libraries/{view.library_id}/callers/{view.id}/metadata"
        body += (
            f'<section class="card"><h2>{localize(locale, "Edit Agent")}</h2>'
            f'<form method="post" action="{escape(path, quote=True)}" autocomplete="off">'
            f"{_csrf(escape(csrf_token, quote=True))}"
            f'<input type="hidden" name="expected_updated_at" value="{view.updated_at}">'
            f'<label for="agent-name">{localize(locale, "Agent name")}</label>'
            f'<input id="agent-name" name="name" type="text" maxlength="200" '
            f'value="{escape(view.name, quote=True)}" required>'
            f'<label for="agent-description">{localize(locale, "Agent description")}</label>'
            '<textarea id="agent-description" name="description" maxlength="4000" rows="3">'
            f"{escape(view.description)}</textarea>"
            f'<button type="submit">{localize(locale, "Save Agent")}</button>'
            "</form></section>"
        )
    if message is not None:
        body = _notice(localize(locale, message), error=True) + body
    return _browser_document(
        csrf_token,
        locale,
        view.name,
        switch_path,
        body,
        crumbs=((localize(locale, "Libraries"), "/admin/libraries"),),
        script_src="/admin/reveal.js" if allow_master_actions and view.kind == "agent" else None,
    )


def agent_grants_page(
    csrf_token: str,
    view: CallerView,
    credential: CredentialItem,
    libraries: tuple[LibraryItem, ...],
    *,
    locale: AdminLocale = "en",
    message: str | None = None,
) -> str:
    title = "编辑 Token 授权" if locale == "zh-CN" else "Edit Token grants"
    guidance = (
        "每个知识库单独保存读取和写入权限；未勾选即无权，写入不自动包含读取。"
        "此设置只影响上方这一枚 Token。跨知识库写入目前仅文件集接口支持。"
        if locale == "zh-CN"
        else "Save read and write for each Library separately. Unchecked means no access; "
        "write does not imply read. Only this Token changes. Cross-Library writes "
        "currently work through the file-set API only."
    )
    base_path = (
        f"/admin/libraries/{view.library_id}/callers/{view.id}/credentials/{credential.id}/grants"
    )
    revisions = dict(credential.grant_revisions)
    forms = []
    for library in libraries:
        actions = {
            LibraryAction(grant.action)
            for grant in credential.library_grants
            if grant.library_id == library.id
        }
        digest = target_library_grants_digest(
            view.library_id,
            view.id,
            credential.id,
            library.id,
            actions,
            revisions.get(library.id, 0),
        )
        path = f"{base_path}/{library.id}"
        checked_read = " checked" if LibraryAction.READ in actions else ""
        checked_write = " checked" if LibraryAction.WRITE in actions else ""
        read_label = "读取" if locale == "zh-CN" else "Read"
        write_label = "写入" if locale == "zh-CN" else "Write"
        save_label = "保存此知识库" if locale == "zh-CN" else "Save this Library"
        forms.append(
            '<section class="card">'
            f"<h2>{escape(library.name)}</h2>"
            f'<form method="post" action="{escape(path, quote=True)}">'
            f"{_csrf(escape(csrf_token, quote=True))}"
            f'<input type="hidden" name="expected_digest" value="{digest}">'
            f'<label><input type="checkbox" name="read" value="true"{checked_read}> '
            f"{read_label}</label> "
            f'<label><input type="checkbox" name="write" value="true"{checked_write}> '
            f"{write_label}</label> "
            f'<button type="submit">{save_label}</button>'
            "</form></section>"
        )
    notice = "" if message is None else _notice(localize(locale, message), error=True)
    body = (
        f"<h1>{title}</h1>"
        f"<p>{escape(view.name)} · <code>{escape(credential.id)}</code></p>"
        f'<p class="section-help">{guidance}</p>'
        f"{notice}{''.join(forms)}"
        f'<p><a href="/admin/libraries/{escape(view.library_id, quote=True)}/callers/'
        f'{escape(view.id, quote=True)}">'
        f"{'返回 Agent 详情' if locale == 'zh-CN' else 'Back to Agent details'}</a></p>"
    )
    return _browser_document(
        csrf_token,
        locale,
        title,
        base_path,
        body,
        crumbs=((localize(locale, "Libraries"), "/admin/libraries"),),
    )


def callers_page(
    csrf_token: str,
    callers: tuple[CallerItem, ...],
    *,
    locale: AdminLocale = "en",
    libraries: tuple[LibraryItem, ...] = (),
    allow_master_actions: bool = False,
    message: str | None = None,
) -> str:
    entries = ""
    for item in callers:
        kind = localize(locale, "Agent / device" if item.kind == "agent" else "Operator")
        status_key = "Identity disabled" if item.disabled_at is not None else "Identity active"
        status = localize(locale, status_key)
        entries += (
            "<li>"
            f'<a href="/admin/libraries/{escape(item.library_id, quote=True)}/callers/'
            f'{escape(item.id, quote=True)}">{escape(item.name)}</a>'
            f'<p class="meta">{escape(item.library_name)} · {kind} · {status}'
            f" · {localize(locale, 'Created')}: {_time(item.created_at)}</p>"
            "</li>"
        )
    body = (
        f'<ul class="item-list">{entries}</ul>'
        if entries
        else f"<p>{localize(locale, 'No identities yet.')}</p>"
    )
    master_form = ""
    if allow_master_actions:
        options = "".join(
            f'<option value="{escape(library_item.id, quote=True)}">'
            f"{escape(library_item.name)}</option>"
            for library_item in libraries
        )
        grant_fields = []
        for library_item in libraries:
            library_id = escape(library_item.id, quote=True)
            read_label = "读取" if locale == "zh-CN" else "Read"
            write_label = "写入" if locale == "zh-CN" else "Write"
            grant_fields.append(
                "<fieldset>"
                f"<legend>{escape(library_item.name)}</legend>"
                f'<label><input type="checkbox" name="grants" value="{library_id}:read"> '
                f"{read_label}</label>"
                f'<label><input type="checkbox" name="grants" value="{library_id}:write"> '
                f"{write_label}</label>"
                "</fieldset>"
            )
        grants = "".join(grant_fields)
        guidance = (
            "先选择 Agent 的归属知识库，再明确勾选每个知识库的读取和写入权限；"
            "未勾选即无权限，写入不自动包含读取。新 Token 默认有效一年，可在详情页再次显示。"
            if locale == "zh-CN"
            else (
                "Choose the Agent's home Library, then explicitly select read and write "
                "for each Library. Unchecked means no access; write does not imply read. "
                "The new Token lasts one year by default and can be revealed again in details."
            )
        )
        ttl_field = _number(
            "credential_ttl_seconds", "Credential lifetime in seconds", 31_536_000, locale
        )
        submit_label = "创建 Agent 和 Token" if locale == "zh-CN" else "Create Agent and Token"
        master_form = (
            '<section class="card">'
            f"<h2>{'创建 Agent' if locale == 'zh-CN' else 'Create Agent'}</h2>"
            f'<p class="section-help">{guidance}</p>'
            '<form method="post" action="/admin/agents/create" autocomplete="off">'
            f"{_csrf(escape(csrf_token, quote=True))}"
            f"<label>{'归属知识库' if locale == 'zh-CN' else 'Home Library'}"
            f'<select name="home_library_id" required>{options}</select></label>'
            f"{_text('agent_name', 'Agent name', locale)}"
            f"{_textarea('agent_description', 'Agent description', locale)}"
            f"{ttl_field}"
            f"{grants}"
            f'<button type="submit">{submit_label}</button>'
            "</form></section>"
        )
    credential_notice = (
        "新签发的有效 Agent Token 可以通过主会话再次显示；旧凭据无法从校验值还原。"
        if locale == "zh-CN"
        else (
            "New active Agent Tokens can be revealed with a master session; "
            "old values cannot be recovered."
        )
    )
    notice = "" if message is None else _notice(localize(locale, message), error=True)
    content = (
        f"{_header(escape(csrf_token, quote=True), locale, switch_path='/admin/agents')}"
        '<div class="admin-shell">'
        f"{_sidebar(locale, current='agents')}"
        f"<main><h1>{localize(locale, 'Identities')}</h1>"
        f'<p class="section-help">{credential_notice}</p>'
        f"{notice}{master_form}{body}</main></div>"
    )
    return _document(localize(locale, "Identities"), content, locale)


def credential_page(
    csrf_token: str,
    *,
    heading: str,
    result: DeliveredCredential,
    locale: AdminLocale = "en",
    recoverable: bool = False,
) -> str:
    csrf = escape(csrf_token, quote=True)
    localized_heading = localize(locale, heading)
    credential_notice = (
        (
            "此值可在 Agent 详情页经主会话再次显示。请勿把它放入网址、截图、日志或聊天。"
            if locale == "zh-CN"
            else "This value can be revealed again from Agent details with a master session. "
            "Keep it out of URLs, screenshots, logs, and chat."
        )
        if recoverable
        else (
            "此值仅在本次响应中显示。离开本页前，请将它存入认可的秘密存储。"
            if locale == "zh-CN"
            else "This value is shown only in this response. Store it in an approved secret "
            "store before leaving this page."
        )
    )
    return_path = (
        f"/admin/libraries/{escape(result.library_id, quote=True)}/callers/"
        f"{escape(result.caller_id, quote=True)}"
        if recoverable
        else "/admin/setup"
    )
    return_label = localize(
        locale, "Return to administration" if recoverable else "Return to setup"
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
    <p><a href="{return_path}">{return_label}</a></p>
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
{_header(csrf, locale, switch_path="/admin/setup")}
<div class="admin-shell">
{_sidebar(locale, current="setup")}
<main>
  <section class="card">
    <h1>{escape(localized_heading)}</h1>
    {_notice(localize(locale, message))}
    <p><a href="/admin/setup">{localize(locale, "Return to setup")}</a></p>
  </section>
</main>
</div>
"""
    return _document(localized_heading, content, locale)


def guide_page(
    csrf_token: str,
    page: str,
    *,
    locale: AdminLocale = "en",
    skill_bundle: SkillBundle,
    retrieval_available: bool,
) -> str:
    csrf = escape(csrf_token, quote=True)
    if locale == "zh-CN":
        pages = {
            "guide": (
                "Operator guide",
                """
<p>首次设置主 Token 后，可按需创建知识库、分区和书籍。请把主 Token 保存在受控的
本机位置；主 Token 轮换后，旧登录会话失效。</p>
<p>旧版初始化与管理员凭据仅用于兼容；恢复管理员凭据会使此前仍有效的管理员凭据失效。</p>
<p>为每个 Agent 明确选择归属知识库与目标知识库的独立读／写权限。
请记录调用方 ID 和凭据 ID，以便之后查看、轮换或撤销凭据。</p>
<p>此面板不能更新或回滚镜像、控制 Docker、恢复备份、执行 Shell 命令或部署。
这些操作仍需通过独立的本地管理员流程完成。</p>
""",
            ),
            "agent": (
                "Agent instructions",
                """
<p><a href="/connect">打开 AI 原生接入页并复制无密钥指令</a>。
这段公开指令可交给本地 Agent；设备 Token 不会出现在指令中。</p>
<p>请在本机终端交互式输入设备 Token，不要在模型聊天中发送。Agent 会先核对
<code>/api/v1/auth/whoami</code> 与 <code>/api/v1/capabilities</code>，再用 Token 下载
Skill 文件及摘要清单。标准 HTTP 是首选，不要求安装 CLI 或 MCP。</p>
<p>支持将单份 Markdown 或多份同层文件通过同一文件集接口上传、准确回读和查看历史；
请先核对目标服务的 <code>file-sets</code> 能力。旧 Archive 接口是兼容路径。
当前页面搜索使用 <code>POST /api/v1/search</code>，支持关键词、Tag、知识库和声明时间筛选。
先检查服务的搜索能力；索引未就绪时返回 503。已安装的 CLI 或 MCP 可继续选用，
但不是接入前提。</p>
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
<p>After setting the master Token, create Libraries, Sections and Books as needed.
Keep the master Token in a controlled local store; rotating it invalidates old sessions.</p>
<p>Legacy initialization and operator credentials remain for compatibility;
recovering an operator credential invalidates prior active operator credentials.</p>
<p>Choose each Agent's home Library and explicit read/write grants for target Libraries.
Record caller and credential IDs for later reveal, rotation or revocation.</p>
<p>This console has no image update or image rollback, Docker, backup restore, shell, or
deployment controls. Those remain separate local operator procedures.</p>
""",
            ),
            "agent": (
                "Agent instructions",
                """
<p><a href="/connect">Open the AI-native connection page and copy the credential-free
instruction</a>. It is public; it never includes a device Token.</p>
<p>Enter the device Token interactively on your own machine, not in model chat.
The Agent first checks <code>/api/v1/auth/whoami</code> and
<code>/api/v1/capabilities</code>, then downloads the protected Skill files and
digest manifest. Standard HTTP is preferred; no CLI or MCP installation is required.</p>
<p>Single Markdown and multiple flat files use the same file-set upload, accurate
readback and history APIs; check the target service's <code>file-sets</code> capability
first. The old Archive API is a compatibility path. Current Page search uses
<code>POST /api/v1/search</code> with keyword, Tag, Library and declared-time filters.
Check the service's search capability; an unready index returns 503.
Existing CLI and MCP clients remain optional.</p>
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
    if page == "guide":
        body += api_guide(locale, retrieval_available=retrieval_available)
    elif page == "agent":
        body += skill_guide(locale, skill_bundle)
    else:
        body += mcp_guide(locale)
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
    message: str | None = None,
) -> str:
    cards = "".join(
        '<li><a href="/admin/libraries/'
        f'{escape(item.id, quote=True)}">{escape(item.name)}</a>'
        f"<p>{escape(item.description)}</p>"
        f'<p class="meta">{localize(locale, "Created")}: {_time(item.created_at)} · '
        f"{localize(locale, 'Page count')}: {item.page_count}</p></li>"
        for item in libraries
    )
    body = (
        f'<ul class="item-list library-grid">{cards}</ul>'
        if libraries
        else f'<p class="card">{localize(locale, "No libraries yet.")}</p>'
    )
    body += (
        '<section class="card"><h2>' + localize(locale, "Create Library") + "</h2>"
        f'<form method="post" action="/admin/libraries">{_csrf(escape(csrf_token, quote=True))}'
        f"{_text('name', 'Library name', locale)}"
        f"{_textarea('description', 'Library description', locale)}"
        f'<button type="submit">{localize(locale, "Create Library")}</button></form></section>'
    )
    if message is not None:
        body = _notice(localize(locale, message), error=True) + body
    return _browser_document(
        csrf_token, locale, localize(locale, "Libraries"), "/admin/libraries", body
    )


def library_scope_index_page(
    csrf_token: str,
    libraries: tuple[LibraryItem, ...],
    *,
    section: Literal["tags", "trash"],
    locale: AdminLocale = "en",
) -> str:
    title = "Tags" if section == "tags" else "Trash"
    description = (
        "Choose a Library to browse its Tags."
        if section == "tags"
        else "Choose a Library to browse its Trash."
    )
    cards = "".join(
        f'<li><a href="/admin/libraries/{escape(item.id, quote=True)}/{section}">'
        f"{escape(item.name)}</a></li>"
        for item in libraries
    )
    body = f'<p class="section-help">{localize(locale, description)}</p>' + (
        f'<ul class="item-list library-grid">{cards}</ul>'
        if libraries
        else f'<p class="card">{localize(locale, "No libraries yet.")}</p>'
    )
    return _browser_document(
        csrf_token,
        locale,
        localize(locale, title),
        f"/admin/{section}",
        body,
        current_section=section,
    )


def library_page(
    csrf_token: str,
    view: LibraryView,
    *,
    locale: AdminLocale = "en",
    master_mode: bool = False,
    message: str | None = None,
) -> str:
    base = f"/admin/libraries/{escape(view.library.id, quote=True)}"
    cards = "".join(
        f'<li><a href="{base}/sections/{escape(item.id, quote=True)}">'
        f"{escape(item.name)}</a><p>{escape(item.description)}</p></li>"
        for item in view.sections
    )
    body = (
        f"<p>{escape(view.library.description)}</p>"
        f'<p class="meta">{localize(locale, "Created")}: {_time(view.library.created_at)} · '
        f"{localize(locale, 'Page count')}: {view.library.page_count}</p>"
        f'<p><a href="{base}/tags">{localize(locale, "Tags")}</a></p>'
        f'<p><a href="{base}/trash">{localize(locale, "Trash")}</a></p>'
        f"<h2>{localize(locale, 'Sections')}</h2>"
        + (
            f'<ul class="item-list">{cards}</ul>'
            if view.sections
            else f'<p class="card">{localize(locale, "No sections yet.")}</p>'
        )
    )
    body += (
        '<section class="card"><h2>' + localize(locale, "Create Section") + "</h2>"
        f'<form method="post" action="{base}/sections">'
        f"{_csrf(escape(csrf_token, quote=True))}"
        f"{_text('name', 'Section name', locale)}"
        f"{_textarea('description', 'Section description', locale)}"
        f'<button type="submit">{localize(locale, "Create Section")}</button>'
        "</form></section>"
    )
    if master_mode:
        body += (
            f'<section class="card"><h2>{localize(locale, "Edit Library")}</h2>'
            f'<form method="post" action="{base}" autocomplete="off">'
            f"{_csrf(escape(csrf_token, quote=True))}"
            f'<input type="hidden" name="expected_updated_at" '
            f'value="{view.library.updated_at}">'
            f'<label for="library-name">{localize(locale, "Library name")}</label>'
            f'<input id="library-name" name="name" type="text" maxlength="200" '
            f'value="{escape(view.library.name, quote=True)}" required>'
            f'<label for="library-description">{localize(locale, "Library description")}</label>'
            f'<textarea id="library-description" name="description" maxlength="4000" rows="3">'
            f"{escape(view.library.description)}</textarea>"
            f'<button type="submit">{localize(locale, "Save Library")}</button>'
            "</form></section>"
        )
    if message is not None:
        body = _notice(localize(locale, message), error=True) + body
    return _browser_document(
        csrf_token,
        locale,
        view.library.name,
        base,
        body,
        crumbs=((localize(locale, "Libraries"), "/admin/libraries"),),
    )


def tag_directory_page(
    csrf_token: str,
    view: TagDirectoryView,
    *,
    locale: AdminLocale = "en",
    message: str | None = None,
    error: bool = False,
    master_mode: bool = False,
) -> str:
    library_path = f"/admin/libraries/{escape(view.library.id, quote=True)}"
    base = f"{library_path}/tags"
    cards = "".join(
        f'<li><a href="{base}/{escape(item.id, quote=True)}">{escape(item.name)}</a>'
        f'<p class="meta">{localize(locale, "Page count")}: {item.page_count} · '
        f"{localize(locale, 'Created')}: {_time(item.created_at)}</p></li>"
        for item in view.tags
    )
    tag_help = _MASTER_TAG_HELP if master_mode else _TAG_TOKEN_HELP
    body = (
        f'<ul class="item-list">{cards}</ul>'
        if view.tags
        else f'<p class="card">{localize(locale, "No tags yet.")}</p>'
    )
    body = (
        (_notice(localize(locale, message), error=error) if message is not None else "")
        + body
        + '<section class="card"><h2>'
        + localize(locale, "Create Tag")
        + "</h2>"
        + f'<p class="section-help">{localize(locale, tag_help)}</p>'
        + f'<form method="post" action="{base}" autocomplete="off">'
        + _csrf(escape(csrf_token, quote=True))
        + _text("name", "Tag name", locale, max_length=100)
        + ("" if master_mode else _secret("operator_token", "Current operator credential", locale))
        + f'<button type="submit">{localize(locale, "Create Tag")}</button>'
        + "</form></section>"
    )
    return _browser_document(
        csrf_token,
        locale,
        localize(locale, "Tags"),
        base,
        body,
        crumbs=(
            (localize(locale, "Libraries"), "/admin/libraries"),
            (view.library.name, library_path),
        ),
        current_section="tags",
    )


def tag_detail_page(
    csrf_token: str,
    view: TagView,
    *,
    locale: AdminLocale = "en",
    message: str | None = None,
    error: bool = False,
) -> str:
    library_path = f"/admin/libraries/{escape(view.library.id, quote=True)}"
    tag_path = f"{library_path}/tags"
    base = f"{tag_path}/{escape(view.tag.id, quote=True)}"
    cards = "".join(
        f'<li><a href="{library_path}/sections/{escape(item.section_id, quote=True)}'
        f'/books/{escape(item.book_id, quote=True)}/pages/{escape(item.id, quote=True)}">'
        f'{escape(item.title)}</a><p class="meta">'
        f"{localize(locale, 'Occurred')}: {_time(item.occurred_at)}</p></li>"
        for item in view.pages
    )
    body = (
        f'<p class="meta">{localize(locale, "Page count")}: {view.tag.page_count} · '
        f"{localize(locale, 'Created')}: {_time(view.tag.created_at)}</p>"
        f"<h2>{localize(locale, 'Tagged pages')}</h2>"
        + (
            f'<ul class="item-list">{cards}</ul>'
            if view.pages
            else f'<p class="card">{localize(locale, "No tagged pages yet.")}</p>'
        )
    )
    if message is not None:
        body = _notice(localize(locale, message), error=error) + body
    return _browser_document(
        csrf_token,
        locale,
        view.tag.name,
        base,
        body,
        crumbs=(
            (localize(locale, "Libraries"), "/admin/libraries"),
            (view.library.name, library_path),
            (localize(locale, "Tags"), tag_path),
        ),
        current_section="tags",
    )


def section_page(
    csrf_token: str,
    view: SectionView,
    *,
    locale: AdminLocale = "en",
    master_mode: bool = False,
    message: str | None = None,
) -> str:
    library_path = f"/admin/libraries/{escape(view.library.id, quote=True)}"
    base = f"{library_path}/sections/{escape(view.section.id, quote=True)}"
    cards = "".join(
        f'<li><a href="{base}/books/{escape(item.id, quote=True)}">'
        f"{escape(item.name)}</a><p>{escape(item.summary)}</p></li>"
        for item in view.books
    )
    body = (
        f"<p>{escape(view.section.description)}</p>"
        f'<p><a href="{base}/trash">{localize(locale, "Trash")}</a></p>'
        f"<h2>{localize(locale, 'Books')}</h2>"
    ) + (
        f'<ul class="item-list">{cards}</ul>'
        if view.books
        else f'<p class="card">{localize(locale, "No books yet.")}</p>'
    )
    body += (
        '<section class="card"><h2>' + localize(locale, "Create Book") + "</h2>"
        f'<form method="post" action="{base}/books">'
        f"{_csrf(escape(csrf_token, quote=True))}"
        f"{_text('name', 'Book name', locale)}"
        f"{_textarea('summary', 'Book summary', locale)}"
        f'<button type="submit">{localize(locale, "Create Book")}</button>'
        "</form></section>"
    )
    if master_mode:
        body += (
            f'<section class="card"><h2>{localize(locale, "Edit Section")}</h2>'
            f'<form method="post" action="{base}" autocomplete="off">'
            f"{_csrf(escape(csrf_token, quote=True))}"
            f'<input type="hidden" name="expected_updated_at" '
            f'value="{view.section.updated_at}">'
            f'<label for="section-name">{localize(locale, "Section name")}</label>'
            f'<input id="section-name" name="name" type="text" maxlength="200" '
            f'value="{escape(view.section.name, quote=True)}" required>'
            f'<label for="section-description">{localize(locale, "Section description")}</label>'
            f'<textarea id="section-description" name="description" maxlength="4000" rows="3">'
            f"{escape(view.section.description)}</textarea>"
            f'<button type="submit">{localize(locale, "Save Section")}</button>'
            "</form></section>"
        )
    if message is not None:
        body = _notice(localize(locale, message), error=True) + body
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


def trash_directory_page(
    csrf_token: str, view: TrashDirectoryView, *, locale: AdminLocale = "en"
) -> str:
    library_path = f"/admin/libraries/{escape(view.library.id, quote=True)}"
    base = (
        f"{library_path}/trash"
        if view.section is None
        else f"{library_path}/sections/{escape(view.section.id, quote=True)}/trash"
    )
    cards = "".join(
        f'<li><a href="{library_path}/sections/{escape(item.section_id, quote=True)}'
        f'/trash/{escape(item.id, quote=True)}">{escape(item.title)}</a>'
        f'<p class="meta">{localize(locale, "Deleted")}: {_time(item.deleted_at)} · '
        f"{localize(locale, 'Sections')}: {escape(item.section_name)} · "
        f"{localize(locale, 'Books')}: {escape(item.book_name)}</p></li>"
        for item in view.pages
    )
    metadata_note = localize(
        locale, "This view shows metadata only; page contents remain unavailable here."
    )
    body = f'<p class="section-help">{metadata_note}</p>' + (
        f'<ul class="item-list">{cards}</ul>'
        if view.pages
        else f'<p class="card">{localize(locale, "No deleted pages.")}</p>'
    )
    if view.next_cursor is not None:
        body += (
            f'<p><a href="{base}?before={escape(view.next_cursor, quote=True)}">'
            f"{localize(locale, 'Next page')}</a></p>"
        )
    crumbs: tuple[tuple[str, str], ...] = (
        (localize(locale, "Libraries"), "/admin/libraries"),
        (view.library.name, library_path),
    )
    if view.section is not None:
        crumbs += (
            (
                view.section.name,
                f"{library_path}/sections/{escape(view.section.id, quote=True)}",
            ),
        )
    return _browser_document(
        csrf_token,
        locale,
        localize(locale, "Trash"),
        base,
        body,
        crumbs=crumbs,
        current_section="trash",
    )


def trash_detail_page(
    csrf_token: str,
    view: TrashPageView,
    *,
    locale: AdminLocale = "en",
    restore_key: str,
    master_mode: bool = False,
) -> str:
    library_path = f"/admin/libraries/{escape(view.library.id, quote=True)}"
    section_path = f"{library_path}/sections/{escape(view.section.id, quote=True)}"
    base = f"{section_path}/trash/{escape(view.page.id, quote=True)}"
    metadata_note = localize(
        locale, "This view shows metadata only; page contents remain unavailable here."
    )
    body = (
        f'<p class="section-help">'
        f"{metadata_note}"
        "</p>"
        f'<dl class="card"><dt>{localize(locale, "Page type")}</dt>'
        f"<dd>{escape(view.page.page_type)}</dd>"
        f"<dt>{localize(locale, 'Sections')}</dt><dd>{escape(view.page.section_name)}</dd>"
        f"<dt>{localize(locale, 'Books')}</dt><dd>{escape(view.page.book_name)}</dd>"
        f"<dt>Page ID</dt><dd><code>{escape(view.page.id)}</code></dd>"
        f"<dt>{localize(locale, 'Occurred')}</dt><dd>{_time(view.page.occurred_at)}</dd>"
        f"<dt>{localize(locale, 'Deleted')}</dt><dd>{_time(view.page.deleted_at)}</dd>"
        f"<dt>{localize(locale, 'Current revision')}</dt>"
        f"<dd>{view.page.revision_number}</dd></dl>"
    )
    if view.restore_etag is not None:
        credential_fields = (
            ""
            if master_mode
            else (
                '<p class="section-help">'
                + localize(locale, _RESTORE_TOKEN_HELP)
                + "</p>"
                + '<input type="hidden" name="idempotency_key" '
                + f'value="{escape(restore_key, quote=True)}">'
                + _secret("operator_token", "Current operator credential", locale)
            )
        )
        body += (
            '<section class="card"><h2>'
            + localize(locale, "Restore page")
            + "</h2>"
            + '<p class="section-help">'
            + localize(locale, "Restore this page and its complete revision history.")
            + "</p>"
            + f'<form method="post" action="{base}/restore" autocomplete="off">'
            + _csrf(escape(csrf_token, quote=True))
            + '<input type="hidden" name="expected_etag" '
            + f'value="{escape(view.restore_etag, quote=True)}">'
            + credential_fields
            + f'<button type="submit">{localize(locale, "Restore page")}</button>'
            + "</form></section>"
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
            (localize(locale, "Trash"), f"{section_path}/trash"),
        ),
        current_section="trash",
    )


def restore_error_page(
    csrf_token: str,
    library_id: str,
    section_id: str,
    message: str,
    *,
    locale: AdminLocale = "en",
) -> str:
    section_path = (
        f"/admin/libraries/{escape(library_id, quote=True)}"
        f"/sections/{escape(section_id, quote=True)}"
    )
    body = _notice(localize(locale, message), error=True)
    body += f'<p><a href="{section_path}/trash">{localize(locale, "Trash")}</a></p>'
    return _browser_document(
        csrf_token,
        locale,
        localize(locale, "Restore page"),
        section_path,
        body,
        current_section="trash",
    )


def book_page(
    csrf_token: str,
    view: BookView,
    *,
    locale: AdminLocale = "en",
    master_mode: bool = False,
    message: str | None = None,
    before_cursor: str | None = None,
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
    if view.next_cursor is not None:
        next_path = f"{base}?before={quote(view.next_cursor, safe='')}"
        body += (
            f'<p><a href="{escape(next_path, quote=True)}">{localize(locale, "Next page")}</a></p>'
        )
    if master_mode:
        body = (
            f'<p><a class="button" href="{base}/new-page">'
            f"{localize(locale, 'Create a document')}</a></p>"
        ) + body
        body += (
            f'<section class="card"><h2>{localize(locale, "Edit Book")}</h2>'
            f'<form method="post" action="{base}" autocomplete="off">'
            f"{_csrf(escape(csrf_token, quote=True))}"
            f'<input type="hidden" name="expected_updated_at" value="{view.book.updated_at}">'
            f'<label for="book-name">{localize(locale, "Book name")}</label>'
            f'<input id="book-name" name="name" type="text" maxlength="200" '
            f'value="{escape(view.book.name, quote=True)}" required>'
            f'<label for="book-summary">{localize(locale, "Book summary")}</label>'
            f'<textarea id="book-summary" name="summary" maxlength="4000" rows="3">'
            f"{escape(view.book.summary)}</textarea>"
            f'<button type="submit">{localize(locale, "Save Book")}</button>'
            "</form></section>"
        )
    if message is not None:
        body = _notice(localize(locale, message), error=True) + body
    return _browser_document(
        csrf_token,
        locale,
        view.book.name,
        base if before_cursor is None else f"{base}?before={quote(before_cursor, safe='')}",
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
    message: str | None = None,
    error: bool = False,
    master_mode: bool = False,
    navigation_base: str | None = None,
    markdown_preview: MarkdownPreview | None = None,
) -> str:
    library_path = f"/admin/libraries/{escape(view.library.id, quote=True)}"
    section_path = f"{library_path}/sections/{escape(view.section.id, quote=True)}"
    book_path = f"{section_path}/books/{escape(view.book.id, quote=True)}"
    base = f"{book_path}/pages/{escape(view.page.id, quote=True)}"
    navigation = base if navigation_base is None else escape(navigation_base, quote=True)
    history_query = (
        ""
        if view.history_is_latest
        else f"?before_revision_number={view.history_before_revision_number}"
    )
    history = "".join(
        "<li"
        + (' aria-current="true"' if item.number == view.selected_revision_number else "")
        + f'><a href="{navigation}/revisions/{item.number}{history_query}">'
        f"{localize(locale, 'Version')} {item.number}</a>"
        + (
            f' <span class="selected-marker">({localize(locale, "Viewing")})</span>'
            if item.number == view.selected_revision_number
            else ""
        )
        + f'<p class="meta">{localize(locale, "Recorded")}: {_time(item.created_at)}</p></li>'
        for item in view.revisions
    )
    files = "".join(
        f'<li><a href="{base}/revisions/{view.selected_revision_number}/files/'
        f'{quote(item.name, safe="")}" download '
        f'aria-label="{escape(localize(locale, "Download file") + ": " + item.name, quote=True)}">'
        f"<code>{escape(item.name)}</code></a>"
        f'<p class="meta">{item.size_bytes} B · SHA-256: '
        f"<code>{item.sha256_hex}</code></p></li>"
        for item in view.files
    )
    current = view.selected_revision_number == view.page.revision_number
    selected_path = (
        navigation if current else f"{navigation}/revisions/{view.selected_revision_number}"
    )
    if markdown_preview is not None:
        selected_path = f"{navigation}/revisions/{view.selected_revision_number}"
    latest_path = (
        navigation
        if current
        else f"{selected_path}?before_revision_number={view.page.revision_number + 1}"
    )
    if markdown_preview is not None:
        latest_path = f"{selected_path}?before_revision_number={view.page.revision_number + 1}"
    if markdown_preview is not None and markdown_preview.selected_filename is not None:
        file_query = "preview_file=" + quote(markdown_preview.selected_filename, safe="")
        latest_path += ("&" if "?" in latest_path else "?") + file_query
        older_path = (
            f"{selected_path}?before_revision_number={view.older_revisions_before}&{file_query}"
        )
    else:
        older_path = f"{selected_path}?before_revision_number={view.older_revisions_before}"
    history_navigation = (
        f'<a href="{escape(older_path, quote=True)}">{localize(locale, "Older revisions")}</a>'
        if view.older_revisions_before is not None
        else ""
    ) + (
        f' <a href="{escape(latest_path, quote=True)}">{localize(locale, "Latest revisions")}</a>'
        if not view.history_is_latest
        else ""
    )
    heading = "Current Markdown" if current else "Markdown body"
    no_preview = localize(locale, "No safe Markdown preview is available for this version.")
    preview_body = ""
    if markdown_preview is None:
        preview_body = (
            f"<h2>{localize(locale, heading)}</h2>"
            f'<pre class="markdown-preview">{escape(view.markdown)}</pre>'
            if view.markdown is not None
            else f"<p>{no_preview}</p>"
        )
    browser_path = selected_path + history_query
    if markdown_preview is not None:
        options = "".join(
            f'<option value="{escape(name, quote=True)}"'
            + (" selected" if name == markdown_preview.selected_filename else "")
            + f">{escape(name)}</option>"
            for name in markdown_preview.filenames
        )
        label = "选择 Markdown 文件" if locale == "zh-CN" else "Choose Markdown file"
        button = "预览" if locale == "zh-CN" else "Preview"
        selector = (
            f'<form method="get" action="{selected_path}">'
            f'<label for="preview-file">{label}</label>'
            f'<select id="preview-file" name="preview_file">{options}</select>'
            f'<input type="hidden" name="lang" value="{locale}">'
            + (
                f'<input type="hidden" name="before_revision_number" '
                f'value="{view.history_before_revision_number}">'
                if not view.history_is_latest
                else ""
            )
            + f'<button type="submit">{button}</button></form>'
            if options
            else ""
        )
        selected_label = (
            f"<p><code>{escape(markdown_preview.selected_filename)}</code></p>"
            if markdown_preview.selected_filename is not None
            else ""
        )
        preview_body = (
            f"<h2>{localize(locale, heading)}</h2>"
            + selector
            + selected_label
            + (
                '<div class="markdown-preview rendered-markdown">'
                + markdown_preview.html
                + "</div>"
                if markdown_preview.html is not None
                else f"<p>{no_preview}</p>"
            )
        )
        if markdown_preview.selected_filename is not None:
            browser_path += ("&" if "?" in browser_path else "?") + (
                "preview_file=" + quote(markdown_preview.selected_filename, safe="")
            )
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
            else f'<p><a href="{navigation}">{localize(locale, "Back to current version")}</a></p>'
        )
        + preview_body
        + f"<h2>{localize(locale, 'Files in this version')}</h2>"
        f'<ul class="item-list revision-files">{files}</ul>'
        f"<h2>{localize(locale, 'Version history')}</h2>"
        f'<ul class="item-list revision-history">{history}</ul>'
        + (
            f'<nav aria-label="{localize(locale, "Version history")}">{history_navigation}</nav>'
            if history_navigation
            else ""
        )
    )
    if master_mode:
        if markdown_preview is None:
            preview_label = "安全 Markdown 预览" if locale == "zh-CN" else "Safe Markdown preview"
            body += (
                f'<p><a class="button" href="{library_path}/pages/'
                f'{escape(view.page.id, quote=True)}/revisions/{view.selected_revision_number}">'
                f"{preview_label}</a></p>"
            )
        restore_label = "恢复此版本" if locale == "zh-CN" else "Restore this revision"
        body += (
            f'<p><a class="button" href="{base}/revisions/'
            f'{view.selected_revision_number}/restore">{restore_label}</a></p>'
        )
    if current and master_mode:
        move_label = "移动文档" if locale == "zh-CN" else "Move document"
        body += (
            f'<p><a class="button" href="{base}/files/edit">'
            f"{localize(locale, 'Update files')}</a></p>"
            f'<p><a class="button" href="{base}/move">{move_label}</a></p>'
        )
        body += (
            f'<section class="card"><h2>{localize(locale, "Edit Page title")}</h2>'
            f'<form method="post" action="{base}" autocomplete="off">'
            f"{_csrf(escape(csrf_token, quote=True))}"
            f'<input type="hidden" name="expected_updated_at" value="{view.page.updated_at}">'
            f'<label for="page-title">{localize(locale, "Page title")}</label>'
            f'<input id="page-title" name="title" type="text" '
            f'value="{escape(view.page.title, quote=True)}" required>'
            f'<button type="submit">{localize(locale, "Save title")}</button>'
            "</form></section>"
        )
        time_help = localize(
            locale,
            "Use a timestamp with a timezone, for example 2026-08-13T10:00:00Z. "
            "This changes search and browse dates, not files, history or Page ID.",
        )
        body += (
            f'<section class="card"><h2>{localize(locale, "Edit declared time")}</h2>'
            f'<p class="meta">{time_help}</p>'
            f'<form method="post" action="{base}/occurrence" autocomplete="off">'
            f"{_csrf(escape(csrf_token, quote=True))}"
            f'<input type="hidden" name="expected_etag" '
            f'value="{escape(view.current_etag, quote=True)}">'
            f'<label for="page-occurrence">{localize(locale, "Declared time (RFC3339)")}</label>'
            '<input id="page-occurrence" name="occurred_at" type="text" maxlength="64" '
            f'value="{canonical_utc_wire(view.page.occurred_at)}" required>'
            f'<button type="submit">{localize(locale, "Save declared time")}</button>'
            "</form></section>"
        )
    tag_path = f"{library_path}/tags"
    if current and master_mode:
        delete_help = localize(
            locale, "Files and history are preserved. You can restore this Page from Trash."
        )
        current_etag = escape(view.current_etag, quote=True)
        body += (
            f'<section class="card"><h2>{localize(locale, "Move to trash")}</h2>'
            f'<p class="meta">{delete_help}</p>'
            f'<form method="post" action="{base}/delete" autocomplete="off">'
            f"{_csrf(escape(csrf_token, quote=True))}"
            f'<input type="hidden" name="expected_etag" value="{current_etag}">'
            '<label><input type="checkbox" name="confirm_delete" value="yes" required> '
            f"{localize(locale, 'Confirm moving this Page and all its versions to Trash.')}</label>"
            f'<button type="submit">{localize(locale, "Move to trash")}</button>'
            "</form></section>"
        )
    tag_choices = "".join(
        f'<option value="{escape(item.id, quote=True)}">{escape(item.name)}'
        + (" ✓" if item.attached else "")
        + "</option>"
        for item in view.tag_choices
    )
    if current and view.tag_choices:
        tag_help = _MASTER_TAG_HELP if master_mode else _TAG_TOKEN_HELP
        body += (
            '<section class="card"><h2>'
            + localize(locale, "Manage page tags")
            + "</h2>"
            + f'<p class="section-help">{localize(locale, tag_help)}</p>'
            + f'<form method="post" action="{base}/tags" autocomplete="off">'
            + _csrf(escape(csrf_token, quote=True))
            + f'<label for="tag_id">{localize(locale, "Choose a tag")}</label>'
            + f'<select id="tag_id" name="tag_id" required>{tag_choices}</select>'
            + (
                ""
                if master_mode
                else _secret("operator_token", "Current operator credential", locale)
            )
            + '<button type="submit" name="operation" value="attach">'
            + f"{localize(locale, 'Attach tag')}</button> "
            + '<button type="submit" name="operation" value="detach">'
            + f"{localize(locale, 'Remove tag')}</button>"
            + "</form></section>"
        )
    elif current:
        body += (
            f'<p><a href="{tag_path}">'
            f"{localize(locale, 'Create a Tag in this Library first.')}</a></p>"
        )
    if message is not None:
        body = _notice(localize(locale, message), error=error) + body
    return _browser_document(
        csrf_token,
        locale,
        view.page.title,
        browser_path,
        body,
        crumbs=(
            (localize(locale, "Libraries"), "/admin/libraries"),
            (view.library.name, library_path),
            (view.section.name, section_path),
            (view.book.name, book_path),
        ),
    )


def page_delete_error_page(
    csrf_token: str,
    library_id: str,
    section_id: str,
    message: str,
    *,
    locale: AdminLocale = "en",
) -> str:
    path = (
        f"/admin/libraries/{escape(library_id, quote=True)}"
        f"/sections/{escape(section_id, quote=True)}"
    )
    body = _notice(localize(locale, message), error=True)
    body += f'<p><a href="{path}">{localize(locale, "Sections")}</a></p>'
    return _browser_document(csrf_token, locale, localize(locale, "Move to trash"), path, body)


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
    script_src: str | None = None,
    current_section: str = "libraries",
) -> str:
    links = "".join(
        f'<a href="{escape(href, quote=True)}">{escape(label)}</a><span aria-hidden="true">/</span>'
        for label, href in crumbs
    )
    heading = escape(title)
    content = (
        f"{_header(escape(csrf_token, quote=True), locale, switch_path=path)}"
        '<div class="admin-shell">'
        f"{_sidebar(locale, current=current_section)}"
        f'<main><nav class="breadcrumb" aria-label="{localize(locale, "Breadcrumb")}">'
        f'{links}<span aria-current="page">{heading}</span></nav>'
        f"<h1>{heading}</h1>{body}</main></div>"
    )
    return _document(title, content, locale, script_src=script_src)


def _sidebar(locale: AdminLocale, *, current: str) -> str:
    entries = [
        ("home", "Home", "/admin"),
        ("libraries", "Libraries", "/admin/libraries"),
        ("search", "Search", "/admin/search"),
        ("tags", "Tags", "/admin/tags"),
        ("trash", "Trash", "/admin/trash"),
        ("agents", "Identities", "/admin/agents"),
        ("setup", "Setup and credentials", "/admin/setup"),
        ("guide", "Guide", "/admin/guide"),
        ("agent", "Agent", "/admin/agent"),
        ("mcp", "MCP", "/admin/mcp"),
    ]
    if current == "requests":
        entries.insert(5, ("requests", "API requests", "/admin/requests"))
    links = "".join(
        f'<a href="{path}"'
        + (' aria-current="page"' if key == current else "")
        + f">{escape(localize(locale, label))}</a>"
        for key, label, path in entries
    )
    return (
        f'<nav class="side-nav" aria-label="{localize(locale, "Administration sections")}">'
        f"{links}</nav>"
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
    <a href="/admin/search">{localize(locale, "Search")}</a>
    <a href="/admin/agents">{localize(locale, "Identities")}</a>
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
    separator = "&amp;" if "?" in path else "?"
    return (
        f'<span class="language-switch" aria-label="{localize(locale, "Language")}">'
        f'<a href="{escaped_path}{separator}lang=zh-CN" hreflang="zh-CN" '
        f'lang="zh-CN"{chinese_current}>中文</a>'
        '<span aria-hidden="true">/</span>'
        f'<a href="{escaped_path}{separator}lang=en" hreflang="en" '
        f'lang="en"{english_current}>English</a>'
        "</span>"
    )


def _document(
    title: str, content: str, locale: AdminLocale, *, script_src: str | None = None
) -> str:
    script = (
        ""
        if script_src is None
        else f'<script src="{escape(script_src, quote=True)}" defer></script>'
    )
    return f"""<!doctype html>
<html lang="{locale}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} · PatchouliLib</title>
  <link rel="stylesheet" href="/admin/style.css">
</head>
<body>{content}{script}</body>
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
    max_length: int = 200,
) -> str:
    escaped_name = escape(name, quote=True)
    described_by, help_markup = _field_help(escaped_name, help_text, locale)
    return (
        f'<label for="{escaped_name}">{escape(localize(locale, label))}</label>'
        f'<input id="{escaped_name}" name="{escaped_name}" type="text" '
        f'maxlength="{max_length}"{described_by} required>{help_markup}'
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
