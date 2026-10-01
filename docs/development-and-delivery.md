# 开发、验证与交付

本文定义公开的工程路径。管理员专用主机、路径、端口、域名和凭据应留在仓库之外。

## 前置条件

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Node.js 24 和 npm
- 用于容器验证的 Docker 与 Compose

## 一条命令完成验证

运行全部源码、测试、迁移和文档检查：

```sh
python scripts/validate.py
```

追加 OCI 构建和回环健康烟雾测试：

```sh
python scripts/validate.py --container
```

容器烟雾测试会向 Docker 申请临时回环端口，不会终止或复用已经占用首选端口的进程。

验证契约包括：

1. 按锁文件同步依赖；
2. Ruff 格式和 lint 检查；
3. 严格 MyPy 检查；
4. pytest，并要求最低 90% 覆盖率；
5. 在临时数据库上执行 Alembic 升级、降级和重复升级；
6. 可复现的 npm 安装和公开 Markdown lint；
7. 可选的镜像构建、启动时迁移和就绪状态烟雾测试。

Python 客户端的独立验证在 `clients/python` 执行：

```sh
uv sync --frozen --all-groups
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy
uv run --frozen pytest
uv build
uv run --frozen python scripts/verify_artifacts.py
```

客户端要求最低 95% 覆盖率；构建的 wheel 和源码包必须保留许可证，并不得包含
本地 `.tmp-*` 测试目录。源码包排除规则和制品检查均覆盖这项边界。
若本机默认测试目录的父目录权限不满足客户端的真实安全检查，可新建独立的可信目录，
将其绝对路径通过仅用于测试的 `PATCHOULI_TEST_TRUSTED_TMP_ROOT` 环境变量提供；
fixture 只创建及清理该根下本次独有的子目录，不修改根目录权限或产品配置检查。
未设置时保留原平台行为；该变量不是产品运行时配置。
服务端源码包也排除任意层的 `.tmp-*` 目录；构建不会遍历这些目录，不删除本地
材料，迁移、Skill 和合成验证所需的公开资源仍随源码包保留。

## 从源码运行

```sh
uv sync --frozen --all-groups
uv run alembic upgrade head
uv run uvicorn patchouli_lib.app:app --reload
```

默认开发数据库是 `data/patchouli.db`。需要本地覆盖配置时，将 `.env.example` 复制到
已忽略的 `.env` 文件。

归档写入接口无需游标密钥即可使用。只有
`PATCHOULI_RETRIEVAL_CURSOR_SIGNING_SECRET` 至少包含 32 个 UTF-8 字节时，五个
非搜索检索接口才会注册。请用密码学安全的随机数生成器生成部署专用值，并保存在已
忽略的环境文件或机密存储中。生产配置在值缺失或过短时按失败关闭处理。更改该值会
使之前签发的分页游标失效。

## 使用安装后软件包的合成 Agent 端到端测试

通过常规验证门槛后，运行明确的软件包边界测试：

```sh
python scripts/agent_e2e.py
```

运行器会构建服务器和 Python 客户端的源码分发包，安装到彼此独立的临时环境，迁移
一个临时 SQLite 数据库，并通过临时回环 TLS 操作安装后的管理员和 Agent 命令行工具。
测试覆盖初始化和限定范围的 Agent 配置、搜索索引重建、归档响应丢失后的重放、
Revision 历史和准确引用、当前 Page 搜索、凭据吊销及清理。

`PATH` 中必须能找到 OpenSSL 和 `uv`。所有身份、内容、凭据、游标密钥、证书、端口
和数据库状态都是合成且临时的。凭据只通过 stdin 或子进程环境变量提供，绝不放入
命令参数，也不会由运行器输出。软件包构建和安装使用 `uv` 离线模式，全部应用流量
仅在回环地址上传输。运行器不会连接已配置的部署；搜索验证只针对该临时数据库。
客户端明确读取临时目录中的无凭据合成配置，不使用运行者的默认配置文件，
也不放宽客户端对配置文件及其父目录的安全检查。

## 使用 Compose 运行

```sh
export PATCHOULI_RETRIEVAL_CURSOR_SIGNING_SECRET="$(openssl rand -base64 32)"
docker compose up --build --wait
```

默认监听器只绑定回环地址。如果端口已被占用，请选择一个明确的空闲端口，不要终止
现有进程：

```sh
PATCHOULI_PORT=18765 docker compose up --build --wait
```

PowerShell 用户可以为当前进程生成并设置游标密钥，再视需要设置端口覆盖值，然后
运行 Compose：

```powershell
$bytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
$env:PATCHOULI_RETRIEVAL_CURSOR_SIGNING_SECRET = [Convert]::ToBase64String($bytes)
$env:PATCHOULI_PORT = 18765
docker compose up --build --wait
```

## 可选网页管理功能

FastAPI 管理面板默认关闭。默认在 HTTPS 反向代理后启用，并配置生成的密码校验值、
独立会话签名密钥。不需要配置域名或 IP 白名单，旧 `PATCHOULI_ADMIN_ORIGIN` 会被忽略。
公开 Compose 文件传入这些管理设置，也传入 Uvicorn 的 `FORWARDED_ALLOW_IPS`。
该项默认只信任回环；若代理从容器网桥连接 API，应在私有环境文件中填入实际直接
代理地址，不能使用任意来源通配信任。代理应保留原始 Host 和正确的协议。

