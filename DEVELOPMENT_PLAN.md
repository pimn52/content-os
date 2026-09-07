# Content OS — 开发计划与最低成本 Agent 分工

> 2026-09-07 开发前修订见 [BASELINE_FREEZE.md](BASELINE_FREEZE.md)，冲突时以该修订为准。


> 对应 PRD：V0.1 Draft  
> 原则：Vertical Slice First / Cheapest-Capable-Agent / Escalate-on-Failure  
> 不按日期排期，按工程 Gate 顺序推进。

---

# 1. Agent 成本策略

当前 Codex/Work 模型分层：

| Agent 档 | 推荐模型 | 用途 | 策略 |
|---|---|---|---|
| L | GPT-5.6 Luna | 脚手架、测试、简单 UI、CRUD、文档、简单适配器 | **默认** |
| T | GPT-5.6 Terra | 核心逻辑、媒体管线、复杂集成、跨文件调试 | 必要时使用 |
| S | GPT-5.6 Sol | 架构 Gate、高风险质量审查、安全审查 | 极少使用 |

模型按已约定的能力分层调度；旧费率快照不作为当前计费承诺，实际消耗以运行平台账单为准。

基本纪律：

> **不让 Sol 写脚手架，不让 Terra 写纯 CRUD，不让 Luna 未经授权改变核心架构。**

---

# 2. Agent 升级规则

```text
Luna
  │
  ├─ Pass → Review / Merge
  │
  └─ Fail / 跨模块歧义
          ↓
        Terra
          │
          ├─ Pass
          │
          └─ 架构冲突 / 关键难题
                  ↓
                 Sol
```

Luna 升级条件：

- 连续两次修复仍不能通过测试；
- 需要同时理解多个核心模块；
- 涉及媒体同步、并发、数据一致性；
- 需要重新定义核心接口；
- 已明确需求但实现持续偏离验收。

---

# 3. 子 Agent 工作协议

每个任务必须提供：

```text
Objective
Files allowed to change
Interfaces that must not change
Acceptance criteria
Tests to run
Non-goals
```

结束必须报告：

```text
Changed files
Tests run
Acceptance criteria status
Known limitations
New dependency/license concern
Recommended next task
```

禁止未经授权：

- 重构无关模块；
- 改核心 Schema；
- 换技术栈；
- 引入大型依赖；
- 改 License；
- 扩产品范围。

---

# 4. Repository 建议

```text
content-os/
├── README.md
├── PRD.md
├── DEVELOPMENT_PLAN.md
├── AGENTS.md
├── LICENSE
├── docker-compose.yml
├── Makefile
│
├── apps/
│   ├── web/                  # React + Vite
│   └── renderer/             # Remotion
│
├── services/
│   └── api/                  # FastAPI
│       ├── app/
│       │   ├── api/
│       │   ├── core/
│       │   ├── domain/
│       │   ├── providers/
│       │   ├── jobs/
│       │   └── db/
│       └── tests/
│
├── contracts/
│   ├── schemas/
│   └── examples/
├── scripts/
├── docs/
└── tests/
    └── fixtures/
```

运行数据放独立目录：

```text
content-os-data/
```

---

# 5. Gate 0 — 架构冻结

## G0.1 Core Schemas

定义：

- IPProfile；
- AccountConnection；
- HistoricalContent；
- Asset；
- Clip；
- VoiceProfile；
- TalkingProfile；
- Project；
- ScenePlan；
- CandidateAsset；
- VideoSpec；
- Job；
- UsageCost。

**最低 Agent：Terra**

### 验收

- Pydantic Schema 可序列化；
- 有 JSON examples；
- Provider-agnostic；
- schema tests 通过；
- 不阻断未来 Market/Performance 扩展。

## G0.2 Architecture Review

检查：

- Python/TypeScript 边界；
- Provider 抽象；
- VideoSpec；
- Job 模型；
- V0.2 可扩展性。

**Agent：Sol**

这是第一次使用 Sol，只 Review，不负责大量实现。

---

# 6. Gate 1 — Repo 与最小骨架

## G1.1 Monorepo Scaffold

- FastAPI；
- React/Vite；
- Remotion；
- SQLite；
- lint/test；
- Docker Compose；
- Makefile。

**Agent：Luna**

验收：

- 单命令启动开发环境；
- `/health` 可访问；
- Web 能调用 API；
- Renderer 能生成最小测试视频。

## G1.2 DB Persistence

核心实体 SQLite repository 与测试。

**Agent：Luna**

## G1.3 Secret Storage

