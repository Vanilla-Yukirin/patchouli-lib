# 网页管理面板

这个可选面板把现有的本地管理员功能放到一个简单网页中。它不是部署
控制台，没有主机命令、Docker、镜像更新、回滚或备份恢复权限。

## 启用面板

只有同时提供下面三个配置时，服务才会注册 `/admin` 页面：

- `PATCHOULI_ADMIN_PASSWORD_HASH`：带盐的密码校验值；
- `PATCHOULI_ADMIN_SESSION_SIGNING_SECRET`：单独生成、至少包含 32 个
  UTF-8 字节的随机值；
- `PATCHOULI_ADMIN_ORIGIN`：浏览器访问面板时唯一、准确的来源地址，
  例如仅作示例的 `https://admin.example.invalid`。主机名必须已使用 ASCII
  或 Punycode 形式；原始 Unicode 主机名会被拒绝。

在本机生成密码校验值：

```text
patchouli-admin-password
Administration password:
Confirm administration password:
pbkdf2_sha256$...
```

这个命令不接受命令行参数；在交互式终端中输入密码时不会显示字符。
它只输出密码校验值，不输出原密码。密码和校验值都不能放进命令行参数、
仓库文件、截图、Issue 或日志。

如果通过 Compose 的 `.env` 文件提供校验值，要用单引号包住完整内容，
避免其中的 `$` 分隔符被解释；这个私有 `.env` 文件也不能加入版本控制。

会话签名密钥必须用密码学安全的随机数生成器单独生成。不要复用管理
密码、密码校验值、Agent 凭据、operator 凭据、检索游标签名密钥或 TLS
私钥。可选的 `PATCHOULI_ADMIN_SESSION_TTL_SECONDS` 允许 300 到 86400 秒，
默认是 1800 秒。

生产环境只接受 HTTPS 来源地址。三个必需配置全部为空时面板保持关闭；
只设置其中一部分时，应用会拒绝启动。

## 在前面放置 TLS 入口

公开的 Compose 服务仍然只监听本机回环地址。把
`deploy/nginx/patchouli-admin.conf.example` 复制到管理员自己的私有配置，
替换示例域名与证书位置，并在重新加载前验证 Nginx 配置。

这个 Nginx 示例会：

- 负责 TLS，并把原始 Host 传给 FastAPI；
- 限制登录请求的频率；
- 把管理表单请求体限制为 16 KiB；
- 单独保留 Archive 接口所需的请求体上限；
- 只转发到本机回环 API，不在公开仓库记录真实地址或证书位置。

`PATCHOULI_ADMIN_ORIGIN` 必须与浏览器实际发送的来源完全一致。不要为了
绕过 TLS 和登录限速边界而把回环 API 直接暴露到不可信网络。

## 使用面板

访问 `/admin/login` 并输入管理密码。短期会话 Cookie 只包含到期时间和
随机 CSRF 值，不包含管理密码、operator 凭据或 Agent 凭据。

右上角的 `中文 / English` 可以切换整个面板，包括表单校验和错误提示。
语言选择保存在另一个非敏感 Cookie 中；它仅限 `/admin` 路径，使用
HttpOnly 和 SameSite=Strict，只包含 `zh-CN` 或 `en`，退出登录后仍会保留。
它既不是管理会话，也不会包含或替代任何 bearer 凭据。
一次性凭据结果页会有意隐藏语言切换，避免用户在保存唯一一份明文凭据前
因切换语言而离开页面。

第一版可以完成：

1. 一次性创建 Library、Section、Book 和第一个本地 operator；
2. 恢复 operator 凭据，同时吊销此前仍有效的 operator 凭据；
3. 为一个明确的 Section 创建 Agent、精确权限和一份凭据；
4. 吊销指定的 Agent 凭据；
5. 查看只读的管理员、Agent 和 MCP 使用说明。

操作表单中的 operator 凭据只用于当前请求。新 operator 或 Agent 凭据只
会在禁止缓存的结果页面显示一次。离开页面前，应记录 Library、caller 和
credential ID，并把新凭据保存到批准的密码管理工具中；丢失后不能再次
读取同一个值，只能按正常审计流程吊销或恢复。

面板不会把 bearer 凭据写入 Cookie 或其他浏览器存储。不要安装会捕获
表单内容或页面秘密的浏览器扩展，也不要添加记录请求体或响应体的中间件。

英文说明见 [Web administration console](admin-web-console.md)。
