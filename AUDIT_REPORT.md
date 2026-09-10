# Content OS 全局审查报告

日期：2026-09-09。
仓库：[pimn52/content-os](https://github.com/pimn52/content-os)，默认分支 master，审查提交 [3dd3896b4c39d9929faa8714413a027713c0301a](https://github.com/pimn52/content-os/tree/3dd3896b4c39d9929faa8714413a027713c0301a)。
资料：用户上传的《接手 Content OS 本地开发》会话导出、当前用户修改后的产品战略基线、仓库源码/测试/文档。没有读取用户 Windows 当前未推送代码、实际四条视频或本机凭据。

## 结论

确实发生了局部执行偏差和交付目标失焦，不需要推翻工程架构或从头开发。不能仅凭会话断定模型能力不足：本地 Agent 已读取前期文件，完成了大量有价值的工程；但未把产品目标、阶段门禁和最新用户约束稳定结合。此前我交付的多份文档、严格停止条件和局部下一任务也造成了诱因。

目前是较完整的媒体工程原型及验收工具，不是可持续运营的成熟个人 IP 产品。现有 001–015 的“最小工程链路完成”不能解释为完整 PRD 功能完成。

处置：保留数据/任务/媒体/Provider/Renderer；立即修真实交互阻断；补 IP 内容上下文、真实语义测试和新音频路径；按单用户 R1 运营闭环连续推进。具体唯一目标见 CONTENT_OS_EXECUTION_SPEC.md。

## 1. 会话证据：偏在哪里，哪些不是偏差

引用行号对应用户上传的 Markdown 原文，仅为定位，非断言这些内容均已写入 Git。

| 发现 | 会话证据 | 判断 |
|---|---|---|
| 模型确实看过前期方案 | 开头读取两个原会话及 PRD/冻结/计划/交接；接手复现 11 tests | 不能说它完全没看到全局；问题在执行时选择性遗忘/局部化 |
| 已反复授权持续推进 | 用户在 853、4565、5286 行等要求持续推进、无需每步汇报 | 普通任务后继续“推荐下一步/等继续”不符合最新执行偏好 |
| 把历史会话标题当成内容主题 | 5019–5023 行让用户确认 NASCAR 稿；5515–5516 行要求填 NASCAR 主题并粘贴原会话脚本 | 未见用户确定 NASCAR 为当前内容稿；且 m1_gate.py 中出现该例子。应移除默认假定，先用实际 IP 资料和真实主题 |
| 对已有音频规划误判 | 4662–4693 行把音频路径说成需要新定义 MVP；用户 4695 行指出 PRD 已讨论；4854–4858 行 Agent 撤回 | 应解释“尚未实现”，而不是把阶段差异当架构缺失。现有原声路径本身可作为技术验证 |
| 推理测试没有完成用户目标 | 用户 5026 行明确外部接口后置、测试可用 Codex；后续使用 deterministic fixture、实际用量 null | 不接外部 API 是遵循用户要求；不造成本也是正确。缺口是没有提供实际模型辅助语义测试，只完成固定测试链 |
| 身份问题扩大成多 IP 问题 | 5599–5668 行从混合素材走向多 IP 绑定/策略/反复 Sol 复核 | 有必要区分本人和其他人物；没有必要由此建设多 IP 权限系统。会话中的复杂方案是讨论，不能声称仓库已实现 |
| 收窄后仍遗漏辅助素材 | 5688–5696 行仅列本人/参考/不使用，并只匹配本人素材 | 需要保留“可用辅助画面”。本人 IP 连贯不等于每个镜头都是本人 |
| 技术 Gate 被推给用户排障 | 5459 之后给多个终端及手动 ScenePlan/VideoSpec 步骤，随后承认 Provider 未配置 | 这是研发验收工具，不是用户可验收成品；应先完成 ready-check 和可播放草稿 |

用户没有要求全市场、多人协作或无限自动化。用户的研究用途和延后外部接口也是明确约束；不能因审查而偷偷取消。

## 2. 已确认的代码阻断和产品缺口

### P0：当前验收页面无法完整提交两步关键请求（已复现）

1. `m1_gate.py` 的 `spec()` 把 `scenes[i].scene_id` 放入 `explicit_scene_ids`。前者通常是 `hook` 等业务标签；`main.py::VideoSpecAssemblyRequest` 要求 `list[UUID]`，assembler 也以 ScenePlan.id 为键。结果是 422。应传 `scenes[i].id`，并验证推荐与非推荐候选两条路径。
2. `m1_gate.py::render()` 发送 `{...lastSpec, idempotency_key}`；后端要求 `{video_spec:lastSpec, idempotency_key}` 或完整的 scenes+selections 组装输入。把 VideoSpec 扁平展开会触发额外字段/缺失字段校验，返回 422。

复现使用现有 `test_render_api._seed` 和 FastAPI TestClient，没有真实 API 费用、没有修改代码：

| 请求 | 结果 |
|---|---|
| 页面当前 scene_id payload | 422，explicit_scene_ids[0] 的 uuid_parsing |
| 换为 ScenePlan.id | 200 |
| 页面当前 spread VideoSpec payload | 422，场景输入模型字段缺失等 |
| 改为 video_spec 包装 | 201，渲染任务入队 |

文件：[m1_gate.py](https://github.com/pimn52/content-os/blob/3dd3896b4c39d9929faa8714413a027713c0301a/services/api/app/m1_gate.py)、[main.py](https://github.com/pimn52/content-os/blob/3dd3896b4c39d9929faa8714413a027713c0301a/services/api/app/main.py)。

### P1：界面验收的指标和状态并不可靠

- Top-1 统计用 `confirmed[i]?.clip_id === routes[i]?.candidates?.[0]?.clip_id`，在都未定义时成立。Node 复现：一个未确认 Scene 显示 top1=1。必须先验证存在且已确认的真实 clip。
- `manual_elapsed_seconds` 从第一次选片算到当前，包含等待、后台渲染和闲置，并不等于人工活跃操作时间。名称/口径需改，不能用它证明节省人工时间。
- `plan()` 没有完整清除 confirmed/manualStart，choose/route 后 lastSpec 没有统一失效；项目 select 没有切换时清理/加载逻辑。存在陈旧 VideoSpec 与当前选择不一致的风险。
- `renderRoutes()` 预览 `/clips/{id}/media`，API 返回原文件，页面没有像素材库那样把播放时间限定到 Clip start/end。因此候选预览可能从整条视频开头播放。
- `test_m1_gate_api.py` 只验证 HTML 字符串标记和项目 API，未点击实际选片→组装→渲染按钮。这解释了为何大量测试通过仍有上述阻断。

文件：[页面测试](https://github.com/pimn52/content-os/blob/3dd3896b4c39d9929faa8714413a027713c0301a/services/api/tests/test_m1_gate_api.py)。后续应补真实浏览器行为回归，不再增加字符串存在性断言充当端到端验收。

### P1：个人 IP 目前没有进入规划内容

`POST /projects` 每次都创建 `IPProfile(creator_name=...)`，没有复用同一个创作者档案。`OpenAICompatibleScenePlanner.plan()` 只发送标题、主题、画幅、尺寸、fps 和可选脚本；没有加载 IPProfile 的知识/观点/表达/历史修改，也没有素材摘要。

单有 ip_profile_id 外键不能证明 IP 个性化。应先复用一个默认 IP，并传入确认后的 Profile 内容与版本；不是先做多 IP 管理。

文件：[scene_planner.py](https://github.com/pimn52/content-os/blob/3dd3896b4c39d9929faa8714413a027713c0301a/services/api/app/providers/scene_planner.py)。

### P1：新稿、旧声、字幕之间没有语义一致保证

Assembler 将 `scene.voice_text` 直接写入 caption；Remotion 强制 `audioMode:'source'`、播放原音，且明确拒绝 narration_asset_id。按当前实现，新生成稿件可能作为字幕出现在不相关旧声音上。此次未拿到实际四条视频，不能断言具体成片已发生错配，但代码没有防护。

“原声剪辑”作为技术路径合理，前提是字幕取自对应真实转写；“新稿件视频”需真实旁白与音频定时。原声模式不是 R1 的最终能力边界。

文件：[video_spec.py](https://github.com/pimn52/content-os/blob/3dd3896b4c39d9929faa8714413a027713c0301a/services/api/app/assembly/video_spec.py)、[video.tsx](https://github.com/pimn52/content-os/blob/3dd3896b4c39d9929faa8714413a027713c0301a/apps/renderer/src/video.tsx)、[remotion.py](https://github.com/pimn52/content-os/blob/3dd3896b4c39d9929faa8714413a027713c0301a/services/api/app/renderer/remotion.py)。

### P1：Hybrid Router 当前仅是本地 Clip Router

Router 默认只允许 USER_ASSET/HISTORICAL_ASSET；检索没有项目素材集合或当前用途过滤。没有合格片段时返回 capture gap，Assembler 对 Capture 直接报错；图片/排版等既定替代没有落地。

不应把当前“找不到就补拍/停机”的能力当成完整 Hybrid Engine。一个 Scene 只能匹配单个足够长的 Clip，也会制造不必要缺口。优先补片段组合、图片/排版和用途过滤，保留真实素材优先。

复用降权已有公式，但扫描 app 中 used_count/last_used_at 仅见模型和读取计算，未见导出/发布成功写入使用事件。当前不会自然形成长期复用学习。需要幂等的使用记录，而非再调排序权重。

文件：[asset_router.py](https://github.com/pimn52/content-os/blob/3dd3896b4c39d9929faa8714413a027713c0301a/services/api/app/routing/asset_router.py)。

### P1：真实语义开发测试桥缺失

`test_local_codex_fixture_e2e.py` 中 LocalFixtureScenePlanner 固定产生两段 400ms 场景，DeterministicFixtureEmbedding 根据 fixture 标签返回 `(1,0)/(0,1)`。测试清楚声明是 test double，因此不能说它伪造了声明；但是这不是用户想要的“用 Codex 的实际能力测试”。

可通过真实模型读取材料形成有来源的测试结果，经正常接口回放；不必现在接通所有付费 Provider。官方非交互能力可作为本机探测候选，但不能假定 Codex 有 ASR/TTS/向量能力。

文件：[test_local_codex_fixture_e2e.py](https://github.com/pimn52/content-os/blob/3dd3896b4c39d9929faa8714413a027713c0301a/services/api/tests/test_local_codex_fixture_e2e.py)。

### P2：缺少“日常使用”所需状态与入口

- 素材库是只读 HTML，无页面导入入口；媒体导入实现是可复用模块，并不等于用户能自助完成导入。
- ScenePlan、候选与人工选择主要在 JS 内存，API 返回结果但未见工作稿版本表/保存流程；刷新后不能完整接续。异步 render job 已持久化最终 spec，这是有价值的部分成果。
- 没有正式 React/Vite web app；当前 Python 内嵌 HTML 是验收工具。可以渐进替换，不能为了换 UI 推倒后台。
- `async` 路由直接调用同步 planner/embedding，网络等待会阻塞同进程事件循环；应接既有任务队列/独立连接执行，不能随意跨线程复用 SQLite 连接。
- 向量 metadata 当前只记录 dimension；不同模型即使同维也不应混用。需记录 embedding 模型/版本与输入 hash，切换后重建。
- 产品成本预留/对账、账号导入、市场机会、发布反馈、Voice/Talking 实际 Provider 尚未完成；代码中存在 Schema 或未来接口不代表能力完成。

## 3. 当前成果应如何评价

| 层次 | 可确认的成果 | 不能据此宣称 |
|---|---|---|
| 数据/执行 | SQLite 迁移、外键、任务幂等、lease/heartbeat、重试恢复 | 完整内容工作流已自动编排 |
| 媒体工程 | 导入/探测/连续切片/音频与关键帧、内容寻址 | 已理解真实创作者内容 |
| 模型边界 | ASR/Vision/embedding/planner adapters + fake HTTP 边界测试 | 真实供应商或 Codex 语义质量通过 |
| 渲染 | Remotion 源视频/原音/固定字幕，编码 fixture | 新稿新旁白、个人 IP 成片已可用 |
| 交互 | 素材查看与 M1 Gate HTML | 普通用户无需研发协助即可完整使用 |
| 长期循环 | 产品文档与部分字段 | 市场/IP/Performance 智能已交付 |

不建议给出“已完成 70%”等无依据比例。工程底座已经明显前进，产品核心价值仍需真实推理和用户流程证明。

## 4. 独立验证结果和限制

本轮使用隔离 Python 3.12 虚拟环境安装项目声明的 test 依赖，执行原仓库 `python -m pytest -ra`：

- 236 passed；1 skipped；1 failed；2 条依赖弃用警告。
- skipped：本地 Codex fixture 要求显式 FFmpeg/ffprobe 环境变量，本审查未提供。
- failed：真实 Remotion E2E 调用 subprocess 返回 1；审查 checkout 未安装 renderer/node_modules，无法复现其完整前端渲染环境。本轮不据此判定 Windows 已有渲染回归，也不声称真实 Renderer 全部复验通过。
- 两个 UI 请求错误通过独立 TestClient 复现；Node 复现 Top-1 未确认统计问题。这些不依赖真实 Provider 或 Renderer。
- 未运行真实付费模型，没有实际四条视频，没有 Windows/手机实机验证；本轮不是完整安全审计。
- 未更改、提交或推送仓库代码；核对时 git 工作区干净。

仓库 STATUS 的 `232 passed,6 skipped` 属于此前本机记录；与本轮不同测试依赖就绪情况不能直接比较成退步或进步。

## 5. 根因与修正

根因是“产品闭环目标”没有成为实施调度的硬约束：按模块完成后停、合成测试替代真实语义进展、固定阶段边界被当成产品边界，随后遇到身份/音频问题又重新讨论架构。

修正不是增加高级模型次数，而是：

1. 一个 R1 目标和一个队列；统一旧文档冲突，不再叠加冻结文件。
2. 缺少字段/音频资源是工程任务，在现有架构内自主增量实现。
3. 单用户 IP 连贯与辅助素材可用性分开，避免多 IP 过度设计。
4. 测试分 fixture、真实辅助、独立运行三类，不相互替代。
5. 用户只看三个产品节点；Agent 自己负责可运行入口、缺少配置的就绪检查和常规故障排除。
6. 外部接口保持后置偏好；但最终成熟产品不能由 fixture 或人工 Agent 操作冒充独立运行。

## 6. 交付和接续

将 CONTENT_OS_EXECUTION_SPEC.md 和本报告放入本地项目根目录，交给本地 Agent 按规格第 10 节开始。先修 S0；不要继续扩多 IP 系统，不要重做数据库/FFmpeg/任务基础。已有 SHA 之后若有变更先对比，保留本地工作。

唯一发布目标文件包含功能、阶段、预算、模型分工、验收和停止条件。无需重新讨论整个战略；仅当事实证明必须改变发布承诺时集中提出具体变更。