本地 API Key 采用安全抽象/OS keyring，日志禁止 Secret。

**Agent：Terra**

---

# 7. Gate 2 — Asset Vertical Slice

目标：

> 用户拖入一个视频，系统得到可搜索的连续 Clip。

## G2.1 Media Inbox

- 文件上传；
- 文件路径导入；
- hash 去重。

**Agent：Luna**

## G2.2 ffprobe Metadata

- duration；
- resolution；
- fps；
- orientation；
- audio tracks。

**Agent：Luna**

## G2.3 Continuous Clip Segmentation

Scene/Shot Detection → start/end。

**Agent：Terra**

验收：

- 原始视频不改变；
- Clip 可精确预览；
- 无越界、负时长；
- 最终素材单位仍是连续视频。

## G2.4 Keyframes

**Agent：Luna**

## G2.5 Audio Extraction

自动生成标准音频，不要求手工 MP3。

**Agent：Luna**

## G2.6 ASR Provider

接口 + 至少一个实现。

**Agent：Terra**

## G2.7 Vision Provider

输出：

- people；
- objects；
- location；
- action；
- shot type；
- quality；
- talking candidate。

**Agent：Terra**

## G2.8 Asset Index/Search

Transcript + Vision + Metadata → Embedding + Filter。

**Agent：Terra**

验收：

输入：

> “本人坐在电脑前操作软件”

能返回合理连续 Clip Top K。

## G2.9 Asset Library UI

- 原视频；
- Clip；
- transcript；
- tags；
- preview；
- search。

**Agent：Luna**

---

# 8. Gate 3 — Scene Planner + Asset Router

## G3.1 ScenePlan Contract

**Agent：Terra**

## G3.2 Structured Planner

每 Scene 包含：

- purpose；
- voice_text；
- duration；
- visual_intent；
- preferred_source；
- fallback；
- caption。

**Agent：Terra**

## G3.3 Asset Ranking

实现：

- SemanticMatch；
- Quality；
- IPRelevance；
- Diversity；
- ReusePenalty。

**Agent：Terra**

## G3.4 Candidate UI

展示：

- 首选；
- Alternatives；
- Match；
- Why；
- Cost；
- Reuse。

**Agent：Luna**

## G3.5 Capture Gap / Shoot List

识别低匹配场景并生成最低成本补拍建议。

Backend：**Terra**  
UI：**Luna**

---

# 9. Gate 4 — Renderer

## G4.1 VideoSpec Assembly

ScenePlan + Selected Assets → VideoSpec。

**Agent：Terra**

## G4.2 Remotion Timeline

实现：

- 9:16；
- continuous video clip；
- image；
- typography；
- captions；
- transitions；
- audio。

**Agent：Terra**

## G4.3 FFmpeg Finalization

- trim；
- mux；
- audio normalization；
- codec；
- final MP4。

**Agent：Terra**

## G4.4 Simple Style UI

基础字幕/品牌配置。

**Agent：Luna**

## G4.5 First E2E Fixture

3–5 个视频 + 1 个 Script → MP4。

**Agent：Terra**

> 完成后立即做第一个 Go/No-Go Gate，不继续扩功能。

---

# 10. Gate 5 — Voice Profile

## G5.1 Voice Candidate Discovery

从视频中自动寻找高质量本人讲话音频。

**Agent：Terra**

## G5.2 VoiceProvider Contract

**Agent：Luna**

## G5.3 First Voice Clone Provider

至少跑通一个 Local 或 BYOK Provider，另一个类型保留 Stub。

**Agent：Terra**

## G5.4 Voice UX

```text
发现可用本人语音
→ 试听
→ 授权确认
→ Create Voice Profile
```

**Agent：Luna**

---

# 11. Gate 6 — Talking Profile

这是高风险核心能力。

## G6.1 Talking Candidate Scoring

- face visibility；
- mouth visibility；
- stability；
- occlusion；
- motion；
- duration。

**Agent：Terra**

## G6.2 TalkingHeadProvider Contract

**Agent：Luna**

## G6.3 First Lip-sync Provider

完成：

```text
reference talking clip
+ cloned audio
→ new talking video
```

**Agent：Terra**

## G6.4 Talking QA Harness

建立：

- 正脸；
- 轻侧脸；
- 小动作；
- 遮挡；
- 长短句；

测试样本。

Harness：**Luna**  
阈值与问题分析：**Terra**

## G6.5 Talking Quality Gate

审查：

- 对口型；
- 节奏；
- 是否可发布；
- 无 GPU fallback；
- 成本；
- 失败条件。

