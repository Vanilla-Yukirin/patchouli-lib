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

`whoami` 返回身份名称、说明、到期时间、调用方与凭据 ID。新服务还同时返回
`policy_mode` 与 `library_grants`；旧服务可能两个都不返回，届时权限模式是未知，
旧 `grants` 仍表示旧契约的 Section 授权，但不能据此推断新的 Library 权限。
`policy_mode` 有三种：`operator` 为管理员身份；
`legacy_section` 使用旧 `grants` 的 Section 操作，写入需 `archive:write`、读取需
`page:read`；`library_grants` 使用每个知识库的 `read`／`write` 开关，检查目标
`library_id` 对应的条目。Library 模式中的 `grants: []` 是预期形态，不代表
无权限。身份验证成功不等于接口已开放，仍应查看 `/api/v1/capabilities`；其中
没有搜索能力时不要把搜索当作已实现功能。

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
相对 `href`；不要用「当前版本」替代已选历史版本。这组旧发现列表只覆盖凭据归属
Library，不能据此发现其他 Library 的 Section／Book。跨库文件集写入虽可按精确
Library 授权执行，但目标 Library／Section／Book ID 仍需由管理员或其他已授权
来源提供并核实，不可推测或扫描。

## 统一文件集：单 Markdown 与多文件使用同一接口

先确认实际服务的 `/api/v1/capabilities` 包含 `file-sets`，并核对其中
`limits.file_set`；不能只凭本仓库代码推测目标服务已升级。本库目标先用受保护
列表确认；跨库目标的 ID 须由管理员或已授权来源提供并核实。新建与修订都上传
**完整文件集合**：
一个 Markdown 文件就是一个 `file`，Markdown 加图片是多个 `file`，只有二进制
文件也使用同一格式。Page 内仅允许同层文件名，不接受目录路径。

```text
POST /api/v1/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages
POST /api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/file-revisions
```

两个请求都带本次操作的 `Idempotency-Key` 和 Bearer 头。请求体为
`multipart/form-data`，第一部分必须是名为 `metadata` 的 `application/json`
UTF-8 对象、不得有 `filename`；后续每部分均名为 `file`，其 `filename` 是一个
安全的平面文件名，正文是该本机文件的**原始字节**。不得先把二进制文件转换为
Markdown，也不得把文件字节仅写在 JSON 中。上传顺序不决定文件集身份。

- 新建元数据：`{"title":"示例页面","source":{"kind":"manual"}}`；可选
  `occurred_at` 是带时区的 RFC 3339 时间。确实未知时省略，服务器代填并标记；
  错误时间须修正，不能用 `null` 代替省略。新建不得带 `If-Match`。
- 修订元数据：仅 `{"source":{"kind":"manual"}}`。先读取当前 Page 的强
  ETag，按原值发送 `If-Match`；缺失为 428，过期为 412。修订替换**整组**文件，
  不是追加某一文件或对 Markdown 打补丁。文件字节完全相同时返回
  `changed: false`，不新增 Revision；来源或标题校正不能借同内容修订完成。
- 每次新操作生成新的随机幂等键并在本机与原始内容一起保留。响应不确定时，只有
  目标、元数据、文件名和字节、条件头都保持不变，才可用**同一个键**原样重试；
  同键异请求返回 409。明确 412／428 后不要盲目重放。

当前状态、历史与准确文件读取：

```text
GET /api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}
GET /api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/revisions
GET /api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/revisions/{revision_id}/files
GET /api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/revisions/{revision_id}/files/{file_name}
```

当前状态返回清单与强 ETag；历史列表按版本倒序分页，列出的是身份而非文件字节。
新建响应为 201，修订响应为 200；均含准确 Page／Revision 身份、快照摘要、
每文件 `filename`、`size_bytes`、`content_sha256`，以及响应 ETag。仅新建响应的
`Location` 指向准确 Revision 的文件清单；修订应使用响应中的 ID 构造上述精确
清单地址。相同请求重放会有 `Idempotency-Replayed: true`。
按响应中的准确 Revision 读取清单，再按需下载原始字节并核对 SHA-256；不要仅凭
状态码或全库同哈希查询认定上传完成。文件下载为附件，不应在浏览器中执行。

`library_grants` 模式要求目标 Library 的明确 `write` 授权；
`legacy_section` 模式仅能在归属 Library 内、按目标 Section 的
`archive:write` 授权写入。`write` **不隐含** `read`；仅写 Token 不能自行回读
清单与文件，须由具有读取权限的身份或管理侧另行核验，不能越权尝试。旧 Section
凭据不能借新路径取得其他知识库权限。用户原始文件不得因上传成功而自动删除。

## 旧 Markdown Archive 写入（仅兼容旧客户端）

下述 `metadata`＋`content` 单 Markdown 接口不是新客户端的另一种文件集格式。
只有目标服务尚未开放 `file-sets`，且用户接受单份 Markdown 限制时，才考虑
此兼容路径；不能用它改写当前已是文件集的 Page。

