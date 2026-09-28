# 本机输入设备 Token

接入指令本身是公开无密钥文字。Agent 应先告诉用户到管理后台领取设备 Token，
然后在**本机终端**输入，不要请用户把 Token 发到聊天窗口。已有 Token 只在签发时
显示过一次的部署中，无法从校验值还原；请管理员新签或轮换，不要猜测。

以下命令只包含变量名，不含 Token 字面值；输入时请避开录屏、终端日志和共享屏幕。
一次任务结束后清除进程变量。不要把 `.env` 放进受跟踪目录，也不要将 Token 写入
仓库文件。若用户选择本地机密存储，应遵循当前系统的权限与解锁方式。
下方 Python 示例如果没有设置 `PATCHOULI_BASE_URL`，会在启动时询问非机密的服务
根地址；不要输入管理页面路径或把 Token 附在地址上。

## Bash（Linux/macOS）

```bash
read -r -s -p 'Patchouli device Token: ' PATCHOULI_TOKEN
printf '\n'
export PATCHOULI_TOKEN
python patchouli_local.py
unset PATCHOULI_TOKEN
```

## PowerShell（Windows）

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

本机环境变量仍可被同一用户权限下的进程读取；它不是长期机密库。避免在不受信任的
机器上运行，且不要把变量打印、传给无关子进程或写入 shell profile。

## Python（任意系统）

独立脚本可直接 `getpass.getpass("Patchouli device Token: ")`，只在进程内存中
组成 `Authorization: Bearer …` 请求头。**不要**把 Token 作为脚本参数、URL 参数、
模型工具参数或日志字段。下方 HTTP 示例使用这种方式，也接受已经设置好的
`PATCHOULI_TOKEN` 环境变量。
