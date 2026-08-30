---
name: patchouli-agent
description: 使用随附的 PatchouliLib CLI 或 stdio MCP 适配器诊断调用方访问权限、发现获授的 Section 和 Book、明确归档或修订 Markdown、执行限定 Section 的搜索，并取得准确 Revision 引用。适用于 Agent 需要操作 PatchouliLib，但不应自行实现 HTTP、身份验证、重试或幂等逻辑的场景。
---

# Patchouli Agent 使用指南

只使用已安装的 `patchouli` CLI 或已连接的 `patchouli-mcp` 工具。两者都是同一类型化
客户端和操作日志之上的展示层。

## 守住边界

- 绝不自行实现或调用原始 HTTP、授权头、multipart 正文、重试循环、幂等键或凭据
  生命周期操作。
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

## 搜索并取得准确引用

只搜索一个明确的 Section：

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
`operation_id`。省略操作 ID 会开始新操作。路由、元数据、准确内容字节、调用方或
配置档来源有任何不同，都必须开始新操作。不要声称可以跨设备恢复，也不要按标题、
Source 定位值、时间戳或内容对另一个键去重。如果结果丢失且调用方没有收到操作 ID，
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
`source_kind`、可选的 `source_locator` 和完整 `content`。准确保留 ETag，包括它的
强引号。请求归档 Revision 前，确认所取得的 Page 属于归档类型。

收到明确的 412 或 428 响应时，Revision 没有被应用。不要重放失败操作，也不要悄然
改变输入后重试。重新获取当前状态、审查它，再用新的强 ETag 有意开始新操作。只有
结果不确定、且原操作 ID 和每项原参数仍可用时，才进行准确重放。报告最终的准确引用。

## 安全报告

返回操作结果、存在时可安全公开的请求 ID、用于可恢复写入的非机密操作 ID，以及
准确引用。除非用户明确要求查看非机密内容本身，绝不回显查询文字、元数据、内容、
Source 定位值、凭据材料、幂等键或部署细节。
