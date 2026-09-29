# 统一文件集 HTTP v1 草案

Status: Proposed

本草案描述 Page 文件集的候选 HTTP 契约，供产品、兼容性和安全审查。**它不是已接受的
接口规范，也不表示已部署服务开放这些地址。** 单份 Markdown 是仅含一个文件的 Page；
它与混合文件、仅二进制文件的 Page 使用完全相同的新上传、读取和版本机制。

## 1. 决定、代码与发布状态

| 层次 | 当前状态 |
| --- | --- |
| 存储方向 | [ADR 0004](../decisions/0004-page-file-storage.md)只接受“文件字节、清单、历史 Revision 均在 SQLite 中”及不静默丢失旧格式内容。 |
| 产品目标 | [下一阶段设计](next-phase-product-and-api.md)确认新客户端使用统一文件集；具体 HTTP 契约仍为 Proposed。 |
| 开发分支 | 已编写 0013 迁移、内部新建与整组修订服务、当前状态及精确历史读取、备份校验、有界 multipart 解析器及 HTTP 读写路由，并在开发分支的 `create_app` 中成对注册；有合成测试和真实应用测试。首版仅处理现有 Archive Page 和 Section 授权。 |
| 已发布服务 | 此开发分支尚未合并、发布或部署，也未导入真实资料。不能把开发树上的注册及测试当成线上 API 已可用。 |

下面凡称“草稿当前行为”，仅指本开发分支的实现。凡称“建议契约”，均须经
独立审查、决定记录与兼容性验收后才可公布。其他 Page 类型、多 Library Token、
一般资料导入和正式恢复支持不随本草案自动实现。

## 2. 一个 Page、一组文件、一个版本

- Page 位于指定 Library／Section／Book 中，有稳定 Page ID；显示标题不是唯一身份。
- 每个 Revision 是该 Page 内 **1 至多份同层文件的完整快照**。新建首版和后续修订
  均提交整组文件；删除某文件意味着下一次整组提交不再包含它。一个成功的内容变化
  只生成一个新 Revision，不能逐文件生成多个版本。
- `content.md` 只是普通文件名；单份 Markdown、Markdown 加图片、只有演示文稿，
  都走同一格式。没有“主文件／附件”区分，也不接受嵌套目录或文件系统路径。
- 文件字节、排序后的清单、单文件 SHA-256、整组快照摘要、Revision 和当前指针
  在 SQLite 的同一写入事务内提交。摘要校验的是精确目标 Page 与 Revision，不是
  “全库某处有相同哈希”。快照摘要不充当数字签名或授权凭据。
- 首版只保证保存、准确读回和安全下载；保存图片、脚本或演示文稿，不代表已经支持
  在线预览、执行、病毒扫描、文字提取或多模态搜索。

## 3. 候选地址与共同请求体

下面地址是开发分支的草稿实现，**还不是已发布的 API**。地址中的 ID 必须与请求身份可访问
的 Library／Section／Book／Page 相符；创建与修订的目标不同，但文件传输完全相同。

| 操作 | 草稿地址 | 含义 |
| --- | --- | --- |
| 新建 | `POST /api/v1/libraries/{library_id}/sections/{section_id}/books/{book_id}/pages` | 在既有 Book 中直接创建首版文件集。 |
| 修订 | `POST /api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/file-revisions` | 原子替换指定 Page 当前整组文件；不创建另一个 Page。 |
| 当前状态 | `GET /api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}` | 取得当前 Revision 的已核验文件清单和强 Page ETag，以便重新打开后安全修订；不下载文件字节。 |
| 精确清单 | `GET /api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/revisions/{revision_id}/files` | 读取指定历史 Revision 的完整文件清单。 |
| 精确文件 | `GET /api/v1/libraries/{library_id}/sections/{section_id}/pages/{page_id}/revisions/{revision_id}/files/{file_name}` | 下载该 Revision 清单中的一个文件。 |

两个写入请求都采用 `multipart/form-data`，只允许以下有序部分：

1. 第一部分名为 `metadata`，`Content-Type: application/json`，正文是 UTF-8 JSON 对象；
   不携带 `filename`。
