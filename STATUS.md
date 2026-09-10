# Content OS 开发交接

日期：2026-09-07。

## R1 当前接续（2026-09-09）

统一执行规格：[CONTENT_OS_EXECUTION_SPEC.md](CONTENT_OS_EXECUTION_SPEC.md)；审查起点：[AUDIT_REPORT.md](AUDIT_REPORT.md)。以下是当前事实，不把历史 Task 001–015 的夹具通过写成真实 IP 质量通过。

### 2026-09-10 接续增量

- G12.2 Security Review 已收口：只读审查覆盖私网 Bearer token 与 loopback/LAN 绑定门禁、上传文件名/扩展名/5 GiB 流式上限、渲染输出与 ZIP 恢复路径、Provider URL/重定向不转发凭据、显式 argv 且 `shell=False` 的本地子进程，以及正式 React/旧入口的输出转义；未发现新的可利用问题，未做无依据的安全改动。安全边界定向回归 `87 passed, 2 warnings`；`apps/web` 与 `apps/renderer` 的 `npm audit --omit=dev --audit-level=high` 均报告 `0 vulnerabilities`。当前领取 G12.3 License Inventory，按本机安装元数据和锁文件整理直接依赖许可证与运行时注意事项。
- G12.3 License Inventory 初步收口：更新 `docs/DEPENDENCIES.md`，补列运行时直接依赖 `python-multipart`，把 FastAPI/Uvicorn/Pydantic/pytest/Av 的本机许可证元数据纠正为 MIT/BSD-3-Clause/Apache-2.0，并明确 `faster-whisper` 的模型权重、FFmpeg/Remotion 浏览器二进制、模型卡和再分发仍需发布前逐项复核；Web/Renderer 生产依赖漏洞审计均为 0。当前继续领取 G12.4 Release Gate，先做只读验收矩阵与工作区差异核对。
- G12.4 Release Gate 已收口：HEAD 仍为 `3dd3896 feat: add async render polling to M1 gate`，工作区保留统一规格/旧入口同步、S0–S5 增量和用户素材成果，未执行 reset、checkout、提交或删除；契约导出 32 份、Python compileall、正式 Web `tsc -b && vite build`、Remotion CLI 自检、`git diff --check` 通过；使用本机真实 FFmpeg/ffprobe 的全量回归为 `324 passed, 2 warnings`，Web/Renderer 生产依赖 `npm audit --omit=dev --audit-level=high` 均为 `0 vulnerabilities`。普通工程队列已完成，当前 ready 为 U3 产品验收：没有授权新旁白时不把 source-led 草稿标成新文案成片通过，等待用户对两条真实结果的内容/听感判断；TTS、口型/声音克隆、自动发布继续保持 `not_developed`/后置。
- U3 前置真实成片复验继续推进：只读检查发现 H.265 草稿原 `codec-03` 结束在“或者发热”，而同一授权素材的真实 ASR 仍有完整收尾“如果你喜欢……那么我们下次见”。已将该 ScenePlan 改为这段真实 ASR，显式选择对应真实 Clip `97541ea0-536c-5799-aa5e-7fcbf32989ce`，草稿升至 v5 并正式本地 Remotion 重渲染；产物 [0b83f6a7-e6a4-4c61-afed-3d4c57a5c5fe.mp4](content-os-data/renders/161996a7-2607-4a4b-9247-d305537ce839/0b83f6a7-e6a4-4c61-afed-3d4c57a5c5fe.mp4) 经 ffprobe 为 1080×1920 H.264/AAC、914 帧、30.466667 秒。没有生成新音频、没有复用无关原声承载新字幕、没有记录 production usage；古迹项目仍保留真实“到原片末尾”的警告。当前仍待 U3 主观内容/听感验收，不能把 v5 标为最终通过。
- G11.3 降本策略补齐：新增只读 `GET /projects/{project_id}/cost-reduction` 与正式 Web“按低成本替代方案”按钮；仅比较已知同币种价格且匹配度下降不超过 0.15 的候选，未知价格不会被当作便宜替代，应用仍需用户显式点击并保留原草稿可恢复。真实 Windows 浏览器加载 U1 古迹草稿，实际看到按钮、`0 USD`、3/3 场景及 `01 heritage-01 hook` / `02 heritage-02 development` / `03 heritage-03 close` 明细；未调用外部 Provider、未改变草稿。定向成本回归 `6 passed`，全量回归 `316 passed, 7 skipped, 2 warnings`，契约导出 32 份，compileall、Web 构建和 `git diff --check` 通过。
- G12.1 Full E2E Tests 补齐：新增 `test_full_local_e2e.py`，在真实 Windows FFmpeg/ffprobe 与实际 Remotion 上完成 API 导入 → 独立 worker 本地分析/连续 Clip → ScenePlan → 候选路由 → 两条授权本地录音绑定 → 1080×1920 MP4 渲染，并用 ffprobe 校验真实文件、分辨率、帧数和音频流；规划/向量仅为明确标注的编排协议替身，不冒充语义结果。测试另行断言 TTS/Talking readiness 为 `not_developed`，没有生成假音频或伪造口型。定向 E2E+成本/预算 `7 passed`，使用本机可用的真实 FFmpeg/ffprobe 时全量回归 `324 passed, 2 warnings`，契约导出 32 份、compileall、Web 构建和 `git diff --check` 通过。当前领取 G12.2 Security Review，先做只读安全边界审查，发现可修复项后继续最小修复与回归。
- G11.1/G11.2 成本规划前置补齐：新增 provider-neutral `ProviderCostEstimator` 接口与显式 `CatalogProviderCostEstimator`，默认缺价保持 unknown；新增 `CostLineItem`/`CostEstimate` 契约和 `GET /projects/{project_id}/cost-estimate`，从已保存的逐场景选择汇总已知金额、未知项、选片完整度与本地渲染项。正式 React 在真实 Windows 浏览器切换 U1 古迹草稿后实际显示“项目执行前成本估价”、3/3 场景、`0 USD` 与逐场景/本地渲染明细；没有调用外部 Provider。新增成本单元/API 回归，契约导出为 30 份，全量回归 `315 passed, 7 skipped, 2 warnings`，Web TypeScript/Vite 构建和 `git diff --check` 通过。未修改预算账本既有语义：未知价格不按 0 处理，Provider 仍须先 reserve。
- G10.4 Inbox 自动分析前置闭环补齐：`POST /inbox/scan` 对新导入且经 hash 去重的素材默认创建幂等 `ANALYZE_ASSET` 本地 Job，并返回 `analysis_jobs`；重复扫描不会重复入队，`enqueue_analysis=false` 可用于只发现/导入。正式 `/app/` 增加“新导入素材自动排入本地媒体分析”开关，素材卡片独立显示“媒体分析：排队中/处理中/已完成”和 ASR 状态；浏览器实际完成一次 Inbox 扫描并收到 `发现 49 个，新导入 44 个，已自动排入 44 个本地媒体分析任务`，随后已清理这次超出“最新 4 个”范围的 44 个本地副本和 Job，恢复扫描前 5 个 Asset/173 个 Clip。定向 Inbox/Job API 与全量回归均通过，Web 构建通过；未触发外部付费 Provider。
- S3/G10 Shoot List 补拍闭环补齐：`POST /projects/{id}/asset-routes` 的 capture gap 现在返回结构化 `shoot_list`（拍什么、机位、时长、是否需要说话、拒绝补拍后的本地替代/明确缺口）；正式 `/app/` 在候选结果下展示“补拍清单”，标明“可选，不阻断制作”，无缺口时也给出可见空状态。新增 schema 17 的 `shoot_tasks` 持久化、确认/拒绝/恢复 API；浏览器/手机视频上传可选择已确认任务，真实导入成功后自动标记 `fulfilled` 并绑定 Asset。强制 capture gap、幂等确认、拒绝/恢复和上传绑定的回归均通过；真实 Windows 浏览器加载 U1 项目并刷新候选，页面显示补拍区域与上传任务选择器；Web 构建通过（`index-BlGTO3Zb.js` / `index-DADfMX5p.css`），全量回归更新为 `312 passed, 7 skipped, 2 warnings`。当前用户库无已确认拍摄任务，未写入用户素材或改变现有 U1 草稿。
- 新旁白真实进入路径补齐：正式 `/app/` 新增“浏览器 / 手机上传本人录音”，服务端 `POST /audio-uploads` 以 multipart 接收 WAV/MP3/M4A/AAC/FLAC/OGG/Opus/WebM，执行授权引用、扩展名、5 GiB 流式上限、ffprobe 实际时长、content hash 去重与本地 `AudioAsset` 持久化；Vite 正式代理已覆盖该路径。定向回归覆盖成功导入与不支持扩展名拒绝；全量回归为 `309 passed, 7 skipped, 2 warnings`，`compileall` 通过；正式 Windows 服务加载新版 React 页面，浏览器实际看到 1 个 `audio/*` 文件控件和 1 个“上传并导入录音”按钮，服务停止后 8823 端口已释放。未凭空生成或从原视频抽取旁白，等待用户提供/录制真实且明确授权的新旁白。
- 当前领取的 ready 任务：U3 真实旁白成片复验；新录音进入后，按场景绑定并核验实际音频时长、字幕时间轴、源声轨静音、竖屏画面与导出/usage 记录。没有新旁白时继续保留古迹末尾警告，不把现有两条 source-led 草稿标为通过；TTS、口型/声音克隆和自动发布仍按规格后置。
- U3 结尾缺口可见性补齐：正式 `/app/` 的 Source-led 结果区现在检查末场 Clip 与原文件时长；若已接近文件末尾，明确显示“没有后续真实语音可补完”，并引导绑定新旁白或重新选择结尾镜头。真实浏览器加载 U1 古迹 v4 草稿实际显示该提示；H.265 复验因尾句延展后未触发该警告。Web 构建通过（`index-gcKmtRbf.js`），未改变草稿/素材或新增依赖。
- U1 结尾连续性修复并复验：source-led 装配对 `close/boundary/CTA` 场景只消费同一真实 Clip 中连续的 ASR 尾段，遇到自然停顿/标点或 4 秒上限即停止，不生成任何文字、不跨授权区间。实际重组 H.265 草稿后，`codec-03` 从 `257240–266800ms` 延展到真实连续语音 `257240–270640ms`，包含“播放…会产生明显的卡顿或者发热”；古迹 `heritage-03` 已到原文件末尾，未伪造补话。真实本地渲染产物 [dd18b58e-d376-4cc6-a432-2a53abe72fa4.mp4](content-os-data/renders/161996a7-2607-4a4b-9247-d305537ce839/dd18b58e-d376-4cc6-a432-2a53abe72fa4.mp4) 经 ffprobe 为 1080×1920 H.264/AAC、35.366667 秒；全量回归更新为 `308 passed, 7 skipped, 2 warnings`。
- 场景/脚本编辑边界补齐：正式 React 页面编辑脚本或任一场景文案时立即清空旧候选、场景音频绑定、VideoSpec 与本地渲染预览；保存后仍需重新生成 ScenePlan 并选片，避免新文字沿用旧语义路由。浏览器实际加载 U1 古迹项目，临时修改场景文案后 9 个候选按钮变为 0、VideoSpec 消失且“本地渲染”禁用；重新加载恢复未保存的 v4 草稿与候选。Web 构建产物 `index-E5O4C-tb.js` 已通过，未新增依赖。
- S2 assisted-test 正式入口补齐：`/app/` 新增 `AnalysisResultBundle JSON` 导入，可把模型实际产出的 `analysis-bundle.json` 交给 `/analysis-results` 做 input hash、asset ID、时间区间和来源校验，再回放到本地 Clip；浏览器实际加载入口，当前数据库已有 3 个 `assisted_test` bundle（`scripts.real_assisted_test` / `kimi-k3`）。该入口不接受固定夹具冒充语义结果。
- S2 provenance 可见性补齐：正式素材卡片显示已绑定分析的来源与模型；真实浏览器页面实际显示 `分析：scripts.real_assisted_test / kimi-k3`，并同时显示四个用户 MP4 的 ASR 阶段。构建产物已重新生成，未新增依赖。
- 本轮真实回放：使用 `content-os-data/assisted-runs/kimi-20260909-k3-video1-video2/analysis-bundle.json` 通过正确的 FastAPI lifespan 调用本地 `/analysis-results`，返回 `201` 并按既有 `input_hash` 幂等复用 bundle `186d86cf-c42a-56ba-9f24-690942785652`，包含 9 个结果；未重新调用 Kimi、未改变原始视频。
- S2/S4 正式入口补齐：`/app/` 现在逐素材显示 ASR 阶段，并可提交幂等 `transcribe_audio` 任务；排队/转写中自动轮询，失败/取消可重试，已完成可显式重新转写。入口只提交真实 Worker 任务，不生成字幕或假语义结果。浏览器以 `CONTENT_OS_ASR_PROVIDER=local` 实际加载正式页面，显示四个用户 MP4 的 `ASR：未开始/已完成` 状态和“提交 ASR/重新转写”按钮；`npm --prefix apps/web run build` 通过。
- S5 发布候选工程复验：定向回归 `48 passed, 2 warnings`（备份/恢复、浏览器上传与私网 token、渲染 API、Worker/任务租约与失败边界）；真实 Windows `start.ps1 -NoWorker -Port 8812` 完成启动→健康→停止→再次启动→项目恢复，前后均保留 4 个项目，正式 `/app/` 返回 200，第二次停止后端口已释放。正式 React 页面实际加载了本地素材、上传入口、运行能力和 U1 项目恢复入口；未调用外部 Provider，未产生付费接口费用。
- 当前领取的 ready 任务：S5 工程边界已复验，进入 U3 最终真实使用验收准备。U3 前仍不把无旁白 source-led 草稿宣称为最终成片；自动 TTS、口型/声音克隆、自动发布和无持久 Provider 的 runtime 闭环继续标记 `not_developed`/未配置。若用户在 U3 反馈修改，继续以真实素材和已授权录音推进，不生成假音频。
- S2/U1 真实本地 ASR 已落地：新增可替换的 `FasterWhisperASRProvider`（CPU `small`、`int8`），在四个用户授权 MP4 的现有 Clip 上完成真实转写；本地 `provider_call_records` 记录为 `faster-whisper:small`、已完成、0 USD，不把本地执行伪装成外部账单。模型权重仅保存在被忽略的 `content-os-data/models/`，未写入仓库或日志。
- S2 语义边界修复并复验：连续 ASR 段现在按归一化文本精确匹配，source-led 场景使用完整 ASR 段边界；Remotion 授权校验允许 Clip 内的已授权子区间，并以同一子区间计算 trim 帧，不再因为语义子剪辑回退整段或被渲染器拒绝。对应装配/渲染定向回归 `19 passed`。
- 两个 U1 项目已保留旧结果并升为 source-led ASR 草稿 v3；正式浏览器通过“按原声 + ASR 组装”落盘为 v4，页面明确显示 `Source-led VideoSpec 已组装`、`本地渲染完成`、预览与下载控件。古迹与 H.265 两条新本地 MP4 均为 1080×1920 H.264/AAC，分别约 29.952s、31.533s；旧 source-only 渲染文件未删除。
- 正式 React `/app/` 新增明确的 source-led 组装入口：带真实 ASR/SRT/VTT 字幕的原声稿可直接按时间轴渲染；新文案仍必须逐场景绑定已授权录音。这样把当前可用能力与后置能力分开呈现，不把原声裁切误称为自动文案、TTS 或对口型。
- 当前产品阶段结论：自动文案规划/ScenePlan、真实 ASR、候选路由、ASR 句界剪辑、原声字幕和本地渲染已可验证；真正“改写后的新文案 + 新配音”仍需导入本人/已授权录音；TTS、口型/声音克隆和自动发布仍是规格明确后置的 `not_developed`，外部付费接口保持后置。
- S5 发布候选补齐：正式 React `/app/` 在本地渲染完成后现在提供真实 MP4 下载、基础可编辑 SVG 封面模板下载和 UTF-8 文案下载；“确认导出并记录素材使用”仍是独立的人工确认动作，预览/失败/重试不会记 production usage。服务端受保护预览/下载路径和 sessionStorage token 行为保持不变。
- 本轮验证：`npm --prefix apps/web run build` 通过（TypeScript/Vite）；正式 FastAPI `-NoWorker -Port 8803` Windows 健康启动并在真实 `/app/` 浏览器页面加载 5 个本地素材、旁白门禁、运行能力状态与项目恢复入口；新增导出代码未触发外部 Provider。该轮未把当前无旁白的 U1 草稿渲染成“已通过”成片。
- S5/S2 开发入口修复：`apps/web/vite.config.mjs` 现在把 `/budget` 与 `/audio-imports` 纳入 Vite → FastAPI 代理；沙箱内 esbuild 首次启动受 Windows 路径权限限制，使用一次受限本地权限后实际启动 Vite，`GET /budget` 代理返回 200，`GET /audio-imports` 到达 API 并返回预期 405，随后 API/Vite 进程均已清理。
- S5 正式启动前置补齐：默认 API + Worker 模式下，`scripts/start.ps1` 现在把 `apps/web/dist/index.html` 与静态资源目录列为正式 Web 必需项；缺少构建产物会明确提示执行 `npm --prefix apps/web run build`，不再让默认入口静默回退到旧 `/workspace` 诊断页，`-NoWorker` 仍可独立用于 API-only 诊断。本轮 `start.ps1 -Port 8807` 的 API + Worker 预检全部 OK，浏览器打开 `http://127.0.0.1:8807/app/` 实测加载正式 React 页面、5 个本地视频、173 个 Clip、运行能力与项目恢复入口，随后已清理进程。
- S2 Hybrid 入口补齐：正式 React `/app/` 新增本地图片/截图/图表导入，复用后端 `POST /image-imports` 的 PNG/JPEG/WebP、用途类型和授权引用校验；Vite 开发代理同步纳入该路径。Windows 正式页面实测显示入口，页面加载 5 个本地视频/173 个 Clip 和运行能力状态；未把静态视觉当语义匹配或外部生成素材。
- 图片导入/路由相关 API 回归 `14 passed`（`test_asset_library_api.py`、`test_asset_route_api.py`）；`npm --prefix apps/web run build` 与 `git diff --check` 继续通过，工作区未新增依赖。
- 本轮收口回归：全量 `pytest -o addopts= --tb=no` 为 `300 passed, 7 skipped, 2 warnings`（307 collected）；Formal Web `tsc -b && vite build` 通过，PowerShell 启动脚本语法检查与 `git diff --check` 通过；8807、8806、8000、5173 端口均已释放。前端首次构建的 Windows 沙箱路径权限失败已用一次受限本地权限复核通过，不是产品代码失败。
- 当前领取的 ready 任务：S2/U1 的真实 ASR、句界剪辑和 source-led 音画字幕一致性已完成本轮验证；下一项直接推进不依赖外部付费接口的“新文案 + 用户授权录音”复验（若素材目录出现可绑定录音）或继续 S5 发布候选边界。Talking/口型仍为 `not_developed`，不重开战略讨论。