`POST /api/v1/sections/{section_id}/books/{book_id}/pages` 只创建新的 Archive Page。
请求包含新的 `Idempotency-Key`，`multipart/form-data` 必须正好有两部分：

- `metadata`: `application/json`，UTF-8 JSON 对象，必须有 `title`、`source`；
  `occurred_at` 可选，提供时必须是带时区的 RFC 3339 时间。例如
  `{"title":"Synthetic archive","occurred_at":"2026-08-11T09:15:00Z","source":{"kind":"conversation"}}`。
- `content`: `text/markdown; charset=utf-8`，本机 Markdown 文件的完整字节。

仅在确实不知道发生时间时省略 `occurred_at`；服务器会用本次 UTC 时间代填，
创建响应增加 `occurrence_notice`，其中 `warning_code` 为
`occurred_at_defaulted`。显式 `null` 或错误时间返回 422。同键重试必须保持字段
仍然省略，不能把服务器返回的时间填回原请求。

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
if ("policy_mode" in identity) != ("library_grants" in identity):
    raise SystemExit("whoami 权限字段不完整；不要推测读写权限")
capabilities = json.loads(get("/api/v1/capabilities"))
print(
    json.dumps(
        {
            "name": identity["name"],
            "kind": identity["kind"],
            "policy_mode": identity.get("policy_mode", "unknown"),
            "library_grants": identity.get("library_grants"),
            "legacy_section_grants": identity["grants"],
            "features": capabilities["features"],
        },
        ensure_ascii=False,
    )
)
manifest = json.loads(get("/api/v1/agent/skill/manifest"))
# 后续按 manifest 的 files 条目下载并校验 SHA-256；不要打印 Token。
```

下例仅演示旧兼容接口如何从本机文件新建单份 Markdown 归档。把它追加到上述本机脚本中，
仅在用户明确指定目标 Section、Book、标题和文件后才调用。已知发生时间就传入；
确实未知时令 `occurred_at=None` 以省略字段。先从本机文件
读取一次正文，再将固定的 `markdown_bytes` 传入函数；`operation_key` 是本次写入的
非秘密幂等标识。若响应丢失，重试须复用相同的键、元数据和正文字节，不能重新读取
可能已变化的原文件。需要跨进程重试时，应在本机受控位置保留原始内容和键，不必
另造专有待上传包。正文长度上限由 `/api/v1/capabilities` 公布。

```python
import secrets
from pathlib import Path


def create_archive(section_id, book_id, title, occurred_at, markdown_bytes, operation_key):
    boundary = "patchouli-" + secrets.token_hex(16)
    metadata_value = {"title": title, "source": {"kind": "conversation"}}
    if occurred_at is not None:
        metadata_value["occurred_at"] = occurred_at
    metadata = json.dumps(
        metadata_value,
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

## 已实现的 Tag 接口

Tag 是当前 Library 内的标记，不改变 Page 的 Section／Book 归属。所有请求都带设备
Token。旧凭据仍按 Section 授权；启用新版 Library 授权的凭据则按目标 Library 的
`read`／`write` 开关判断。跨 Library 的实际路由范围尚需以已部署服务核对。

```text
GET    /api/v1/libraries/{library_id}/tags?q={name}&limit=20&offset=0
POST   /api/v1/libraries/{library_id}/tags
GET    /api/v1/libraries/{library_id}/tags/{tag_id}/pages?limit=20&offset=0
GET    /api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/tags
PUT    /api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/tags/{tag_id}
DELETE /api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/tags/{tag_id}
```

创建请求是 `application/json` 的 `{"name":"示例标签"}`，目前只允许本地管理员凭据。
已存在的规范化名称返回原 Tag。列表可用 `limit`、`offset` 分页，Tag 名称可用 `q`
过滤；按 Tag 列 Page、列 Page 的 Tag 也支持同样的分页参数。响应中的
`next_offset` 为 `null` 表示没有下一页。关联和解除关联没有请求体，返回
`{"changed":true}` 或 `{"changed":false}`；重复操作不会产生第二条关联。

旧 Section 模式下，Agent 列 Tag 与列 Tag 下 Page 只会看到同时获
`section:query`、`page:read` 授权的 Section 中仍有效的 Page；列指定 Page 的 Tag
需要 `page:read`，修改关联还需要 `archive:write`。Library 模式下按目标知识库的
`read`／`write` 判断，不应把一个 Library 的 Tag ID 用于另一个 Library，也不要把
Tag 列表当作全文检索结果。

本参考列出了开发分支的统一文件集接口，但它尚未合并或部署；目标服务仍须按能力
响应与实际请求核对。文件集 API 已有显式跨 Library 写入路径；旧 Archive／Tag
写路径不应据此被认为都能跨库。真实搜索仍未完成。已有 CLI/MCP 若可用，仍能
完成各自已实现的兼容流程，但并非下载本 Skill 或调用 HTTP API 的前提。