**Agent：Sol**

若效果不足：

> Talking 保留可选 Provider，核心不被拖垮；回退 Original Talking + Voice + B-roll。

---

# 12. Gate 7 — Job Runner

## G7.1 Job Model/UI

Model：**Luna**  
UI：**Luna**

## G7.2 Retry / Resume / Idempotency

覆盖 Analyze / Voice / Talking / Render。

**Agent：Terra**

## G7.3 Crash Recovery

本地进程重启后恢复任务。

**Agent：Terra**

---

# 13. Gate 8 — IP Engine

## G8.1 Manual Profile UI

**Agent：Luna**

## G8.2 IP Extraction

从 transcript / historical text / user corrections 推断。

**Agent：Terra**

## G8.3 Planner 注入 IP Context

**Agent：Terra**

---

# 14. Gate 9 — YouTube Account Intelligence

## G9.1 OAuth

只读。

**Agent：Terra**

## G9.2 Historical Upload Sync

基础 metadata。

**Agent：Luna**

## G9.3 Captions Import

**Agent：Terra**

## G9.4 Analytics Import

同步可用账号指标。

**Agent：Terra**

## G9.5 Incremental Sync

**Agent：Luna**

## G9.6 Account History → IP Profile

**Agent：Terra**

---

# 15. Gate 10 — Mobile Capture / Remote

## G10.1 Responsive Shoot List

**Agent：Luna**

## G10.2 Mobile Camera/File Upload

**Agent：Luna**

## G10.3 Shoot Task Binding

**Agent：Luna**

## G10.4 Inbox Auto-analysis

**Agent：Terra**

## G10.5 Tailscale Remote Access Docs

仅文档与配置指引，不自动修改网络。

**Agent：Luna**

---

# 16. Gate 11 — Cost Planner

## G11.1 Provider Cost Interface

**Agent：Luna**

## G11.2 Scene Cost Calculation

**Agent：Luna**

## G11.3 Reduce Cost Strategy

例如：

```text
AI Video
→ Capture
→ Stock
→ Typography
```

**Agent：Terra**

---

# 17. Gate 12 — Alpha Hardening

## G12.1 Full E2E Tests

```text
import
→ analyze
→ plan
→ match
→ voice
→ talking
→ render
```

**Agent：Terra**

## G12.2 Security Review

重点：

- API Key；
- path traversal；
- uploaded filename；
- shell/FFmpeg invocation；
- remote access；
- logs。

**Agent：Sol**

## G12.3 License Inventory

整理：**Luna**  
冲突判断：**Terra**

## G12.4 Packaging

Docker：**Luna**  
Windows 一键包：**Terra**

## G12.5 Docs

README / Quick Start / Providers / Privacy。

**Agent：Luna**

---

# 18. 第一条 Vertical Slice 的严格顺序

```text
Schema
↓
Repo
↓
Upload Video
↓
Continuous Clip Detection
↓
ASR
↓
Vision
↓
Embedding Search
↓
Script → ScenePlan
↓
Asset Match
↓
Remotion Render
↓
Voice
↓
Talking
```

第一目标：

> **没有人工切素材，系统自动找到合适的本人连续镜头并生成新成片。**

---

# 19. 第一 Go / No-Go Gate

完成基础 Render 后，用真实素材检查：

1. 切片是否合理；
2. AI 是否理解素材；
3. Top-1/Top-3 匹配是否可信；
4. 连续 Clip 是否自然；
5. 成片是否明显比通用 Stock 拼接更“像用户本人”。

若失败：

> 只修 Asset Intelligence / Router，不继续 YouTube、Mobile、Market。

---

# 20. 第二 Go / No-Go Gate

Voice + Talking 后检查：

> 用户是否真的可以少拍或不重新完整录口播？

若 Talking：

- 大量不可用；
- 素材要求过高；
- 成本不合理；

则降级为 Optional Provider，不拖垮核心 V0.1。

---

# 21. 并行开发建议

## Stream A — Core / Backend

Schema、Asset、Planner、Router、Jobs。

主 Agent：Terra  
辅助：Luna

## Stream B — UI

Asset Library、Scene、Candidate、Settings、Jobs。

主 Agent：Luna

## Stream C — Renderer

Remotion、FFmpeg、VideoSpec。

主 Agent：Terra

## Stream D — Voice / Talking

Asset 基础稳定后启动。

主 Agent：Terra

约束：

> 同一核心 Schema 不允许多个 Agent 并行修改。

---

# 22. 最低成本任务矩阵

