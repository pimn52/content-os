# Content OS — 产品需求文档（PRD）

> 本文保留为历史产品设计参考；当前 R1 工程范围、阶段门禁和验收以 [CONTENT_OS_EXECUTION_SPEC.md](CONTENT_OS_EXECUTION_SPEC.md) 为准，当前复审证据见 [CONTENT_OS_REVIEW_2026-09-10.md](CONTENT_OS_REVIEW_2026-09-10.md)，[AUDIT_REPORT.md](AUDIT_REPORT.md) 仅保留历史起点。


> 版本：V0.1 Draft  
> 基准日期：2026-09-05  
> 产品形态：Local-first / BYOK / Open Source  
> 当前阶段：进入工程开发；暂不进入完整 SaaS 商业化开发

---

## 0. 文档目的

本文冻结当前讨论形成的最新 V0.1 产品边界，作为开发、Agent 分工、验收和后续迭代的统一基线。

V0.1 不一次解决“全自动内容运营”，而先验证最关键、最可控的核心闭环：

> **系统能否理解创作者本人、本人声音、历史内容和本地素材，并以尽可能少的重新拍摄与剪辑，把一个新的内容主题变成具有本人 IP 特征的短视频。**

V0.1 验证成功后，再进入 Market Opportunity 与 Performance Learning。

---

# 1. 产品定位

## 1.1 一句话定义

**一个真正理解你的声音、表达方式和素材库的本地 AI 内容引擎。**

面向用户的价值表达：

> **你负责低成本留下真实素材，AI 负责理解、整理、复用、补缺和成片。**

产品原则：

> **Shoot for reuse, not for perfection.**  
> 不是每次拍到完美，而是让每次拍摄都成为可复用资产。

## 1.2 产品不是什么

V0.1 不是：

- 通用 AI 视频生成器；
- Stock + TTS + FFmpeg 的自动拼接器；
- 全市场趋势预测平台；
- 社交媒体矩阵管理平台；
- 完全无人审核的自动运营机器人；
- 单纯数字人 SaaS；
- 专业 NLE 视频编辑器；
- 必须依赖高端 GPU 才能工作的工具。

---

# 2. 第一目标用户

## 2.1 专业知识型个人 IP

例如顾问、教师、独立开发者、产品/设计从业者、健身教练、专业服务人员、SaaS Founder、创业者。

共同特征：

- 有内容可以说；
- 不愿长期耗费时间重复拍摄、重录和剪辑；
- 已有部分历史视频、历史账号内容或真实素材；
- 希望保留本人辨识度，而不是生成一个陌生 AI 账号。

## 2.2 中小型视频创作者

- 已形成部分语言风格和受众；
- 有持续更新压力；
- 尚未拥有完整编导/剪辑/运营团队。

## 2.3 暂不优先

- 完全零基础、没有定位和内容资产的新手；
- 已有完整专业内容团队的头部 IP；
- 强剧情/强娱乐/影视级创作；
- 大规模营销矩阵；
- 企业级多人协作场景。

---

# 3. 核心 JTBD

> **“我有专业内容和真实生活/工作素材，但不想为了每条短视频反复写、反复录、反复拍、反复剪。希望系统复用我的历史资产，在必要时只告诉我最低成本补拍什么，然后自动做成一条仍然像我的视频。”**

---

# 4. V0.1 核心假设

1. **Asset Intelligence 有价值**：AI 可以从杂乱原始视频中自动发现可用连续片段，无需用户预先整理。
2. **真实素材优先可显著降低成本**：用户自己的素材应优先于 Stock、AI Image、AI Video。
3. **“拍到可利用”可以替代“拍到完美”**：通过切片、重组、B-roll、Voice Clone 与 lip-sync 降低重复拍摄。
4. **个人 IP 可以低训练初始化**：从账号历史、视频、字幕、声音和后续修改中自动推断。
5. **口播是核心场景**：本人讲话、本人声音与 Talking Profile 必须进入 V0.1 架构。
6. **Local-first 不等于 Desktop-only**：电脑承担存储/计算，手机承担采集、审核与控制。
7. **更懂市场暂缓，但更懂本人账号现在就做**：V0.1 做 Account Intelligence，不做全市场 Market Brain。

---

# 5. 核心产品原则

## P1. Upload Once → Extract Everything