- HEAD：`3dd3896 feat: add async render polling to M1 gate`。
- 工作区：保留用户提供的 `AUDIT_REPORT.md`、`CONTENT_OS_EXECUTION_SPEC.md`、`START_HERE.md` 未跟踪文件；本轮新增/修改为入口文档同步、S0/S1/S5 代码、迁移、契约和测试，未执行 reset 或覆盖已有成果。
- S0 已通过：`m1_gate.py` 现在传 `ScenePlan.id`，异步渲染传 `{video_spec: ...}`；未确认项不计 Top‑1；选片、重新规划、切项目会使旧 VideoSpec 失效；候选预览按 Clip 区间 seek 并在区间末停止；项目创建复用首个默认 IPProfile。
- S0 浏览器证据：本地真实 MP4 + 注入测试 Provider 的可点击回归已完成：创建项目 → ScenePlan → Top‑3 → 两场均选第二候选 → VideoSpec 200 → render job `pending` → 播放候选 → 刷新回到空流程。第二轮还验证了草稿保存后刷新自动恢复（显示 `已恢复草稿 v4`、两场确认保留）。该服务只验证交互边界，不代表 assisted-test 语义或 runtime 通过；`agent-browser` CLI 在本机未安装/不可启动，已用本机浏览器控制面完成等价点击。
- S1/S3 已完成的最小持久化：SQLite schema v16 新增 `project_drafts`、`analysis_result_bundles`、`image_assets`、`audio_assets`、`asset_usage_events`、`publication_records`、`content_feedback`、`budget_policies`、`provider_call_records`、`content_opportunities`、`account_connections`、`historical_content`、`voice_profiles` 与 `talking_profiles`；草稿含 script/topic、ScenePlan、候选、确认选择、VideoSpec，支持版本号、PUT/GET、页面保存队列、localStorage 活跃项目恢复和应用重启恢复。正式 Web 现会在规划、候选和选片节点自动保存，并提供可恢复的脚本/旁白稿编辑入口。
- S1 已完成的最小 IP/用途入口：IPProfile 增加 knowledge/opinions/style_notes/boundaries；`/ip-profile`、`/ip-profile/revisions` 支持单默认 IP 的集中编辑和版本记录；`PATCH /assets/{id}/usage` 独立记录 `r1_usage` 与 `r1_identity`，不改变素材来源类型。
- S1 workspace 壳已接线：`/workspace` 可编辑默认 IP、查看项目草稿版本、创建主题项目并进入 Gate、按“用途/身份”保存素材分组；浏览器已实际保存一条 IP 资料版本和一条辅助画面用途记录。`POST /inbox/scan` 提供本地目录按需增量轮询，明确返回 discovered/imported/existing/errors，不伪装下游分析已完成。
- S1 正式 Web UI 已接线：`apps/web` 的 React/Vite/TypeScript 工作区把资料、素材/Inbox、选题机会、项目草稿、ScenePlan、候选、VideoSpec 和本地渲染串成同一条可操作流程；`npm run build` 通过，FastAPI `/` 实测 307 到 `/app/`，`/app/` 实测 200，未构建时仍回退 `/workspace`。
- S1 正式 Web 用途确认补齐：`/app/` 素材卡片现在可直接选择 `unknown/reference/production` 与 `creator/other/none/unknown`，保存时复用既有 `PATCH /assets/{asset_id}/usage`，成功后立即更新本地状态并提示生产候选边界。真实 Windows Edge 点击首个素材“保存分组”通过；`npm --prefix apps/web run build` 通过。
- S5 Windows 启动入口已加固：`scripts/start.ps1` 默认启动 API 与本地 analyze/render Worker，健康检查通过后再报告就绪，Ctrl+C/异常时清理本轮子进程；`-NoWorker` 保留诊断模式，外部 Provider Worker 不默认启用。
- S5 本地升级恢复边界已接线：`scripts/backup_local.py` 支持 `create/verify/restore`；创建使用 SQLite 一致快照、媒体/派生文件 SHA-256 manifest，恢复只允许新目录并重定位库内数据根媒体路径，不包含 live WAL/SHM 或 Provider 密钥。
- S5 移动入口与私网边界已接线：`POST /uploads` 接收单个浏览器/手机 multipart 视频，5 GiB 上限、扩展名校验、流式 hash/ffprobe 导入；`CONTENT_OS_ACCESS_TOKEN` 开启后保护 API，`start.ps1 -HostAddress` 对非 loopback 绑定强制要求 token，Web sessionStorage 仅临时附加 Bearer header，受保护的渲染预览通过临时 blob URL 携带凭据读取。
- S1 本地导入入口已接线：`POST /imports` 接受本机文件或目录路径，递归扫描支持的视频扩展名，复用 `MediaImporter` 的 content hash 去重、独立数据根和 ffprobe 探测；workspace 已提供路径/授权引用输入和失败提示。
- S1 阶段可见性已接线：`GET /assets/{id}/readiness` 汇总导入、预处理、转写、视觉和索引状态；workspace 可从真实素材卡片重跑对应 Job，状态会从 `not_started` 变为 `pending`，不把未执行阶段伪装成完成。
- S2 前置桥已接线：`POST/GET /analysis-results` 接受带 `input_hash`、assisted-test/runtime 模式、来源、模型/工具、时间戳、IP 快照、片段区间、字幕、关键帧和置信度的结果包；校验资产与时间戳后回放 Clip 语义，重复 hash 幂等，冲突拒绝，不自动赋予 production 用途。`scripts/real_assisted_test.py` 现在会在本地 Asset 完整按 content hash 对齐时生成 schema 可校验的 `analysis-bundle.json`，`--persist` 才会显式写入本机 API；画面文字不转写成 transcript。
- S2 IP-aware 与混合生产已推进：ScenePlan 带可追溯 IP/分析证据引用；运行时规划上下文包含当前 IP、版本和素材摘要；本地 Typography、PNG/JPEG/WebP 截图/图表可作为显式 fallback，经 VideoSpec 授权校验后由 Remotion 渲染；已有 WAV/MP3/M4A 等配音/录音可导入、区间校验、试听并混入，不把补拍缺口或旧原声伪装成新旁白。
- S2 旁白路径已接入正式流程：VideoSpec API 与正式 React 页面可按 Scene 绑定已导入的本地 `AudioAsset`，刷新/重启从草稿恢复；绑定场景源音频自动静音，渲染成功后的 usage 记录包含 audio。真实本机 FFmpeg 生成临时 WAV + Remotion smoke 已验证产出带旁白音轨的 MP4；仍不把该媒体 fixture 当作语义或 TTS 质量验收。
- S3 使用记录基础已接线：`POST/GET /projects/{id}/usage-events` 按项目、输出版本、媒体和 Clip 生成确定性 key；重复成功导出不会重复增加 `used_count`，预览/失败/重试不计 production usage。
- S3 发布/反馈基础已接线：人工发布记录按项目/输出版本/平台幂等；指标必须带来源；反馈记录保存接受、修改字段、拒绝原因和备注；`GET /projects/{id}/next-suggestions` 只基于最近反馈生成带 evidence refs 的规则建议，不自动发帖、不虚构指标。
- S3 有来源选题通道已接线：`GET/POST /opportunities` 接受人工、历史内容或账号信号记录，以 `source_type:source_ref` 幂等，支持 new/used/dismissed 状态；机会及其 evidence refs 注入 bounded ScenePlan 上下文，不自动生成趋势、不抓取全网。
- S3 只读账号/历史内容入口已接线：`GET/POST /account-connections` 与 `GET/POST /historical-content` 保存可追溯的账号外部标识、历史字幕/元数据/指标；重复导入幂等、内容变化冲突、缺失账号拒绝，API 不接受令牌且不触发账号授权或发布。
- S3 Voice/Talking 前置入口已接线：`GET/POST /voice-profiles` 与 `GET/POST /talking-profiles` 只登记已确认 consent、已存在参考 Clip 和 Provider Profile 元数据；不存在的 Clip、重复引用、未确认 consent 拒绝，生成/试听/质量验证仍未开发。
- S3 正式 Web 运营面板已接线：账号/历史内容只读记录、Voice/Talking consent-gated 登记、人工发布、反馈和 evidence-backed 下一轮建议均可在 `/app/` 操作；真实浏览器加载本地库时显示素材、173 个 Clip 与全部面板，控制台无错误。
- S4 能力检查已接线：`GET /runtime/readiness` 只检查本机 ffmpeg/ffprobe/npm 与 Provider 配置，不发送外部请求、不回显密钥，并区分可用、Provider 未配置、尚未开发和本机不可用；TTS/Talking/自动发布明确保持后置。
- S2/S4 Provider 边界补齐：ScenePlanner 默认仍使用 `/responses`，新增显式 `CONTENT_OS_LLM_PROTOCOL=chat_completions` 以兼容已验证的 Kimi/OpenAI-compatible Chat Completions；请求仍只在运行时读取环境凭据，返回内容继续经过本地严格 ScenePlan 校验。定向 ScenePlanner/API 回归 `18 passed`，未发送新的外部请求。
- S2 检索降级边界补齐：新增显式 `CONTENT_OS_RETRIEVAL_MODE=lexical` 的小样本文字重叠召回；不写入、不编造向量，`/clips/search` 与 Asset Router 均返回 `score_basis=lexical_overlap`/可追溯说明，默认 Embedding 路径保持不变。该模式只解决无 Embedding Provider 时的候选召回验证，不宣称语义相似度或模型重排质量。
- S4 readiness 与检索降级一致：新增独立 `retrieval` 能力状态；lexical 模式显示本地检索 ready，但 `embedding` 仍显示 Provider 未配置，完整 runtime_ready 门槛不放宽。定向 readiness/search 回归 `7 passed`。
- 本地运行时阻断已修复：`resolve_local_executable` 统一采用环境变量覆盖、PATH、Remotion 随附 `ffmpeg.exe`/`ffprobe.exe` 的顺序；Readiness、视频/目录/Inbox/音频导入入口和 Worker 使用同一解析结果。正式 `/app/` 浏览器只读复核显示 `local_media=可用`、`render=可用`。
- S4 预算/调用边界已接线：`/budget` 与项目预算支持金额/次数上限和未知价格显式策略；Provider 调用必须先 reserve，完成/失败/取消写入实际或未知成本，幂等键防重复预留，失败重试不会隐形吞掉预算。
- S4 运行调用已真正接入统一 ledger：runtime ScenePlan、项目级 embedding 路由，以及 Worker 的 ASR/逐关键帧 vision/embedding 会在 Provider 前 reserve、成功/失败后 reconcile；未知价格不转成 0，Provider Job 没有项目归属时安全终止而不调用外部服务。正式 `/app/` 新增一次性全局预算设置（金额/次数/是否显式允许未知价格）和调用快照。
- Provider 证据：当前进程的 `ANTHROPIC_API_KEY` 可读取但已返回 `401 authentication_error: API key is invalid`，没有 OpenAI key；按用户明确授权，从桌面 `moonshot key.txt` 临时读取并仅注入一次进程环境，未写入仓库/数据库/日志。Moonshot `/v1/models` 返回账号可用的 `kimi-k3` 等模型；用 `kimi-k3`、真实抽样帧完成 4 个 MP4 的视觉 assisted-test，模型原文、帧、输入 hash 和时间区间已保存到被忽略的 `content-os-data/assisted-runs/`，3 个 hash-bound bundle（共 19 个模型分段）已写入本机 `/analysis-results`。没有跨 Provider 回退，也未将字幕伪造成 transcript；Kimi key 不作为持久运行时配置保存。
- 证据：S0/S1/S2/S3 前置定向回归和 S4 readiness/预算定向回归已覆盖阶段状态、重试、分析结果回放、静态图片导入/候选/渲染、已有配音导入/区间校验/混入、IP-aware 规划、选题机会证据、只读账号/历史内容幂等、Inbox 增量扫描、Voice/Talking consent 边界、usage 幂等、发布/反馈幂等、反馈驱动建议、密钥不回显、预算上限和未知成本；全量回归、静态检查和契约导出已完成，契约为 26 份。

