# 为 PatchouliLib 做贡献

感谢你帮助完善 PatchouliLib。知识领域仍处于设计阶段，因此清晰的使用场景或失败
方式通常比大规模实现更有价值。

## 开始之前

1. 阅读[公开设计索引](docs/README.md)和[开放问题](docs/08-open-questions.md)。
2. 新建提案前，先搜索现有 Issue 和 Discussion。
3. 若改动会影响领域模型、持久化不变量、安全模型或公开 API，请先提交设计提案。

小型文档修正可以直接提交 Pull Request。

## 公开数据边界

只使用合成示例或明确公开的示例。请勿提交：

- 凭据、令牌、私钥、Cookie 或连接字符串；
- 个人主机名、用户名、电子邮箱、文件系统路径或 IP 地址；
- 私有对话归档、生产日志、数据库转储或部署拓扑；
- 未经人类贡献者审查的生成内容。

如果提案源自真实事件，请用最小化的合成复现替换所有可识别信息。

## 开发环境

安装 Python 3.13、uv、Node.js 24 和 npm，然后运行完整的非容器验证：

```sh
python scripts/validate.py
```

发布或交付相关改动还应启动 Docker，并运行
`python scripts/validate.py --container`。详情见
[开发、验证与交付](docs/development-and-delivery.md)。

AI 辅助或并行贡献还必须遵守 [Agent 贡献工作流](docs/agent-contribution-workflow.md)，
其中规定了任务所有权、共享 worktree 安全、独立审查和交接证据。

## 提交 Pull Request

- 每个 Pull Request 只包含一项连贯改动。
- 说明改了什么、为何要改，以及影响了哪个设计不变量。
- 行为发生变化时，同步更新相关设计文档、决策记录和变更日志。
- 附上验证结果或可复现的验证命令。
- 提交主题应简短并使用祈使语气；建议遵循 Conventional Commits。
- 确认差异中不含私有信息或管理员专用信息。

如果共识讨论和实现需要分别审查，维护者可能要求拆分提案。