加密私网可以明确启用 HTTP Cookie；配置、监听范围和仍保留的登录
防护见[私网 HTTP 说明](admin-web-console.md#仅在加密私网中使用-http)。不要用切换到
开发环境或跳过身份验证来实现它。

密码校验值生成、与目标无关的 Nginx 示例、会话和 CSRF 边界、受支持的本地操作，
以及明确不存在的部署权限，见[网页管理面板](admin-web-console.md)。

## 数据库迁移

- 每项数据库结构变更都需要一份 Alembic revision 和测试。
- CI 会验证全新升级、降级到基线和重复升级。
- 在更严格的回滚契约被接受前，已发布迁移必须与前一应用镜像保持兼容。
- 在真实知识数据被视为可用于生产前，仍须验收备份与恢复。

`20260929_0007` 为历史 Revision 增加文件记录。`20260929_0008` 只为旧版 Markdown
Revision 增加文件集合封存：升级前检查每个历史 Revision 恰好有一份与原正文相同的
`content.md`，不满足时拒绝升级；升级后旧写入接口仍可使用，但不能向已封存的
Revision 追加文件。后续开发分支已实现并测试统一文件集写入及多文件封存，
但契约仍为提案，尚未合并或部署。
开发分支的 `20260930_0021` 仅为按身份查看最近成功内容事件增加审计索引；
`20260930_0022` 记录受审计的 Page 标题变化，使历史响应和备份验证能够还原
当时的标题。`0020`、`0021` 备份仍按各自原结构独立验证。这些迁移尚未合并或部署。

后续开发分支的 `20260930_0023` 增加脱敏 API 请求记录，`20260930_0024` 增加
可重建的搜索索引，`20261001_0025` 增加主会话整页软删除历史；这些迁移也尚未合并
或部署。产生主删除历史后拒绝丢失这些记录的降级。

当前开发分支的备份工具默认创建、验证和恢复准确的 `20261001_0025` 数据库。验证或恢复旧版
备份时，须在服务函数或 `verify`／`restore` 命令中显式指定实际修订号，例如
`--schema-revision 20260930_0024`、`--schema-revision 20260930_0022` 或
`--schema-revision 20260929_0007`；`create`
不接受该参数，也不会创建旧版备份。各修订按各自结构独立验证，不会把旧版误认作
当前格式或自动迁移。恢复始终只生成新的非活动数据库文件，不会切换正在运行的服务。
升级前应
保留由旧版工具验证的备份及对应旧镜像。若 `0007` 数据库已经写入 `content.md` 以外
的文件，必须先确定保全与迁移方案，不得强行启用 `0008`。从 `0007` 降回 `0006` 会
丢失额外文件，因此迁移会先拒绝这种降级；应采用经过验证的备份与数据槽恢复方案。

## CI 与制品

Pull Request 会在没有部署凭据的情况下运行文档、Python、迁移和容器作业。可信推送
成功后，或对可信 ref 手动触发工作流后，会向 GHCR 发布 `linux/amd64` 镜像，并附：

- 不可变的 `sha-<commit>` 标签；
- `main` 对应的 `edge` 标签；
- `vX.Y.Z` Git 标签对应的语义化版本标签；
- 以 Registry 为后端的构建来源证明。

镜像发布和版本发布作业依赖全部验证作业。GitHub Actions 发布已验证制品后即停止，
不会连接私有运行环境。

## 手动私有更新契约

私有更新由管理员发起。可信工作流发布镜像后，管理员从 Registry 或工作流证据中取得
准确的 `repository@sha256:digest` 身份，通过管理员控制的路径登录私有运行环境，
然后运行：

```sh
sh deploy/manual-update.sh \
  ghcr.io/example/patchouli-lib@sha256:<64-lowercase-hex-characters>
```

示例仓库是合成值。不要把私有 Registry 名称、运行路径、账号或访问方式复制到受
跟踪文件或公开日志中。

本地辅助工具要求管理员在本地环境中设置 `PATCHOULI_DEPLOY_ROOT` 和
`PATCHOULI_IMAGE_REPOSITORY`。可选的 `PATCHOULI_COMPOSE_FILE`、
`PATCHOULI_RUNTIME_ENV_FILE` 和 `PATCHOULI_STATE_FILE` 可以覆盖本地文件名。
相对覆盖路径以 `PATCHOULI_DEPLOY_ROOT` 为基准解析；绝对路径保持不变。辅助工具会：

1. 只接受一个镜像身份作为命令行参数；
2. 拒绝不同仓库或非规范 SHA-256 摘要；
3. 验证本地 Compose 配置；
4. 只拉取并启动 `api` 服务，然后等待健康检查；
5. 以原子方式记录成功的镜像身份。

它不读取 `SSH_ORIGINAL_COMMAND`，也不提供从 GitHub 到运行环境的连接。健康检查失败
不会触发自动镜像回滚：数据库迁移可能使旧应用镜像不再兼容，因此管理员选择恢复
操作前必须检查应用和数据库状态。

未来的网页管理面板在首个版本中不得获得容器运行时、Registry 或镜像更新权限。
初始范围仅限本地应用管理、文档、Agent 指令和 MCP 指引。

工作流改动合并到默认分支后，可以手动移除过时的 GitHub 部署变量、机密和
Environment。仍有活动工作流引用这些值时不得移除。

## 发布

项目确立版本策略后，在已验证提交上创建带注释的 `vX.Y.Z` 标签，会发布语义化版本
镜像标签和 GitHub Release。不得仅为部署未经审查的开发提交而创建版本发布。