2. 后续 1 至多份部分均名为 `file`，每部分以 `filename` 给出一个 UTF-8、平面文件名，
   正文为原始字节。不能使用不同字段名表示单 Markdown，也不能把文件清单只放在 JSON
   中而省略字节。上传时的文件顺序不决定快照身份，服务按规范化文件名排序。

草稿的 `metadata` 顶层字段严格如下，额外字段和重复 JSON 键均拒绝：

| 操作 | 字段 | 草稿语义 |
| --- | --- | --- |
| 新建 | `title`、`source`，可选 `occurred_at` | `title` 是非空展示名；`occurred_at` 是严格 RFC 3339 日期时间，可含时区，服务转为 UTC。省略时取服务器当前 UTC 时间并在响应标记代填；提供但无效则报错。 |
| 修订 | `source` | Page 标题和声明时间不随文件修订改变；须走单独的元数据操作。 |
| 两者共用 | `source` | 必填对象；`kind` 为非空且首尾无空白的来源类别，`locator` 和 `captured_at` 可选。`locator` 是不含 NUL 的非空文本；`captured_at` 在当前内部模型中为 UTC 微秒整数，而非 `occurred_at` 的 RFC 3339 字符串。来源不是文件身份。 |

示意元数据可写作 `{"title":"示例页面","source":{"kind":"manual"}}`；把
`content.md` 作为唯一 `file` 部分，就是单 Markdown 上传。该示例不含任何凭据，
也不表示可以省略 `Authorization` 或 `Idempotency-Key`。`source` 的公开表示，
尤其 `captured_at` 是否维持微秒整数，仍须在接受契约前定稿。

草稿解析器按流读取，并在构造完整清单前拒绝畸形、重复或不支持的字段／MIME 头；
不调用无界的整表单解析。文件名先按 Unicode NFC 规范化，拒绝空名、`.`、`..`、
路径分隔符、跨平台保留设备名、控制／格式字符、尾点或含糊空格；大小写折叠后
碰撞的名字不能共存。上传者标注的文件 MIME 只检查语法，**不作为真实类型证明**。

开发分支当前上限为 64 文件、单文件 16 MiB、整组文件 64 MiB、元数据 64 KiB；
文件名另有限长，multipart 总量还包含协议开销。开发分支仅在宣告 `file-sets`
能力时，以 `limits.file_set` 给出前三项；旧 `max_content_bytes` 的 2 MiB 仍仅指旧
单 Markdown 接口。它们是**候选配额，不是已发布的永久承诺**。应结合备份体积、
并发内存和目标容量确定正式配额及变更政策；
超过大小边界不能以悄悄截断方式返回成功。

## 4. 认证、授权与并发条件

- 所有读写均要求 `Authorization: Bearer …`，不得将凭据放进 URL、JSON、日志或
  公开示例。草稿在读取上传正文前先鉴权，并在写事务内重新核对权限；未知或跨库
  目标不应成为资源枚举通道。
- 草稿仍沿用既有单 Library 凭据、Section grant：新建和修订要求
  `ARCHIVE_WRITE`；精确清单和下载要求 `PAGE_READ`。**写权限不隐含读权限**。
  仅写凭据可完成上传，却不能自行执行写后精确回读；要验证字节，需另有读取授权
  的凭据或管理侧核验。未来按 Library 读／写开关的迁移另行审查，不能借此把旧
  Section 凭据悄然扩权。
- 新建与修订都必须提供单个 `Idempotency-Key`。同一调用方在对应操作范围内，
  完全相同的目标、元数据和规范化文件集使用同一键重试；改变任一语义字段或字节
  应换新键。同键异请求返回冲突，不得覆盖已存记录。服务只持久化键摘要，
  不记录原始键。
- 修订还必须携带当前 Page 的强 `If-Match`，格式为服务返回的带引号 ETag；
  `If-Match` 缺失报 428，格式错误或重复报 422，当前 Page 已变化报 412。
  新建不得携带 `If-Match`。ETag 是 Page 当前状态的并发条件，不能用单个文件哈希
  代替。修订时重放已成功、完全相同的幂等请求，即使原 `If-Match` 已不再是当前值，
  也应重放原响应，而非再执行一次写入。
