# Content OS

Content OS 是一个 **Local-first / BYOK** 的个人内容引擎，长期方向是帮助专业创作者和小团队发现值得表达的内容，以自己的方式、可控成本持续经营内容资产：Know what to create → Create it as you → Learn what works。当前工程先验证本地生产链路，不能把本批次脚手架当作完整产品验收。

## 当前开发批次

已可用：

- 核心 Pydantic 数据契约、JSON Schema 与关联示例；
- Python + FastAPI 本地 API；
- `GET /health` 健康检查；
- pytest 配置与健康端点测试；
- Windows PowerShell 本地启动和检查脚本；
- SQLite Repository、幂等 Job、租约/自动心跳/重试/崩溃恢复及单次 Worker 执行边界；
- 本地 Job API：Asset 分析/转写幂等入队与状态查询；
- 本地媒体内容寻址导入、ffprobe 探测、连续 Clip 切分、分析音频和关键帧提取；
- provider-neutral ASR 契约、OpenAI-compatible BYOK 适配器及时间戳转写到 Clip 的原子写回；
- provider-neutral Vision 契约、OpenAI-compatible Responses BYOK 适配器，以及关键帧视觉元数据到 Clip 的原子写回；
- provider-neutral Embedding 契约、本地 SQLite 向量索引、过滤余弦检索与自然语言 Clip 搜索 API；
- 无前端构建依赖的本地素材库页面、只读 Asset/Clip API 与原视频区间预览；
- provider-neutral ScenePlanner、严格结构化 ScenePlan 输出与本地 Project→ScenePlan API；
- 可配置本地 Asset Router：连续 Clip Top‑K、真实素材优先、质量/适配/新鲜度/复用评分与补拍缺口；
- 严格 VideoSpec 组装：授权绑定、精确帧换算、连续时间线及 Capture gap 阻断；
- 基础 Remotion 渲染器：只读取已授权本地连续 Clip，支持 cut、字幕、原素材音轨与 9:16 MP4 输出；
- 可重复的双素材 FFmpeg → SQLite → VideoSpec → Remotion 端到端成片夹具；
- 第一真实素材 Gate 本地验收页 `/m1-gate`：选择/创建项目、输入脚本或主题、查看 ScenePlan、Top-1/Top-3 候选及理由/成本/复用信息、预览 Clip、人工替换、生成 VideoSpec 并导出 Gate JSON；
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

Task 001–015 的最小工程链路已完成；当前提供第一真实素材 Gate 验收工具，需要使用创作者授权的 3–5 条视频和一份脚本记录实际选择与人工替换结果。Gate JSON 包含 Scene 数、Top-1 接受率、Top-3 覆盖率、本人素材使用率、替换次数、人工耗时和未知成本数。该工具只是 M1 真实素材验收界面，不代表个性化成片质量已通过。Voice/Talking 仍按 Gate 5/6 后续推进。合成夹具通过不代表个性化成片质量通过。移动端及完整生产链路待开发。ASR/Vision/Embedding/ScenePlanner 密钥只通过运行时构造 Provider 注入，不写入数据库或仓库；测试仅使用本地 fake server，没有调用付费 API。Worker CLI 支持前台轮询与 `--once`，并可启用独立连接自动心跳。

默认 SQLite 文件为 `content-os-data/content-os.sqlite3`，可通过 `CONTENT_OS_DB_PATH` 覆盖。Job API：

- `POST /assets/{asset_id}/jobs/analyze_asset`
- `POST /assets/{asset_id}/jobs/transcribe_audio`
- `POST /assets/{asset_id}/jobs/index_clips`
- `POST /clips/search`
- `GET /asset-library`
- `GET /m1-gate`
- `GET /projects`、`POST /projects`（本地 Gate 测试项目）
- `GET /assets`、`GET /assets/{asset_id}/clips`、`GET /clips/{clip_id}`
- `POST /projects/{project_id}/scene-plan`
- `POST /projects/{project_id}/asset-routes`
- `POST /projects/{project_id}/video-spec`
- `POST /projects/{project_id}/render`（同步本地渲染）
- `POST /projects/{project_id}/render-jobs`（幂等入队，Worker 异步渲染）
- `GET /jobs/{job_id}`

本地 Worker CLI（前台进程，不启动独立 daemon 线程）：

