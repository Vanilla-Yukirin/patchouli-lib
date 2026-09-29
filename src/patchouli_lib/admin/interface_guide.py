"""Reviewed, read-only interface descriptions for the protected admin console.

The examples below are synthetic constants. Rendering them never dispatches an
API request or uses the administrator's browser session as a bearer credential.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from html import escape
from typing import Literal

from patchouli_lib.api.agent_skill_routes import SkillBundle

GuideLocale = Literal["en", "zh-CN"]

_API_ENDPOINTS = (
    ("GET", "/api/v1/capabilities", "Service capabilities", "服务能力", "always"),
    (
        "GET",
        "/api/v1/auth/whoami",
        "Caller identity and Section grants",
        "调用方身份与分区授权",
        "always",
    ),
    ("GET", "/api/v1/sections", "Granted Sections", "已授权的分区", "retrieval"),
    (
        "GET",
        "/api/v1/sections/{section_id}/books",
        "Books in a Section",
        "分区中的书籍",
        "retrieval",
    ),
    (
        "GET",
        "/api/v1/sections/{section_id}/pages/{page_id}",
        "Current Page and Revision",
        "当前页面与版本",
        "retrieval",
    ),
    (
        "POST",
        "/api/v1/sections/{section_id}/books/{book_id}/pages",
        "Create one Markdown Archive",
        "创建单份 Markdown 归档",
        "always",
    ),
    (
        "GET",
        "/api/v1/agent/skill/manifest",
        "Protected Skill manifest",
        "受保护的 Skill 清单",
        "always",
    ),
    (
        "POST",
        "/api/v1/sections/{section_id}/search",
        "Search: unavailable (503)",
        "搜索：不可用（503）",
        "unavailable",
    ),
)

_CAPABILITIES_EXAMPLE: dict[str, object] = {
    "api_versions": ["v1"],
    "features": ["archive", "retrieval", "tags"],
    "limits": {
        "max_content_bytes": 2_097_152,
        "default_page_size": 20,
        "max_page_size": 100,
        "max_query_bytes": 4096,
    },
    "idempotency": {
        "content_mutations": True,
        "successful_replay_retention": "indefinite-alpha",
    },
}

_SEARCH_EXAMPLE: dict[str, object] = {
    "type": "about:blank",
    "title": "Service unavailable",
    "status": 503,
    "detail": "Search is temporarily unavailable.",
    "code": "search_unavailable",
    "request_id": "req_00000000000000000000000000000000",
    "details": {},
}

_MCP_SUCCESS_EXAMPLE: dict[str, object] = {
    "ok": True,
    "data": {
        "caller_id": "example-caller",
        "kind": "agent",
        "expires_at": "2030-01-01T00:00:00.000000Z",
        "policy_version": 1,
        "grants": [{"section_id": "example-section", "actions": ["page:read"]}],
    },
    "metadata": {
        "request_id": "req_00000000000000000000000000000000",
        "cache_control": ["private", "no-store"],
        "etag": None,
        "location": None,
        "idempotency_replayed": False,
    },
}

_MCP_ERROR_EXAMPLE: dict[str, object] = {
    "ok": False,
    "error": {
        "category": "service",
        "code": "search_unavailable",
        "message": "service is temporarily unavailable",
        "request_id": "req_00000000000000000000000000000000",
    },
}

_MCP_TOOLS = (
    ("capabilities", "Read capabilities", "读取服务能力"),
    ("whoami", "Read caller identity and grants", "读取调用方身份与授权"),
    ("sections_list", "List granted Sections", "列出已授权分区"),
    ("books_list", "List Books in a Section", "列出分区中的书籍"),
    ("section_search", "Unavailable while HTTP search returns 503", "HTTP 搜索返回 503，暂不可用"),
    ("page_current", "Read current Page", "读取当前页面"),
    ("page_revision", "Read an exact Revision", "读取指定版本"),
    ("archive_create", "Create one Markdown Archive", "创建单份 Markdown 归档"),
    ("archive_revise", "Append a complete Revision", "追加完整版本"),
)


def _text(locale: GuideLocale, english: str, chinese: str) -> str:
    return chinese if locale == "zh-CN" else english


def _preview(
    identifier: str,
    title: str,
    value: Mapping[str, object],
    locale: GuideLocale,
    *,
    packaged: bool = False,
) -> str:
    label = (
        _text(
            locale,
            "Packaged data — not a live HTTP response",
            "本地打包数据，非实时 HTTP 响应",
        )
        if packaged
        else _text(locale, "Synthetic response — not live data", "合成响应，非实时数据")
    )
    encoded = escape(json.dumps(value, ensure_ascii=False, indent=2))
    return (
        f'<section class="card" aria-labelledby="{identifier}">'
        f'<h3 id="{identifier}">{escape(title)}</h3>'
        f'<p class="meta">{label}</p>'
        f'<pre data-preview="{identifier}">{encoded}</pre></section>'
    )


def api_guide(locale: GuideLocale, *, retrieval_available: bool) -> str:
    capabilities_example = {
        **_CAPABILITIES_EXAMPLE,
        "features": ["archive", "retrieval", "tags"]
        if retrieval_available
        else ["archive", "tags"],
    }
    rows = "".join(
        "<li><strong>"
        + escape(method)
        + "</strong> <code>"
        + escape(path)
        + '</code><p class="meta">'
        + escape(chinese if locale == "zh-CN" else english)
        + " · "
        + (
            _text(locale, "Unavailable", "不可用")
            if status == "unavailable"
            else _text(locale, "Route registered; authorization required", "接口已注册，仍需授权")
            if status == "always" or retrieval_available
            else _text(locale, "Requires retrieval configuration", "需要检索配置")
        )
        + "</p></li>"
        for method, path, english, chinese, status in _API_ENDPOINTS
    )
    return (
        '<section aria-labelledby="api-guide"><h2 id="api-guide">API</h2>'
        "<p>"
        + _text(
            locale,
            "This is a selected, reviewed interface directory, not a request console. "
            "Content API and protected Skill downloads require a separate Bearer token; "
            "the admin login cookie does not authorize them. Confirm actual features with "
            "capabilities. This page sends no content API requests.",
            "这里是经过审核的精选接口目录，不是请求控制台。内容 API 和受保护的 Skill "
            "下载需要独立的 Bearer Token；管理登录 Cookie 不能代替它。实际能力请以 "
            "capabilities 为准。本页不会调用内容 API。",
        )
        + '</p><ul class="item-list interface-list">'
        + rows
        + "</ul></section>"
        + _preview(
            "capabilities-preview",
            "GET /api/v1/capabilities",
            capabilities_example,
            locale,
        )
        + _preview(
            "search-preview",
            "POST /api/v1/sections/{section_id}/search · 503",
            _SEARCH_EXAMPLE,
            locale,
        )
    )


def skill_guide(locale: GuideLocale, bundle: SkillBundle) -> str:
    files = "".join(
        '<details class="card"><summary><code>'
        + escape(path)
        + '</code></summary><p class="meta">'
        + _text(locale, "Packaged content, not a live download", "打包内容，非实时下载")
        + '</p><pre class="markdown-preview">'
        + escape(bundle.contents[path].decode("utf-8"))
        + "</pre></details>"
        for path in bundle.contents
    )
    manifest = {"version": bundle.version, "files": bundle.file_entries}
    return (
        '<section aria-labelledby="skill-guide"><h2 id="skill-guide">Skill</h2><p>'
        + _text(
            locale,
            "This is the packaged Skill version and its fixed file list. To install, "
            "use a device Bearer token on your own machine to download the protected "
            "manifest and files, then verify byte counts and SHA-256. Never paste a token "
            "into this page or a model conversation.",
            "这里展示打包 Skill 的版本与固定文件清单。安装时请在自己的设备上使用 Bearer "
            "Token 下载受保护的清单和文件，并核对字节数与 SHA-256。不要把 Token "
            "粘贴到本页或模型对话中。",
        )
        + "</p>"
        + _preview("skill-manifest-preview", "Skill manifest", manifest, locale, packaged=True)
        + files
        + "</section>"
    )


def mcp_guide(locale: GuideLocale) -> str:
    rows = "".join(
        f'<li><code>{name}</code><p class="meta">'
        + escape(chinese if locale == "zh-CN" else english)
        + "</p></li>"
        for name, english, chinese in _MCP_TOOLS
    )
    return (
        '<section aria-labelledby="mcp-tools"><h2 id="mcp-tools">'
        + _text(locale, "Available MCP tool names", "MCP 工具名称")
        + "</h2><p>"
        + _text(
            locale,
            "MCP uses the separate local stdio adapter. It is optional, and its tools "
            "do not accept a token, endpoint, local file path, or journal path as input. "
            "The search tool is listed for compatibility but currently returns an error.",
            "MCP 通过独立的本地 stdio 适配器提供，是可选接入方式。工具参数不接收 "
            "Token、服务地址、本地文件路径或日志路径。搜索工具保留兼容入口，但目前返回错误。",
        )
        + '</p><ul class="item-list interface-list">'
        + rows
        + "</ul></section>"
        + _preview(
            "mcp-success-preview", "whoami · structuredContent", _MCP_SUCCESS_EXAMPLE, locale
        )
        + _preview(
            "mcp-error-preview", "section_search · structuredContent", _MCP_ERROR_EXAMPLE, locale
        )
    )


__all__ = ["api_guide", "mcp_guide", "skill_guide"]
