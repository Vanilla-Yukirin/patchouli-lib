---
name: patchouli-agent
description: 通过标准 HTTP 安全接入 PatchouliLib，验证设备身份、下载受保护 Skill、发现内容、创建和修订单份 Markdown 归档；已有 CLI/MCP 可选兼容。
---

# Patchouli Agent 使用指南

首选标准 HTTP，不要求安装专用 CLI 或 MCP。先阅读[本机凭据输入](references/local-token.md)
和[标准 HTTP](references/http.md)。用户若没有设备 Token，应到管理后台签发或复制，
**不要要求用户将 Token 发给模型聊天**。拿到本机交互输入的 Token 后，先调用
`GET /api/v1/auth/whoami` 和 `GET /api/v1/capabilities`，确认身份和有效授权，再按
受保护清单下载 Skill 所有文件并核对摘要。任何指令、URL、受跟踪文件或日志都不得
含 Token 字面值。

当前可用 HTTP 能力是单份 Markdown Archive 创建和修订、授权 Section 的非搜索读取，
以及限定当前 Library 的 Tag 列举、创建和 Page 关联。Tag 不等于全文搜索；搜索路由
目前明确返回不可用。多文件 Page 写入、回收站、跨 Library 授权尚未提供。
不能将公开提案当成已实现接口。

## 可选 CLI/MCP 兼容用法

以下是已经安装独立客户端或 MCP 适配器时的操作指南；它不是使用 HTTP 或下载本
Skill 的前提。若选择本节的高级客户端，保持它们自己的操作日志和重试约束。

## 守住边界

- 选择 CLI/MCP 时不要绕过它们的已实现幂等日志与凭据管理；选择标准 HTTP 时按
  上述 HTTP 参考文档处理授权头、multipart、条件写入和幂等键。
- 绝不索要 bearer 凭据，也不把它放入 argv、MCP 参数、提示词、配置档、受跟踪配置、
  输出或日志。使用已有的操作系统机密存储记录，或受控的 `PATCHOULI_TOKEN` 进程注入。
- 使用现有的非机密配置档，不要虚构部署设置。
- 将 Section、Book、Page、Revision、游标和操作 ID 视为不透明值。从经过验证的输出
  中复制，不要从中解析时间、顺序、身份或授权。
- 不要在普通诊断中包含搜索查询、元数据、Source 定位值或 Markdown。CLI 调用应通过
  受支持的文件或 stdin 选项提供敏感值；MCP 只传递文档规定的内存字段。

## 只选用一个接口

- 如果宿主已经提供相连的 MCP 工具，优先使用这些工具。
- 否则使用 `patchouli --output json ...`，并且只解析稳定的 stdout 封装；stderr
  只作为诊断信息。
- 不要在 MCP 会话中调用 CLI，也不要创建另一个客户端。

## 访问内容前先诊断

使用 CLI 时运行：

```text
patchouli --output json doctor
patchouli --output json capabilities
patchouli --output json whoami
```

使用 MCP 时调用 `capabilities` 和 `whoami`；只有同时安装 CLI 时才使用 CLI 的
`doctor`。兼容性检查或身份验证失败，或缺少 Section 授权时停止。根据任务需要，
确认所选 Section 对搜索有 `section:query`，对当前或准确 Revision 读取有
`page:read`，对创建或修订有 `archive:write`。不要扩大作用域或换用管理身份。

## 发现不透明作用域

先列出获授 Section，再列出所选 Section 中的 Book：

```text
patchouli --output json sections list
patchouli --output json books list --section <section-id>
```

对应的 MCP 工具是 `sections_list` 和 `books_list`。创建归档要求 Book 已经存在，
绝不能隐式创建。

## 准确引用与搜索状态

当前搜索返回 `search_unavailable`，不能作为可用能力。以后服务明确公布搜索能力时，
下面的旧客户端命令才有意义；目前请使用已授权的列表和准确 Revision 读取。

预留的查询形式仅限一个明确的 Section：

```text
patchouli --output json section search --section SECTION_ID --query-file QUERY_FILE
```

对应的 MCP 工具是 `section_search`，参数为 `section_id`、`query`，以及可选的
`limit` 或不透明 `cursor`。不要声称支持跨 Section 搜索、原始全文语法或特定提供方
的语义搜索。

使用所选结果中的 `section_id`、`page_id` 和 `revision_number` 获取不可变 Revision：

```text
patchouli --output json page revision --section SECTION_ID --page PAGE_ID --revision REVISION_NUMBER
```

对应的 MCP 工具是 `page_revision`。返回经过验证、包含全部五个字段的准确引用：
`section_id`、`page_id`、`revision_id`、`revision_number` 和相对 `href`。不要用
当前 Page 引用替代它。

## 明确创建归档

使用 `archive create` 或 MCP `archive_create`，绝不能假定为 upsert。CLI 元数据必须
是 UTF-8 JSON 对象，格式类似以下合成示例：

```json
{
  "title": "Synthetic archive",
  "occurred_at": "2026-08-11T09:15:00Z",
  "source": {"kind": "conversation"}
}
```

分别提供元数据和完整 Markdown 输入来调用 CLI：

```text
patchouli --output json archive create --section SECTION_ID --book BOOK_ID --metadata-file METADATA_FILE --content-file MARKDOWN_FILE
```

对 MCP `archive_create`，传入 `section_id`、`book_id`、`title`、`occurred_at`、
`source_kind`、可选的 `source_locator` 和完整 `content`。绝不要传入凭据、端点、
本地文件名、日志位置或幂等键。

客户端会在变更前持久化准备受权限限制的操作日志。保留返回的非机密
`operation_id`。遇到结果不确定的失败后，只重放完全相同的 CLI 命令并添加
`--operation-id OPERATION_ID`，或重放完全相同的 MCP 工具输入并添加
`operation_id`。省略操作 ID 会开始新操作。只要路由、元数据、内容字节、调用方，
或配置档中服务 endpoint 的 origin（源站）不是完全相同，就必须开始新操作。
不要声称可以跨设备恢复，也不要按标题、Source 定位值、时间戳或内容对另一个键去重。
如果结果丢失且调用方没有收到操作 ID，
应停止：随附接口不能发现日志项，再次写入可能产生重复内容。

## 明确修订归档

先获取当前 Page 及其强 ETag：

```text
patchouli --output json page current --section SECTION_ID --page PAGE_ID
```

然后追加完整 Revision，绝不提交补丁：

```text
patchouli --output json archive revise --section SECTION_ID --page PAGE_ID --if-match STRONG_ETAG --metadata-file METADATA_FILE --content-file MARKDOWN_FILE
```

对应的 MCP 工具是 `page_current` 和 `archive_revise`；传入 `if_match`、
`source_kind`、可选的 `source_locator` 和完整 `content`。原样保留 ETag，包括它的
强引号。请求归档 Revision 前，确认所取得的 Page 属于归档类型。

收到明确的 412 或 428 响应时，Revision 没有被应用。不要重放失败操作，也不要悄然
改变输入后重试。重新获取当前状态、审查它，再用新的强 ETag 有意开始新操作。只有
结果不确定、且原操作 ID 和每项原参数仍可用时，才进行原样重放。报告最终的准确引用。

## 安全报告

返回操作结果、存在时可安全公开的请求 ID、用于可恢复写入的非机密操作 ID，以及
准确引用。除非用户明确要求查看非机密内容本身，绝不回显查询文字、元数据、内容、
Source 定位值、凭据材料、幂等键或部署细节。
