# Page 版本、历史与恢复

> 当前实现只支持单份 Markdown 正文的 Revision。维护者提出的多文件完整快照、
> 相同内容不新增 Revision、声明时间校正的公开接口和回收站界面见
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
旧幂等请求仍重放原响应，旧备份也仍按原算法校验。这个并发基础不等于已经提供声明
时间校正的公开接口，更不表示分支已经合并或部署。

## 移动内容

把 Page 移到另一个 Book 只改变 Page 元数据，不会创建副本或重写 Revision 正文。
移动本身是一条审计事件。

## 软删除

普通删除会设置 `deleted_at` 等墓碑标记。已删除的 Page 不出现在默认读取和搜索中，
但获授权的操作者可以恢复它。

## 管理性擦除

不可变历史不应意味着泄漏的凭据、非法内容或个人数据永远无法删除。后续实现必须
提供少见且严格授权的擦除路径，包含影响预览、审计元数据和备份指引。发布受支持
版本前必须确定擦除语义。

## 备份与导出

必须提供应用层导出和经过测试的恢复。版本控制工具可以保存导出的文本快照，但它们
不是数据库事务引擎，也不能被描述为唯一的灾难恢复机制。
