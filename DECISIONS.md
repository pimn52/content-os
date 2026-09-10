# Content OS R1 重大取舍

仅记录会影响后续实现的短决策；产品范围和验收以 `CONTENT_OS_EXECUTION_SPEC.md` 为准，事实证据以 `STATUS.md` 为准。

## 2026-09-09

- 保留现有 FastAPI、SQLite、Job Runner、媒体管线、Provider 边界和 Remotion；不因审查起点重建或回滚。
- 单用户 R1 的项目复用第一个本地 IPProfile；创建后续项目不再生成空白 IP。多用户、多 IP 权限系统后置。
- `/m1-gate` 继续作为开发诊断入口。S0 先修真实请求体、状态失效、指标口径和 Clip 区间预览；正式 React/Vite 页面按 S1 逐步迁移。
- 浏览器 S0 回归可以注入测试 Provider 验证交互边界，但不计为 assisted-test 语义质量或 runtime 通过；真实模型辅助结果必须单独记录来源和模式。
- S2 前置采用 `analysis_result_bundles` 作为受控回放边界：结果包保留 input hash、模式、来源、模型/工具、时间戳、IP 快照、区间、字幕、关键帧和置信度；导入只回放已有资产/Clip 语义，重复 hash 幂等，冲突拒绝，且不改变素材的 production 用途。这样可接入真实分析结果而不把 fixture 或未授权素材冒充语义验收。
- 混合视觉先落地本地可审计路径：Typography 不依赖外部媒体，截图/图表用独立 `ImageAsset` 与 hash 去重；两者都必须经过候选显式选择、来源授权绑定和 Renderer 校验，不能伪称为视频语义匹配或自动生产授权。
- 配音先走已有本地音频：独立 `AudioAsset` 保留采样率、声道、时长、语言和授权引用；VideoScene 只允许经过时长校验的音频区间，Renderer 在存在新旁白时静音原声并混入该音频。TTS、Voice/Talking 和声音克隆继续后置，不能用旧原声覆盖新稿。
- 成功导出后的素材使用通过按项目/输出版本/媒体区间生成确定性 event key 幂等记录；只有 production usage endpoint 会增加 Clip 使用次数，预览、失败和重试不计入复用统计。
- S4 先落地 side-effect-free runtime readiness：只检查本机 ffmpeg/ffprobe/npm 和运行时 Provider 配置，不探测外部服务、不回显密钥，并明确区分 `provider_not_configured`、`not_developed` 与 `unavailable`；真实付费调用和预算授权仍集中后置到 U1/U2 的运行时准备环节。
- S4 预算边界先以 SQLite `budget_policies` + `provider_call_records` 落地：外部调用必须先 reserve，未知价格不能默认为零，失败/重试各自保留记录，complete 才写 actual cost；该边界不自动启用任何付费 Provider。
- S3 选题机会先落地为 `content_opportunities` 的人工/历史内容/账号信号输入记录：以来源引用幂等、可标记使用/忽略，并将机会与底层证据引用注入 ScenePlan；不在 R1 内抓取全网或虚构趋势。
- S3 账号路径先落地为 schema v15 的只读 `account_connections` + `historical_content` 导入边界：只保存外部标识、历史元数据、字幕和指标，不接受令牌，不自动同步或发布；未来 YouTube 等 Provider 可替换接入。
- 本地 Inbox 采用按需轮询 `POST /inbox/scan`，复用 Asset content hash 去重并返回每次扫描的新增/已有/失败计数；不在 R1 引入常驻 watcher、线程或新的队列基础设施，也不把导入冒充分析完成。
- Voice/Talking 先落地 schema v16 的 consent-gated profile registry：参考 Clip 必须已存在且 consent 明确确认，profile 只保存 provider/profile 标识与授权记录；生成、试听和质量验证继续保持未开发，不以旧口型或旧原声替代新口播。
- 正式 Web UI 采用 `apps/web` 的 React/Vite 增量迁移：后端 API、SQLite、任务队列与 Remotion 保持不变；生产构建静态挂载到 `/app/`，未构建时保留 `/workspace` 过渡壳，避免把 UI 迁移误做成后台重建。
- S5 升级恢复采用本地 zip + SQLite backup API + SHA-256 manifest：恢复必须落到新的不存在目录，并重定位数据根内媒体路径；不覆盖现有数据、不备份 live WAL/SHM、不接触 Provider 密钥。
- 手机/浏览器素材入口采用 `POST /uploads` 的单文件 multipart 流式导入，受 5 GiB 上限和视频扩展名校验约束；私网访问使用可选 `CONTENT_OS_ACCESS_TOKEN`，LAN 绑定没有 token 时由启动脚本阻断，默认 localhost 不增加登录步骤。
- assisted-test 允许显式选择 Anthropic 或 OpenAI-compatible 适配路由，各自只读取对应 key/base URL/model 环境变量；适配层只保存真实返回和结构校验结果，不在 Provider 不可用时跨服务猜测或生成语义替代物。