```powershell
& ".\.venv\Scripts\content-os-worker.exe" --once --job-type analyze_asset
& ".\.venv\Scripts\content-os-worker.exe" --job-type analyze_asset --job-type transcribe_audio
& ".\.venv\Scripts\content-os-worker.exe" --job-type index_clips
& ".\.venv\Scripts\content-os-worker.exe" --once --job-type render
```

异步渲染请求需要在 JSON 中提供 `idempotency_key`，并附带与同步渲染相同的 `video_spec` 或 `scenes` + `selections`。接口立即返回 `pending` Job；执行 `--job-type render` 的本地 Worker 后，通过 `GET /jobs/{job_id}` 轮询状态，完成后使用返回的 `download_url`/`preview_url`。输出始终由服务端写入本地数据根的 `renders/<project_id>/<render_id>.mp4`，请求不会传入输出路径或密钥。

转录任务启动前需要运行时环境变量 `OPENAI_API_KEY` 或 `CONTENT_OS_ASR_API_KEY`。`index_clips` 会先执行 Vision 再写入 Embedding 索引，需要 `CONTENT_OS_VISION_API_KEY`、`CONTENT_OS_EMBEDDING_API_KEY`，二者均可回退到 `OPENAI_API_KEY`；各自可用 `CONTENT_OS_VISION_*`、`CONTENT_OS_EMBEDDING_*` 覆盖 base URL、模型和维度等配置。搜索 API 使用相同的 Embedding 运行时配置。密钥不会写入数据库或日志。`--db`、`--data-root`、`--worker-id`、租约/心跳/轮询间隔和 `--max-attempts` 可覆盖默认值。

ScenePlan API 使用 `CONTENT_OS_LLM_API_KEY`（可回退 `OPENAI_API_KEY`），并支持 `CONTENT_OS_LLM_BASE_URL` / `CONTENT_OS_LLM_MODEL`。当前返回内存中的 ScenePlan，不持久化用户脚本。`/m1-gate` 在 ScenePlanner 或 Embedding Provider 未配置时会明确显示“未配置/不可用”，不会用假数据冒充真实匹配或计划结果。

安装并验证本地 Remotion Renderer：

```powershell
Set-Location -LiteralPath ".\apps\renderer"
npm install
npm exec -- remotion compositions src/index.ts
```

渲染命令由 Python 边界调用，可直接以模块方式运行：

```powershell
Set-Location -LiteralPath "C:\Users\ASUS\Documents\AI coding\Content OS"
& ".\.venv\Scripts\python.exe" -m app.renderer.cli --database ".\content-os-data\content-os.sqlite3" --spec ".\video-spec.json" --output ".\output.mp4" --renderer-dir ".\apps\renderer"
```

渲染器当前只接受已持久化且授权信息完全匹配的本地 `user_asset` / `historical_asset` Clip，只支持 cut、9:16、字幕和源音轨；不实现旁白资产、配音混音或 Voice/Talking。

## 文档与契约

开发基线以 `BASELINE_FREEZE.md` 修订为准，详见 `PRD.md`、`DEVELOPMENT_PLAN.md`。完成状态与限制见 `STATUS.md`。

重新导出契约：

```powershell
& ".\.venv\Scripts\python.exe" contracts/export_schemas.py
```

Worker CLI 是前台轮询进程，可用 `--once` 执行一个兼容任务，也可持续轮询直到收到 SIGINT/SIGTERM；它不启动独立 daemon 线程。首次安装需要 Python 3.11+ 和可用的包下载网络；媒体分析还需要本机可执行的 FFmpeg / ffprobe。解压时将包内文件直接放到目标目录，避免再套一层 `Content OS` 子目录。

可重复的本地闭环报告（真实本机 FFmpeg/ffprobe、无外部 Provider）可用以下命令生成。两个二进制路径必须显式注入；成功报告会原子地写入被 Git 忽略的 `content-os-data/test-runs/<run-id>/`，其中包含夹具输入、ScenePlan、路由候选、VideoSpec、MP4、ffprobe JSON 和明确为 unmetered 的 usage ledger。

```powershell
$env:CONTENT_OS_FFMPEG = "C:\\path\\to\\ffmpeg.exe"
$env:CONTENT_OS_FFPROBE = "C:\\path\\to\\ffprobe.exe"
python scripts/run_local_fixture_report.py --run-id local-check
```