当前阻塞与边界：用户已提供 `Downloads` 中按修改时间最新的 4 个 MP4；已在本地导入并保存 hash/ffprobe 结果，四个素材已按用户授权标记为 `r1_usage=production`、`r1_identity=creator` 并归入默认 IP。S2 真实视觉 assisted-test 已完成并写回本地，但这不等于用户对语义质量、脚本/旁白或个性化成片的主观验收；Kimi key 仅本次临时使用，正式 runtime 仍需用户自行配置持久 BYOK 环境。尚无用户脚本或本人主观确认；正式 React/Vite 页面已可构建并静态挂载，Inbox 仍是按需轮询、尚无常驻 watcher，TTS/Voice/Talking 生成/试听/质量验证、账号 Provider 实际同步、完整 S3、S4 runtime 真实闭环和 S5 发布候选仍未宣称通过。

最新回归证据（2026-09-09）：机会/账号历史/Inbox/ScenePlan 定向回归 `5 passed`，Voice/Talking consent 定向回归 `33 passed, 1 skipped`；备份/恢复、浏览器上传/私网 token、assisted-test Provider 路由定向回归 `6 passed`；运行时解析/Worker 定向回归 `20 passed`；本轮真实备份 CLI Unicode 回归 `3 passed`；ScenePlanner/Embedding runtime ledger、预算边界、Provider 失败重试及 S0 页面定向回归 `49 passed, 2 warnings`；新增 ScenePlanner/API 协议定向回归 `18 passed`；旁白绑定/真实本地 WAV Remotion smoke、readiness/search 定向回归通过；当前全量 `pytest -o addopts= --tb=no` 为 `300 passed, 7 skipped, 2 warnings`（307 collected）；`apps/web` `npm run build` 通过，Remotion composition discovery 通过，FastAPI `/`→`/app/` 静态入口实测 `307/200`，S3 正式运营面板与 Readiness 真实浏览器渲染通过；`pip check` 无冲突；本轮 schema 导出为 28 份，`compileall` 与 `git diff --check` 通过。