- 草稿的当前状态 GET 在 `PAGE_READ` 授权下，从同一数据库读快照取当前指针、完整
  文件清单和强 ETag。客户端遗失上次写入响应时可重新读取它，再提交新的修订；
  精确历史 Revision 清单不冒充当前 Page 状态，也不提供当前 ETag。只有写权限而
  无读权限的凭据仍不能自行取得该 ETag；这属于公开契约接受前要明确的工作流边界。
- 来源描述和 `If-Match` 进入幂等请求身份；但如果提交的**规范化文件名集合和每份
  文件字节**与当前快照完全相同，草稿返回“未变化”，不新增 Revision、Source
  或内容变更审计，只留下该次成功幂等结果。仅变更 `source` 不是元数据更新路径；
  来源校正应有单独、可审计的操作，不能误认为同内容修订已修改来源。

新建 Page、首版 Revision、全部文件、Source、内容审计与成功幂等记录应一起提交；
修订变更同理。失败不允许留下半个 Page、半组文件或可错误重放的成功记录。
旧历史 Revision 保持不可变；恢复旧内容若不同于当前内容，应生成新 Revision。

## 5. 草稿响应与精确回读

| 情况 | 草稿结果 |
| --- | --- |
| 新建成功 | `201 Created`；JSON 含 `section_id`、`book_id`、`page_id`、`revision_id`、`revision_number: 1`、规范化的 `occurred_at`、`occurrence_defaulted`、`snapshot_sha256`、`files`；响应头含当前强 `ETag` 和指向该首版清单的 `Location`。 |
| 修订成功或同内容 | `200 OK`；JSON 含 `changed`、`section_id`、`page_id`、准确的当前 `revision_id`／`revision_number`、`snapshot_sha256`、`files`；`changed: false` 时 Revision 不增加，ETag 不变。 |
| 当前状态 | `200 OK`；JSON 给出当前 Revision 的已核验文件集清单，不含文件字节；响应头携带强当前 Page `ETag`。当前草稿沿用历史清单的字段，尚未包含 Book、标题和声明时间，接受契约前需决定是否补齐。 |
| 完全相同请求重放 | 保留首次成功的状态码、响应正文和 ETag，并添加 `Idempotency-Replayed: true`；重放得到的历史 ETag 不保证仍是当前 ETag。当前这次 HTTP 请求的 `X-Request-ID` 仍单独生成。 |
| 精确清单 | `200 OK`；按指定 Page／Revision 返回 `page_id`、`revision_id`、`revision_number`、`snapshot_sha256` 和文件名、字节数、SHA-256。历史清单不因当前 Revision 变化而改指针。 |
| 精确文件 | `200 OK`、`application/octet-stream`、`Content-Disposition: attachment`、`X-Content-Type-Options: nosniff`；返回准确原始字节，不因扩展名或上传 MIME 在浏览器内联执行。 |

草稿的新建、修订与读取响应统一使用文件条目字段 `filename`、`size_bytes`、
`content_sha256`。这仍是未发布草稿，不构成最终字段规范或兼容性承诺。
受保护响应设置 `Cache-Control:
private, no-store`。服务按准确 Revision、文件名、长度及哈希核验读取；损坏或
不完整的存储快照不能返回看似成功的部分文件。上传后应使用本次响应中的 Page ID、
Revision ID 与摘要，逐项核验清单与必要的下载字节；全库哈希检索不能替代这一步。
调用方创建的临时本地副本可在核验成功后清理，用户原有文件不得自动删除。

## 6. 错误边界

草稿错误沿用受保护 API 的结构化问题响应，响应不得包含凭据、上传字节或来源定位。
下列映射需要在接口审查时固定并补全端到端测试：

| 状态 | 草稿含义与边界 |
| --- | --- |
| 401 | 缺少、无效、到期或已撤销的 Bearer 凭据；不读取并保存文件正文。 |
| 403 | 已识别身份，但缺当前 Section 必需的写或读权限。 |
| 404 | 目标不在可见范围、Book／Page／Revision／文件不存在或 Page 已进入回收站；避免跨库资源探测。 |
| 409 | 同一幂等键与已成功请求的语义内容不同：`idempotency_mismatch`。旧单 Markdown 修订路线面对无法表达的当前文件集时使用 `revision_format_unsupported`。 |
| 412 | 修订所给强 ETag 不再匹配当前 Page：`revision_conflict`。 |
| 413 | 请求、元数据、单文件、文件数或整组字节超过当前保护上限：`content_too_large`；正式限额仍待决定。 |
| 415 | multipart 或部分 MIME 头不受支持：`unsupported_media_type`。上传时的文件 MIME 并非内容鉴定。 |
| 422 | 畸形 multipart／JSON、重复字段、不安全文件名、无效声明时间、非法／重复头、缺少文件或非法 ID：`request_validation_failed`。 |
| 428 | 修订缺少强 `If-Match`：`precondition_required`。 |

