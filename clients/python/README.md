# PatchouliLib Python 客户端

本目录是可独立构建的 Python 分发包，其中包含类型化的 PatchouliLib Agent HTTP
wire protocol 实现。它有意不安装 PatchouliLib 服务器运行时。

Alpha 阶段的接口范围包括：

- `/api/v1` 能力与调用方自述；
- Section、Book、Page、Revision、游标和准确引用模型；
- 限定 Section 的 POST 搜索；
- 明确的归档创建和 Revision 追加操作；
- RFC 9457 Problem Details 和受保护响应头；
- 次数有限、理解幂等语义的传输重试。

这个类型化 wire protocol 接受 call-scoped bearer 令牌：每次调用时显式传入，不由
客户端持久保存。内置 CLI 还提供非机密配置档、安全的凭据输入抽象、确定性输出和
退出码，以及受权限限制的操作日志。可选 MCP 适配器通过本地 stdio 提供相同的客户端
和日志语义。Agent Skill 和真实网络端到端支持属于后续层次。

## 命令行（CLI）

安装软件包后运行 `patchouli --help`。配置档是带版本的 TOML，默认使用 HTTPS URL
origin（源站）和兼容性版本。这里的 origin 只含 scheme、host 和可选 port：

```toml
version = 1

[profiles.default]
endpoint = "https://library.example.invalid"
api_version = "v1"
```

Windows 上的默认配置档路径遵循 `%APPDATA%`，POSIX 上遵循 `$XDG_CONFIG_HOME`
（或 `~/.config`）。`PATCHOULI_CONFIG_FILE`、`PATCHOULI_PROFILE`、
`PATCHOULI_ENDPOINT` 和 `PATCHOULI_API_VERSION` 提供非机密的进程级覆盖值。配置
若含令牌等未知字段会被拒绝。读取配置文件时只通过经过验证且非重解析的句柄执行一次；
端点与 bearer 凭据一起使用前，文件及已有的上级目录链必须具有可信所有者和权限。

### 加密私网 HTTP 入口

如果服务只通过已建立的加密私网访问，可在指定配置档中加入
`allow_private_http = true`，并将 `endpoint` 改成管理员给出的精确 HTTP IP 入口。
进程环境变量 `PATCHOULI_ALLOW_PRIVATE_HTTP=true` 也可开启，`false` 可明确关闭；
它同时适用于 CLI 和 MCP。默认仍只接受 HTTPS。

直接使用 Python 时，向 `PatchouliClient(endpoint, allow_private_http=True)` 传入
相同选择。HTTP 仅接受私有 IPv4、共享地址段、回环或 IPv6 唯一本地 IP，不接受域名、
公网 IP 或通配地址。软件不会仅凭 IP 判定网络已经加密，部署方必须保证访问路径。

HTTP 请求不读取环境代理配置，也不跟随重定向，以免把 Token 送到外部代理或另一入口。
Token、限定权限、输入保护、重试和幂等规则不变。HTTPS 的证书验证和代理行为不变。

### 凭据和调用

调用方凭据没有命令行选项，解析顺序为：

1. 对没有其他 stdin 输入的命令明确选择 `--token-stdin`；
2. 注入的 `PATCHOULI_TOKEN` 环境变量；
3. 可选的操作系统密钥环记录，服务名为 `patchouli-client`，账号等于配置档名称。

使用 `patchouli-client[secret-store]` 安装可选密钥环适配器。未安装时，CLI 明确只从
环境变量或 stdin 读取凭据，绝不会写入明文令牌文件。

命令范围如下：

```text
patchouli doctor
patchouli capabilities
patchouli whoami
patchouli sections list [--limit N] [--cursor CURSOR]
patchouli books list --section SECTION [--limit N] [--cursor CURSOR]
patchouli pages list --section SECTION [--limit N] [--cursor CURSOR]
patchouli search (--query-file FILE | --query-stdin)
patchouli page current --section SECTION --page PAGE
patchouli page revision --section SECTION --page PAGE --revision NUMBER
patchouli archive create --section SECTION --book BOOK \
  (--metadata-file FILE | --metadata-stdin) \
  (--content-file FILE | --content-stdin)
patchouli archive revise --section SECTION --page PAGE --if-match '"strong-etag"' \
  (--metadata-file FILE | --metadata-stdin) \
  (--content-file FILE | --content-stdin)
```

搜索查询、归档元数据和 Markdown 内容只允许从文件或 stdin 输入。包括令牌在内，最多
只有一个值可以占用 stdin。文件输入必须位于当前目录或
`--input-root`/`PATCHOULI_INPUT_ROOT` 下；读取器会拒绝符号链接、重解析点、非普通
文件、路径越界、NUL、适用场景下无效的 UTF-8，以及超大输入。本地文件名绝不进入
请求元数据、幂等指纹、输出或诊断。每个路径组成部分都相对于已经验证的目录句柄遍历，
因此并发替换路径名无法重定向已打开的读取。生产入口以二进制模式读取 stdin，避免
Markdown 正文字节（包括换行符）被终端文本层规范化。