| 任务类型 | 最低推荐 Agent |
|---|---|
| README / docs | Luna |
| React 表单/列表 | Luna |
| FastAPI 单端点 | Luna |
| SQLite CRUD | Luna |
| 普通 Pydantic 模型 | Luna |
| 单元测试 | Luna |
| Mock Provider | Luna |
| Docker 基础 | Luna |
| ffprobe wrapper | Luna |
| Keyframe extraction | Luna |
| Media segmentation | Terra |
| ASR/Vision 集成 | Terra |
| Embedding + filters | Terra |
| Scene Planner | Terra |
| Asset Router | Terra |
| VideoSpec assembler | Terra |
| Remotion/FFmpeg 多轨同步 | Terra |
| Voice candidate scoring | Terra |
| Voice clone integration | Terra |
| Lip-sync integration | Terra |
| YouTube OAuth/Analytics | Terra |
| Crash recovery | Terra |
| Windows packaging | Terra |
| 核心架构冻结 | Sol |
| Talking 质量 Gate | Sol |
| 安全/发布 Gate | Sol |
| 跨模块疑难 Bug | Terra → 必要时 Sol |

---

# 23. Codex 调度策略

## 默认 Worker

`GPT-5.6 Luna + low/medium reasoning`

用于小型、边界清晰、可测试任务。

## Core Worker

`GPT-5.6 Terra + medium/high`

用于媒体处理、多文件逻辑和复杂 Provider。

## Gate Reviewer

`GPT-5.6 Sol + high`

只用于：

- G0 架构审查；
- Talking 质量审查；
- 安全审查；
- Terra 无法解决的核心 Bug。

---

# 24. 子 Agent Prompt 模板

```text
You are implementing one scoped task in Content OS.

Objective:
<single objective>

Read first:
- PRD.md
- DEVELOPMENT_PLAN.md
- relevant schemas/interfaces

Allowed files:
- ...

Do not change:
- core schemas unless explicitly allowed
- unrelated modules
- technology stack
- provider interfaces unless requested

Acceptance criteria:
1. ...
2. ...
3. ...

Tests:
- ...

Non-goals:
- ...

Before finishing:
- run tests
- list changed files
- report acceptance criteria status
- report known limitations
- report dependency/license concerns
- do not perform unrelated refactors
```

---

# 25. 第一批可立即下发的任务

按顺序：

1. **Task 001 — Core Schemas** — Terra
2. **Task 002 — Repo Scaffold** — Luna
3. **Task 003 — SQLite Persistence** — Luna
4. **Task 004 — Media Upload + ffprobe** — Luna
5. **Task 005 — Continuous Clip Segmentation** — Terra
6. **Task 006 — Audio Extract + Keyframes** — Luna
7. **Task 007 — ASR Provider** — Terra
8. **Task 008 — Vision Provider** — Terra
9. **Task 009 — Asset Index/Search** — Terra
10. **Task 010 — Asset Library UI** — Luna
11. **Task 011 — ScenePlan** — Terra
12. **Task 012 — Asset Router** — Terra
13. **Task 013 — VideoSpec** — Terra
14. **Task 014 — Basic Remotion Renderer** — Terra
15. **Task 015 — E2E Fixture Video** — Terra

完成 Task 015 后暂停扩展，先做真实素材效果验收。

---

# 26. Merge Gate

每个任务合并前：

1. Tests pass；
2. 无无关重构；
3. 无 Provider 锁定；
4. 无硬编码 Key；
5. Schema 变更单独说明；
6. Media fixture 可复现；
7. 新依赖进入 License Inventory；
8. 行为与 PRD 一致。

普通 Review：Luna  
核心模块 Review：Terra  
里程碑 Gate：Sol（仅少数节点）

---

# 27. 开发纪律

1. 不提前上 Kubernetes/Redis/微服务；
2. 不为了未来可能性提前写复杂插件框架；
3. 先接口抽象，后多 Provider；
4. 每个 Gate 必须有可运行 Demo；
5. 不做不改变用户体验的工程炫技；
6. 高成本 API 必须可估价；
7. Voice/Face 必须保存授权确认；
8. 所有素材优先复用真实资产。

---

# 28. 结论

可以从 **Task 001 — Core Schemas** 正式开始。

最低成本策略：

- **Luna：承担绝大多数机械性和边界清晰任务；**
- **Terra：承担真正决定产品质量的核心链路；**
- **Sol：仅用于 3 个关键 Gate 与极少数升级问题。**

这能在不牺牲核心质量的前提下，最大程度压低 Codex 多 Agent 开发成本。
