# Content OS

Content OS 是一个 **Local-first / BYOK** 的个人内容引擎，长期方向是帮助专业创作者和小团队发现值得表达的内容，以自己的方式、可控成本持续经营内容资产：Know what to create → Create it as you → Learn what works。当前工程先验证本地生产链路，不能把本批次脚手架当作完整产品验收。

## 当前开发批次

已可用：

- 核心 Pydantic 数据契约、JSON Schema 与关联示例；
- Python + FastAPI 本地 API；
- `GET /health` 健康检查；
- pytest 配置与健康端点测试；
- Windows PowerShell 本地启动和检查脚本；
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

本批次完成数据契约与后端最小骨架。ScenePlan / VideoSpec 目前只有数据模型，尚未实现自动规划或渲染。前端、素材导入、切片、ASR、Vision、Provider、移动端及完整生产链路待开发。后续按 `DEVELOPMENT_PLAN.md` 推进；本地绑定固定为 `127.0.0.1`，没有部署、公开网络绑定、密钥或付费 API。

## 文档与契约

开发基线以 `BASELINE_FREEZE.md` 修订为准，详见 `PRD.md`、`DEVELOPMENT_PLAN.md`。完成状态与限制见 `STATUS.md`。

重新导出契约：

```powershell
& ".\.venv\Scripts\python.exe" contracts/export_schemas.py
```

本包在 Linux / Python 3.12 环境验证；Windows PowerShell 启动脚本尚未实机验证。首次安装需要 Python 3.11+ 和可用的包下载网络。解压时将包内文件直接放到目标目录，避免再套一层 `Content OS` 子目录。