已完成：S3 运营循环基础已推进到有来源选题、只读账号/历史内容输入、本地 Inbox 增量轮询、素材使用、人工发布、反馈记录和 evidence-backed next suggestions；S4 已补上无副作用 runtime readiness 与预算/Provider-call accounting，保持现有媒体、Provider、Job Runner 和 Renderer 不变。

当前领取的 ready 任务：U1 产品节点已收到用户反馈，当前不通过。已按用户选择的 2、4 分别准备“古迹与文化传承”和“H.265 与码率画质”两条候选；用户指出两条均有话未说完、横屏素材被直接裁成竖屏，未体现自动文案、语义剪辑或对口型，不能把当前结果当作产品验收通过。

本轮 U1 反馈后的安全门禁已完成：正式 `/app/` 在“新稿件 + 未绑定已授权旁白”时不再允许组装 VideoSpec；浏览器实际选择“U1候选 2｜古迹与文化传承”并点击“组装 VideoSpec”后显示“场景 hook 有新稿件但没有旁白”，服务端日志无 `/video-spec` 请求。API 层同时支持 `narration_required` 并拒绝相同违规请求；定向回归 `12 passed`，`compileall` 与 `git diff --check` 通过，Web 构建已通过。该门禁保留现有草稿/渲染成果，但不把两条旧产物标为通过。

