# Page 版本、历史与恢复

> 当前实现只支持单份 Markdown 正文的 Revision。维护者提出的多文件完整快照、
> 相同内容不新增 Revision 和回收站界面见
> [下一阶段提案](proposals/next-phase-product-and-api.md)；其中仅软删除方向已在
> 本文接受，不能把未来接口当作已上线。

内部数据库从 `20260929_0007` 起为每个既有 Revision 镜像一条 `content.md` 文件记录，
`20260929_0008` 起封存旧版单文件集合，并为后续同级多文件保存预留结构。当前公开写入
仍只处理原来的 Markdown，不能据此宣称多文件 Page 已可用。

持有相应 Section 的 Page 读取权限的 Agent 可以按准确的 Page 与 Revision 获取文件清单
（`GET /api/v1/sections/{section_id}/pages/{page_id}/revisions/{revision_number}/files`），
或在该路径后附加经 URL 编码的文件名下载文件。当前清单只有 `content.md`；响应包含文件名、
字节数和 SHA-256。下载以附件形式返回原始字节，不进行 Markdown 渲染或文件类型推断。
软删除 Page 不会通过这些接口重新暴露，响应禁止缓存。文件清单与下载并不改变现有
Markdown 正文读取接口。

## 不变量

- 普通编辑会创建新的不可变 Revision。
- 一个 Page 只有一个当前 Revision 指针。
- 默认读取和搜索只使用当前 Revision。
- 历史 Revision 只能通过明确的历史操作访问。
- 创建 Revision 和更新当前指针必须在同一事务中完成。

## 版本号

Revision 编号在一个 Page 内递增：`1`、`2`、`3`，依此类推。它们是方便人类阅读的
局部序号，不是全局标识符或时间戳。

## 恢复

恢复会依据早期正文创建一个新 Revision，而不是把当前指针向后移动。

```text
Revision 2 (historical content)
    |
    | restore with an explanatory message
    v
Revision 6 (new current revision)
```

这样既能保持历史线性，也会记录恢复操作确实发生过。

## 并发写入

服务通过单一 API 对写入进行串行化处理，绝不丢弃已提交的 Revision。后续被接受的
写入可以成为当前版本，较早的写入则继续保留在历史中。

API 应支持可选的预期当前值，例如 Revision 编号或实体标签。值不匹配时可返回冲突，
而不是悄然接受基于过期输入的写入。具体默认行为记录在
[08-open-questions.md](08-open-questions.md) 中。

本开发分支的当前 Page 强 ETag 为 `page-v2`，同时绑定 Page 身份、当前 Revision、
声明发生时间及严格递增的服务器更新时间。新修订必须使用最新 ETag；历史 `page-v1`
格式的 `If-Match` 在新请求中返回 412，调用方应重新读取 Page 后重试。已经成功保存的
旧幂等请求仍重放原响应，旧备份也仍按原算法校验。本开发分支另外实现了声明时间
校正接口；这些改动尚未合并或部署。

## 声明时间校正（本开发分支）

`PATCH /api/v1/sections/{section_id}/pages/{page_id}/occurrence` 仅处理现存、未删除的
Archive Page。请求须携带本 Section 的 `archive:write` bearer 凭据、`Idempotency-Key`、
当前 `page-v2` `If-Match`，正文为 `application/json`：

```json
{"occurred_at":"2026-08-13T10:00:00.123456Z"}
```

时间按 RFC 3339 解析并以 UTC 规范形式保存。成功返回 200、新 ETag、Page 的稳定 ID、
校正前后的时间及当前 Revision 引用；不会创建新的 Revision，也不会返回正文。相同
幂等请求重放原响应；旧 ETag 或并发修改返回 412；时间与当前值完全相同则返回 409；
错误格式返回 422。数据库同时记录不可变校正链和审计，备份校验会核对该校正与
幂等响应。

本开发分支创建 Archive Page 时允许省略 `occurred_at`；服务在事务内查询幂等重放后，
以本次服务器 UTC 时间代填，并在创建响应中附加
`occurrence_notice: {"source":"server_utc","warning_code":"occurred_at_defaulted"}`。
同键重试重放原响应，不重新取时间；显式传入 `null` 或错误时间仍返回 422。该提示只在
创建响应及其幂等记录中保存，普通 Page 读取仍仅展示实际声明时间，不声称保留了
“当年代填”的独立元数据。

## 移动内容

把 Page 移到另一个 Book 只改变 Page 元数据，不会创建副本或重写 Revision 正文。
移动本身是一条审计事件。

## 软删除

普通删除会设置 `deleted_at` 等墓碑标记。已删除的 Page 不出现在默认读取和搜索中，
但获授权的操作者可以恢复它。

本开发分支为 Archive Page 增加了删除和恢复接口（尚未合并或部署）：

- `DELETE /api/v1/sections/{section_id}/pages/{page_id}` 将整个 Page 移入回收站；
  `POST /api/v1/sections/{section_id}/pages/{page_id}/restore` 恢复同一 Page ID。
  两者均要求本 Section 的 `archive:write`、`Idempotency-Key`、当前强 `If-Match`
  和空请求正文；成功返回 200、新 ETag、状态、删除时间与当前 Revision 引用。
- 同键同请求重试重放原成功响应；过期 ETag 返回 412，已处于目标状态返回 409。
  删除和恢复不创建 Revision，不修改历史正文、Source 或 Tag；数据库记录连续、
  不可变的状态事件和成功审计。若旧数据库已有无法说明来源的删除标记，新迁移
  拒绝为其虚构事件史。
- 删除后，新修订与新声明时间校正均返回 404；删除前已成功的相同幂等请求仍可
  重放原成功响应，不会再次改动已删除 Page。
- `GET /api/v1/sections/{section_id}/trash` 列出本 Section 的已删除 Page，
  同时要求 `archive:write` 与 `section:query`；带 Page ID 的回收站详情还要求
  `page:read`。写权限本身不授予枚举或读取权限。回收站只提供状态和引用，
  不在此返回正文；恢复后仍可按原 Page ID 浏览已有 Revision。

回收站读取的分页游标需要服务端配置签名密钥；未配置该密钥时不会注册列表路由。
正常 Page 的读取与检索继续排除删除标记。

## 管理性擦除

不可变历史不应意味着泄漏的凭据、非法内容或个人数据永远无法删除。后续实现必须
提供少见且严格授权的擦除路径，包含影响预览、审计元数据和备份指引。发布受支持
版本前必须确定擦除语义。

## 备份与导出

必须提供应用层导出和经过测试的恢复。版本控制工具可以保存导出的文本快照，但它们
不是数据库事务引擎，也不能被描述为唯一的灾难恢复机制。
