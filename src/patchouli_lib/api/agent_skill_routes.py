"""Public credential-free onboarding and Bearer-protected packaged Skill files."""

from hashlib import sha256
from html import escape
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import Engine

from patchouli_lib import __version__
from patchouli_lib.api.authentication import AuthenticatedRequestContext, BearerAuthentication
from patchouli_lib.api.contracts import API_V1_PREFIX, PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import resource_not_found

_SKILL_PATHS: Final = (
    "SKILL.md",
    "references/http.md",
    "references/local-token.md",
)
_RESOURCE_ROOT = "agent_resources"
_PROTECTED_HEADERS: Final = {
    "Cache-Control": PROTECTED_CACHE_CONTROL,
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}
_PUBLIC_HEADERS: Final = {
    "Cache-Control": "no-store, max-age=0",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
    ),
}
_INSTRUCTIONS: Final = """请帮我将 PatchouliLib 接入当前本地 Agent 环境。服务入口：{{BASE_URL}}

先检查本机是否已经有该服务的设备 Token。若没有，请指导我在管理后台创建或复制
设备 Token，但不要让我把 Token 发给你、放进聊天/提示词/URL/命令行参数，或上传
给模型服务。请提供适合当前操作系统的终端交互式输入方法，让我在本机亲自输入。

用标准 HTTP 和本机输入的 Bearer Token 调用 /api/v1/auth/whoami 与
/api/v1/capabilities，确认身份、说明、到期时间和实际权限。然后下载
/api/v1/agent/skill/manifest，按清单下载每个受保护的 Skill 文件，核对字节数和
SHA-256，再按当前 Agent 环境规则安装。已有本地 Skill 如有修改，先比较差异，
不要静默覆盖。优先使用标准 HTTP；不要求安装专用 CLI 或 MCP。

Token 只能保留在本机终端、进程内存或我选择的本机机密存储，不要回显或记录。
先读取下载的 SKILL.md 与相关参考，再只调用实际可用的 API。上传前可先将内容写在
本机文件中；成功后读取准确的 Page/Revision 核对。搜索、多文件等若服务尚未实现，
请明确告知我，不要编造结果或接口。"""
_CONNECT_SCRIPT: Final = """const instruction = document.getElementById('instruction');
const copyButton = document.getElementById('copy-instruction');
const status = document.getElementById('copy-status');
instruction.value = instruction.value.replace('{{BASE_URL}}', window.location.origin);
copyButton.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(instruction.value);
    status.textContent = '已复制无密钥接入指令。';
  } catch (_error) {
    instruction.select();
    status.textContent = '自动复制不可用，请手动复制已选中的文字。';
  }
});
"""


class SkillBundle:
    """A fixed allow-list of packaged immutable Skill resources."""

    def __init__(self) -> None:
        root = files("patchouli_lib").joinpath(_RESOURCE_ROOT)
        if not root.joinpath("SKILL.md").is_file():
            # Editable source checkout: the wheel force-includes the same tracked files.
            source_root = Path(__file__).resolve().parents[3]
            source_file = source_root / "src" / "patchouli_lib" / "api" / "agent_skill_routes.py"
            if (
                Path(__file__).resolve() != source_file
                or not (source_root / "pyproject.toml").is_file()
            ):
                raise RuntimeError("Packaged Agent Skill resources are missing.")
            root = source_root / "skills" / "patchouli-agent"
        self.contents = {
            path: root.joinpath(*path.split("/")).read_bytes() for path in _SKILL_PATHS
        }
        self.file_entries: list[dict[str, str | int]] = [
            {
                "path": path,
                "href": f"{API_V1_PREFIX}/agent/skill/files/{path}",
                "bytes": len(body),
                "sha256": sha256(body).hexdigest(),
            }
            for path, body in self.contents.items()
        ]
        version_bytes = "".join(
            path + ":" + sha256(body).hexdigest() + "\n" for path, body in self.contents.items()
        ).encode("ascii")
        self.version = f"{__version__}+{sha256(version_bytes).hexdigest()[:16]}"


def create_agent_skill_router(engine: Engine) -> APIRouter:
    """Create a route group without taking a dependency on the admin login state."""

    router = APIRouter()
    authenticate = BearerAuthentication(engine)
    bundle = SkillBundle()

    @router.get("/connect", include_in_schema=False)
    def connect() -> HTMLResponse:
        page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PatchouliLib · AI 原生接入</title></head><body>
<main><h1>AI 原生接入</h1>
<p>复制下面的无密钥指令，交给你的本地 Agent。设备 Token 只在本机输入，
不要发送给模型。</p>
<p>如果还没有设备 Token，请先在管理后台创建；已有 Token 无法从校验值找回。</p>
<textarea id="instruction" readonly rows="20" cols="85">{escape(_INSTRUCTIONS)}</textarea>
<p><button id="copy-instruction" type="button">复制接入指令</button>
<span id="copy-status" role="status"></span></p>
<script src="/connect.js" defer></script></main></body></html>"""
        return HTMLResponse(page, headers=_PUBLIC_HEADERS)

    @router.get("/connect.js", include_in_schema=False)
    def connect_script() -> Response:
        return Response(
            content=_CONNECT_SCRIPT,
            media_type="text/javascript",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.get(f"{API_V1_PREFIX}/agent/skill/manifest")
    def manifest(
        _context: Annotated[AuthenticatedRequestContext, Depends(authenticate)],
    ) -> JSONResponse:
        return JSONResponse(
            {"version": bundle.version, "files": bundle.file_entries},
            headers=_PROTECTED_HEADERS,
        )

    @router.get(f"{API_V1_PREFIX}/agent/skill/files/{{resource_path:path}}")
    def skill_file(
        resource_path: str,
        _context: Annotated[AuthenticatedRequestContext, Depends(authenticate)],
    ) -> Response:
        body = bundle.contents.get(resource_path)
        if body is None:
            raise resource_not_found()
        return Response(
            content=body,
            media_type="text/markdown; charset=utf-8",
            headers={
                **_PROTECTED_HEADERS,
                "Content-Disposition": f'attachment; filename="{resource_path.rsplit("/", 1)[-1]}"',
            },
        )

    return router


__all__ = ["SkillBundle", "create_agent_skill_router"]
