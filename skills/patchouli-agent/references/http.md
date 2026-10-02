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

Section 和 Book 列表可用可选的 `library_id` 选择目标知识库，复用同一 GET 路径：

```text
GET /api/v1/sections?library_id={library_id}&limit=20
GET /api/v1/sections/{section_id}/books?library_id={library_id}&limit=20
```

省略 `library_id` 仍只访问凭据归属 Library，不会自动合并全部已授权库。显式传入
归属 Library 与省略等价。`library_grants` 模式需要该精确凭据对目标 Library 的
`read`；`write` 不隐含读取，原有 Section grant 也不能代替新版 Library 授权。
`legacy_section` 模式只能访问归属库，Section 列表仍只显示有 `section:query` 的
分区，Book 列表仍需该分区的 `section:query`。无目标读取权限或旧模式跨库返回
`403 insufficient_scope`，已授权目标库中不存在的 Section 返回
`404 resource_not_found`。这两列表仍只返回原有字段：Section 的 `section_id`、
`name`，Book 的 `section_id`、`book_id`、`title`，不会返回内容正文。

列表按 ID 升序，用 `limit`（默认 20，范围 1–100）和返回的 `next_cursor` 继续；
`next_cursor: null` 表示末页，不自行编造游标。继续翻页必须保留相同目标 Library、
凭据、路径、Section 和 `limit`；省略与显式归属库可互换。游标跨目标或凭据返回
`400 invalid_cursor`，每页仍核对当前授权，旧游标不能绕过撤销。两列表加强了
Library／精确凭据绑定，因此升级前取得的旧游标须重新从第一页开始。`library_id`
必须是完整的 32 位小写十六进制 ID，空值、重复参数和未知列表参数返回
`422 request_validation_failed`。此查询参数不适用于旧 Page 列表或单 Page／Revision
读取；跨库 Page 发现使用以下显式 Library 路径，准确文件读取仍使用下方文件集路径。

```text
GET /api/v1/libraries/{library_id}/sections/{section_id}/pages?limit=20
GET /api/v1/libraries/{library_id}/pages/{page_id}
```

调用这两个入口前，确认 `capabilities.features` 包含 `retrieval`；它们仅在服务配置
检索游标签名密钥时注册。只有 `search` 或 `file-sets` 能力不代表这两个入口可用；
缺少 `retrieval` 时的 404 不能据此判定 Page 不存在或没有读取权限。

新列表按 Page ID 升序，默认 20 项、最多 100 项；以 `next_cursor` 在相同 Library、
Section、凭据和 `limit` 下继续，末页为 `null`。稳定 Page ID 查询不要求事先知道
当前 Section；Page 移动后可查得新 Section／Book。两者返回的是当前 Page 元数据，
包含当前归属及 `current_files_href`、`revision_files_href`，不包含文件字节，也不
是写入所需的当前强 ETag。下载前继续按返回的准确路径读取清单，并重新核对授权；
移动、删除或撤销权限可能让旧链接失效。Library 模式需要目标库 `read`（仅有
`write` 不够）；旧 Section 模式不跨归属库，列表需目标 Section 的
`section:query`，稳定 Page 查询需其**当前位置**的 `page:read`，不会因为持有旧
位置授权而扩权。旧模式跨库、明确仅写或目标 Page 不存在时，新发现路径返回 404；
已撤销凭据返回 401，目标库没有可用凭据也不能据错误码推断其是否存在。旧
Section／Book 查询参数跨库的 403 行为保持不变。新列表游标不能与旧 Page 列表
或其他目标、凭据、`limit` 互换；不匹配时返回 `400 invalid_cursor`。

先从 `whoami.library_grants` 确认有 `read` 的目标 Library，再发现其中的 Section／Book。
仅有 `write` 时，目标 ID 仍须由管理员或其他已授权来源提供并核实，不能扫描或猜造。
引用准确 Revision 时保留服务器返回的 `section_id`、`page_id`、`revision_id`、
`revision_number` 和相对 `href`；不要用「当前版本」替代已选历史版本。开发分支
接口不代表目标服务已部署；旧服务不支持此选择器时不得假定返回结果属于目标库。

## 当前 Page 搜索

