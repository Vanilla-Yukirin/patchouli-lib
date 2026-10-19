# 本机多凭据配置

## 每个用途一份凭据、一个变量名

同一电脑可保存多份凭据，例如编辑器使用 `PATCHOULI_TOKEN_EDITOR`，只读助手使用
`PATCHOULI_TOKEN_READER`。这些只是示例；先与用户确认名字、用途及持久保存位置。
变量名采用 `[A-Za-z_][A-Za-z0-9_]*`，Windows 下不区分大小写。不要复用 PATH、HOME
等系统变量，也不自动回退到另一 Agent 的 Token。若选定变量已有值，只检查存在性，
不要打印；未经用户确认不覆盖。轮换同一凭据时才在明确授权下更新相应变量。

环境变量是便利存储，不是加密机密库；同一系统用户下的程序可能读取其他变量，
本方案不实现 Agent 间隔离。把变量名写进当前 Agent 的 Skill、AGENTS.md 或
用户指定的记忆文件即可，不在共享 Skill 中写死个人变量名，也不记录真实值。

可复制的非秘密约定示例：

```markdown
PatchouliLib 的服务入口由本机配置提供。
本 Agent 使用 PATCHOULI_TOKEN_EDITOR，不使用其他 Agent 的凭据。
由本机脚本读取该变量并组成 Authorization: Bearer 请求头；不要输出变量值，
也不要列举全部环境变量。先调用 whoami 核对身份、有效期及实际读写范围。
变量缺失时询问用户，不自动签发或借用管理员 Token。
```

## 获取凭据

接入指令本身是公开无密钥文字。Agent 应先告诉用户到管理后台领取设备 Token，
然后在**本机终端**输入，不要请用户把 Token 发到聊天窗口。新版本签发且仍有效的
Agent Token 可由管理员登录后在受保护的详情页再次显示／复制；早期只存校验值的
旧 Token 无法还原，需要管理员重新签发新凭据，并按需吊销旧凭据。先核对目标服务
实际版本，不要猜测。

以下命令只包含变量名，不含 Token 字面值；输入时请避开录屏、终端日志和共享屏幕。
一次任务结束后清除临时进程变量，用户选择的持久配置保留。不要把 `.env` 放进
受跟踪目录，也不要将 Token 写入仓库文件。用户可选择持久环境变量或 shell 配置，
应遵循当前系统的权限与解锁方式。[接入小票](onboarding-ticket.md)提供另一种首次接入方式。
下方 Python 示例如果没有设置 `PATCHOULI_BASE_URL`，会在启动时询问非机密的服务
根地址；不要输入管理页面路径或把 Token 附在地址上。

## Bash 临时输入（Linux/macOS）

```bash
read -r -s -p 'Patchouli device Token: ' PATCHOULI_TOKEN
printf '\n'
export PATCHOULI_TOKEN
python patchouli_local.py
unset PATCHOULI_TOKEN
```

## PowerShell 临时输入（Windows）

```powershell
$secret = Read-Host 'Patchouli device Token' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $env:PATCHOULI_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    $secret.Dispose()
}
python .\patchouli_local.py
Remove-Item Env:PATCHOULI_TOKEN
```

上面的 `PATCHOULI_TOKEN` 是临时示例，实际使用用户确认的变量名。不要打印变量、
传给无关子进程，或在不受信任的机器上运行。

## 持久保存和读取

Windows 可使用用户级环境变量。下面由用户在自己的 PowerShell 7 中执行；
`-MaskInput` 只隐藏输入，最终 User 存储不加密。代码只检查存在性，不展示已有值：

```powershell
$patchouliVariable = 'PATCHOULI_TOKEN_EDITOR' # 换成已确认的变量名
if ($null -ne [Environment]::GetEnvironmentVariable($patchouliVariable, 'User')) {
    throw '该变量已存在，请先确认是否需要替换。'
}
$patchouliSecret = Read-Host '粘贴设备 Token（隐藏输入）' -MaskInput
try {
    if ([string]::IsNullOrWhiteSpace($patchouliSecret)) { throw 'Token 不能为空。' }
    [Environment]::SetEnvironmentVariable($patchouliVariable, $patchouliSecret, 'User')
    if ([Environment]::GetEnvironmentVariable($patchouliVariable, 'User') -cne $patchouliSecret) {
        throw '保存核对失败。'
    }
    Write-Host '已保存；接下来由脚本验证身份和权限。'
} finally {
    Remove-Variable patchouliSecret -ErrorAction SilentlyContinue
}
```

已有进程不会自动继承新变量。本地 PowerShell 调用脚本可直接用
`[Environment]::GetEnvironmentVariable('PATCHOULI_TOKEN_EDITOR', 'User')`
读取到内存，不回显；再按需要传给本次 Python 子进程，并在结束后清除临时变量。
Python 可通过 `os.environ[variable_name]` 读取已继承的自定义变量，不要打印它。

Linux/macOS 可以按用户选择写到 `.bashrc`、`.zshrc` 或独立受限环境文件。推荐
本地脚本保存到仓库外独立文件，shell 配置仅 `source` 该文件。保存脚本先检查
同名变量、文件和旧配置，不静默覆盖；新文件使用仅当前用户可读写权限，不跟随
符号链接。文件中的 `export VARIABLE='value'` 由脚本从内存生成并正确引用，
不要让模型生成含真实长期 Token 的命令。修改已有 shell 配置需保留原内容和权限。
自定义变量名必须先验证，Token 字节也不能拼成待执行命令。

`.bashrc` 通常面向交互 Bash，不能保证桌面应用、服务或非交互工具自动加载。
为实际启动 Agent 的进程明确加载所选环境文件；仅报告变量存在和认证结果，不运行
`env`、`set` 或其他全量环境转储。跨操作系统不要把 Windows User 注册表变量当成
WSL/Linux 环境已配置。

## Python（任意系统）

独立脚本可直接 `getpass.getpass("Patchouli device Token: ")`，只在进程内存中
组成 `Authorization: Bearer …` 请求头。**不要**把 Token 作为脚本参数、URL 参数、
模型工具参数或日志字段。HTTP 示例先询问非秘密的变量名，或通过非秘密的
`PATCHOULI_TOKEN_VARIABLE` 指定名字；只读取选中的变量，缺失时停止。需要临时
隐藏输入时，先按上面示例配置该变量，不自动退回其他名字。
