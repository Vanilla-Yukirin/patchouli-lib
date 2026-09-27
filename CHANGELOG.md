# 变更日志

PatchouliLib 的所有重要变更都会记录在本文件中。

本文件格式基于 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)。
项目发布受支持的实现后，计划采用[语义化版本](https://semver.org/)。

## [尚未发布]

### 新增

- 初始公开产品与架构文档。
- 社区治理、贡献、支持和安全策略。
- 文档验证工作流与贡献模板。
- 带存活和就绪端点的 Python/FastAPI 服务骨架。
- SQLite/FTS5 验证及可逆的 Alembic 迁移基础设施。
- 跨平台源码、测试、迁移、文档和容器检查。
- GHCR 镜像发布、来源证明、版本发布，以及由管理员发起的私有更新辅助工具。
- 可选启用、受密码保护的 FastAPI 管理面板，用于初始化、管理员恢复、限定范围的
  Agent 配置、凭据吊销和 Agent/MCP 指引；另含与目标无关的 Nginx TLS 示例。

### 变更

- 管理面板移除固定入口地址配置和 Host 白名单，同一服务可通过多个入口访问；
  保留密码、签名会话、当前请求同源校验和 CSRF 防护。旧 `PATCHOULI_ADMIN_ORIGIN`
  会被忽略，启用面板只需密码校验值与会话签名密钥。HTTP 开关不再验证预设 IP，
  仅控制 HTTP Cookie；HTTPS 入口代理需正确传递原始协议和 Host。
- GitHub Actions 不再保存私有 SSH 部署设置，也不连接私有目标；管理员必须另行
  登录，并选择已发布镜像的准确摘要。
