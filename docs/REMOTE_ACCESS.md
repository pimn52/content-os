# 私网 / Tailscale 手机访问指引

本文只提供手工配置步骤，不安装、启用或修改 Tailscale、Windows 防火墙和路由器设置。Content OS 的远程入口仍是本地 API；不要把端口暴露到公网。

## 1. 启动前设置一次性私网 token

在运行 Content OS 的 Windows PowerShell 窗口中生成一个高熵 token，并只保留在当前进程环境：

```powershell
$contentOsTokenBytes = [byte[]]::new(32)
[Security.Cryptography.RandomNumberGenerator]::Fill($contentOsTokenBytes)
$env:CONTENT_OS_ACCESS_TOKEN = [Convert]::ToBase64String($contentOsTokenBytes)
```

也可以由用户自行设置更容易轮换的 token。不要把 token 写入仓库、SQLite、备份包、聊天记录或启动脚本。

## 2. 绑定私网地址

确认手机和 Windows 主机处于同一个受信任的 Tailscale tailnet 后，在项目根目录运行：

```powershell
Set-Location -LiteralPath "C:\Users\ASUS\Documents\AI coding\Content OS"
& npm --version
& ".\scripts\start.ps1" -HostAddress 0.0.0.0 -Port 8784
```

第一条命令仅用于检查 npm；正式启动由 `start.ps1` 完成。脚本会拒绝没有 `CONTENT_OS_ACCESS_TOKEN` 的非 loopback 绑定。启动后，在手机浏览器打开：

```text
http://<Windows 主机的 Tailscale IPv4>:8784/app/
```

在页面顶部输入同一个 token，点击“连接”。token 只写入当前手机标签页的 `sessionStorage`；关闭标签页或清除站点数据后会失效。

## 3. 最小检查与停止

先在手机确认 `/app/` 能加载，再验证一个不含敏感内容的读取动作。不要把 token 放到 URL、截图或日志中。完成后回到 Windows 启动窗口按 `Ctrl+C`，并确认端口已释放。

若 token 泄露或需要撤销，停止服务、关闭当前 PowerShell，再用新 token 启动；不要复用旧 token。若需要长期、多用户或公网访问，应先设计正式身份认证、HTTPS、文件权限和审计，不把本入口当作账号系统。

## 4. 数据与能力边界

- 文件仍写入 Windows 本机 `content-os-data`；手机上传不会自动上传到第三方。
- 手机可以上传视频/本人录音、查看草稿、轻改、确认补拍和审核；发布仍是人工记录，不自动发帖。
- 远程访问不等于 Provider 已配置；TTS、Talking/口型和自动发布仍按 R1 能力状态显示。
- 升级前先按 [本地备份指引](../README.md#升级前可创建并校验本地备份) 创建并验证备份；恢复只能写入新目标目录。
