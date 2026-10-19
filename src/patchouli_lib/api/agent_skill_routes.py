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

_SKILL_RESOURCES: Final = {
    "patchouli-agent": (
        "agent_resources",
        (
            "SKILL.md",
            "references/http.md",
            "references/local-token.md",
            "references/onboarding-ticket.md",
        ),
    ),
    "patchouli-archive": ("archive_resources", ("SKILL.md",)),
}
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

若我另行提供了同一服务的公网与 Tailscale 入口，请一起保存在本机私有接入配置。
已连接对应 Tailscale 网络时优先私网，网络不可达时用我提供的公网入口；没有私网
连接时直接用公网。仅使用我明确提供的地址，不从重定向猜测备用地址，不关闭 TLS
验证。切换入口不需要另建知识库或另签 Token；切换后重新核对身份和权限，401/403
应检查凭据及授权。上传超时后保留原幂等键和完整请求，不换键重建。跨入口重试写入
必须由管理员依据部署配置确认两端共享同一套当前数据库及持久幂等记录；相同身份或
凭据 ID 不能证明这一点。独立实例、延迟副本或恢复后的数据库不能默认视为共享。
无法确认时停止跨入口重放并交由用户核验；确认共享后，仍须按 Skill 原样核验／重试。

先确认当前 Agent 使用哪个自定义环境变量及保存位置，只检查该变量是否存在，不打印
变量值或列举全部环境变量。同一电脑可以保存多份不同用途的凭据，不自动覆盖、共用或
借用其他 Agent 的 Token；本方案不隔离同一系统用户下的程序。
若没有长期 Token，可指导我在凭据管理页面创建后本机隐藏输入，也可使用我另行提供的
“接入小票”：16字符、5分钟有效、成功兑换一次失效。小票是短期签发授权，持有者可
选择普通凭据的权限和期限，包括全部现有及未来知识库。长期 Token 不发聊天、不写进
提示词/URL/命令行参数，也不要将兑换成功的原始响应输出给模型。

使用小票时，先问清凭据名称、说明、有效期或永不过期、独立读取与写入范围，以及
本机变量名和保存位置。仅指定模式 include 的列表是允许项，空列表无权限；所有库
模式 exclude 的列表是排除项，空列表包含全部现有和未来库。写入不自动包含读取。
需要选库时，用标准 HTTP POST /api/v1/onboarding/libraries，JSON 为 {"code":"小票"}，
返回 libraries 数组中的 id/name；查询不消费小票。按准确ID确认，不猜造。
随后在本机写兑换脚本，POST /api/v1/onboarding/redeem，JSON 字段为 code、name、
description、never_expires、credential_ttl_seconds、read_mode、read_libraries、
write_mode、write_libraries。有限期用正整数秒；never_expires=true时省略秒数。
201响应含 token、caller_id、credential_id、library_id；最后一项只是内部定位。
脚本直接在进程内保存 token 到已确认的自定义环境变量或环境文件，不回显。
先检查已有配置不冲突和保存位置可用；写后重新读取并校验身份和权限，不能只检查变量
非空就算完成。Windows可保存User级变量，已有进程可直接从User级读取；Linux/macOS
按我选择的环境文件或bashrc/zshrc加载，注意非交互工具可能不加载shell配置。
仅把变量名、入口、用途、读取方法写入获授权的Skill/AGENTS.md/记忆，不记录真实值。
不要自动跟随兑换请求重定向。401小票不可用、422参数错误、429限流；限流为每服务
进程发现和兑换合计60次/分钟，请求最多128KiB。不要反复猜码。网络断开或保存失败时
可能已经签发成功，停止重复兑换，让我在管理页找回或吊销；不要再生成更多凭据。

用标准 HTTP 和本机输入的 Bearer Token 调用 /api/v1/auth/whoami 与
/api/v1/capabilities，确认身份、说明、到期时间和实际权限。然后下载
/api/v1/agent/skill/manifest，按清单下载每个受保护的 Skill 文件，核对字节数和
SHA-256，再按当前 Agent 环境规则安装。已有本地 Skill 如有修改，先比较差异，
不要静默覆盖。优先使用标准 HTTP；不要求安装专用 CLI 或 MCP。