用户上传一个视频，不再被要求手工：

- 导出 MP3；
- 切片；
- 转录；
- 打标签；
- 为 Voice Clone 重复上传同一来源。

系统自动完成：

- 连续 Clip 切分；
- 音轨提取；
- ASR；
- Speaker Detection；
- Keyframe 抽取；
- 视觉理解；
- 素材标签；
- Voice 样本候选；
- Talking Profile 候选。

## P2. 连续 Clip 是素材单位

Keyframe 主要用于 AI 理解，最终剪辑仍使用连续视频区间，例如：

```text
office.mp4 00:13.2 → 00:19.8
```

## P3. 用户真实资产优先

默认优先级：

1. 用户已有真实素材；
2. 历史素材复用；
3. 低成本补拍；
4. Stock；
5. Typography / Chart / Screenshot；
6. AI Image；
7. AI Video；
8. 高成本 Digital Human。

## P4. 自动化必须可解释

关键选择尽量展示：

- 为什么选这段素材；
- 匹配度；
- 是否近期重复使用；
- 为什么建议补拍；
- AI 生成预计成本；
- 更低成本替代方案。

## P5. BYOK + Provider 可替换

Core 不绑定单一供应商。所有 LLM、Vision、Voice、Talking、Image、Video、Stock 等通过 Provider 接口接入。

---

# 6. V0.1 功能范围

## 6.1 必做

1. Local Project 基础；
2. IP Profile；
3. Account Intelligence（YouTube First，只读）；
4. Media Inbox；
5. 增量素材导入；
6. Asset Intelligence；
7. Voice Profile；
8. Talking Profile；
9. Scene-based Content Planner；
10. Hybrid Asset Router；
11. Capture Gap / Shoot List；
12. VideoSpec；
13. Remotion + FFmpeg Renderer；
14. Job Runner / Retry / Resume；
15. Cost Planner；
16. 响应式 Web UI；
17. 手机上传素材；
18. 局域网/私网远程访问方案；
19. 本地安全保存 API Key；
20. 项目保存与 MP4 输出。

## 6.2 明确不做

- 全市场 Market Brain；
- 全网热点抓取与预测；
- 自动评论/私信；
- 多账号矩阵；
- 团队协作与 Billing；
- 原生手机 App；
- 重型时间线编辑器；
- 大量 AI Video 作为默认方案；
- 强制云端；
- 强制 GPU；
- 未授权声音/肖像克隆；
- 自动发布作为第一条核心链路。

---

# 7. 首次启动与 IP 初始化

## 7.1 Connect Account（可选）

优先支持 YouTube，只读读取可授权获取的：

- 上传历史；
- 标题/描述；
- 发布时间；
- Thumbnail；
- 可用字幕；
- Analytics。

形成：

- Historical Content；
- Topic History；
- Style / Hook / Vocabulary；
- Performance Profile。

## 7.2 Import Media

用户可以：

- 选择整个文件夹；
- 拖入文件；
- 手机浏览器上传；
- 后续将文件放入受监控 Inbox。

无需命名规范。

## 7.3 自动形成 Initial IP Profile

系统从账号与素材推断：

- Domain；
- Audience；
- Topics；
- Knowledge；
- Opinions；
- Style；
- Vocabulary；
- Avoided Expressions；
- Common Hooks；
- Visual Identity；
- Voice Profile；
- Talking Profile。

用户只需 Confirm / Edit。

---

# 8. 内容创建流程

V0.1 由用户主动输入 Topic，例如：

> 为什么很多人其实不适合 Vibe Coding？

系统输出结构化 ScenePlan，而不是先写整篇文案再机械切割。

示例：

```yaml
scene_id: scene_01
order: 1
purpose: hook
voice_text: "很多人把 Vibe Coding 理解错了。"
duration_target: 3.5
visual_intent:
  subject: creator
  action: working_on_laptop
  framing: close
preferred_source:
  - user_asset
  - talking_profile
fallback:
  - typography
  - stock
caption_emphasis:
  - "理解错了"
```

---

# 9. Asset Intelligence

## 9.1 处理链

```text
Original Video
    ↓
ffprobe
    ↓
Scene / Shot Detection
    ↓
Continuous Clip Segmentation
    ↓
Keyframe Extraction
    ↓
Audio Extraction
    ↓
ASR
    ↓
Speaker Detection
    ↓
Vision Understanding
    ↓
Structured Metadata
    ↓
Embedding
    ↓
Asset Index
```

