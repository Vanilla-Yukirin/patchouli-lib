# PatchouliLib 公开设计

本目录是 PatchouliLib 的公开设计事实源。文档描述已接受的原则、暂定契约和明确的
开放问题，不得包含私有部署细节或管理员数据。

## 当前实现状态

[PR #63](https://github.com/Vanilla-Yukirin/patchouli-lib/pull/63) 已合并至 `main`
提交 `d730c12f`；[对应主分支工作流](https://github.com/Vanilla-Yukirin/patchouli-lib/actions/runs/37132313866)
已成功并发布 OCI 镜像。当前代码能力以[路线图](../ROADMAP.md)和该 PR 为准。
各设计文档保留的“开发分支”“尚未合并”等实施切片描述记录当时的状态，不能据此
否定已合并实现。实现合并不自动接受全部 Proposed 契约，也不建立长期兼容承诺；
镜像发布不表示任何实例已更新，部署与现场验收仍是独立管理员操作。

## 索引

| 文档 | 用途 | 状态 |
| --- | --- | --- |
| [01-product-positioning.md](01-product-positioning.md) | 产品范围与原则 | 已接受方向（Accepted direction） |
| [02-library-domain-model.md](02-library-domain-model.md) | 核心实体与不变量 | 已接受方向（Accepted direction） |
| [03-page-revision-and-history.md](03-page-revision-and-history.md) | 版本、恢复与删除语义 | 已接受方向（Accepted direction） |
| [04-distillation-and-summary.md](04-distillation-and-summary.md) | 摘要与派生事实 | 部分开放（Partly open） |
| [05-retrieval-and-cloud-agent.md](05-retrieval-and-cloud-agent.md) | 检索接口与 Agent 职责 | 部分开放（Partly open） |
| [06-identifiers-and-references.md](06-identifiers-and-references.md) | 稳定 ID 与内容引用 | 部分开放（Partly open） |
| [07-authentication-and-audit.md](07-authentication-and-audit.md) | 凭据、授权与审计 | 部分开放（Partly open） |
| [08-open-questions.md](08-open-questions.md) | 决策台账 | 活跃（Active） |
| [09-automatic-organization.md](09-automatic-organization.md) | 可审查的拆分与合并建议 | 实验方向（Experimental direction） |

## 工程文档

- [实施路线图与当前状态](../ROADMAP.md)
- [下一阶段改造清单](next-phase-todo.md)：区分已合并能力、验收证据和后置事项。
- [下一阶段管理后台、内容模型与 Agent 接入设计](proposals/next-phase-product-and-api.md)：
  产品目标已确认，接口、存储、安全和迁移方案仍为提案中。
- [当前 Page 搜索 v2 提案](proposals/current-page-search-v2.md)：跨知识库字符倒排、
  筛选、排序与索引恢复的待审契约；不代表生产搜索已启用。
- [主会话网页文件集写入与重试记录](proposals/master-web-file-set-writes.md)：网页
  上传、修订和成功回执已合并，提案的长期兼容状态独立管理。
- [主会话网页历史文件组恢复](proposals/master-web-revision-restore.md)：复用现有
  修订和成功回执的已合并实现；不是数据库恢复，也不改变历史或 Page 归属。
- [同库 Page 移动](proposals/master-web-page-move.md)：稳定 Page 身份、历史路径证明、
  原成功重试及备份兼容已合并。
- [开发、验证与交付](development-and-delivery.md)
- [搜索索引的显式启用与重建](search-index-maintenance.md)：本地维护命令、停写、
  失败回滚及恢复后的就绪核查；迁移本身不会自动启用搜索。
- [网页管理面板](admin-web-console.md) / [简体中文兼容文件](admin-web-console.zh-CN.md)
- [Agent 贡献工作流](agent-contribution-workflow.md)
- [ADR 0001：实现与交付基线](decisions/0001-implementation-baseline.md)
- [ADR 0002：由管理员发起的私有更新](decisions/0002-manual-private-updates.md)
- [ADR 0003：受限的网页管理面板](decisions/0003-admin-web-console.md)
- [ADR 0004：Page 文件字节保存在 SQLite](decisions/0004-page-file-storage.md)
- [管理面板入口简化提案](proposals/admin-request-origin.md)

ADR 0004 确定多文件的 SQLite 存储方向；主分支已实现统一文件集写入、逐库授权及
受保护的 Token 再次显示。旧 Archive 与旧 Section 凭据保留兼容边界，旧搜索入口
已明确退役；不能把新实现的存在等同于所有旧接口都具有新能力。读者应结合路线图、
公开代码和逐项验证记录判断**当前行为**，实现状态与提案的稳定兼容状态分别管理。

## 状态词汇

- **已接受方向（Accepted direction）**：足以稳定地指导原型，但在首次受支持版本
  发布前仍可能变化。
- **部分开放（Partly open）**：核心意图已经接受；契约细节仍需公开提案确定。
- **实验方向（Experimental direction）**：有价值的假设，成为兼容性承诺前必须
  经过评估。
- **活跃（Active）**：决定尚未解决；实现不得悄然选择一个永久答案。

## 修改设计

若改动会影响实体、存储不变量、授权或公开接口，请使用设计提案 Issue 模板。提案
应说明问题、约束、备选方案、迁移影响，以及安全或隐私后果。
