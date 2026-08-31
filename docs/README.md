# PatchouliLib 公开设计

本目录是 PatchouliLib 的公开设计事实源。文档描述已接受的原则、暂定契约和明确的
开放问题，不得包含私有部署细节或管理员数据。

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
- [开发、验证与交付](development-and-delivery.md)
- [网页管理面板](admin-web-console.md) / [简体中文兼容文件](admin-web-console.zh-CN.md)
- [Agent 贡献工作流](agent-contribution-workflow.md)
- [ADR 0001：实现与交付基线](decisions/0001-implementation-baseline.md)
- [ADR 0002：由管理员发起的私有更新](decisions/0002-manual-private-updates.md)
- [ADR 0003：受限的网页管理面板](decisions/0003-admin-web-console.md)

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
