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
from patchouli_lib.api.auth_contracts import (
    FILE_SET_FEATURE,
    CapabilityConfiguration,
    capabilities_response,
)
from patchouli_lib.content.file_manifest import build_file_manifest

GuideLocale = Literal["en", "zh-CN"]

_API_ENDPOINTS = (
    ("GET", "/api/v1/capabilities", "Service capabilities", "服务能力", "always"),
    (
        "GET",
        "/api/v1/auth/whoami",
        "Caller identity and effective authorization mode",
        "调用方身份与当前授权模式",
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
        "/api/v1/sections/{section_id}/pages",
        "Pages in a Section",
        "分区中的页面",
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
        "GET",
        "/api/v1/sections/{section_id}/pages/{page_id}/revisions/{revision_number}",
        "Read an exact Markdown Revision",
        "读取指定 Markdown 版本",
        "retrieval",
    ),
    (
        "GET",
        "/api/v1/sections/{section_id}/pages/{page_id}/revisions/{revision_number}/files",
        "Files in an exact Revision",
        "指定版本的文件清单",
        "retrieval",
    ),
    (
        "GET",
        "/api/v1/sections/{section_id}/pages/{page_id}/revisions/{revision_number}/files/{filename}",
        "Download a file from an exact Revision",
        "下载指定版本中的文件",
        "retrieval",
    ),
    (
        "POST",
        "/api/v1/sections/{section_id}/books/{book_id}/pages",
        "Create one Markdown Archive (legacy-compatible route)",
        "创建单份 Markdown 归档（旧版兼容接口）",
        "always",
    ),
    (
        "POST",
        "/api/v1/sections/{section_id}/pages/{page_id}/revisions",
        "Revise a Markdown Archive (legacy-compatible route)",
        "修订单份 Markdown 归档（旧版兼容接口）",
        "always",
    ),
    (
        "PATCH",
        "/api/v1/sections/{section_id}/pages/{page_id}/occurrence",
        "Correct a Page's declared occurrence time",
        "更正页面声明的发生时间",
        "always",
    ),
    (
        "DELETE",
        "/api/v1/sections/{section_id}/pages/{page_id}",
        "Move a Page to Trash",
        "将页面移入回收站",
        "always",
    ),
    (
        "POST",
        "/api/v1/sections/{section_id}/pages/{page_id}/restore",
        "Restore a Page from Trash",
        "从回收站恢复页面",
        "always",
    ),
    (
        "GET",
        "/api/v1/sections/{section_id}/trash/{page_id}",
        "Read one Page in Trash",
        "查看回收站中的指定页面",
        "always",
    ),
    (
        "GET",
        "/api/v1/sections/{section_id}/trash",
        "List Pages in Trash",
        "列出回收站中的页面",
        "retrieval",
    ),
    (
        "POST",
        "/api/v1/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages",
        "Create a flat file-set Page (one Markdown file uses the same route)",
        "创建同层文件集页面（单份 Markdown 也用此接口）",
        "always",
    ),
    (
        "POST",
        "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/file-revisions",
        "Replace the complete file set with a new Revision",
        "用新版本替换整组文件",
        "always",
    ),
    (
        "GET",
        "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}",
        "Read the current file-set manifest and ETag",
        "读取当前文件清单及 ETag",
        "always",
    ),
    (
        "GET",
        "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/revisions",
        "List file-set Revision history",
        "列出文件集版本历史",
        "always",
    ),
    (
        "GET",
        "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/revisions/{revision_id}/files",
        "List files in an exact file-set Revision",
        "列出指定文件集版本中的文件",
        "always",
    ),
    (
        "GET",
        "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/revisions/{revision_id}/files/{file_name}",
        "Safely download a file from an exact Revision",
        "安全下载指定版本中的文件",
        "always",
    ),
    (
        "GET",
        "/api/v1/libraries/{library_id}/tags",
        "List or find Tags within one Library",
        "列出或查找知识库中的标签",
        "always",
    ),
    (
        "GET",
        "/api/v1/libraries/{library_id}/tags/{tag_id}/pages",
        "List Pages carrying one Tag",
        "列出带有指定标签的页面",
        "always",
    ),
    (
        "POST",
        "/api/v1/libraries/{library_id}/tags",
        "Create a Tag in one Library",
        "在知识库中创建标签",
        "always",
    ),
    (
        "GET",
        "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/tags",
        "List a Page's Tags",
        "列出页面的标签",
        "always",
    ),
    (
        "PUT",
        "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/tags/{tag_id}",
        "Attach a Tag to a Page",
        "为页面关联标签",
        "always",
    ),
    (
        "DELETE",
        "/api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/tags/{tag_id}",
        "Detach a Tag from a Page",
        "解除页面的标签关联",
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
        "GET",
        "/api/v1/agent/skill/files/{resource_path}",
        "Download a protected Skill file",
        "下载受保护的 Skill 文件",
        "always",
    ),
    (
        "POST",
        "/api/v1/search",
        "Current-Page search (requires a rebuilt index)",
        "当前页面搜索（需先重建索引）",
        "always",
    ),
)

_SEARCH_EXAMPLE: dict[str, object] = {
    "type": "about:blank",
    "title": "Service unavailable",
    "status": 503,
    "detail": "Search is temporarily unavailable while its index is rebuilt.",
    "code": "search_unavailable",
    "request_id": "req_00000000000000000000000000000000",
    "details": {},
}

