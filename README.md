# Content OS

Content OS 是一个 **Local-first / BYOK** 的个人内容引擎，长期方向是帮助专业创作者和小团队发现值得表达的内容，以自己的方式、可控成本持续经营内容资产：Know what to create → Create it as you → Learn what works。当前工程先验证本地生产链路，不能把本批次脚手架当作完整产品验收。

当前统一执行入口是 [CONTENT_OS_EXECUTION_SPEC.md](CONTENT_OS_EXECUTION_SPEC.md)，当前复审证据是 [CONTENT_OS_REVIEW_2026-09-10.md](CONTENT_OS_REVIEW_2026-09-10.md)，[AUDIT_REPORT.md](AUDIT_REPORT.md) 仅保留历史起点；旧 PRD、计划和冻结文件保留为历史设计参考，最新事实与接续任务见 [STATUS.md](STATUS.md)。

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
- R1 S1 过渡 workspace `/workspace`：编辑默认 IP 资料与版本、按用途/身份确认素材、从本地文件/目录导入并 hash 去重、从主题创建可恢复草稿；
- R1 S1 正式 React/Vite 工作区 `/app/`：把资料、素材/Inbox、选题机会、项目草稿、ScenePlan、候选、VideoSpec 和本地渲染串成同一条可操作流程；开发服务器通过 Vite 代理本地 API，生产构建由 FastAPI 静态挂载；
- R1 S2 assisted-test 结果桥：导入带来源、模型/工具、输入 hash、时间戳、字幕、关键帧和置信度的分析包，并回放到真实 Clip，不自动授予 production 用途；
- R1 S2 混合视觉路径：本地 Typography 与 PNG/JPEG/WebP 截图/图表导入、显式候选、VideoSpec 授权绑定和 Remotion 渲染；素材不足时保留可选补拍，不生成假媒体；
- R1 S2 已有音频路径：本地 WAV/MP3/M4A 等配音/录音导入、时长/采样率探测、授权引用、区间校验和 Remotion 混入；不包含 TTS 或声音克隆；
- [依赖清单与许可证核查提示](docs/DEPENDENCIES.md)。

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

`start.ps1` 默认以一个入口启动 API 和本地 analyze/render Worker，启动前会打印 Python/Node/npm/FFmpeg/ffprobe 与 Remotion 版本预检；子进程继承当前控制台，按 `Ctrl+C` 一起停止；仅做 API 诊断时可加 `-NoWorker`。需要外部 Provider 的转写/视觉/索引任务仍须在运行时配置后显式启动对应 Worker 类型。默认只绑定 `127.0.0.1`；需要手机在私网访问时使用 `-HostAddress`，并先设置 `CONTENT_OS_ACCESS_TOKEN`，脚本会拒绝无 token 的 LAN 绑定。

如果要在本机用 CPU 做真实 ASR（不使用付费 API），可安装可选依赖并显式选择本地 provider：

```powershell
& ".\.venv\Scripts\python.exe" -m pip install -e ".[local-asr]"
$env:CONTENT_OS_ASR_PROVIDER = "local"
$env:CONTENT_OS_ASR_LOCAL_MODEL = "small"
$env:CONTENT_OS_ASR_DEVICE = "cpu"
$env:CONTENT_OS_ASR_COMPUTE_TYPE = "int8"
& ".\scripts\start.ps1"
```

配置本地 ASR 后，`start.ps1` 会把 `transcribe_audio` 加入本地 Worker；首次显式转写可能从模型仓库下载权重，模型文件只保存在本地数据根，不会提交到仓库。未设置 `CONTENT_OS_ASR_PROVIDER` 时仍保持原有 openai-compatible BYOK 行为。

构建正式 Web 工作区（首次需要下载已锁定的 React/Vite 依赖；React、Vite、TypeScript 及插件分别按其上游 MIT/Apache-2.0 许可核验）：

```powershell
Set-Location -LiteralPath ".\\apps\\web"
npm install
npm run build
```