当前领取的 ready 任务：S2 新稿音画一致性已完成第一道阻断修复，当前仍不通过；下一项直接推进真实句界/ASR 对齐与可用新旁白绑定路径。无完整转写或用户授权旁白前，不生成新的假音频、不复用旧原声承载新字幕；对口型继续保持 `not_developed`，外部付费接口仍后置。

本轮 S2 音画一致性推进：ASR 返回的真实 timestamped segments 现在随 Clip 持久化；源音频场景在有可匹配转写时按真实句界收窄 Clip，无法匹配时保守保留原区间，不猜测语义。新稿件的正式组装按每条已授权 AudioAsset 的 ffprobe 实际时长设置场景和旁白区间、同步收窄画面区间并静音源音频；同一整段录音不能隐式复用于多个场景。正式 `/app/` 新增“导入旁白 / 本人录音”入口，不再要求回旧 workspace。

本轮 S2 真实时间轴输入已补齐：正式 `/app/` 新增“导入真实时间轴字幕”和“为旁白绑定真实字幕”入口，分别接入 `POST /assets/{asset_id}/transcript-imports` 与 `POST /audio-assets/{audio_id}/transcript-imports`；只接受用户提供的 UTF-8 SRT/VTT，按整数毫秒解析、保留真实句段和 `transcript_source`，并把 VideoSpec/Remotion 的字幕按句段定时。该入口不把画面字幕转成 transcript，不生成音频，也不宣称 ASR/TTS 质量；后续句界剪辑只消费这类有来源的时间轴或真实 Provider ASR 结果。

本轮验证：正式 Web 浏览器显示旁白导入、素材时间轴字幕和旁白字幕绑定入口；选择 U1 古迹草稿并点击组装仍实际拦截为“新稿件但没有旁白”，服务端无 `/video-spec` 请求；服务端启动/停止后 8798 端口已释放。真实字幕解析/持久化/API、句段 VideoSpec 和 Remotion 定时字幕定向回归已通过；全量回归为 `297 passed, 7 skipped, 2 warnings`（304 collected），Web `npm run build`、Remotion composition discovery、契约导出、`compileall`、`git diff --check` 通过。U1 两条旧渲染成果仍保留且不标通过。

当前领取的 ready 任务：S2 真实 ASR/runtime 音频验证仍受当前进程无 ASR Provider 凭据、且 Kimi 当前 assisted-test 只证明视觉能力所限；已补“可追溯真实字幕/转写输入 → 句界剪辑 → 新旁白成片”的正式入口，并把真实句段传到 VideoSpec/Remotion 的定时字幕路径。保持付费 ASR/TTS 后置，不以固定字幕或合成音频替代真实语义结果；下一项继续进入 U1 成片复验，需真实 ASR 或用户提供 SRT/VTT 与已授权旁白后才可判定。

本轮 S2/U1 继续推进：Remotion 对真实视频源改为 `contain` + 深色背景，横屏素材完整保留而不再静默裁切；source-audio 模式保留脚本字段用于草稿/幂等指纹，但不显示未经转写验证的新文案，只消费真实时间轴字幕。定向回归 `22 passed`，全量回归已更新为 `300 passed, 7 skipped, 2 warnings`；正式 Web 构建、Remotion composition discovery、compileall 和 `git diff --check` 通过；本地 8801 浏览器实测看到完整横屏画面，且点击 U1 古迹项目“组装 VideoSpec”仍被“新稿件但没有旁白”门禁拦截，服务端无成功组装请求。旧 U1 MP4 未覆盖、未确认导出、未标通过。
- 追加本地最终渲染复验：`start.ps1 -Port 8802 -NoWorker` 健康启动并调用正式 `/render`，产物 [1ae87eae-0976-4db9-ac7d-837e5ce281af.mp4](<C:\Users\ASUS\Documents\AI coding\Content OS\content-os-data\renders\d626f16a-7082-439e-85a5-283ffc5f7c82\1ae87eae-0976-4db9-ac7d-837e5ce281af.mp4>) 经 ffprobe 为 1080×1920 H.264、`30.058667s`；抽帧确认完整横屏画面、原画面字幕保留、候选新文案不再叠到旧原声。该文件仅为 source-only 适配回归，不是 U1 语义验收产物；8802 已释放。
- 本地 ASR 复核：当前环境无 `whisper`/`faster-whisper`/`vosk`/`torch`/`transformers` 命令或 Python 包，也未发现本地模型权重；现有 WAV 只是媒体预处理派生文件，没有转写结果。因此继续保持“需真实 ASR 或用户 SRT/VTT + 已授权旁白”的阻塞边界。
- 已新增 `scripts/real_sceneplan_assisted_test.py` 及其 3 个边界测试：目标是用真实模型生成两条带 `input_hash`/证据引用的 ScenePlan artifact，默认不改草稿、不进 runtime ledger；本轮按桌面 Kimi key 发起外发前置申请时被本机安全审查拦截，原因是未对“本地项目/IP/素材摘要发送到 api.moonshot.cn”作足够具体的外发授权。未绕过、未发送该请求，脚本保留待明确数据外发授权后执行；Kimi key 未写入仓库、数据库或日志。
- 当前 ready 仍为 U1/S2 成片复验：无真实 ASR 或用户提供的 SRT/VTT、且无已授权旁白前，不生成假音频、不把旧原声承载新字幕、不宣称自动文案/语义剪辑/对口型完成；Talking 仍为 `not_developed`，外部付费接口保持后置。

接续命令：`& ".\.venv\Scripts\python.exe" -m pytest`；若要复现 S0 浏览器边界，运行 `& ".\.venv\Scripts\python.exe" ".\scripts\s0_browser_server.py"` 后打开 `http://127.0.0.1:8765/m1-gate`。运行时 Provider 仍只通过环境/依赖注入提供，禁止把测试 Provider 当成产品能力。

### 2026-09-09 接续增量