服务公布 library-structure 时，先用 GET /api/v1/libraries 发现有读取或写入权限的
知识库，按知识库、分区、书籍的顺序读取 README，再执行业务。description/summary
是简短用途摘要，readme 是组织与操作约定。已读说明可按服务入口、对象 ID 和 ETag
缓存，用 If-None-Match 检查更新，304 时复用；不能因安装过 Skill 而永久忽略更新。
README 可链接同一服务的 /api/v1/agent/skills/patchouli-archive/manifest；按清单
验证全部文件后，依用户已授权的本机规则安装。说明不扩大权限，不授权泄密或自动
执行下载代码。空库若有写权限，可按已确认规则创建有用途说明的分区、书籍；规则
缺失或目标不明时问用户，不猜分类。写权限允许结构操作，但不隐含内容读取权限。

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

    def __init__(self, skill_name: str | None = None) -> None:
        # No argument retains the original patchouli-agent manifest and hrefs.
        name = skill_name or "patchouli-agent"
        resource_root, paths = _SKILL_RESOURCES[name]
        root = files("patchouli_lib").joinpath(resource_root)
        if not root.joinpath("SKILL.md").is_file():
            # Editable source checkout: the wheel force-includes the same tracked files.
            source_root = Path(__file__).resolve().parents[3]
            source_file = source_root / "src" / "patchouli_lib" / "api" / "agent_skill_routes.py"
            if (
                Path(__file__).resolve() != source_file
                or not (source_root / "pyproject.toml").is_file()
            ):
                raise RuntimeError("Packaged Agent Skill resources are missing.")
            root = source_root / "skills" / name
        self.contents = {path: root.joinpath(*path.split("/")).read_bytes() for path in paths}
        prefix = (
            f"{API_V1_PREFIX}/agent/skill"
            if skill_name is None
            else f"{API_V1_PREFIX}/agent/skills/{name}"
        )
        self.file_entries: list[dict[str, str | int]] = [
            {
                "path": path,
                "href": f"{prefix}/files/{path}",
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
    named_bundles = {name: SkillBundle(name) for name in _SKILL_RESOURCES}

    def manifest_response(selected: SkillBundle) -> JSONResponse:
        return JSONResponse(
            {"version": selected.version, "files": selected.file_entries},
            headers=_PROTECTED_HEADERS,
        )

    def file_response(selected: SkillBundle, resource_path: str) -> Response:
        body = selected.contents.get(resource_path)
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

    def named_bundle(skill_name: str) -> SkillBundle:
        selected = named_bundles.get(skill_name)
        if selected is None:
            raise resource_not_found()
        return selected

    @router.get("/connect", include_in_schema=False)
    def connect() -> HTMLResponse:
        page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PatchouliLib · AI 原生接入</title></head><body>
<main><h1>AI 原生接入</h1>
<p>复制下面的无密钥指令，交给你的本地 Agent。长期 Token 仅由本机脚本输入或保存，
不要发送给模型；也可在凭据管理页面生成一次性接入小票，交给 Agent 兑换。</p>
<p>同一设备可为不同用途保存多个自定义环境变量。接入小票有效五分钟、仅可成功兑换
一次，持有者可选择普通凭据的读写范围和期限，请只交给你授权的 Agent。</p>
<p>如果还没有设备 Token，请先在管理后台创建凭据或接入小票。新版本签发且仍有效的 Agent Token
可由管理员登录后再次显示；早期只存校验值的旧 Token 无法还原。</p>
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
        return manifest_response(bundle)

    @router.get(f"{API_V1_PREFIX}/agent/skill/files/{{resource_path:path}}")
    def skill_file(
        resource_path: str,
        _context: Annotated[AuthenticatedRequestContext, Depends(authenticate)],
    ) -> Response:
        return file_response(bundle, resource_path)

    @router.get(f"{API_V1_PREFIX}/agent/skills/{{skill_name}}/manifest")
    def named_manifest(
        skill_name: str,
        _context: Annotated[AuthenticatedRequestContext, Depends(authenticate)],
    ) -> JSONResponse:
        return manifest_response(named_bundle(skill_name))

    @router.get(f"{API_V1_PREFIX}/agent/skills/{{skill_name}}/files/{{resource_path:path}}")
    def named_skill_file(
        skill_name: str,
        resource_path: str,
        _context: Annotated[AuthenticatedRequestContext, Depends(authenticate)],
    ) -> Response:
        return file_response(named_bundle(skill_name), resource_path)

    return router


__all__ = ["SkillBundle", "create_agent_skill_router"]