仅当能力响应包含 `search` 时，发送受保护的 `POST /api/v1/search`，正文为 JSON：

```json
{
  "keywords": ["技术", "报告"],
  "tags_any": [],
  "libraries": null,
  "occurred_from_us": null,
  "occurred_before_us": null,
  "limit": 20,
  "cursor": null
}
```

关键词数组按任一项命中；每项是同一字段内的连续字面片段，不自动分词或纠错。
`tags_any` 若非空，元素必须同时提供 `library_id` 与 `tag_id`，至少命中其中
一个 Tag；Tag 与关键词组合时两种条件都要满足。`libraries: null` 表示凭据
实际可读的全部知识库；显式列出无权访问的库或不存在／不可见的 Tag 不会扩大权限。
时间为文档声明时间的 UTC Unix 微秒，范围是左闭右开，任一端可为 `null`。
关键词、Tag、时间条件不能全部为空；不填条件时改用浏览列表。返回按 Page 排序
的 `items`，每项携带 Library、Section、Book、Page、准确 Revision ID／序号、
标题、声明时间、命中字段、准确 `revision_files_href` 与可用时的 `snippet`。
`snippet` 为 `{file_name, text, matched}` 或 `null`：`text` 至多 240 个 Unicode
字符，是已授权当前版本的**规范化索引文本**，不等同于文件原始字节；二进制独占
Page 可没有摘录。`matched: false` 表示摘录文本本身未命中（例如标题命中）；
不要把摘录当完整正文或渲染为可信 HTML／Markdown。启用续页时，页级 `next_cursor`
为 `null` 表示末页；未配置游标签名密钥时，`null` 只表示不提供续页，不证明结果已穷尽。
返回非空游标时，保持同一查询、凭据及 `limit`，把返回值放进下一次 POST JSON 的
`cursor` 字段，不能修改或放进 URL。查询、权限、可见结果或索引世代变化以及游标
篡改均可能返回 `400 invalid_cursor`；此时不带 `cursor` 从第一页重新搜索，不能
把两次不一致的页拼起来。
能力响应的 `limits.search` 给出 JSON 正文字节、关键词合计字节及数组项数上限；
旧字段 `max_query_bytes` 不代表结构化搜索的正文上限。显式 `libraries: []`
是无效范围，不等同于 `null`。
索引未就绪时返回 `search_unavailable`，不得当作无结果。搜索词只放请求正文，
不要放 URL、普通诊断或日志中。生产配置要求检索游标签名密钥；本仓库开发／测试
配置未启用该密钥时可能仅返回首批结果，目标服务行为须按实际能力和响应核对。

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

只有 Agent 为本次上传专门新建的临时副本，才可在上述准确 Revision 清单及全部
文件字节摘要核验成功后清理；确认目标是该临时副本，不扩大到它的父目录或其他文件。
响应不确定、校验失败或只有写权限且尚未获得独立回读结果时，保留原文件与重试信息。
全库存在同哈希文件、单独收到成功状态码，都不能代替本次准确上传的验证。

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

## 同一知识库内移动 Page

仅在服务公布 `page-move` 能力时使用：

```text
POST /api/v1/libraries/{library_id}/pages/{page_id}/move
```

使用 `library_grants` 模式的 Agent Token，需目标库的 `write`；旧 Section 凭据和
operator 不适用。源和目标必须同属一个 Library，可以跨 Section／Book。此操作
保留 Page ID、全部文件和历史、标签、标题与声明时间，不生成内容 Revision。

请求带原 `Idempotency-Key`、当前强 `If-Match` 和 `Content-Type: application/json`；
JSON 只含以下四个字段，替换为已核实的实际 ID（以下全部为合成值）：

```json
{
  "source_section_id": "11111111111111111111111111111111",
  "source_book_id": "22222222222222222222222222222222",
  "target_section_id": "33333333333333333333333333333333",
  "target_book_id": "44444444444444444444444444444444"
}
```

