# 现有标准 HTTP 接口

根地址由用户正在使用的服务入口提供；例子中的 `PATCHOULI_BASE_URL` 应是该入口的
`http://` 或 `https://` origin，不包含 `/admin`。不要把真实部署地址写入 Skill。
所有下面的 API 请求都使用 `Authorization: Bearer <设备 Token>` 请求头。不要把
Token 放进 URL、命令行参数或模型对话。

## 先核对身份和能力

```text
GET /api/v1/auth/whoami
GET /api/v1/capabilities
```

`whoami` 返回身份名称、说明、到期时间、调用方与凭据 ID，以及当前实际授予的
Section 操作。Agent 写入需要目标 Section 的 `archive:write`；读取需要 `page:read`。
`capabilities` 中没有搜索能力时不要调用搜索当作已实现功能。

受保护的 Skill 资源：

```text
GET /api/v1/agent/skill/manifest
GET /api/v1/agent/skill/files/SKILL.md
GET /api/v1/agent/skill/files/references/http.md
GET /api/v1/agent/skill/files/references/local-token.md
```

清单给每个文件的字节数和 SHA-256。只下载清单列出的相对路径；逐字节核对后安装。
Token 无效或到期时返回 401，不应尝试用网页登录 Cookie 代替设备 Token。

## 已实现的发现与读取

仅在服务启用了读取能力时使用：

```text
GET /api/v1/sections
GET /api/v1/sections/{section_id}/books
GET /api/v1/sections/{section_id}/pages
GET /api/v1/sections/{section_id}/pages/{page_id}
GET /api/v1/sections/{section_id}/pages/{page_id}/revisions/{revision_number}
```

列表可能分页，按返回的 `next_cursor` 继续，不自行编造游标。引用准确 Revision 时
保留服务器返回的 `section_id`、`page_id`、`revision_id`、`revision_number` 和
相对 `href`；不要用「当前版本」替代已选历史版本。

## 当前 Markdown Archive 写入

`POST /api/v1/sections/{section_id}/books/{book_id}/pages` 只创建新的 Archive Page。
请求包含新的 `Idempotency-Key`，`multipart/form-data` 必须正好有两部分：

- `metadata`: `application/json`，UTF-8 JSON 对象，字段正好是 `title`、
  `occurred_at`（带时区的 RFC 3339 时间）和 `source`，例如
  `{"title":"Synthetic archive","occurred_at":"2026-08-11T09:15:00Z","source":{"kind":"conversation"}}`。
- `content`: `text/markdown; charset=utf-8`，本机 Markdown 文件的完整字节。

修订既有 Archive Page 使用
`POST /api/v1/sections/{section_id}/pages/{page_id}/revisions`；先 GET 当前 Page
取得强 ETag，写入时传 `If-Match` 原值、新 `Idempotency-Key`，以及同样两部分，
但 `metadata` 正好只包含 `source`。提交完整正文而非补丁。`412` 或 `428` 表示
当前条件未满足，重新读取并人工判断，不自动覆盖。创建和修订都必须在结果不确定时
保留原键与原始字节原样重试；改任何字段就用新键。成功后读取响应指向的准确
Revision，核对标题、正文与引用。不要在成功核验前删除本次生成的临时副本。

## 本机 Python 标准库示例

先把以下代码写进本机的 `patchouli_local.py`，将根地址交由用户通过环境变量设置。
脚本在没有环境变量时会交互式输入 Token。不要把 Token 或真实地址回传给模型。

```python
import getpass
import json
import os
import urllib.request
from urllib.parse import urlsplit

base = (os.environ.get("PATCHOULI_BASE_URL") or input("Patchouli base URL: ").strip()).rstrip("/")
parts = urlsplit(base)
if (
    parts.scheme not in {"http", "https"}
    or not parts.netloc
    or parts.username is not None
    or parts.password is not None
    or parts.path
    or parts.query
    or parts.fragment
):
    raise SystemExit("PATCHOULI_BASE_URL 必须是服务入口的 origin")
token = os.environ.get("PATCHOULI_TOKEN") or getpass.getpass("Patchouli device Token: ")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)


def get(path):
    request = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + token})
    with opener.open(request, timeout=15) as response:
        return response.read()


identity = json.loads(get("/api/v1/auth/whoami"))
capabilities = json.loads(get("/api/v1/capabilities"))
print(
    json.dumps(
        {
            "name": identity["name"],
            "kind": identity["kind"],
            "grants": identity["grants"],
            "features": capabilities["features"],
        },
        ensure_ascii=False,
    )
)
manifest = json.loads(get("/api/v1/agent/skill/manifest"))
# 后续按 manifest 的 files 条目下载并校验 SHA-256；不要打印 Token。
```

下例演示如何从本机文件新建单份 Markdown 归档。把它追加到上述本机脚本中，
仅在用户明确指定目标 Section、Book、标题、发生时间和文件后才调用。先从本机文件
读取一次正文，再将固定的 `markdown_bytes` 传入函数；`operation_key` 是本次写入的
非秘密幂等标识。若响应丢失，重试须复用相同的键、元数据和正文字节，不能重新读取
可能已变化的原文件。需要跨进程重试时，应在本机受控位置保留原始内容和键，不必
另造专有待上传包。正文长度上限由 `/api/v1/capabilities` 公布。

```python
import secrets
from pathlib import Path


def create_archive(section_id, book_id, title, occurred_at, markdown_bytes, operation_key):
    boundary = "patchouli-" + secrets.token_hex(16)
    metadata = json.dumps(
        {"title": title, "occurred_at": occurred_at, "source": {"kind": "conversation"}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    marker = boundary.encode("ascii")
    body = b"".join(
        [
            b"--" + marker + b"\r\n",
            b'Content-Disposition: form-data; name="metadata"\r\n',
            b"Content-Type: application/json\r\n\r\n",
            metadata,
            b"\r\n--" + marker + b"\r\n",
            b'Content-Disposition: form-data; name="content"\r\n',
            b"Content-Type: text/markdown; charset=utf-8\r\n\r\n",
            markdown_bytes,
            b"\r\n--" + marker + b"--\r\n",
        ]
    )
    request = urllib.request.Request(
        base + "/api/v1/sections/" + section_id + "/books/" + book_id + "/pages",
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Idempotency-Key": operation_key,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        },
        method="POST",
    )
    with opener.open(request, timeout=30) as response:
        result = json.loads(response.read())
        return result, response.headers["Location"], response.headers["ETag"]


# 在实际调用前只读一次：markdown_bytes = Path(markdown_path).read_bytes()。
# 先生成并保留同一个 key：secrets.token_hex(24)。
# 重试时复用原 markdown_bytes 和 key，不重新读取可能已变化的文件。
```

上例中 `source.kind` 只是合成的对话归档例子；其他来源要按实际允许的类型填写。
字节与元数据必须和重试时保持相同。成功后按响应引用读取准确 Revision 进行核对，
不是只看 `201`。修订同理，但还要从当前 Page 获取强 ETag，并传 `If-Match`；
`metadata` 只含 `source`。

Bash 和 PowerShell 的交互式输入方式见 [本机凭据输入](local-token.md)，两者都可运行
同一标准库脚本。后续上传程序应从本机文件读取 Markdown 与元数据、在进程内存里
组装 multipart、通过请求头提交随机幂等键；不要把敏感正文拼进 Shell 参数。

目前没有多文件 Page 上传、Tag、回收站、跨 Library 授权或真实搜索；对应目标仍见
公开设计提案。已有 CLI/MCP 若可用，仍能完成其已实现的单份 Markdown 流程，
但并非下载本 Skill 或调用 HTTP API 的前提。
