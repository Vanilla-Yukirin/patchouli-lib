# 使用说明与 Agent 结构管理

状态：已接受（维护者确认本轮实现范围）；alpha 接口，不承诺稳定版本兼容性。

## 目标

空知识库也能向获授权 Agent 说明用途、组织规则与首次使用步骤。Library、Section、
Book 各保存一份独立 Markdown README；建议简短，详细流程链接到专用 Skill，
不把完整 Skill 塞进 README。不设 README 字数上限；HTTP 请求仍有资源保护上限。

README 不占 Page，不需要先创建 Section 或 Book，也不改变 Page/Revision 历史。
已有 description/summary 继续作为导航摘要；创建分区、书籍时应填写用途说明。
README 是库内工作约定，不得提升权限、代替用户授权或要求泄露凭据。

## 授权

新增结构接口只面向 library_grants Agent。目标库 read 或 write 任一权限可读取
结构元数据与 README，使仅写 Agent 也能发现目标；不开放 Page、文件或搜索正文。
write 允许创建目标库内 Section、Book 及更新三层 README，不允许管理凭据、
创建其他 Library 或修改其他库。旧 Section 凭据不自动获得新权限。
每次操作在同一事务内重新验证凭据、目标授权和父子归属。
实际结构创建和 README 修改复用不可变的凭据审计，只记操作者、资源和请求 ID，
不记录 README 内容；自然幂等重放和无变化更新不新增变更审计。

## HTTP

结构元数据包含稳定 id、名称、用途说明、readme、updated_at；读取返回强 ETag。
GET 支持 If-None-Match，未变化返回 304。更新 README 使用 PUT、JSON
`{"readme":"Markdown"}` 和准确 If-Match；缺失 428，陈旧 412。正文 UTF-8
JSON 上限 2 MiB，非 README 字数限制。空 README 可以清除；原样 Markdown 保留。

- GET `/api/v1/libraries`：列出当前凭据有 read 或 write 的 Library。
- GET `/api/v1/libraries/{library_id}`：读取库元数据与 README。
- GET/POST `/api/v1/libraries/{library_id}/sections`：发现／创建分区。
- GET `/api/v1/libraries/{library_id}/sections/{section_id}`：读取分区。
- GET/POST `/api/v1/libraries/{library_id}/sections/{section_id}/books`：发现／创建书籍。
- GET `/api/v1/libraries/{library_id}/sections/{section_id}/books/{book_id}`：读取书籍。
- PUT 上述三个单资源路径加 `/readme`：更新对应 README。

三个列表使用 limit（1–100，默认20）、offset，返回 items/next_offset；不返回正文。
列表也不返回 README；选定对象后读取单资源，使用其 ETag 缓存说明。offset 必须在
SQLite 有符号 64 位非负整数范围内。凭据最近使用时间沿用既有合并更新策略。
POST 分区使用 name、description、可选 readme；POST 书籍使用 name、summary、
可选 readme。用途说明须非空。首次创建 201，同一父级同名且元数据完全相同返回
200 原身份；同名但不同元数据 409，不覆盖已有对象。超时后保留原请求，不能换名
盲目重建。旧发现接口保持原有字段与授权，新增能力标识 library-structure。

## 网页与 Skill

三层详情直接展示安全渲染的 README，主会话可展开编辑，空态说明用途。创建表单
保留摘要并增加可选 README；提示保持简洁、链接详细流程。复用现有 Markdown
安全渲染、语言和表单机制，不新增前端框架或阅读打卡。

通用 patchouli-agent 先发现库、按库→分区→书籍读取 README，然后执行业务。
按服务入口、对象 ID 和 ETag 缓存读取结果；已读且版本未变不必再次加载全文。
README 可链接同服务受保护的 Skill manifest；先验证全部文件摘要与字节数，再
按宿主 Agent 的授权安装。已有 Skill 与库说明不一致时核实，不能永久忽略规则更新。
提供可移植的 patchouli-archive Skill，凭据与目标从用户确认的私有配置读取，
不内置维护者实例或某个 Agent 的变量名。通用示例不固化个人库的分类规则。

## 存储与验收

迁移为三张结构表增加默认空字符串的非空 readme TEXT，不改已有行的其他字段。
非空 README 时拒绝降级，防止静默丢弃说明。备份验证支持旧 schema 与新 schema。
针对迁移保真、授权/跨库边界、空库创建和 README 并发编辑执行必要测试；复用已有
网页/Skill 检查，不增加重复浏览器矩阵。私有库的具体 README 和分类不进入仓库。
