---
name: patchouli-agent
description: 通过标准 HTTP 接入 PatchouliLib，验证设备身份、下载受保护 Skill，并按实际能力安全读取或上传 Page 文件集。
---

# Patchouli Agent 使用指南

首选标准 HTTP；不要求安装专用 CLI 或 MCP。先读[本机输入设备 Token](references/local-token.md)
和[标准 HTTP 接口](references/http.md)。用户没有设备 Token 时，指导其在管理后台签发
或复制。**不要让用户把 Token 发到模型聊天，也不要把 Token 放进接入提示词。**

在本机交互式输入 Token，用 Bearer 请求头调用 `GET /api/v1/auth/whoami` 和
`GET /api/v1/capabilities`。核对身份、说明、有效期、目标 Library 的实际权限与服务
实际公布的能力。随后按受保护的 Skill manifest 下载列出的全部文件，逐项核对字节数
和 SHA-256，再依当前 Agent 环境的规则安装。已有本地 Skill 如被修改，先比较差异，
不要静默覆盖。目标服务未公布或未实现的能力不可猜测。

`whoami.policy_mode` 为 `library_grants` 时，只按每个目标 Library 明示的 `read` 和
`write` 判断；两者独立，写入不隐含读取。`legacy_section` 时继续按旧 Section
授权，绝不自动扩大为整库权限。`operator` 是管理员身份；不要为了弥补 Agent 权限
不足而换用管理员凭据。旧服务缺少新版身份字段时标记权限模式未知，先核对实际契约。

## 内容操作

- 先从已授权列表确认本库 Section、Book 与现有 Page；这些 ID 和 Revision、ETag
  都是不透明值，不从标题、路径或 ID 字面猜测身份。当前列表不能发现其他 Library
  的 Section／Book；跨库写入时由管理员或已授权来源提供并核实目标 ID，绝不猜造。
  新建 Page 前必须已有目标 Book。
- 用户要上传时，先把完整内容准备为本机文件，再按[统一文件集 HTTP 流程](references/http.md)
  提交：单份 Markdown 也是一个 `file`，多文件是同一请求中的多个 `file`。新建必须
  使用幂等键；修订还需当前 Page 的强 `If-Match`，提交的是整组文件快照而非补丁。
- 结果不确定时，只有保留原幂等键、目标、元数据和全部原始字节，才可原样重试。
  参数变化或明确的条件冲突应重新读取状态并由用户决定，不自动覆盖。有读取权限时
  按响应中的准确 Page／Revision 清单和文件哈希回读核对；仅有写入权限时须交由
  另一获授权身份或管理侧验收，并如实报告自己无法回读。需要时在本机记录 Page ID。
  不用全库同哈希查询代替准确回读，也不自动删除用户原始文件。
- 旧单 Markdown Archive 写入是兼容路径，新客户端优先统一文件集。目标服务若尚未
  公布文件集能力，不能盲调新接口；是否改用旧接口应先说明其单文件限制。
- 目标服务的能力响应含 `search` 时，按[标准 HTTP 搜索契约](references/http.md)
  调用跨知识库当前 Page 搜索。索引未重建或不完整会明确返回 `search_unavailable`；
  不可把列表浏览或静态示例冒充真实搜索，也不要继续调用旧单 Section 搜索入口。

## 凭据与报告边界

Token 只放在本机受控输入、进程内存或用户选择的本机机密存储。不得写入 URL、
命令行参数、模型工具参数、受跟踪文件或日志；不要在普通诊断里回显正文、搜索词、
Source 定位值或 Token。报告操作结果、准确引用和必要的非机密请求 ID；用户未要求
展示内容时不附上正文。CLI/MCP 若已存在，只是兼容选择，不是接入前置条件。

本仓库开发分支的接口不等于目标服务已经合并或部署；始终以实际能力响应和请求结果
为准。真实资料导入、部署及权限扩大必须遵守当前任务授权。