有 `read` 且服务还公布 `retrieval` 时，可先用稳定 Page 查询确认当前位置，再从当前
文件集接口取得强 ETag。没有 `retrieval` 但已知准确 Section／Page 路径时，直接用
当前文件集 GET 取得 ETag；位置不明则请已授权来源提供，不把缺少发现接口的 404
当作 Page 不存在。只有 `write` 时由已授权来源提供准确位置与 ETag，不会因此开放内容读取。
成功返回 200、`changed`、源／目标 ID、未变的 Revision 信息和结果 ETag。同目标
返回 `changed=false`，也会记住这次成功。请求结果不确定时原样重试，不能自动换源、
换目标、换键或换 ETag；旧版本冲突返回 412，应重新核对而不是强行覆盖。

`Idempotency-Replayed: true` 表示返回原成功，即使页面后来再次移动或进入回收站，
也不会再次移动或恢复它。原成功描述当时的位置；需要当前位置时，若服务公布
`retrieval`，可重新做获授权的稳定 Page 查询；否则按上段从已授权来源确认。
旧深层链接不会自动改写。现有 SDK／CLI／MCP 的服务能力透传不代表它们
已有移动方法或工具；此功能使用本节的标准 HTTP 请求。

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

创建请求是 `application/json` 的 `{"name":"示例标签"}`。`library_grants` 模式的
Agent 在目标 Library 有 `write` 时可创建新 Tag，成功返回 201；若规范化名称已存在，
还必须有该 Library 的 `read` 才能返回原 Tag（200）。只有写权限而同名已存在时返回
403，不应改用管理员凭据绕过。旧 Section 模式的 Agent 不能创建 Tag；兼容的
operator 仍按原有权限使用此入口。列表可用 `limit`、`offset` 分页，Tag 名称可用 `q`
过滤；按 Tag 列 Page、列 Page 的 Tag 也支持同样的分页参数。响应中的
`next_offset` 为 `null` 表示没有下一页。关联和解除关联没有请求体，返回
`{"changed":true}` 或 `{"changed":false}`；重复操作不会产生第二条关联。

### 上传后给 Page 添加多个标签

文件上传与打标签是独立请求，不必为了标签重新上传文件。使用同时拥有目标 Library
`read` 与 `write` 的设备 Token，先保存上传响应的 `page_id`，再按以下顺序操作：

1. 对每个所需名称调用 `POST /api/v1/libraries/{library_id}/tags`，例如
   `{"name":"开发"}`、`{"name":"归档"}`；保存响应 `tag_id`。新名称返回 201，
   同一规范化名称返回已有 Tag 和 200，不创建同名副本。
2. 对每个 Tag 调用上表的 Page Tag `PUT`，不带请求体。这是追加关联，不会覆盖
   Page 已有的其他标签；一个 Page 可以关联多个 Tag。
3. 用 Page Tag `GET` 核对结果。若中途失败，保留已上传的 Page，只重试未完成的
   标签请求；重复创建／关联不会产生同名标签或重复关联，不要重新创建 Page。
4. 搜索时向 `POST /api/v1/search` 传
   `{"tags_any":[{"library_id":"目标库 ID","tag_id":"标签 ID"}]}`，多个标签
   放在同一数组中表示命中任意一个。解除关联用 `DELETE`；不会删除 Tag 定义、
   Page 文件或历史版本。标签变化不生成文件内容的新 Revision。

以上请求不构成一次整体事务：上传成功但标签失败时，应分别报告状态。暂不自动推断
或推荐标签；使用用户指定、任务明确要求或已确认分类中的标签。

旧 Section 模式下，Agent 列 Tag 与列 Tag 下 Page 只会看到同时获
`section:query`、`page:read` 授权的 Section 中仍有效的 Page；列指定 Page 的 Tag
需要 `page:read`，修改关联还需要 `archive:write`。Library 模式下按目标知识库的
`read`／`write` 判断，不应把一个 Library 的 Tag ID 用于另一个 Library，也不要把
Tag 列表当作全文检索结果。

本参考列出了开发分支的统一文件集接口，但它尚未合并或部署；目标服务仍须按能力
响应与实际请求核对。文件集及 Tag API 已支持按精确凭据的目标 Library 授权跨库
读写；旧 Archive 写路径仍只限归属库。搜索已在开发分支实现，目标服务是否已合并、
部署及重建索引必须实际核对。已有 CLI/MCP 若可用，仍能
完成各自已实现的兼容流程，但并非下载本 Skill 或调用 HTTP API 的前提。