不能将持久层校验错误翻译为成功或部分结果。错误分类还需覆盖数据库不可用、
损坏快照和请求取消时的事务结果；正式发布前应验证重试指导不会导致重复写入。

## 7. 旧接口、迁移、备份与回退

现有 Archive `metadata`＋`content` 单 Markdown 接口是旧客户端兼容路径，
**不是新客户端的另一种单文件协议**。新接口精确读取可把旧 `legacy_markdown`
Revision 展示为仅有 `content.md` 的文件集；旧创建路径可暂时继续写旧格式。
旧读取／修订接口只能表示单 Markdown：面对 `file_set_v1` 当前 Revision，即便
该快照碰巧仅含 `content.md`，也应明确拒绝旧格式改写，而不是静默丢失或覆盖文件。
旧接口兼容期限、客户端提示与最终退役需要单独公布。

0013 迁移把已有 Revision 映射为 `legacy_markdown` 文件集并校验旧
`content.md` 镜像；新格式无需伪造占位 Markdown。开发分支的备份数据库校验
已覆盖文件字节、封口、清单摘要及文件集写入的成功幂等记录。**这只说明合成数据
上的代码路径，不等于正式备份／真实数据恢复已验收。** 正式启用前至少需要：

1. 用合成旧库核对升级前后 Page／Revision 数、字节与哈希、旧客户端兼容；
   用合成新库验证单 Markdown、多文件、仅二进制、重放、并发与历史读取。
2. 对备份副本执行完整校验与隔离的恢复演练，验证 Source、审计和重放记录，
   然后才讨论真实数据升级或导入；不得在原库上直接做试验性回退。
3. 区分“回退应用代码”与“降低数据库版本”：一旦存在任何 `file_set_v1` Revision，
   包括只含一份 Markdown 的新协议写入，0013 降级会拒绝执行。需要预先设计可读
   新格式的旧版应用／前向修复路径，或经授权在隔离环境完成数据转换；不能把
   降级失败当作可忽略警告。
4. 所有写入开放、迁移、真实备份恢复、部署与流量切换分别设门槛；文档合入或
   合成测试通过均不自动授权这些运维动作。
5. 开发草稿早期曾把成功幂等响应的文件条目写成 `name`／`sha256`，现改为
   `filename`／`content_sha256`。部署前须对目标库只读核对是否存在旧草稿文件集
   成功记录；若存在，先单独设计并验证精确重放与备份兼容迁移，不得直接启用新路由。
   路由曾未公开不等于可以假定所有目标库都没有这类内部记录。

## 8. 接受前必须关闭的问题

- 正式 URL、响应文件字段命名的最终接受、`Location` 的稳定性、`source.captured_at` 的公开
  时间表示、错误详情及 API 版本如何冻结。
- 文件数量、文件名、单文件、整组文件、元数据和 multipart 总体的正式配额，
  并发上传内存预算、长期 SQLite／备份体积，以及配额调整的兼容方式。
- 文件类型与安全策略：扩展名和 MIME 的信任边界、恶意文件处理、同页相对引用、
  安全预览是否另设接口；下载默认只能是附件。
- 仅写凭据如何完成写后准确回读：维持读写分离并由管理侧验证，还是在未来
  Library 授权模型中要求同时开启读权限；**不得把当前写授权隐式当成读授权**。
- Source 等元数据的独立修订／校正契约、同内容 no-op 时来源信息是否保留，以及
  Page 类型从仅 Archive 扩展为一般资料的迁移顺序。
- 旧客户端退役期限、0013 后的可操作回退方案、正式备份与恢复验收门槛。

这些问题关闭前，应持续使用“已编码草稿、已测试、已合并、设计已接受、已部署、
真实数据已验收”六种不同表述，不把其中一种当成另一种。