- 正式 Web 的脚本/旁白稿可编辑并单独保存；ScenePlan 生成、候选路由、候选选择和 VideoSpec 组装都会写回当前草稿，刷新/切换项目不会只留在前端内存。
- `scripts/real_assisted_test.py` 增加内容 hash 绑定：模型分析成功后，只有全部来源能匹配本地 Asset 才生成 `analysis-bundle.json`；`--persist` 通过本机 API 写入既有 `/analysis-results` 边界。画面文字仍只落在 `available_subtitles`，不伪造 transcript。
- 本增量验证：`services/api/tests/test_real_assisted_test.py` `4 passed`；`npm --prefix apps/web run build` `passed`（Vite 5.4.8）；`git diff --check` 无错误。沙箱内 esbuild 的 Windows 路径权限失败已用一次受限本地构建复核，不是代码失败。
- 本增量验证：统一本地 ffmpeg/ffprobe 解析及 Worker 默认路径；`test_runtime_readiness.py` 与 `test_worker_cli.py` `20 passed`；正式 `/app/` 只读浏览器确认 `local_media`、`render` 均显示可用；Kimi `kimi-k3` 真实抽样视觉分析覆盖 4 个最新 MP4，3 个 bundle 共 19 个分段已按 content hash 写回本地 API；`real_assisted_test.py` 增加显式 `--temperature` 以满足模型约束，`compileall`、`git diff --check` 和全量回归通过。
- S5 真实上传 smoke：使用 Downloads 中的真实 MP4 通过 `curl.exe` multipart 调用 `/uploads`，返回 `201`，经 hash 去重命中既有 Asset（数据库仍为 5 个 Asset），并由默认 Remotion `ffprobe` 返回 720×1280、136302ms、含音频元数据；未新增副本。
- S5 启动 smoke：`scripts/start.ps1 -Port 8782` 在真实 Windows 工作区启动 API 与 analyze/render Worker，健康等待后报告 `API + local analyze/render worker`，Ctrl+C 后端口已释放；无外部 Provider 请求。
- S5 真实备份恢复：当前 `content-os-data` 创建约 990 MB、306 文件的 schema v16 归档并成功 verify；恢复到新目录后保留 5 个 Asset、173 个 Clip、3 个分析 bundle，媒体路径全部重定位且可读。发现并修复 Windows Unicode manifest 路径导致 CLI 打印崩溃的问题，`backup_local.py` 现以 ASCII-safe JSON 输出并有回归覆盖；临时归档/恢复目录已清理。
- S5 移动 UI 加固：正式 Web 在窄屏下将项目标题/操作区改为纵向布局、操作按钮满宽可收缩、token 输入自适应，避免操作区横向溢出；`npm --prefix apps/web run build` 通过。
- S5 私网浏览器 smoke：在真实 Windows 以 `start.ps1 -Port 8783 -HostAddress 0.0.0.0` 启动并注入一次性测试 token；`/health`、`/app/` 分别返回 `200/200`，无 token 访问 `/assets` 返回 `401`，带 token 返回 `200`。本机 Edge 实际打开 `/app/`、输入 token 并点击“连接”后恢复工作区，显示 5 个真实素材、173 个 Clip，`local_media`/`render` 均为“可用”；验证后已停止进程并释放端口，token 未写入仓库或日志。
- S5 许可入口补齐：`docs/DEPENDENCIES.md` 增加正式 `apps/web/package-lock.json` 直接依赖的版本/许可证表，README 已链接；FFmpeg、Remotion 浏览器二进制与模型权重仍保留为发布前按具体构建核验的边界。
- 本增量验证：新增显式 `CONTENT_OS_RETRIEVAL_MODE=lexical` 的本地文字召回，覆盖无向量写入、搜索 API 的 `score_basis=lexical_overlap`、Asset Router 的可追溯说明及默认 embedding 路径不变；定向检索/路由回归 `16 passed`，后续全量回归 `283 passed, 7 skipped, 2 warnings`（290 collected），`compileall` 与 `git diff --check` 通过。
- 本增量验证：已有配音/本人录音现在可按 Scene 绑定到 VideoSpec；正式 React 页面显示已导入音频并在组装时提交绑定，刷新/重启从草稿恢复，渲染时仅绑定场景静音源音频并叠加本地旁白，确认导出会记录 audio usage。真实本机媒体回归 `test_real_remotion_renders_authorized_local_narration_over_source_clip` `1 passed`；`apps/web` 构建与 Remotion composition 清单通过。该验证证明本地音画路径，不证明 TTS/Voice/Talking 或 U1 语义质量。
- 下一 ready 任务：以已写回的真实分析、当前 IP/选题证据和素材约束，直接规划、路由、组装并渲染两条 U1 草稿；不把 Kimi 桌面 key 保存为持久配置，若后续 runtime 需要 Provider 继续沿 BYOK 环境注入。U1 仅在需要用户选择两条主题/脚本或主观判断成片时集中返回。
- 本轮增量验证：用户明确授权 Downloads 最新四个 MP4 按默认 IP 归类并用于生产，已将对应 Asset 元数据更新为 `r1_usage=production`、`r1_identity=creator`，并保留原件、hash、Clip、分析 bundle 不变；未将“可用于生产”扩大解释为语义质量或成片主观验收。
- 本轮增量验证：`ProviderCallLedger` 统一复用 reserve/finish 边界并接入 runtime ScenePlan、项目 embedding 路由和 Worker ASR/vision/embedding；未知价格在无显式策略时于 Provider 前阻断，失败重试使用新的调用记录，正式 Web 提供全局预算/次数/未知价格设置。相关定向回归覆盖成功、失败、预算阻断和重试；随后全量回归为 `288 passed, 7 skipped, 2 warnings`（295 collected）。
- 本轮 U1 候选准备：用户选择 2、4 已按前述真实 Kimi 分析结果映射为两个默认 IP 项目；两份 `ProjectDraft` 均为 v2、3 个真实 Clip、约 30 秒，并使用稳定的 ScenePlan ID、真实分析 bundle/Clip 引用。检索链曾出现词面召回跨素材，已收紧到对应生产 Asset；词面分数仍仅作为可追溯弱证据，没有冒充语义验收。
- 本轮 U1 本地播放证据：两份 VideoSpec 均通过本地 Remotion 实渲染并经 `ffprobe` 校验为 1080×1920 H.264 + AAC，古迹候选 `30.058667s`、编码候选 `30.000000s`；正式 `/app/` 浏览器实际打开项目并点击古迹候选播放，编码候选通过页面渲染入口进入缓冲态，直接产物已独立校验，刷新后编码项目恢复为草稿 v2。产物分别为 `content-os-data/renders/d626f16a-7082-439e-85a5-283ffc5f7c82/f70bf9ee-c1ac-431d-8b8d-abea2e892cbb.mp4` 与 `content-os-data/renders/161996a7-2607-4a4b-9247-d305537ce839/965a8d47-805c-4630-9863-07885d887bc3.mp4`；未点击“确认导出并记录素材使用”，未发布。
- 本轮 U1 恢复修复：正式 Web 按项目在当前浏览器标签页保存成功渲染 ID，刷新/切换后先用受保护的 `HEAD /projects/{project_id}/renders/{render_id}` 校验产物，再恢复视频 URL；带私网 token 时改为受保护 blob URL。脚本、选片、旁白变化会清除旧渲染引用；新增 `test_render_api.py` HEAD 断言 `2 passed`，`npm run build` 通过，真实浏览器重新渲染后刷新显示“已恢复上次本地渲染”，播放控件仍可用。
- 本轮 U1 一致性加固：渲染缓存现在同时保存当前 VideoSpec 指纹，草稿已换稿/换片后不会恢复旧 MP4；旧格式缓存会安全丢弃。类型检查与 `npm run build` 通过；重新走真实编码候选渲染并刷新，页面显示“已恢复上次本地渲染”，VideoSpec 与三场景草稿仍保持 v2。未确认导出，未写入 usage。
- 本轮 S5 启动生命周期修复：`scripts/start.ps1` 的 API 与 analyze/render Worker 改为继承同一 Windows 控制台，不再用隐藏脱离控制台的子进程；保留健康等待、Worker 异常回收和 finally 精确清理。PowerShell 语法检查通过；真实工作区 `-NoWorker -Port 8790` 健康后可运行，API+Worker `-Port 8791` 精确终止 API 后入口自动回收 Worker，端口释放且无本轮残留；未触发外部 Provider。
- 本轮 S5 启动预检补齐：单入口启动前显示 Python/Node/npm/FFmpeg/ffprobe 实际版本及 Remotion `package-lock` 版本；显式环境变量指向不存在的本地可执行文件时会阻断默认 Worker 模式并给出恢复动作，`-NoWorker` 仍可用于 API 诊断。真实 Windows 正常预检/健康启动、缺失 FFmpeg 模拟阻断、`git diff --check` 和 PowerShell 语法检查均通过。
- U1 反馈后的事实核对：两份当前草稿的 `narration_asset_id` 均为 `null`，VideoSpec 仅把候选 `voice_text` 放入静态 `caption`，同时保留源视频音轨；当时渲染器对视频使用 `objectFit: cover`，所以横屏素材会被裁成竖屏。该问题已在后续改动中修为 `contain`；两条原产物均为约 30 秒 H.264/AAC，而不是新旁白或口型生成结果。
- U1 结论与后续边界：用户反馈的“戛然而止”是当前候选的实际缺陷，不是应被验收的降级体验；根因是没有完整 ASR/句界对齐，当前固定 Clip 区间按约 10 秒硬切。ScenePlanner/脚本接口虽已存在，但这两条未经过持久 runtime Provider 自动生成并未绑定新音频；S2 下一任务应先做真实句界剪辑与新稿音频对齐，再做混合 B-roll/排版。`talking` 在 runtime 仍明确为 `not_developed`，对口型不能以旧原声或旧口型冒充完成。