_SEARCH_V2_REQUEST_EXAMPLE: dict[str, object] = {
    "keywords": ["示例", "example"],
    "tags_any": [],
    "libraries": None,
    "occurred_from_us": None,
    "occurred_before_us": None,
    "limit": 20,
}

_SEARCH_V2_SUCCESS_EXAMPLE: dict[str, object] = {
    "items": [
        {
            "library_id": "0" * 32,
            "section_id": "1" * 32,
            "book_id": "2" * 32,
            "page_id": "example-page",
            "revision_id": "rev_" + "3" * 32,
            "revision_number": 1,
            "title": "示例页面",
            "occurred_at": 1_893_456_000_000_000,
            "match_sources": [{"kind": "title", "file_name": None}],
        }
    ]
}


def _file_set_example() -> dict[str, object]:
    """Build a synthetic wire example from the same digest rules as the API."""

    manifest = build_file_manifest([("content.md", b"# Example\n")])
    return {
        "page_id": "example-page",
        "revision_id": "rev_" + "0" * 32,
        "revision_number": 1,
        "snapshot_sha256": manifest.snapshot_sha256.hex(),
        "files": [
            {
                "filename": item.name,
                "size_bytes": item.content_size_bytes,
                "content_sha256": item.content_sha256.hex(),
            }
            for item in manifest.files
        ],
    }


_TAGS_EXAMPLE: dict[str, object] = {
    "items": [
        {
            "tag_id": "0" * 32,
            "name": "Example Tag",
            "created_at": 1_893_456_000_000_000,
            "page_count": 1,
        }
    ],
    "next_offset": None,
}

_MCP_SUCCESS_EXAMPLE: dict[str, object] = {
    "ok": True,
    "data": {
        "caller_id": "example-caller",
        "kind": "agent",
        "name": "Example Agent",
        "description": "Synthetic integration example",
        "expires_at": "2030-01-01T00:00:00.000000Z",
        "policy_version": 1,
        "policy_mode": "library_grants",
        "library_grants": [{"library_id": "example-library", "actions": ["read"]}],
        "grants": [],
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
    (
        "pages_search",
        "Search current Pages when the index is ready",
        "索引就绪后搜索当前页面",
    ),
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
    capabilities_example = capabilities_response(
        CapabilityConfiguration(
            features=("archive", FILE_SET_FEATURE, "retrieval", "tags")
            if retrieval_available
            else ("archive", FILE_SET_FEATURE, "tags"),
            content_mutation_idempotency=True,
            successful_replay_retention="indefinite-alpha",
        )
    ).model_dump(mode="json")
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
        if status != "retrieval" or retrieval_available
    )
    return (
        '<section aria-labelledby="api-guide"><h2 id="api-guide">API</h2>'
        "<p>"
        + _text(
            locale,
            "This lists the registered v1 interfaces, not a request console. "
            "Content API and protected Skill downloads require a separate Bearer token; "
            "the admin login cookie does not authorize them. Confirm actual features with "
            "capabilities. limits.max_query_bytes is a deprecated legacy field; current "
            "search uses limits.search when the index is ready. This page sends no "
            "content API requests.",
            "这里列出已注册的 v1 接口，不是请求控制台。内容 API 和受保护的 Skill "
            "下载需要独立的 Bearer Token；管理登录 Cookie 不能代替它。实际能力请以 "
            "capabilities 为准。limits.max_query_bytes 是旧接口遗留字段；当前搜索在索引就绪时"
            "使用 limits.search。本页不会调用内容 API。",
        )
        + "</p>"
        + (
            ""
            if retrieval_available
            else '<p class="meta">'
            + _text(
                locale,
                "Retrieval routes are not registered; they require retrieval configuration.",
                "检索接口尚未注册，需要检索配置。",
            )
            + "</p>"
        )
        + '<ul class="item-list interface-list">'
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
            "POST /api/v1/search · 503 before index rebuild",
            _SEARCH_EXAMPLE,
            locale,
        )
        + _preview(
            "search-v2-preview",
            "POST /api/v1/search · JSON request",
            _SEARCH_V2_REQUEST_EXAMPLE,
            locale,
        )
        + _preview(
            "search-success-preview",
            "POST /api/v1/search · 200 synthetic response",
            _SEARCH_V2_SUCCESS_EXAMPLE,
            locale,
        )
        + _preview(
            "file-set-preview",
            "GET /api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}",
            _file_set_example(),
            locale,
        )
        + _preview(
            "tags-preview",
            "GET /api/v1/libraries/{library_id}/tags",
            _TAGS_EXAMPLE,
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
            "The whoami result reports the caller name, description, policy_mode and "
            "library_grants without a credential ID. "
            "Older servers leave both fields unknown. The pages_search tool requires "
            "a ready search index; otherwise it reports search_unavailable.",
            "MCP 通过独立的本地 stdio 适配器提供，是可选接入方式。工具参数不接收 "
            "Token、服务地址、本地文件路径或日志路径。whoami 结果会报告调用方名称、"
            "说明、policy_mode 和 library_grants，但不包含凭据 ID；连接旧服务时后两个"
            "字段为未知。pages_search 需要搜索索引就绪；否则会报告 search_unavailable。",
        )
        + '</p><ul class="item-list interface-list">'
        + rows
        + "</ul></section>"
        + _preview(
            "mcp-success-preview", "whoami · structuredContent", _MCP_SUCCESS_EXAMPLE, locale
        )
        + _preview(
            "mcp-error-preview", "pages_search · structuredContent", _MCP_ERROR_EXAMPLE, locale
        )
    )


__all__ = ["api_guide", "mcp_guide", "skill_guide"]
