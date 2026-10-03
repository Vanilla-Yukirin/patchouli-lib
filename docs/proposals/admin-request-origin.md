# 管理面板：取消固定入口地址配置

- 状态：已实现并合并（#60）；本文保留为设计记录，当前行为以代码和管理面板文档为准
- 范围：管理页面的入口检查与配置；不改变 API Token、权限或持久化契约

## 问题与目标

管理面板只能预先配置一个访问地址，导致同一服务通过多个合法入口访问时，登录页和
样式文件被拒绝。新增入口不应要求维护应用内的域名或 IP 白名单。

## 方案

- 删除 `PATCHOULI_ADMIN_ORIGIN` 配置及固定 Host 白名单；旧环境变量会被忽略。
- 启用面板只要求密码校验值与独立会话签名密钥同时存在。
- 页面和样式不再按预设地址拒绝访问；管理页面仍要求有效登录会话。
- 登录、退出及管理操作保留同源检查：浏览器提交的 Origin 必须与本次请求的协议、
  Host 和端口一致。拒绝缺失、重复、空来源及格式含糊的值，不从 Origin 反向决定
  请求的协议。管理操作仍必须提供会话内的 CSRF 值。
- Cookie 默认保留 Secure。已有 `PATCHOULI_ADMIN_ALLOW_PRIVATE_HTTP=true` 只允许
  HTTP 请求省略 Secure；HTTPS 请求始终设置 Secure。它不再核查某个预设 IP，
  私网隔离和监听范围由部署方保证，不能把这个开关当作网络访问控制。
- HTTPS 入口代理须保留原始 Host（含非默认端口），并传递原始协议。只由 ASGI 服务
  的受信代理处理转发头；应用不直接信任任意 `X-Forwarded-*` 请求头。

## 备选方案与取舍

多地址白名单仍然需要逐项配置，不符合简化目标；完全删除同源与 CSRF 检查则会让
其他网页更容易借用浏览器会话提交操作。因此只删除固定入口列表，保留自动同源校验。

此改动不再用固定 Host 检查阻止 DNS 重绑定等异常入口。密码、短期签名会话、CSRF、
SameSite、Token 和权限仍保留；服务应只在管理员控制的网络或入口后运行。虚拟组网
不自动证明浏览器内的所有网页可信，也不等于已启用额外身份认证。

## 迁移与验证

旧配置可以删除 `PATCHOULI_ADMIN_ORIGIN`，不需要修改数据库或重置密码。HTTP 后端
位于 HTTPS 代理后时，必须正确配置 ASGI 的受信代理范围，例如 Uvicorn 的
`FORWARDED_ALLOW_IPS`；不要使用任意来源通配信任。代理仍负责登录限速，本次不删除
已有代理、不修改公网入口，也不自动部署。

验证覆盖多个域名、私网 IP、非默认端口、登录与样式、跨站及跨协议提交、缺失和
重复请求头、受信和非受信转发头、Cookie 属性、会话、CSRF 与 API Token 边界。

## 参考

- [OWASP 跨站请求伪造防护](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [Uvicorn 代理转发头设置](https://www.uvicorn.org/settings/#http)
