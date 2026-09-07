# 本地开发接续

## 1. 放入目录

将开发包内所有文件直接解压到：

`C:\Users\ASUS\Documents\AI coding\Content OS`

根目录应直接包含 `pyproject.toml`、`AGENTS.md` 和 `services`。若目录里已有文件，先对比，避免覆盖本地修改。

## 2. 安装并验证

在该目录打开 PowerShell，确认已有 Python 3.11+，执行：

```powershell
Set-Location -LiteralPath "C:\Users\ASUS\Documents\AI coding\Content OS"
py -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -e ".[test]"
& ".\.venv\Scripts\python.exe" -m pytest
& ".\.venv\Scripts\python.exe" contracts/export_schemas.py
& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --app-dir services/api --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000/health`，预期 status 为 ok。终端保持运行，Ctrl+C 停止服务。目前没有产品网页。

## 3. 交给本地开发 Agent

在有文件访问权限的本地开发环境中打开上述项目目录，然后发送以下任务：

> 继续开发 Content OS。先读 AGENTS.md、STRATEGY_BASELINE.md、BASELINE_FREEZE.md、STATUS.md、PRD.md、DEVELOPMENT_PLAN.md，以及现有模型与测试。产品方向已经确定，不要重写脚手架或重新做战略讨论。先运行现有测试与健康检查，确认本地 Windows 环境。下一任务为 SQLite Persistence：为 IPProfile、Project、Asset、Clip、Job 建立轻量 repository，使用参数化 SQL、外键、迁移版本及事务；校验 Clip 所属 Asset 与真实 duration 一致，保留任务幂等键唯一约束。简单 CRUD 分派 Luna，任务 claim/recovery/concurrency 分派 Terra；若环境无这些模型，说明可用模型，不冒称已调度。不要同时改核心契约。验收至少包括重启后数据保留、重复导入/任务去重、跨素材时间越界拒绝、事务回滚。暂不调用付费 Provider、不自动发布、不修改网络设置。完成后更新 STATUS.md，并报告测试及下一步。

任务执行依赖本地 Agent 的实际工具和模型可用性，本包本身不会自动创建子 Agent。

## 4. 后续工程边界

- Task 001 / 002a 为起步实现，不是完整 Gate 1 或 Alpha。
- VideoSpec 为初版契约；资源路径解析、媒体种类、生成素材实际落盘、外部授权撤销与跨实体验证，在 assembler/repository 阶段继续补齐。
- 前端与 Renderer 骨架可以在持久化接口稳定后启动。
- 媒体导入阶段需要 FFmpeg / ffprobe；此刻无需安装 GPU 工具或配置模型密钥。
- 建议完成首次本地测试后初始化 Git 并保存起始提交；未在本包中创建远端仓库。