### 本批次交付报告

- Changed files：见当前工作区 diff；主要包括旧文档入口、`m1_gate.py`、`workspace.py`、`scripts/start.ps1`、`scripts/backup_local.py`、`scripts/real_assisted_test.py`、`services/api/app/backup.py`、`services/api/app/main.py` 上传/私网边界、项目/IPProfile/草稿/用途/视频/图片/音频导入/分析结果/IP-aware 规划/混合渲染/usage/发布/反馈/选题机会/账号历史/Inbox/Voice-Talking API、schema v16、Repository、JSON Schema 导出及针对性测试；新增 `apps/web` React/Vite 页面、`services/api/tests/test_backup_local.py`、`services/api/tests/test_upload_access.py`、`services/api/tests/test_real_assisted_test.py`、`scripts/s0_browser_server.py` 和 `scripts/import_local_media.py`，前者用于交互回归，后两者分别用于无网络本地导入和受控抽样帧 assisted-test。
- Tests run：S0/S1/S2 前置定向 pytest、机会/账号历史/Inbox/ScenePlan 定向 pytest、Voice/Talking consent 定向 pytest、Repository/media migration pytest、S4 readiness/预算定向 pytest、备份/恢复与浏览器上传/私网 token 定向 pytest、assisted-test Provider 路由定向 pytest、全量 `pytest -o addopts= --tb=no`（`265 passed, 6 skipped, 2 warnings`，271 collected）；`apps/web` `npm run build`、FastAPI `/`→`/app/` 静态入口实测、`pip check`、`compileall`、`git diff --check`、26 份契约导出通过；`start.ps1 -NoWorker` 默认 loopback 端口 8772 实际启动并通过健康等待，LAN 无 token 拒绝检查通过，随后已清理测试子进程；浏览器实际点击回归如上。
- Acceptance：S0 工程与浏览器验收通过；S1 默认 IP、草稿重启恢复、IPProfile 版本、用途/身份、目录导入/hash 去重 API、按需 Inbox 扫描、阶段状态与重试、workspace 过渡壳和正式 React/Vite 静态工作区通过；S2 分析结果导入/回放、IP-aware 规划上下文、视频/图片/排版 fallback、已有音频导入/区间校验/混入边界通过；S3 有来源选题、只读账号/历史内容记录、consent-gated Voice/Talking 注册、正式运营面板、usage、发布、反馈幂等及反馈证据建议边界通过；S4 readiness 分类、密钥不回显、预算预留/未知成本/失败重试记录边界通过；S5 本地 Windows 单入口启动/健康等待、可验证本地备份/恢复、浏览器/手机上传、可选私网 token 工程边界已实现并测试；真实 assisted-test、用户真实旁白质量、Voice/Talking 生成/试听、常驻 Inbox watcher、账号 Provider 实际同步、完整 S3、S4 runtime 真实闭环、S5 发布候选尚未宣称通过。
- Known limitations：正式运行时仍没有可用的持久 Provider（Anthropic 401，不能跨服务猜测 Provider）；TTS/Voice/Talking 生成/试听/质量验证、常驻 Inbox watcher、移动端真实设备检查、预算/发布反馈/runtime 独立运行仍缺；browser smoke 的语义标签是测试控制值，四个本地视频仍保持 reference/unknown；正式 Web 目前覆盖核心工作流，仍需 U1 真实草稿主观验收；私网 token 仍是单机预共享凭据，不替代正式账号/权限系统。
- Dependency/license concerns：新增 `apps/web` React 18、Vite 5、TypeScript 5 及 React 插件，依赖已锁定在 `apps/web/package-lock.json`；新增 PyPI `python-multipart` 0.0.32（multipart 解析运行时依赖，当前发行元数据未声明 License，发布前必须核验上游许可）；生产 Node 依赖 `npm audit --omit=dev` 为 0 漏洞，开发链报告 esbuild moderate/high 风险，自动修复会强制升级 Vite 超出当前依赖范围，暂不自动升级。Provider assisted-test 路由实现不新增网络依赖。React/Vite/TypeScript 及插件许可、FFmpeg/Remotion/模型权重许可仍按发布前清单核验。
- Recommended next task（执行器应直接领取，不等待确认）：在有效 Provider 配置就绪后直接重跑 `scripts/real_assisted_test.py`，产出带 provenance 的 S2 结果和两条可播放草稿；不以 browser smoke 的测试 Provider 或固定夹具替代语义结果。

## 本批次范围

已完成 Task 001–015 的最小工程链路，包括核心契约、SQLite/任务基础、媒体导入与理解、检索、素材库 UI、ScenePlan、Asset Router、VideoSpec、基础 Remotion Renderer 与可重复端到端成片夹具。新增 `/m1-gate` 本地验收页及 `GET/POST /projects` 测试项目接口，可记录 ScenePlan、Top-1/Top-3 候选、人工替换、VideoSpec 与 Gate JSON 指标（Scene 数、Top-1 接受率、Top-3 覆盖率、本人素材使用率、替换次数、人工耗时、未知成本数）。合成媒体工程门禁已通过，但尚未进行创作者真实素材质量验收，因此不将真实素材 Gate 标记通过。

- Terra：核心数据模型、JSON Schema、示例、校验测试。
- Luna：FastAPI、健康检查、Python 项目配置、Windows 启动文档。
- Sol：G0 时间/序列化/渲染契约审查。
- 主 Agent：基线对齐、集成与交付。
- Luna：SQLite 迁移、IPProfile / Project / Asset / Clip / Job Repository、事务与关系校验。
- Terra：任务幂等入队、原子领取、租约、心跳、重试上限与崩溃恢复。
- Luna：文件/流式媒体导入、内容寻址存储、ffprobe 封装与基础 fixture 测试。
- Terra：媒体并发去重、数据库/文件原子性、探测回退与迁移失败强化。
- Terra：连续 Clip 检测、时间边界合并、确定性 Clip ID 与原子 Clip 集替换。
- Luna：标准分析音频与关键帧派生文件；Terra 完成跨模块幂等/并发集成。
- Terra：provider-neutral ASR 契约、OpenAI-compatible BYOK 适配器、安全错误分类与本地 HTTP 边界测试。
- Luna：时间戳转写到连续 Clip 的重叠映射、权威快照替换与 SQLite 原子持久化。
- Luna：Asset Job target 迁移、原子绑定、跨进程幂等与冲突回滚。
- Terra：同步 JobRunner、终态/重试/租约丢失语义，以及媒体分析/ASR handler 集成。
- Terra：长任务独立 SQLite 连接自动心跳、续租失败检测与线程清理。
- Luna：持久化默认路径、Asset Job 入队/状态 API、lifespan 连接管理与并发请求验证。
- Terra：可中断轮询 Worker、启动租约恢复、按已注册类型安全领取与优雅停机。
- Luna：Worker CLI 运行时组装、信号处理、BYOK 配置校验与 console script。
- Terra：provider-neutral Vision 契约、OpenAI-compatible Responses 适配器与本地 HTTP 边界测试。
- Luna：视觉元数据映射、原子持久化与视觉管线隔离测试；主 Agent 完成 `INDEX_CLIPS` handler/Worker/API 接线。
- Terra：provider-neutral Embedding 契约与 OpenAI-compatible 批量适配器。
- Luna：SQLite Clip 向量持久化与余弦过滤检索；主 Agent 完成版本化迁移、索引管线、自然语言搜索 API 与 Vision→Embedding 作业串联。
- Luna：Task 010 本地素材库页面、只读 Asset/Clip/媒体 API；主 Agent 修正语义搜索接线并完成浏览器交互验收。
- Terra：Task 011 provider-neutral ScenePlanner、OpenAI-compatible 严格 JSON Schema 适配器；主 Agent 完成 Project→ScenePlan API。
- Terra：Task 012 provider-neutral Asset Router、本地连续 Clip 多因子排序与补拍缺口；主 Agent 加固时长/来源约束并完成路由 API。
- Terra：Task 013 纯本地 VideoSpec Assembler、精确有理帧换算与契约边界；主 Agent 完成组装 API。
- Terra：Task 014 基础 Remotion Renderer、授权本地 Clip 校验、Windows npm shim 与源音轨时间线。
- Terra：Task 015 双素材真实编码夹具，验证 FFmpeg 导入、SQLite、VideoSpec、Remotion MP4 与 ffprobe 输出。

