# 本地开发接续

> 当前接续入口是 [CONTENT_OS_EXECUTION_SPEC.md](CONTENT_OS_EXECUTION_SPEC.md)，状态事实见 [STATUS.md](STATUS.md)，当前复审证据见 [CONTENT_OS_REVIEW_2026-09-10.md](CONTENT_OS_REVIEW_2026-09-10.md)，[AUDIT_REPORT.md](AUDIT_REPORT.md) 仅保留历史起点。本文件的早期 Task 001 提示仅保留作历史参考。

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

浏览器打开 `http://127.0.0.1:8000/app/`，预期进入 R1 正式 React/Vite 工作区；`/health` 仍用于诊断。`/m1-gate` 保留为旧版开发验收入口，不是当前用户主流程。终端保持运行，Ctrl+C 停止服务。

## 3. 交给本地开发 Agent

在有文件访问权限的本地开发环境中打开上述项目目录，然后发送以下任务：

> 继续开发 Content OS。先读 CONTENT_OS_EXECUTION_SPEC.md、CONTENT_OS_REVIEW_2026-09-10.md、STATUS.md、DECISIONS.md 和本仓库现有模型/测试。按 STATUS.md 的当前 ready 任务继续，不重做已完成的媒体/任务基础，不重新讨论战略。普通工程选择直接执行；外部付费接口保持后置；真实模型辅助测试与 fixture、runtime 分开记录。每个工作包完成后运行针对性测试，更新 STATUS.md 和必要的 DECISIONS.md，然后直接领取下一个 ready 任务。只有 U-Voice/U-Product、首次付费/预算、权限、不可逆数据变更或发布授权才集中等待用户。

任务执行依赖本地 Agent 的实际工具和模型可用性，本包本身不会自动创建子 Agent。

## 4. 后续工程边界

- Task 001 / 002a 为起步实现，不是完整 Gate 1 或 Alpha。
- VideoSpec 为初版契约；资源路径解析、媒体种类、生成素材实际落盘、外部授权撤销与跨实体验证，在 assembler/repository 阶段继续补齐。
- 前端与 Renderer 骨架可以在持久化接口稳定后启动。
- 媒体导入阶段需要 FFmpeg / ffprobe；此刻无需安装 GPU 工具或配置模型密钥。
- 建议完成首次本地测试后初始化 Git 并保存起始提交；未在本包中创建远端仓库。
