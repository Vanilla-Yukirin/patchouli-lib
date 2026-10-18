# Agent 的库明确 Page 生命周期

- 状态：Proposed，开发分支独立审查已完成，整体交付验收待完成
- 行为版本：`20261003_0030`，继承 `20261002_0029` 的物理结构

## 范围

补齐新文件集 Page 的 Agent 软删除与恢复。旧 Archive 的 Section 路由、权限、
响应及历史回执不变；不新增 CLI、永久删除、跨库移动或 READ 隐含授权。

## HTTP 契约

`DELETE /api/v1/libraries/{library_id}/pages/{page_id}` 软删除，
`POST /api/v1/libraries/{library_id}/pages/{page_id}/restore` 恢复。
两者只接受空正文、无查询参数，要求 Bearer、单一 `Idempotency-Key` 和强
`If-Match`。只有显式 `library_grants` Agent 对目标 Library 的 WRITE 可调用；
旧 Section 凭据与 operator 不扩权，主 Token 不进入 Agent Bearer API。

收正文之前预检，写事务内重新认证并授权；当前权限先于回执查找与冲突判断。
200 只返回目标 Library、稳定 Page ID、操作时 Section/Book、生命周期状态、
保留的 Revision ID/number、发生时间、原/新更新时间及请求 ETag，不返回标题、
正文或文件清单。响应含 ETag 和 `private, no-store`。WRITE 不因此获得读取能力。

401 表示无效凭据，403 表示权限不足，404 表示目标 Page 不存在，428 表示
缺少 If-Match，412 表示陈旧 ETag，409 表示幂等冲突或状态已是所请求状态，
422 表示输入不满足空正文等契约；错误不回显凭据或输入内容。

## 历史、幂等与活动

稳定 Page 身份、当前及历史 Revision、封印和文件字节保持不变。删除仅写
生命周期事件与 tombstone；恢复仅撤销 tombstone。成功原子地写事件、真实
Caller 审计及回执，活动不另造一份重复事件。

幂等键摘要使用独立操作域并绑定 actor home Library。成功重放验证精确原
历史状态、事件、审计及回执绑定；后续修改、移动或生命周期操作不改变原成功
响应。撤销或失去目标 WRITE 后不能重放。目标库不冒充 Caller 所属库。

## 迁移与备份

0030 不重建表，也不改 0028/0029 的 exact SQL hash。独立行为版本让备份
校验器识别新路由与 `content.page.delete/restore` 审计，同时继续验证所有旧
格式。新事件、审计、回执须双向一一对应，包含真实 actor home、Caller、
credential、目标 Page、request ID、时间及状态；孤立或错绑数据拒绝备份。

升级先验证旧结构和历史图。降级遇任何新成功回执或新审计（包括孤立审计）
拒绝；无新增行为时完整验证后可返回 0029，不静默丢弃历史。

## 验证边界

定向测试覆盖跨库 WRITE、拒绝旧凭据、授权时序、精确 ETag、删除/恢复、
成功后续重放、真实活动归属、备份恢复与精确反例及安全降级。合成测试不是
正式部署或真实设备验收；整体验证与部署仍由集成者串行完成。