未调用任何生成供应商，不产生本产品的媒体生成 API 费用；Agent 本身的运行消耗由当前平台计量。

## 当前限制

- 已能导入、探测、切分视频、提取音频/关键帧，通过可替换 ASR/Vision 边界写回 Clip，并将 transcript + Vision + Clip metadata 生成 Embedding 后在 SQLite 本地 Top-K 检索，再由 ScenePlan/Router/VideoSpec/Remotion 输出基础 MP4；尚未实现自动写稿、克隆声音、口播生成或发布。
- Project / Asset / Clip / Job 的基础外键，以及 Clip 与数据库 Asset 时长一致性已由 Repository 校验；源文件是否存在、数据库时长是否与真实媒体一致，留待导入/ffprobe 与 assembler 验证。
- 授权记录是数据凭据，无法仅凭字符串核验权利；撤销授权与生成前检查在对应业务流程实现。
- 任务状态、幂等、领取、租约、自动心跳、有限重试、崩溃恢复、媒体/ASR handler、最小任务 API 与前台轮询 Worker CLI 已接通；尚无系统后台服务、预算预留/对账或任务 UI。
- Python 依赖范围不是锁文件；Windows 安装与完整依赖审计仍待后续。FFmpeg/ffprobe 当前作为外部工具，不随仓库分发，发布前需按具体构建核验 GPL/LGPL 与编解码库许可。

## 继续开发阶段

Task 014/015 已完成。按冻结基线暂停功能扩展，进入首个真实素材 Gate。执行器不会在长耗时媒体/网络调用期间持有 SQLite 写事务，也不会把供应商密钥持久化。

真实媒体验收阶段再使用用户授权的 3–5 个视频和一个脚本。没有这批真实素材前，不能声称个性化成片质量通过。

## 本批次验证结果

- `python -m pip install -e ".[test]"` 成功，验证了项目安装配置。
- `python -m pytest`：11 passed，2 条依赖弃用警告。
- 13 份 JSON Schema 导出成功；3 个关联示例 JSON 往返校验通过。
- Uvicorn 实际启动及 `/health` 请求此前已通过。
- Sol 初审提出的非有限 metadata、时间线标识/时长问题已修复；Sol 最终复核受用量限制未完成，主 Agent 已检查最新实现并运行测试。G0 为本批次内部通过，不宣称完整独立审查通过。
- Windows 本地 Python 3.12 虚拟环境、PowerShell 启动脚本及 `/health` 已验证。
- Task 003 完整测试为 `25 passed`；文件型 SQLite 多连接并发入队/领取压力用例连续复跑 20 轮通过，`pip check` 与 `compileall` 通过。
- 已建立本地 Git 基线提交 `0c033aa`，后续开发变更均可通过 Git 审查与回滚。
- Task 004A 使用真实 FFmpeg 9.0.1 / ffprobe 生成并导入带音频视频，完整测试 `54 passed`；媒体并发导入压力用例连续复跑 10 轮通过。
- Task 005/006 使用三段硬切画面及真实音轨验证连续 Clip 时间戳、WAV 时长/起点/采样率、JPEG 尺寸、源文件不变与重复运行稳定；集成后完整测试 `76 passed`。
- Task 007 通过本地 fake HTTP server 验证 multipart、segment 时间戳、错误分类、BYOK 不泄露及禁用携密重定向；Clip 映射验证跨镜头重叠、清理过期转写、重复执行稳定与事务回滚；完整测试 `105 passed`。
- Job 执行边界验证 Asset target 同事务入队、并发幂等、handler 事务外执行、重试/终态/中断恢复、错误脱敏，以及 ASR segment 经 Worker 写回 Clip；集成后完整测试 `126 passed`。
- 自动心跳与 Job API 集成后完整测试 `135 passed`；8 路 API 并发入队及 heartbeat/runner 聚焦套件连续复跑 10 轮通过。
- 可控轮询 Worker/CLI 已验证空闲中断、多任务、启动恢复、部分类型隔离、handler 内停机等待、参数安全与连接关闭；console script 实际安装/帮助命令通过，完整测试 `156 passed`。
- Vision 阶段通过本地 fake transport 验证 Responses 请求、JPEG data URL、严格 JSON Schema、错误分类、携密重定向禁用与 BYOK 脱敏；视觉管线验证 Clip/keyframe 身份绑定、供应商失败零写入、原子替换、重复执行稳定，并完成 `INDEX_CLIPS` API/Worker 接线；完整测试 `183 passed`。
- Embedding/检索阶段通过本地 fake transport 验证批量顺序、维度/有限值、错误分类、携密重定向禁用与 BYOK 脱敏；SQLite schema v4 保存向量与 `embedding_ref`，支持 asset/orientation/talking filters、稳定余弦 Top-K，并以“本人坐在电脑前操作软件”用例返回合理 Clip；完整测试 `197 passed`，未调用真实供应商。
- Task 010 素材库通过 API 测试验证 Asset/Clip 列表、详情、404、媒体文件与区间校验；实际浏览器验证页面加载、空库状态、查询输入和 `/clips/search` 错误反馈，完整测试 `199 passed`。
- Task 011 通过本地 fake server 验证 Project/脚本/主题请求、严格 ScenePlan JSON Schema、连续 order、唯一 scene_id、真实素材优先、错误分类、禁携密重定向与 BYOK 脱敏；Project→ScenePlan API 验证项目绑定、404、运行时配置和拒绝请求内密钥，完整测试 `209 passed`。
- Task 012 以本地 SQLite fixture 验证“本人坐在电脑前操作软件”返回合理连续 Clip Top‑1/Top‑3、真实素材来源过滤、时长满足、语义/视觉质量/镜头适配/新鲜度/复用惩罚、稳定排序、零供应商成本和补拍缺口；路由 API 验证 Project 绑定，完整测试 `215 passed`。
- Task 013 验证 29.97/30fps 精确毫秒→帧换算、连续 start_frame、Project/Asset/Clip 身份、授权引用、短素材和 Capture gap 阻断、显式候选覆盖、确定性与 VideoSpec JSON 往返；本地组装 API 已接通，完整测试 `222 passed`。
- Task 014 验证授权/身份/本地路径、源帧裁切、字幕、源音轨、9:16/cut 限制、失败清理和 Windows `npm.cmd` 解析；Remotion 4.0.522 完整打包并启动 Chrome Headless Shell。
- Task 015 以两段真实编码的 360×640、30fps、AAC 合成素材贯穿导入、SQLite、双场景 VideoSpec 与实际 Remotion 渲染；ffprobe 验证输出为 24 帧、约 0.8 秒且含音轨，原始与内容寻址文件哈希未改变；完整测试 `227 passed`。
- 便携 FFmpeg 仅放在本机临时目录用于开发验收，下载包 SHA-256 已按发布方值核对，未提交仓库。
- 尚未使用用户真实 IP 素材；当前仅证明媒体工程链路，不代表个性化素材理解通过。
- M1 Gate 工具仅用于第一真实素材验收：Provider 未配置时明确返回不可用状态，不伪造 ScenePlan 或匹配结果；Voice/Talking 仍按 Gate 5/6 后续推进。
- Gate 7.2/7.3 本地渲染任务已纳入 Job Runner：`POST /projects/{project_id}/render-jobs` 以幂等键入队，Worker 支持 server-owned MP4 输出、超时/进程失败有限重试、输入/授权/资源错误终态失败；`GET /jobs/{id}` 返回状态及预览/下载链接，重启后的过期租约继续由既有 recovery 流程接管。
- 本轮完整测试：`232 passed, 6 skipped`；未新增依赖。异步渲染测试使用本地确定性 fake renderer，真实 Remotion/FFmpeg 路径仍由 Task 015 与本地 fixture 报告覆盖。
