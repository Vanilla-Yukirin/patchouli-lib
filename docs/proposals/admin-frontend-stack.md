# 管理前端技术选型

- 状态：提案中（Proposed）；React 路线已获维护者认可，具体库在实现阶段逐项验证
- 范围：新管理前端的框架、组件库与工程工具；不涉及 Agent 公开 API 或部署方式

## 结论

采用 **React + TypeScript 的静态单页应用**：由 Vite 构建，产物由 FastAPI 在
`/admin` 下同源提供。组件采用 shadcn/ui（Radix + Tailwind CSS），数据层采用
TanStack Router 与 TanStack Query。

## 选择 React 而非 Vue 的理由

两者在能力上都满足需求。选择 React 主要基于本仓库的协作方式：

- 仓库大量采用 AI 辅助贡献（见 [AGENTS.md](../../AGENTS.md)），React 与
  TypeScript 是 AI 工具生成和修改最稳定的组合；
- shadcn/ui 以源码形式复制进仓库，没有黑盒依赖，协作者可以直接审查和修改组件；
- shadcn/ui 生态在 React 上最成熟，Vue 移植版（shadcn-vue）相对滞后。

如果后续以人工手写前端为主且更偏好单文件组件，可改用 Vue 3 + shadcn-vue +
TanStack Query（Vue 版），其余架构与本文一致。

## 技术栈

| 用途 | 选型 | 说明 |
| --- | --- | --- |
| 构建 | Vite + TypeScript | 沿用仓库已有的 Node.js 24 与 npm |
| 框架 | React 19 | |
| 路由 | TanStack Router | 路径参数类型安全，适合知识库／分区／书籍／页面／版本的深层路径 |
| 服务端数据 | TanStack Query | 缓存、失效、乐观更新与失败重试；原则上不需要单独的全局状态库 |
| 接口类型 | openapi-typescript + openapi-fetch | 由管理端 JSON 接口的 OpenAPI 生成类型，避免前后端契约漂移 |
| 组件 | shadcn/ui（Radix）+ Tailwind CSS v4 | Radix 提供无障碍交互；Tailwind 在构建期生成静态 CSS；内置暗色模式支持 |
| 图标 | lucide-react | |
| 表格 | TanStack Table | 排序、筛选、多选与批量操作 |
| 表单 | react-hook-form + zod | 行内校验，错误显示在字段旁 |
| 弹窗与提示 | shadcn Dialog / AlertDialog、Sonner | 危险操作二次确认，操作结果用 Toast 反馈并提供撤销入口 |
| 命令面板 | cmdk（shadcn Command） | Ctrl/Cmd+K 全局搜索与快捷操作 |
| 拖放 | dnd-kit | 目录树内拖动移动文档；树组件先自行实现，数据量大时再评估 react-arborist |
| 上传 | react-dropzone + 自建上传队列 | 拖放、逐文件进度；现有幂等键与重试协议在内部处理 |
| Markdown 编辑 | CodeMirror 6 | 体积小，支持 CSP nonce；不采用 Monaco |
| Markdown 渲染 | 继续由后端 `markdown_preview.py` 安全渲染 | 保证网页与其他入口看到一致的结果，不在前端另建一套清洗规则 |
| 版本差异 | jsdiff + 自建差异视图 | |
| 多语言 | i18next + react-i18next | 使用语义化键；默认语言按 `Accept-Language` 选择 |
| 时间 | 原生 Intl，必要时加 date-fns；shadcn Calendar | 按浏览器时区显示，提交时转换为 UTC；不写死固定时差 |
| 代码规范 | Biome | 同时负责 lint 与格式化 |
| 单元／组件测试 | Vitest + Testing Library | |
| 端到端测试 | Playwright | 使用合成数据与临时数据库 |

## 明确不采用

| 方案 | 原因 |
| --- | --- |
| Next.js、Nuxt 等 SSR 框架 | 需要额外的 Node 服务端运行时，增加部署与安全面；静态单页应用已满足需求 |
| Ant Design、MUI 等运行时注入样式的组件库 | 运行时插入 `<style>` 与严格 CSP 冲突，体积较大，视觉风格较强 |
| Element Plus（若改用 Vue） | 可用，但视觉风格偏传统后台；优先 shadcn-vue |
| Redux、Zustand 等全局状态库 | 服务端状态交给 TanStack Query 后，剩余全局状态很少；确有需要时再引入 Zustand |
| 在浏览器存储中保存任何 Token | 违反 ADR 0003 的安全边界 |
| 从 CDN 加载脚本或样式 | 违反同源 CSP；所有依赖在构建期打包 |

## 依赖管理

- 前端依赖使用 `package-lock.json` 锁定版本，与现有 npm 工具链一致；
- 新增依赖需在 PR 中说明用途与替代方案，优先选择维护活跃、无运行时样式注入、
  许可证与本项目兼容的库；
- 构建产物不提交到仓库，由构建流程生成并打包进镜像。