## 9.2 Clip 数据

每个连续 Clip 至少保存：

```text
id
asset_id
source_file
start_time
end_time
duration
transcript
speaker_ids
visual_description
people
objects
location
action
shot_type
orientation
motion_level
quality_score
speech_quality
face_visibility
mouth_visibility
talking_candidate
voice_candidate
embedding_ref
used_count
last_used_at
```

## 9.3 初始排序

```text
AssetScore =
SemanticMatch      * 0.40
+ IPRelevance      * 0.15
+ VisualQuality    * 0.15
+ ShotSuitability  * 0.10
+ Diversity        * 0.10
+ Freshness        * 0.05
- ReusePenalty     * 0.05
```

权重必须可配置，后续由 Performance Brain 学习。

---

# 10. Voice Profile

若历史视频存在清晰本人声音：

> “发现 82 秒适合建立本人声音的清晰样本，是否创建 Voice Profile？”

无需用户再次导出 MP3。

若样本不足，提示录制一段干净语音。

Provider 接口：

```python
class VoiceProvider:
    def create_profile(self, reference_audio, metadata): ...
    def synthesize(self, text, voice_profile_id, language): ...
    def estimate_cost(self, text, voice_profile_id): ...
```

必须同时允许 Local 与 BYOK Cloud Provider。

---

# 11. Talking Profile

## 11.1 目标

让用户在不重新完整拍摄口播的情况下生成本人讲话内容。

## 11.2 路径

### A. Original Talking Clip

原讲话内容可直接使用时优先，成本最低、真实性最高。

### B. Lip-synced User Clip

```text
新 Script
↓
Voice Clone
↓
本人音频
↓
已有本人 Talking Clip
↓
Lip Sync
↓
本人新口播
```

### C. Cloud Digital Twin

作为可插拔高质量 Provider。

### D. Local Generative Avatar

作为可选 Provider，不得成为普通电脑强制依赖。

接口：

```python
class TalkingHeadProvider:
    def validate_reference(self, video_clip): ...
    def generate(self, video_or_profile, audio, config): ...
    def estimate_cost(self, duration, config): ...
```

## 11.3 Talking Candidate 自动发现

导入时自动分析：

- face visibility；
- mouth visibility；
- occlusion；
- stability；
- motion；
- lighting；
- usable duration。

用户只需确认 Talking Profile Candidate Pack。

---

# 12. Hybrid Asset Router

## 12.1 TALKING

1. Original Talking Clip；
2. Lip-synced User Clip；
3. Cloud Digital Twin；
4. AI Avatar。

## 12.2 B_ROLL

1. User Asset；
2. Historical Asset；
3. Capture；
4. Stock；
5. AI Video。

## 12.3 EXPLAINER

1. Screenshot；
2. Typography；
3. Chart；
4. User Asset；
5. AI Image。

Router 必须返回：

- 推荐方案；
- 匹配度；
- Why；
- 成本；
- Reuse 信息；
- 是否值得补拍；
- 备选方案。

---

# 13. Capture Gap / Shoot List

当某 Scene 没有足够素材时，不立即调用昂贵生成模型，而先评估低成本补拍。

示例：

```text
Scene 4
缺少：本人手持手机操作 AI App 的近景

方案 A：补拍
成本：¥0
建议：竖屏、近景、5 秒、滑动屏幕一次、不需讲话

方案 B：Stock
匹配度：72%

方案 C：AI Video
预计 API 成本：¥1.80
```

用户可在手机端直接点击 `[开始拍摄]`。

上传后：

```text
绑定 Shoot Task
→ 自动分析
→ 自动入库
→ 重新匹配
→ Render
```

长期系统还可以根据素材缺口生成“高复用价值拍摄清单”，让用户一次拍几分钟素材覆盖未来多个视频。

---

# 14. 增量素材与手机采集

## 14.1 Desktop Inbox

用户配置本地目录，Watcher 自动：

```text
发现新文件
→ hash 去重
→ 入库
→ 分析
→ 索引
```

## 14.2 Mobile Upload

响应式 Web 支持：

- 直接拍摄；
- 从相册选择；
- 上传文件；
- 绑定 Shoot Task。