搜索输入现在是 JSON 对象，例如
`{"keywords":["技术","报告"],"tags_any":[],"libraries":null,"limit":20}`。
可选时间字段 `occurred_from_us`／`occurred_before_us` 使用 UTC Unix 微秒。
旧 `section search --section ...` 命令及其游标／片段响应已退役；请改用跨知识库
`search`，返回当前 Page 的准确 Revision ID／序号和命中来源。
服务未完成索引重建时会返回 `search_unavailable`，不是空结果。
能力响应中的 `limits.max_query_bytes` 是兼容旧客户端保留的字段，不能用于截断新搜索；
新搜索的请求、关键词及数组上限以索引就绪后公布的 `limits.search` 为准。

`--output json` 会向 stdout 写入稳定的成功封装。供人阅读的输出也只用 stdout 返回
数据。所有诊断都使用 stderr；错误输出经过脱敏，绝不显示内容、元数据、bearer 令牌
或幂等键。

在第一次创建或修订请求前，CLI 会把生成的幂等键写入受权限限制的操作日志。成功或
失败输出包含非机密的操作 UUID。以 `--operation-id UUID` 重新运行相同命令可复用
该键；路由、元数据、内容或 `If-Match` 只要有任何差异，包括配置档中服务 endpoint 的
origin（源站）发生变化，都会在本地被拒绝。每次尝试都会先解析 `whoami`；稳定调用方
ID 会写入日志和指纹，因此同一调用方可以轮换凭据，但不同调用方会在变更前被拒绝。
凭据 ID 和 bearer 值绝不写入日志。创建和替换期间，日志写入会同步文件及所在目录。
POSIX 模式和受保护的 Windows
DACL 确保状态仅当前用户可读写，日志访问始终绑定到经过验证的句柄。412 或 428 不会
应用 Revision：原样重放必须保留原日志和参数，而新 ETag 需要新操作。日志丢失后，
不保证跨设备去重。

退出状态是确定的：

| 状态 | 类别 |
| ---: | --- |
| 0 | 成功 |
| 2 | 命令用法 |
| 3 | 配置档或配置错误 |
| 4 | 调用方凭据输入或存储错误 |
| 5 | 操作日志安全问题或不匹配 |
| 10 | 身份验证失败 |
| 11 | 作用域不足 |
| 12 | 资源不存在或不可见 |
| 13 | 幂等冲突 |
| 14 | 412/428 Revision 前置条件失败 |
| 15 | 本地或服务器校验失败，包括 413/415/422 |
| 16 | 速率限制或临时服务故障 |
| 17 | 达到上限的传输故障 |
| 18 | 外层边缘网关或不符合约定的上游错误 |
| 19 | wire protocol 或应用协议故障 |
| 70 | 按失败关闭的内部错误 |
| 130 | 操作被中断 |

## MCP 适配器

安装 `patchouli-client[mcp]`，并配置与 CLI 相同的非机密配置档及环境变量或操作系统
密钥环凭据，然后让 MCP 宿主指向 `patchouli-mcp` 可执行文件。它不接受命令行参数，
只使用 stdio，绝不打开 TCP 监听器。

适配器提供以下工具：

```text
capabilities
whoami
sections_list
books_list
pages_search
page_current
page_revision
archive_create
archive_revise
```

工具绝不接受凭据、端点、日志路径、幂等键或本地文件路径。`pages_search` 接受
结构化关键词、Tag 身份、时间范围、Library 范围和 Top K；Markdown 内容是有大小
限制的内存 JSON 字符串。归档创建和修订是两个独立工具；修订需要强
`if_match`。返回的非机密 `operation_id` 可以提供给同一写入工具进行原样重放。
调用方无关的日志绑定会在 `whoami` 前于本地检查，稳定调用方则在变更前检查。原始
操作键始终是私有日志状态。

一个 `PatchouliClient` 在整个 stdio 会话中共享，并在会话结束时关闭。适配器把 HTTP、
身份验证头、重试、multipart 编码、响应验证和写入编排交给已合并的客户端/应用层。
工具失败时返回稳定、脱敏的 MCP 错误，不含响应正文、查询、内容、Source 定位值、
端点或底层异常细节。

MCP 可选依赖使用官方 MIT 许可的 Python SDK 稳定 v1 系列，并限制在 v2 以下。全部
stdout 字节都属于 MCP 帧；经过清理的启动诊断使用 stderr。

## 开发

在本目录中运行：

```sh
uv sync --frozen --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
uv run python scripts/verify_artifacts.py
```

所有示例和测试都使用合成数据及确定性的模拟传输。制品检查会确认两个分发包都包含
MIT 许可证文件，并在核心元数据中声明该许可证。
