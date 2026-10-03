# 当前 Page 搜索索引：显式启用与重建

本文只说明开发分支现有实现的本地维护边界，不表示搜索设计已经获准投产、
代码已合并或服务已部署。搜索索引是 Page、Revision、文件和 Tag 等权威记录的
可重建投影，不是知识内容的另一份权威数据。查询语义与剩余验收门槛见
[当前 Page 搜索 v2 提案](proposals/current-page-search-v2.md)。

## 为什么迁移后还不能搜索

- 从旧数据库（包括 `20260813_0006`）升级到当前 Alembic head，必须先完成
  对应迁移的兼容性检查、经验证的备份和维护窗口；不要把版本跨度当成一条
  可以在有真实数据时直接试跑的命令。旧版文件集合迁移有明确拒绝条件。
- 搜索结构由 `20260930_0024` 引入，但迁移把索引设为未就绪，不会把已有
  Page 假定为已收录。普通容器入口 `docker/entrypoint.sh` 只执行
  `alembic upgrade head`，然后启动应用；**不会自动重建索引**。
- `patchouli-search-index rebuild` 是管理员在本地维护环境执行的命令，
  不是 Agent 客户端必装的 CLI，也不替代 Agent 的标准 HTTP 接口。
  它先核对 SQLite FTS5、外键和当前应用要求的数据库修订，再重建索引；
  数据库版本不符时会失败，不会悄悄迁移。

## 执行前的门槛

1. 核对将要操作的镜像／软件包版本、目标数据库及 `PATCHOULI_DATABASE_URL`。
   命令必须和应用使用**同一数据库及持久化卷**；未显式核对环境变量时，
   不要依赖开发默认路径。配置与凭据留在私有环境文件或机密存储中。
2. 按当前和旧版各自的备份格式创建、验证可恢复备份，确认有可回退的旧镜像。
   真实恢复须先在非活动副本演练；不要以“已有一个 `.db` 文件”代替验证。
3. 安排维护窗口并停止所有写入者，包括 API、后台任务和其他直连 SQLite
   的工具；确认不会有并发迁移或另一个重建。首次迁移及重建期间确实有
   停写成本，不能把成功的空库演练称为有数据时的无停机升级。
4. 为备份、迁移和重建留足磁盘空间。重建在一笔 `BEGIN IMMEDIATE` 写事务
   中构建新世代，成功前旧世代与新世代可能同时占空间，SQLite 日志也需要
   空间；容量取决于实际文本与词元，不能按源 Markdown 字节数等额估算。

## 明确分开迁移与重建

下面是仓库自带 `compose.yaml` 的**通用命令形状**；仅在确认当前 Compose
服务、固定镜像、数据库卷和全部写入者后使用，不把示例当成自动发布脚本：

```sh
docker compose stop api && \
docker compose run --rm --no-deps --entrypoint alembic api upgrade head && \
docker compose run --rm --no-deps --entrypoint patchouli-search-index api rebuild && \
docker compose up -d api
```

Compose 中 `api` 使用 `/data` 持久化卷，数据库环境变量由服务配置传入。
`--entrypoint alembic` 是**显式迁移**；`--entrypoint patchouli-search-index`
替换了镜像原本的 `docker/entrypoint.sh`，所以重建命令**不会暗中再运行**
`alembic upgrade head`。`--rm` 只清理一次性维护容器，不删除持久化卷。
普通 `up` 再次经过原入口执行幂等的 `alembic upgrade head`，仍不自动重建。

从源码安装的本地维护也分两步：先在已核准的目标上显式执行
`alembic upgrade head`，再用**同一个** `PATCHOULI_DATABASE_URL` 执行
`patchouli-search-index rebuild`。源码环境可用 `uv run --frozen` 调用这两个
入口；不要让两个步骤分别落到不同的默认数据库。上述迁移命令不是授权在
现有真实数据库上立即执行升级。

## 结果、失败与恢复

- 重建成功时命令输出 `Search index generation … is ready.` 并以零状态退出。
  此后仍要启动目标版本应用，核对健康检查、`/api/v1/capabilities` 中搜索
  是否就绪，并用**已授权的合成查询**核对当前 Page；不能只凭命令退出
  判断权限、相关性或整体部署已验收。
- 重建在单个 SQLite 写事务里完整投影当前未删除 Page 并原子切换世代。
  任何异常会回滚这次重建；若旧世代仍兼容且完整，可以继续提供旧搜索，
  否则搜索按不可用处理（API 为 `503 search_unavailable`），不能返回
  未完成索引的部分结果。非搜索读取仍按其独立契约工作。
- 失败时保留原始数据库和日志，核对版本、空间、锁等待及权威数据，修复原因
  后再安排维护窗口重试；不要手删索引表、强行标记 `ready` 或直接降级
  Alembic。旧库若使用 WAL，不能手删 `-wal`／`-shm` 旁车或只复制活动主
  `.db`；备份应使用经验证的 SQLite 备份流程。
- 备份产物有意把派生搜索索引重置为未就绪。恢复到新的非活动数据库后，
  先验证备份、修订和权威文件字节，再按目标版本完成必要迁移、显式重建，
  最后核对就绪状态与准确查询。未完成前不要把恢复库切成可搜索的活动库，
  也不要把“恢复成功”直接当作“索引已验证”。

其它验证和手动更新边界见[开发、验证与交付](development-and-delivery.md)。