无需原生 App。

---

# 15. Account Intelligence

V0.1 做“更懂自己的账号”，不做“预测整个市场”。

## 15.1 YouTube First

只读：

- 历史视频；
- Metadata；
- 可用字幕；
- 授权 Analytics；
- 增量同步。

用于回答：

> 我过去讲过什么？  
> 什么结构对我的受众表现更好？

## 15.2 V0.2 Market Intelligence

后续才回答：

> 外部市场正在发生什么？  
> 下一条值得做什么？

---

# 16. 手机与远程运营

Local-first 不等于 Desktop-only。

手机承担：

- Capture；
- Shoot List；
- Script 轻修改；
- Scene Preview；
- 审核；
- Render 触发；
- 成片预览。

电脑承担：

- Asset 分析；
- Voice/Talking；
- 模型调用；
- Render；
- 本地素材库。

V0.1 优先支持：

- 同局域网访问；
- Tailscale 私网访问指南。

不以远程桌面作为产品体验。

本地电脑离线时任务不能执行；未来可扩展 Always-on Mini PC/NAS、Cloud Worker 或 Relay Queue。

---

# 17. VideoSpec

所有智能决策最终收敛为统一协议：

```yaml
project_id: p001
format: vertical
resolution: 1080x1920
fps: 30
voice_profile: my_voice

scenes:
  - scene_id: scene_01
    start: 0
    duration: 3.5
    narration_audio: ...
    selected_visual:
      type: user_clip
      asset_id: ...
      clip_start: 12.1
      clip_end: 15.6
    caption: ...
    transition: cut

estimated_cost:
  total: 0.17
```

Renderer 只执行 VideoSpec，不负责理解 IP 或市场。

---

# 18. 技术架构

```text
Browser / Mobile Browser
          │
          ▼
      React + Vite
          │
          ▼
       FastAPI
          │
 ┌────────┼────────────────────────────┐
 ▼        ▼            ▼               ▼
IP      Asset        Planner          Jobs
Engine  Intelligence Router           Runner
          │            │               │
          └────────────┼───────────────┘
                       ▼
                 Provider Layer
                       │
       ┌───────────────┼───────────────────┐
       ▼               ▼                   ▼
      LLM            Voice               Vision
       │               │                   │
       └───────────────┼───────────────────┘
                       ▼
                  VideoSpec
                       │
                       ▼
             Remotion + FFmpeg
                       │
                       ▼
                     MP4
```

技术选型：

- Frontend：React + Vite + TypeScript；
- Backend：Python + FastAPI + Pydantic；
- DB：SQLite；
- Media：FFmpeg / ffprobe + Scene Detection + ASR；
- Renderer：Remotion + FFmpeg；
- Vector Store：本地实现，先抽象统一接口；
- Job：自研轻量本地 Job Runner，不引入 Redis/Celery/n8n。

---

# 19. 本地数据结构

```text
content-os-data/
├── content.db
├── inbox/
├── profiles/
├── assets/
│   ├── originals/
│   ├── clips/
│   ├── keyframes/
│   └── audio/
├── voices/
├── talking/
├── projects/
├── renders/
├── cache/
└── logs/
```

---

# 20. Job Runner

Job 类型至少包含：

```text
IMPORT_ASSET
ANALYZE_ASSET
TRANSCRIBE_AUDIO
INDEX_CLIPS
BUILD_IP_PROFILE
BUILD_VOICE_PROFILE
BUILD_TALKING_PROFILE
PLAN_CONTENT
MATCH_ASSETS
GENERATE_VOICE
GENERATE_TALKING
RENDER
SYNC_ACCOUNT
```

状态：

```text
pending
running
completed
failed
cancelled
```

必须支持：

- Retry；
- Resume；
- Crash Recovery；
- 幂等。

---

# 21. Provider Interfaces

至少定义：

```text
LLMProvider
VisionProvider
EmbeddingProvider
ASRProvider
VoiceProvider
TalkingHeadProvider
StockProvider
ImageProvider
VideoProvider
RenderProvider
AccountProvider
```

Core 只能依赖接口。

---

# 22. Cost Planner

每个候选方案都要可估算：

```text
User Asset       0
Capture          0
Typography       ~0
Stock            0 / provider-dependent
AI Image         estimated
AI Video         estimated
Digital Twin     estimated
```

