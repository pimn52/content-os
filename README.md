# Content OS

Content OS 是一个 **Local-first / BYOK** 的个人内容引擎，长期方向是帮助专业创作者和小团队发现值得表达的内容，以自己的方式、可控成本持续经营内容资产：Know what to create → Create it as you → Learn what works。当前工程先验证本地生产链路，不能把本批次脚手架当作完整产品验收。

## 当前开发批次

已可用：

- 核心 Pydantic 数据契约、JSON Schema 与关联示例；
- Python + FastAPI 本地 API；
- `GET /health` 健康检查；
- pytest 配置与健康端点测试；
- Windows PowerShell 本地启动和检查脚本；
- SQLite Repository、幂等 Job、租约/重试/崩溃恢复及单次 Worker 执行边界；
- 本地媒体内容寻址导入、ffprobe 探测、连续 Clip 切分、分析音频和关键帧提取；
- provider-neutral ASR 契约、OpenAI-compatible BYOK 适配器及时间戳转写到 Clip 的原子写回；
- 依赖清单与许可证核查提示。

Windows PowerShell 首次运行（支持带空格的路径）：

```powershell
Set-Location -LiteralPath "C:\Users\ASUS\Documents\AI coding\Content OS"
& py -m venv ".\.venv"
& ".\.venv\Scripts\python.exe" -m pip install -e ".[test]"
```

运行 API：

```powershell
& ".\scripts\start.ps1"
```

也可以直接启动：

```powershell
& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --app-dir ".\services\api" --host 127.0.0.1 --port 8000
```

检查健康端点：

```powershell
& ".\scripts\check-health.ps1"
```

运行测试：

```powershell
& ".\.venv\Scripts\python.exe" -m pytest
```

健康检查返回：

```json
{"status":"ok","service":"content-os-api"}
```

## 本批次暂未完成

ScenePlan / VideoSpec 目前只有数据模型，尚未实现自动规划或渲染。前端、Vision、Embedding 索引、移动端及完整生产链路待开发。ASR 密钥只通过运行时构造 Provider 注入，不写入数据库或仓库；测试仅使用本地 fake server，没有调用付费 API。当前 Worker 是显式 `run_once`，不包含轮询守护进程或长任务自动心跳。

## 文档与契约

开发基线以 `BASELINE_FREEZE.md` 修订为准，详见 `PRD.md`、`DEVELOPMENT_PLAN.md`。完成状态与限制见 `STATUS.md`。

重新导出契约：

```powershell
& ".\.venv\Scripts\python.exe" contracts/export_schemas.py
```

本包已在 Windows / Python 3.12 环境验证。首次安装需要 Python 3.11+ 和可用的包下载网络；媒体分析还需要本机可执行的 FFmpeg / ffprobe。解压时将包内文件直接放到目标目录，避免再套一层 `Content OS` 子目录。
