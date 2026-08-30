# 网页管理面板

> [简体中文兼容文件](admin-web-console.zh-CN.md)作为既有链接的入口保留；本文件是
> 规范入口，两份文件的面向读者正文均使用简体中文。

这个可选面板把现有的本地管理员功能放到一个简单网页中。它不是部署控制台，没有
主机、Docker、镜像更新、回滚、备份恢复或 Shell 权限。

## 启用面板

只有同时提供下面三个配置时，服务才会注册 `/admin` 页面：

- `PATCHOULI_ADMIN_PASSWORD_HASH`：带盐的密码校验值；
- `PATCHOULI_ADMIN_SESSION_SIGNING_SECRET`：单独生成、至少包含 32 个 UTF-8 字节
  的随机值；
- `PATCHOULI_ADMIN_ORIGIN`：浏览器访问面板时唯一且精确匹配的 Origin（源站），例如仅作示例的
  `https://admin.example.invalid`。主机名必须已使用 ASCII 或 Punycode 形式；
  原始 Unicode 主机名会被拒绝。

在本机生成密码校验值：

```text
patchouli-admin-password
Administration password:
Confirm administration password:
pbkdf2_sha256$...
```

这个命令不接受命令行参数；在交互式终端中输入密码时不会显示字符。请把输出当作
敏感配置。密码和校验值都不能放进 Shell 参数、受跟踪文件、截图、Issue 或日志。

如果通过 Compose 的 `.env` 文件提供校验值，要用单引号包住完整内容，避免其中的
`$` 分隔符被解释；这个私有 `.env` 文件也不能加入版本控制。

会话签名密钥必须用密码学安全的随机数生成器单独生成。不要复用管理密码、密码
校验值、Agent 凭据、管理员凭据、检索游标签名密钥或 TLS 私钥。可选的
`PATCHOULI_ADMIN_SESSION_TTL_SECONDS` 允许 300 到 86400 秒，默认是 1800 秒。

生产环境只接受 HTTPS Origin（源站）。三个必需配置全部为空时面板保持关闭；只设置其中
一部分时，应用会拒绝启动。

## 在前面放置 TLS 入口

公开的 Compose 服务仍然只监听本机回环地址。把
`deploy/nginx/patchouli-admin.conf.example` 复制到管理员自己的私有配置，替换
示例服务器名称与证书位置，并在重新加载前验证 Nginx 配置。

这个 Nginx 示例会：

- 负责 TLS，并把原始 Host 转发给 FastAPI；
- 限制登录端点的请求频率；
- 把管理表单请求体限制为 16 KiB；
- 单独保留 Archive 接口所需的请求体上限；
- 只转发到本机回环 API，不在公开仓库记录真实目标地址或证书位置。

`PATCHOULI_ADMIN_ORIGIN` 必须与浏览器实际发送的 Origin 完全一致。不要为了绕过 TLS 和
登录限速边界而把回环 API 直接暴露到不可信网络。

## 使用面板

访问 `/admin/login` 并输入管理密码。短期会话 Cookie 只包含到期时间和随机 CSRF 值。

右上角的 `中文 / English` 控件可以切换整个面板，包括表单校验和错误提示。语言选择
保存在另一个非敏感的 HttpOnly、SameSite=Strict Cookie 中；它仅限 `/admin` 路径，
只包含 `zh-CN` 或 `en`，退出登录后仍会保留。它既不是管理会话，也不会包含或替代
任何 bearer 凭据。一次性凭据结果页会有意隐藏语言切换，避免用户因切换语言而丢失
唯一显示的一份凭据。

第一版可以完成：

1. 一次性创建 Library、Section、Book 和第一个本地管理员；
2. 恢复管理员凭据，同时吊销此前仍有效的管理员凭据；
3. 为一个明确的 Section 创建 Agent、精确权限和一份凭据；
4. 吊销指定的 Agent 凭据；
5. 查看只读的管理员、Agent 和 MCP 使用说明。

“首次设置”会创建保存 Page 前必须具备的最小内容结构：

- **知识库（Library）** 是整个知识空间。个人使用通常一个就够了；需要分开管理和
  授权时可以创建多个，但同一次部署中的知识库仍共享服务和数据库，并不是物理上
  完全分开的系统。
- **分区（Section）** 是长期使用的大分类，也是给 Agent 划定权限的范围。
- **书籍（Book）** 是一个分区内更小的内容集合。每个 Page 只属于一本书。

分区说明和书籍摘要都是可选的文字说明。管理员名称用于标识管理员，审计记录会关联
到这个身份；它不是网页登录账号。“管理员令牌有效期”只决定首次生成的管理员
bearer 令牌多久过期，不会让 Library 或网页管理密码失效；默认 `3600` 秒即一小时。

操作表单中的管理员凭据只用于当前请求。新管理员或 Agent 凭据只会在禁止缓存的结果
页面显示一次。离开页面前，应记录 Library、caller 和 credential ID。丢失响应后
无法再次显示同一个令牌。

面板不会把 bearer 值写入 Cookie 或其他浏览器存储。不要安装会捕获表单内容或页面
机密的浏览器扩展，也不要添加记录请求体或响应体的中间件。