API 运行时若检测到 `apps/web/dist`，根路径 `/` 会进入 `/app/`；开发期间可在 API 运行后另开终端执行 `npm run dev`，访问 `http://127.0.0.1:5173/app/`，请求通过 Vite 代理到本地 API。

升级前可创建并校验本地备份；恢复只写入新的目标目录，不会覆盖已有数据目录。备份包含 SQLite 一致快照和本地数据根中的媒体/派生文件，带 SHA-256 manifest；不会读取或写入环境变量中的 Provider 密钥：

```powershell
& ".\\.venv\\Scripts\\python.exe" scripts/backup_local.py create --data-root ".\\content-os-data" --output ".\\content-os-backup.zip"
& ".\\.venv\\Scripts\\python.exe" scripts/backup_local.py verify --archive ".\\content-os-backup.zip"
& ".\\.venv\\Scripts\\python.exe" scripts/backup_local.py restore --archive ".\\content-os-backup.zip" --target-dir ".\\content-os-data-restored"
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

Task 001–015 的最小工程链路已完成；当前提供第一真实素材 Gate 验收工具，需要使用创作者授权的 3–5 条视频和一份脚本记录实际选择与人工替换结果。Gate JSON 包含 Scene 数、Top-1 接受率、Top-3 覆盖率、本人素材使用率、替换次数、人工耗时和未知成本数。该工具只是 M1 真实素材验收界面，不代表个性化成片质量已通过。正式 `/app/` 已接入本地浏览器/手机上传、私网 token、备份恢复、草稿/渲染/导出和人工发布反馈边界；移动设备实机与完整生产闭环仍需 U3/真实 Provider 验收。Voice/Talking 仍按规格后置。ASR/Vision/Embedding/ScenePlanner 密钥只通过运行时构造 Provider 注入，不写入数据库或仓库；测试仅使用本地 fake server，没有调用付费 API。Worker CLI 支持前台轮询与 `--once`，并可启用独立连接自动心跳。

默认 SQLite 文件为 `content-os-data/content-os.sqlite3`，可通过 `CONTENT_OS_DB_PATH` 覆盖。Job API：

- `POST /assets/{asset_id}/jobs/analyze_asset`
- `POST /assets/{asset_id}/jobs/transcribe_audio`
- `POST /assets/{asset_id}/jobs/index_clips`
- `POST /clips/search`
- `GET /asset-library`
- `GET /m1-gate`
- `GET /workspace`
- `GET /`、`GET /app/`（正式 React/Vite 工作区；未构建时根路径回退到 `/workspace`）
- `GET /projects`、`POST /projects`（本地 Gate 测试项目）
- `GET /ip-profile`、`PUT /ip-profile`、`GET /ip-profile/revisions`
- `POST /imports`（本地文件或目录路径；不上传到第三方）
- `POST /uploads`（浏览器/手机 multipart 视频上传；大小上限 5 GiB，写入本地数据根并 hash 去重）
- `POST /audio-uploads`（浏览器/手机 multipart 本人录音上传；本地 ffprobe/hash 去重，必须带授权记录引用）
- `GET/POST /projects/{project_id}/shoot-tasks`、`PATCH /shoot-tasks/{task_id}`（补拍任务确认/拒绝/恢复；视频上传可用 `shoot_task_id` 绑定并在成功入库后标记完成）
- `POST /inbox/scan`（本地目录增量轮询；新导入素材默认自动排入本地 `ANALYZE_ASSET`，返回 `analysis_jobs`；只表示已入队，不宣称分析完成；可传 `enqueue_analysis=false` 关闭）
- `GET /assets/{asset_id}/readiness`（导入/预处理/转写/视觉/索引阶段状态）
- `GET /runtime/readiness`（不发送 Provider 请求的本机能力探测：区分可用、未配置、未开发和本机不可用；显式 lexical 模式会单独显示本地文字检索 ready，而不把 embedding 标成可用；不返回密钥；正式 Web 的运行设置页同时提供全局预算快照）
- 可选私网访问：设置 `CONTENT_OS_ACCESS_TOKEN` 后，除 `/`、`/app/*`、`/health` 外的 API 均要求 `Authorization: Bearer ...`；Web 页面只把用户输入的 token 保存在当前浏览器 sessionStorage，不写数据库。
- Tailscale/私网手机访问的手工配置与撤销步骤见 [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md)；不会自动修改网络、端口转发或防火墙。
- `GET/PUT /budget`、`GET/PUT /projects/{project_id}/budget`（全局/项目预算上限与未知价格策略）
- `POST /projects/{project_id}/provider-calls/reserve`、`POST /projects/{project_id}/provider-calls/{call_id}/complete`、`GET /projects/{project_id}/provider-calls`（Provider 调用预留、实际/未知成本对账和失败重试记录；runtime ScenePlan、项目 embedding 路由与 Worker ASR/vision/embedding 已复用同一 ledger）
- `GET /projects/{project_id}/cost-estimate`（从已保存的场景选择汇总执行前估价；逐场景保留成本明细，未知价格单独计数，不按 0 处理；完整选片后才加入本地渲染项）
- `GET /projects/{project_id}/cost-reduction`（只读提出已知同币种低成本候选；仅在用户点击确认后应用，未知价格不作为便宜替代）
- `POST /analysis-results`、`GET /analysis-results/{bundle_id}`（带 provenance 的 assisted-test/runtime 结果包）
- `POST /image-imports`、`GET /image-assets`、`GET /image-assets/{image_id}/media`（本地静态图片/截图/图表）
- `POST /audio-imports`、`GET /audio-assets`、`GET /audio-assets/{audio_id}/media`（本地已有配音/录音）
- `POST /projects/{project_id}/usage-events`、`GET /projects/{project_id}/usage-events`（成功导出后的素材使用记录；幂等，不计预览/失败/重试）
- `POST/GET /projects/{project_id}/publications`、`POST/GET /projects/{project_id}/feedback`（人工发布记录、来源指标和用户修改/拒绝反馈；不自动发帖）
- `GET/POST /opportunities`、`PATCH /opportunities/{opportunity_id}/status`（人工/历史内容/账号信号的有来源选题记录；幂等，不抓取全网）
- `GET/POST /account-connections`、`GET/POST /historical-content`（只读账号元数据与历史内容/字幕导入记录；不接收令牌、不自动发布）
- `GET/POST /voice-profiles`、`GET/POST /talking-profiles`（显式 consent 的参考 Clip/Profile 元数据登记；仅注册边界，不调用生成）
- `PATCH /assets/{asset_id}/usage`
- `GET /projects/{project_id}/draft`、`PUT /projects/{project_id}/draft`
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

转录任务默认需要运行时环境变量 `OPENAI_API_KEY` 或 `CONTENT_OS_ASR_API_KEY`；也可按上面的说明设置 `CONTENT_OS_ASR_PROVIDER=local`，使用已安装的 `faster-whisper` CPU 模型，不需要 API key。`index_clips` 会先执行 Vision 再写入 Embedding 索引，需要 `CONTENT_OS_VISION_API_KEY`、`CONTENT_OS_EMBEDDING_API_KEY`，二者均可回退到 `OPENAI_API_KEY`；各自可用 `CONTENT_OS_VISION_*`、`CONTENT_OS_EMBEDDING_*` 覆盖 base URL、模型和维度等配置。搜索 API 默认使用相同的 Embedding 运行时配置；没有 Embedding Provider 时，小样本可显式设置 `CONTENT_OS_RETRIEVAL_MODE=lexical` 使用本地文字重叠检索。该模式不写入或伪造向量，响应的 `score_basis` 为 `lexical_overlap`，只是可追溯的候选召回降级，不等同于校准后的语义相似度或模型重排。密钥不会写入数据库或日志。`--db`、`--data-root`、`--worker-id`、租约/心跳/轮询间隔和 `--max-attempts` 可覆盖默认值。

ScenePlan API 使用 `CONTENT_OS_LLM_API_KEY`（可回退 `OPENAI_API_KEY`），并支持 `CONTENT_OS_LLM_BASE_URL` / `CONTENT_OS_LLM_MODEL`。协议默认为 Responses；使用 OpenAI-compatible Chat Completions 的 Provider（例如 Kimi）时显式设置 `CONTENT_OS_LLM_PROTOCOL=chat_completions`，不会改变默认路径。当前返回内存中的 ScenePlan，不持久化用户脚本。`/m1-gate` 在 ScenePlanner 或 Embedding Provider 未配置时会明确显示“未配置/不可用”，不会用假数据冒充真实匹配或计划结果。

真实 assisted-test 使用抽样帧而不是整段视频；必须显式选择已有 Provider 的环境变量，脚本不会把 Anthropic key 交给 OpenAI-compatible 端点，或反向回退：

```powershell
$env:CONTENT_OS_ASSISTED_TEST_PROVIDER = "anthropic"
& ".\\.venv\\Scripts\\python.exe" scripts/real_assisted_test.py `
  "C:\\path\\video-a.mp4" "C:\\path\\video-b.mp4" `
  --output-dir ".\\content-os-data\\real-assisted-run"
```

OpenAI-compatible 路径使用 `OPENAI_API_KEY` 或 `CONTENT_OS_LLM_API_KEY`，可配 `CONTENT_OS_LLM_BASE_URL` / `CONTENT_OS_LLM_MODEL`；Anthropic 路径使用 `ANTHROPIC_API_KEY`，可配 `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL`。若模型有固定采样参数约束，可用 `--temperature` 显式传入。结果保留模型原文和抽样 provenance，但仍是 assisted-test，不自动宣称 runtime 或生产语义通过。

若本地数据库已导入这些原始视频，脚本会按内容 hash 生成可回放的 `analysis-bundle.json`；需要 API 已在本机运行时，可显式加 `--persist --api-url http://127.0.0.1:8000` 写入 `/analysis-results`。未能按 hash 找到全部 Asset 时仍保留原始模型结果，但不会把片段绑定到错误素材；画面中的文字只记录为 `available_subtitles`，不会冒充真实口播转写。

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

渲染器接受已持久化且授权信息完全匹配的本地 `user_asset` / `historical_asset` Clip、截图/图表和 Typography，支持 cut、9:16、字幕与源音轨；VideoSpec 可按场景绑定已导入且有授权引用的 `AudioAsset`，绑定场景会静音源音频并混入本地旁白。仍不生成 TTS，也不实现 Voice/Talking。

## 文档与契约

R1 工程范围以 `CONTENT_OS_EXECUTION_SPEC.md` 为准；`PRD.md`、`DEVELOPMENT_PLAN.md`、`BASELINE_FREEZE.md` 保留历史背景。完成状态、当前阻塞和下一个 ready 任务见 `STATUS.md`。

重新导出契约：

```powershell
& ".\.venv\Scripts\python.exe" contracts/export_schemas.py
```

Worker CLI 是前台轮询进程，可用 `--once` 执行一个兼容任务，也可持续轮询直到收到 SIGINT/SIGTERM；它不启动独立 daemon 线程。首次安装需要 Python 3.11+ 和可用的包下载网络；媒体分析还需要本机可执行的 FFmpeg / ffprobe。API、导入入口和 worker 会按环境变量覆盖、PATH、Remotion 随附二进制的顺序解析这两个工具；解压时将包内文件直接放到目标目录，避免再套一层 `Content OS` 子目录。

可重复的本地闭环报告（真实本机 FFmpeg/ffprobe、无外部 Provider）可用以下命令生成。两个二进制路径必须显式注入；成功报告会原子地写入被 Git 忽略的 `content-os-data/test-runs/<run-id>/`，其中包含夹具输入、ScenePlan、路由候选、VideoSpec、MP4、ffprobe JSON 和明确为 unmetered 的 usage ledger。

```powershell
$env:CONTENT_OS_FFMPEG = "C:\\path\\to\\ffmpeg.exe"
$env:CONTENT_OS_FFPROBE = "C:\\path\\to\\ffprobe.exe"
python scripts/run_local_fixture_report.py --run-id local-check
```