Project 执行前展示总成本，并允许：

- Generate；
- Reduce Cost；
- Change Provider。

---

# 23. 安全、隐私与授权

## API Key

- 不进入 Git；
- 不明文保存在普通配置文件；
- 优先 OS Keychain/Keyring；
- 日志禁止输出 Secret。

## Voice / Face

创建 Voice/Talking Profile 前必须确认：

- 素材属于本人，或；
- 拥有明确合法授权。

保存 consent metadata。

## 本地数据

默认素材本地保存。调用第三方 Provider 前说明哪些内容会离开本机。

## Telemetry

默认关闭。未来若用于 OSS 产品验证：

- 明确 Opt-in；
- 匿名；
- 不上传原素材、脚本和 Key；
- 用户可查看发送内容。

---

# 24. 运行环境

Minimum Target：

- 4 Core CPU；
- 8 GB RAM；
- Windows 优先；
- 足够磁盘空间。

Recommended：

- 6–8 Core CPU；
- 16 GB RAM。

GPU：

- 不强制；
- 本地 Voice/Talking/Vision 可利用 GPU；
- 无 GPU 用户仍可用 BYOK Cloud Provider 完成核心链路。

---

# 25. 核心页面

V0.1 最少：

1. Onboarding；
2. Provider Settings；
3. IP Profile；
4. Account Import；
5. Asset Library；
6. Clip Detail；
7. Voice Profile；
8. Talking Profile；
9. Create Project；
10. Scene Plan；
11. Asset Match / Alternatives；
12. Shoot List；
13. Cost Review；
14. Render Jobs；
15. Video Preview。

---

# 26. 成功指标

- Activation：安装后成功生成第一条视频，目标 `>50%`；
- Time-to-First-Video：初始目标 `<30 min`；
- User Asset Usage Rate：成片 Scene 使用本人真实资产 `>50%`；
- Match Acceptance Rate：首选素材无需替换 `>70%`；
- Voice Acceptance Rate：克隆音色愿意直接使用；
- Talking Acceptance Rate：AI 口播无需重新真人完整拍摄即可使用；
- Repeat Usage：后续是否再次创建项目。

---

# 27. Alpha 验收

必须满足：

1. 一次导入 3–5 个杂乱命名视频；
2. 自动切成连续 Clip；
3. 自动提取音频，无需用户手工 MP3；
4. 自动转录并形成 Clip 语义；
5. Topic/Script 能生成结构化 ScenePlan；
6. 至少 50% Scene 可从本地素材返回合理 Candidate；
7. 展示匹配理由、成本、Reuse 信息；
8. 可建立 Voice Profile 或明确提示补录；
9. 至少一个 TalkingHeadProvider 端到端可运行；
10. 可生成 9:16 MP4；
11. 单步骤失败可 Retry；
12. API Key 不进入日志；
13. 手机浏览器可上传 Shoot Task 素材；
14. 上传后自动入库；
15. 最低链路不强制 GPU。

---

# 28. V0.2 / V0.3 预留

## V0.2

- Market Opportunity；
- YouTube 外部 Market Data；
- Opportunity Score。

## V0.3

- Performance Brain；
- Learning Loop；
- 发布反馈；
- 自动优化。

V0.1 的 IPProfile / HistoricalContent / ScenePlan / VideoSpec 必须支持未来扩展。

---

# 29. 开源与 License

核心开放：

- IPProfile Schema；
- Asset/Clip Schema；
- ScenePlan；
- VideoSpec；
- Provider API；
- Renderer Contract。

社区应能独立增加 Voice/Talking/Vision/Account/Render Provider。

License 初步按 AGPL-3.0 方向设计，但在首次公开 Release 前完成正式 License Review 与依赖 License Inventory。对来源不清晰、限制商用的项目只研究思路，不直接复制核心代码。

---

# 30. 开发启动结论

**现在可以进入开发。**

但不是开发“完整 Content OS”，而是先通过第一条 Vertical Slice：

> **上传真实视频 → 自动理解连续 Clip → 输入新内容 → Scene Planning → 自动匹配本人素材 → 生成本人声音/口播 → 渲染 9:16 成片。**

只有这条链路达到可用质量，才继续扩大 Account、Mobile、Market 和 Cloud 能力。
