# 当前状态

更新：2026-11-08；分支：`recovery/stoploss-20261015`；保留基线：`f32677d13fbba8a6dbdbac48030eaa5ea4f49907`。

## 当前任务

目标：按 Voice 重音 → Talking 同步 → IP/素材/发布学习 → 普通 UI 全片与第二主题的顺序收口。
状态：P1 已由用户确认通过。P2 收口为“整体可用、局部口型未完美”的产品基线：精确基线 Asset `3be87b3f-eab8-48ea-82f9-992afb5722f2` 仍是唯一获用户认可的 Talking 输出；参考质量 v2 候选 `47fc4760...` 已判更差。LatentSync 参数和单音素调优停止，不再为技术满分阻塞整片。P3、P4 已完成。P5 最小闭环已完成：发布包装/人工回执、成片反馈、不可覆盖指标快照、自身基线和下一轮建议可运行；自动发布、指标同步、评论和实验转为后续可选。P6 仍是 `in_progress`：上一片只证明普通 UI 技术链可自主运行，用户已判产品内容不通过；缺 A-roll、全流程卡 B-roll、Voice 错读和日常运行体感问题正在按通用能力修复并重新验证。第二主题尚未开始。完整计划见 [task_plan.md](task_plan.md)。

P1 音频：[bb2b898b5f65e55e7edf45ea20e11caf26c9a1ee266f6824a621e6ae41d1cdc7.wav](content-os-data/assets/audio-originals/bb2b898b5f65e55e7edf45ea20e11caf26c9a1ee266f6824a621e6ae41d1cdc7.wav)。37.850 秒、24 kHz 单声道 PCM16，SHA-256 `bb2b898b5f65e55e7edf45ea20e11caf26c9a1ee266f6824a621e6ae41d1cdc7`。规则集 `zh-focus-emphasis-v1` 将“最后，”和“记住三步：”拆为独立焦点段并应用 +1.58 dB；Voice QA `c4828053...` 通过，missing/duplicate 为 0。证据见 [manifest.json](content-os-data/evaluation-evidence/voice-focus-emphasis-p1-20261107/manifest.json)。

P2 候选与结论：

- 同 4 秒开场、参考改为 `5.000–9.500s`：Talking Asset `963ef90a-8f6b-4fdd-89e0-593b76263e8d`，4.000 秒、100 帧，技术 QA verified，时长差 20ms。
- 稳定短句“先写完整结论和边界”、Master `31.050–33.090s`、参考 `5.000–8.000s`：Talking Asset `3be87b3f-eab8-48ea-82f9-992afb5722f2`，2.040 秒、51 帧，技术 QA verified，时长差 0ms。
- 本地运动包络只作辅助：旧版相关性约 0.211，短候选约 0.301、运动峰值偏移约 -80ms；不是 SyncNet 或音素级口型证明。证据见 [P2 manifest](content-os-data/evaluation-evidence/talking-sync-p2-20261107/manifest.json)。
- 用户已确认上述稳定短句候选可用；同 4 秒新参考候选本身未被用户选为通用口型结论。
- P2 优化实验：同一已批准短句和 `5–8s` 参考下，`28 inference steps` 的本地运动包络相关性约 `0.329`，高于已通过 20-step 基线的 `0.301`；36 steps 与 guidance 1.7 未继续提升。`28 steps` 仅作为待视觉判断候选，产品默认仍是 20 steps。
- 参考质量 v2 将脸型朝向稳定性纳入评分，`6.5–9.5s` 参考在默认 20 steps 下生成 `47fc4760...`；level correlation 约 `0.4604`、motion correlation 约 `0.3611`，均高于已通过的 `5–8s` 基线（约 `0.2619 / 0.3012`），且时长差 0、51/51 帧、QA verified。但用户直接比较确认新候选口型同步更差，技术指标不能覆盖人审，已撤回 v2 自动排序，记录见 [user-decision.json](content-os-data/evaluation-evidence/talking-sync-p2-20261108/user-decision.json)。

LatentSync 时长基础：历史本机 `4460ms` 单次旁白输入通过、`5120ms` 技术通过但人审质量下降；本机 6 GiB VRAM 低于官方 `6.5GB`。当前 W52 精确 scope 已保存 `max_talking_duration_ms=4460`，状态明确为“同机历史保守上限、当前 runtime 尚未重新质量验证”，来源见 [manifest](content-os-data/latentsync-duration-boundary-20260915/manifest.json)。长旁白按完整 ASR 语句拆 Job；换机或换 runtime 必须重新校准，不能沿用。

`max_talking_duration_ms` 已接入普通 UI 的“Provider 与本机高级设置”：默认 `4460ms`，整数可调；当前运行配置能解析时自动带入 exact scope，无法解析时提示用户填写本机标识。默认值已经进入实际调度兜底，不再只是文档或展示值。

高级设置验证：相关后端 112 项、前端 112 项及生产构建通过；实际运行库解析出 W52 runtime + `LAPTOP-UO337HJH` 的 active scope，`max_talking_duration_ms=4460` 显示为当前 machine override，覆盖目标仍为同一 Provider/model/runtime/machine。文档治理、能力索引同步、JSON 解析和 `git diff --check` 通过。未调用模型，也未启动全片。

W52 `4170ms` 完整语句组单次复验：Job `6a130a5a...` 首次尝试完成，Talking Asset `8da57bd1...` 为 4200 ms、105/105 帧、技术 QA verified；参考 `5.000–9.500s` 覆盖 4480 ms 模型窗口。因已批准稿件没有恰好 4460 ms 的连续完整语句组，本次用低于上限的最近完整语句组，没有切词凑数。当前等待用户判断可见口型、likeness 和自然度；技术通过不等于人审通过。

该候选尾部实际文本是“来拆”，不是“来猜”。词级时间显示“来” `3.330–3.570s`、“拆” `3.570–4.170s`；对应音频峰值约 `3.600s`，嘴部开口峰值约 `3.800s`，末字存在约 200 ms 局部滞后。嘴部在 `4.160s` 已回到闭合基线，最后 30 ms 未观察到说话结束后再次张嘴。因此当前问题分类为末字内的 viseme/局部时序，不是尾音收口失效。

历史 `terminal_face_closeout` / `600ms lookahead` 的代码、测试、D6g 视频、末六帧、Voice QA 和 Job 结果仍在；但此前没有独立不可变人审记录，当前 W52 runtime 也未重新验证，且本次候选没有启用收口。已补权威文档与整理证据，不能把 D6g 或本次技术 QA直接外推为 W52 尾音收口通过。

后续已完成同参考窗口的 W52 收口 A/B：匹配无收口 Asset `673e2593...` 的“来/拆”嘴部峰值偏移为 `-200/+200ms`；启用 `terminal_face_closeout=true + 600ms` 的 Asset `f5d08673...` 两者均为 `0ms`。两者技术 QA 均通过，整体相关性几乎相同，说明收益来自收口而非参考窗口变化。当前等待用户视觉判断，产品默认仍未提升为开启。

用户已判断收口候选的提前回头更符合尾句收束，整体优于无收口对照；但末字“拆”的嘴形仍不自然，并可能是模型保留的皱眉/表情所致。该 A/B 记作“相对更优但未通过”，保留两边媒体、词级峰值/嘴宽观察与失败准备证据，不对“拆”写单字规则，也不自动开启产品默认。

按可修范围追加一次同 reference 的 750ms lookahead 候选：Asset `a50b3c2b...` 技术 QA 通过；相对 600ms，“拆”的嘴宽峰值仅再提前约 40ms，3800ms 的二次开口和宽度维持未消失。该参数实验停在待用户视觉比较，不继续递增 lookahead，也不提升默认。

源参考核对：当前 Talking 参考 Asset `9d968a01...` 是静音裁片，但 W43 crop proof 指向原始 Asset `e665fa62...`；原片有 48kHz 双声道 AAC 与转写。源 O 型约 `8.709–8.742s`，small/medium 两套词级时间均落在原句“反而对中华文明有的新认知”的首字“反”，不是目标“拆”。当前 `5.000–10.500s` 窗口把它映射到输出 `3.709–3.742s`，正好重叠“拆”的 `3.570–4.170s`，说明 O 型与源“反”的强开口动作同位。

通用参考窗口 A/B：同 Voice、同 600ms 收口，把 reference 改为 `4.500–10.000s`。该窗口在目标末词映射区间的源嘴部开口上限由 `0.1706` 降至 `0.0954`，但生成 Asset `62429a11...` 的“拆”嘴部峰值从基线 `0ms` 退化到音频后 `+480ms`；整段包络相关虽升至 `0.4025`，局部时序更差，已拒绝且不改产品默认。证据见 [source-reference-original-audio-identity.json](content-os-data/evaluation-evidence/talking-duration-boundary-w52-20261108/source-reference-original-audio-identity.json) 和 [reference-shift-4500-10000.json](content-os-data/evaluation-evidence/talking-duration-boundary-w52-20261108/reference-shift-4500-10000.json)。600ms 仍只是相对更优且未通过，不升级默认。

产品取舍：允许整片以“可用基线”继续创作和发布评估，不把局部嘴形/皱眉写成整片不可用；但这不构成 Talking 能力通过。停止当前 LatentSync 的 lookahead、steps、seed 和单字特例搜索，已写入 [TALKING](docs/product/TALKING.md) 与 D068。

P3 公开账号收口：毕英杰和 TIM 各确认 B站、YouTube、抖音 3 个账号并关联 Profile，共导入 30 条真实历史标题/指标。规划上下文每次最多取 8 条并按平台轮转，链接本身不冒充已分析；小红书两个同名搜索都被登录墙截断，保持未确认。毕英杰 Profile `65ed92be...` 已更新到 v4，覆盖西方哲学经典、思想史/宗教学、学者访谈、哲学与 AI/创业、遗址行走；TIM Profile `09f4099c...` 更新到 v2，覆盖数码科技、影像设备和视频制作。两条新的普通 `scene-plan`：毕英杰项目 `2e74295e...` 5 场/46 秒，TIM 项目 `45036960...` 5 场/60 秒，均 5/5 `creator_fit=matched`，并各引用 3 账号、8 条跨平台历史。两稿事实与人审仍 pending。证据见 [P3 公开账号验证](content-os-data/evaluation-evidence/p3-public-account-validation-20261108/validation.json) 与更新后的[源素材分析](content-os-data/evaluation-evidence/p3-local-creator-sources-20261108/source-analysis.json)。

P3 第一阶段（保留历史）：普通 IP 表单已支持完整创作者 Brief 与公开账号字段；规划只发送主题相关的有界脱敏资料；每场 ScenePlan 必须说明 Creator Fit。毕英杰与 TIM 的源素材、早期双主题规划及旧测试档案排除均保留在证据目录中，不作为当前最新公开账号结果。

P4 素材治理：普通视频导入默认绑定当前创作者；本人、历史与 AI B-roll 素材必须有当前 IP 才进入新选材，stock 可共享。媒体分析保存最多 8 帧 dHash，并由 Worker 自动生成同 IP 近似重复快照。Schema 62 新增 `asset_governance_events`，schema 63 再用数据库触发器保证 append-only：入库、归属/分类变化、生命周期、指纹成败、近似重复扫描成败均由 Content OS 写入，没有产品删除入口。现有 72 Asset 只回填 72 条诚实的 `snapshot_initialized`，不回填伪造历史。普通 UI/API 可查看事件链、当前状态和引用影响。素材可 active/archived，归档只退出新选材，Clip、Voice/Talking、草稿和 ProductionRun 引用保留；`delete_safe=false` 时普通 UI 不提供绕过检查的物理删除。3 个主素材已有 snapshot+scan 事件，3/8/8 帧指纹、距离 12 内无近似重复，引用数 23/21/157。证据见 [P4 素材治理验证](content-os-data/evaluation-evidence/p4-asset-governance-20261108/validation.json)。

A-roll 复用治理已把普通 UI 的固定栏目标识 `signature` 与旧 `brand_marker` 统一为有意复用角色；`ordinary` 仍按无意重复软排序，原样素材可复用，新语音/口型派生表演仍检查来源动作冲突。39 项影响面回归通过，文档治理检查通过；未启动模型或整片。

P4 视觉证据覆盖已进入 readiness 和普通 UI：显示 Clip 总数、已有描述数、缺失范围；当前创作者素材优先，其它绑定素材、共享 stock、旧库未绑定素材和归档项依次后排。用户可按既有视觉 Worker 每次有界补最多 4 个 Clip，缺描述不成为入库或选片硬门。真实库仍为 80/253；3 份创作者主素材为 50/198，其中 TIM `b98...` 缺 134 个、毕英杰 `b534...` 缺 11 个、`e665...` 缺 3 个。实机普通 UI 已显示覆盖摘要、优先素材和补证据入口，未点击会触发模型的按钮。证据见 [P4 视觉覆盖验证](content-os-data/evaluation-evidence/p4-visual-coverage-20261108/validation.json)。

P5 发布与学习第一阶段：普通 UI 只允许从已完成且文件存在的 `render:<render_id>` 选择发布/反馈版本，草稿、失败或未知 Render 被拒绝。发布包装保存标题、描述、封面取帧、平台账号、内容 URL/ID、指标来源和观察窗口；反馈绑定同一成片版本。下一轮建议读取最近 4 条反馈和最近发布回执，单作品指标必须提示先与其它作品比较，不能直接归因。能力权威见 [PUBLISHING_LEARNING.md](docs/product/PUBLISHING_LEARNING.md)。真实库当前发布记录和成片反馈仍为 0；指标观察序列、自动回执和自动发布尚未实现。

P5 历史节目绑定：HistoricalContent 可关联同 IP 本地 Asset 并区分官方原帖、已下架原帖、公开补档与未知状态。毕英杰 B站官方 `BV1QFgMzDEMh`《第一期中文节目：通过希腊，了解中国》已绑定 `e665...`，保留 35.9 万播放、967 弹幕；TIM《清晰度不如4年前！视频变糊是你的错觉吗？》2024-10-08 的 B站与 YouTube 官方原帖已各建 `removed` 记录并绑定 `b98...`，官方指标保持 unknown。B站 `BV1ewTg6TEWt` 与 YouTube `UXYvBLcLmh0`/`kkS8ctrfczQ` 补档只作内容对应证据。证据见 [P5 历史节目绑定](content-os-data/evaluation-evidence/p5-source-publication-links-20261108/validation.json)。

P5 最小运营闭环：schema 64 新增 append-only `publication_metric_observations`；普通 UI 可按发布记录追加 0–8760 小时窗口的指标快照，旧快照不可覆盖或删除。自身基线只比较同创作者、同平台、同窗口历史作品的中位数值，0/1/2+ 对照分别为不足/初步/可比较，并写入现有下一轮建议。真实库当前发布记录仍为 0，`/performance-baseline` 实机返回 `no_publication`，没有伪造学习结论。自动发布、自动指标同步、评论挖掘、实验管理和多平台包装生成暂缓。

## P6 普通 UI 自主主链

上一片 `render:49561326-c355-4fe2-8fbc-15886604bfef` 已被用户判为产品不通过，不能作为 P6 验收样本。项目 `356a2d38-b495-47b3-9062-5c224d13f8cf`，Run `413e0e7d-6cce-4926-a77b-fe9e71b74611`；普通 UI 单次启动 Job `838cdcfe-920b-493e-9da5-3a3e5f9018c4`。Planning、Voice、独立 Voice QA、Render 四个 Job 均为 `attempt=1`、`completed`，没有恢复、重试或人工代生成媒体。

文件：[49561326-c355-4fe2-8fbc-15886604bfef.mp4](content-os-data/renders/356a2d38-b495-47b3-9062-5c224d13f8cf/49561326-c355-4fe2-8fbc-15886604bfef.mp4)

技术事实：18.100 秒；1080×1920；30 fps；543 帧；H.264 + 24 kHz 单声道 AAC；681,444 bytes；SHA-256 `359F9F4C8B58FEAC92471C498303D27949C07334C4F3810A545CEC87DFD45186`。HTTP HEAD 为 200、`video/mp4`、支持 Range；FFmpeg 未检出 ≥0.2 秒黑帧或 ≥1 秒、低于 -50 dB 的断音。

Voice Audio `5bce0e4d-c818-41d2-8271-a5304641b925` 为 18.100 秒、recipe v6/W13；旧 QA 因整稿较长把未解析替换留在容差内，其中“痛苦”被 ASR 识别为“痛哭”，属于语义错误，用户判不通过。QA 已改为未解析替换一律失败，并新增目标字符级 Provider 拼音修复；相关测试待真实音频确认。全片 4 场均为本地文字卡/图解，外部素材搜索与导入 0、stock 0、AI 画面 0；这与测试提示词显式要求“不要本人出镜、不用外部实拍/AI”有关，但普通产品没有主动说明限制并给出真实短片策略，仍判产品不通过。

已确认并修复 A-roll 根因：Talking 输出曾把身份参考原片的可见区间重复记账，导致 12 秒干净人物参考被两次派生结果耗尽。现在 Talking 参考只作生成输入，派生输出以自己的 Asset 哈希登记可见使用；同项目人物池已恢复 usable/longest 均为 12 秒。普通短视频规划新增真实画面主导策略：无显式全本地约束时，实拍目标约 40% 以上、不能连续三场文字/图解，并为历史/科技/自然等主题主动请求应景 stock；当前 IP 有可用人物能力时至少保留一场本人可见讲述。

2026-11-09 普通输入复验补充：项目 `36277a3d-9dfe-4da3-a89f-be8b3961f868` 的规划已自主产出 2 场本人讲述 + 古典遗址/旧书翻页/夜间书写 B-roll。规划前的视觉母题召回和已核验连续区间描述复用已进入普通路径，避免仅换镜头用途就重复调用视觉 Provider。规划中遇到 Provider 额外尾逗号 `,"}` 时，产品以字符串感知的近似无损 JSON 修复后继续完整 schema 校验，离线回放得到 5 场。

该片 Voice 首轮及一次局部返工仍被严格 QA 拒绝：`斯多葛` 分别被 ASR 识别为“斯多哥/斯多噶”，返工修复了“边界是→边界使”，但没有证明术语读音通过。为验证通用能力而非单例，Voice Profile 的术语词条改为无歧义同音别名“斯多阁”，并生成独立 5.760 秒单模块 Audio `5aa3ba25-4083-4fe6-95f2-7cce89de43f7`；ASR 仍把术语写作“斯多噶”，因此当前离线 QA 仍判定未通过，待确切音频人审，不进入整片。证据见 [manifest.json](content-os-data/evaluation-evidence/p6-natural-input-voice-gate-20261109/manifest.json)。

运行耗时要说清：上一成功 Run 本身总耗时 `205.397s`，其中 Planning `81.7s`、Voice `58.6s`、Voice QA `34.0s`、Render `28.7s`；用户感受到的一小时来自前后多次失败、代码修复、服务重启和重复尝试，不是单次成功 Run 的 18 秒视频耗时。当前仍需用修复后的普通 UI 单次启动重新验证，且不能承诺本地模型立即完成。工作区调用账目 `406/409`，已知金额 `50.2332 CNY`，CNY 449 金额上限与未知费用授权未变。

历史普通 UI 技术链基线：`render:f22e0bac-8f45-4eb0-b30a-b98a67ed0575`

文件：[f22e0bac-8f45-4eb0-b30a-b98a67ed0575.mp4](content-os-data/renders/cf7a9b01-9105-44fd-a24d-d65e3da31cbb/f22e0bac-8f45-4eb0-b30a-b98a67ed0575.mp4)
技术事实：36.000 秒；1080×1920；30 fps；1080 帧；H.264 视频 + 24 kHz 单声道 AAC；4,295,068 bytes；SHA-256 `D6EF75CA17CDBE17C4E2B9113C3C245963E5ABDC9A6263308BE48460EFBABAB5`。FFmpeg 未检出 ≥0.2 秒黑帧或 ≥1 秒、低于 -50 dB 的断音。证据：[manifest.json](content-os-data/evaluation-evidence/ordinary-ui-autonomous-v6-20261107/manifest.json)。

该成片只保留为“普通 UI 技术链自主完成”的基线，不作为毕英杰的内容/赛道质量样本：它的“知识短视频结论先行/镜头拆分”主题来自产品测试提示词，与毕英杰当前的西方历史、古典文明与思想研究方向不一致。后续属于毕英杰的整片应使用其真实 Profile 和本赛道主题；TIM 同理使用数码科技 Profile。

B-roll 成本核对：5 场中仅开场为 Talking A-roll；其余 4 场全部是 `local-typography` Remotion 图解，AI 图片/视频 B-roll 为 0，外部 stock 为 0，渲染 provider 成本为 0。AI 只参与了图解文案/版式规划，没有生成 B-roll 像素。项目调用为 K3 规划 1 次（费用未知）+ OmniVoice、Whisper、LatentSync 本地各 1 次（0 CNY），没有图像生成调用。该审计时账目为 381 次；完成 P3 真实规划及其兼容修复后当前全局账目为 390 次/400、剩 10 次，已知金额 `50.2332 CNY`，另有 63 条未知费用调用，实际总额不能写成已知。审计见 [broll-source-cost-audit.json](content-os-data/evaluation-evidence/ordinary-ui-autonomous-v6-20261107/broll-source-cost-audit.json)。

## 自主成片归因与 Voice 单模块复验

已核对两条完成 Render 的自动 Run：`da2edfad... / d959006e...` 和 `b9b52e65... / d16fe150...` 均为 `automatic=1`；其 Planning、Voice、Voice QA、Talking（有依赖时）与 Render 均为 Content OS Job/Worker 的 `attempt=1` 产物，没有候选 Run、恢复 Job 或助手手工生成的媒体。产品运行内的成片流程不是助手代做。

但不能把整个开发过程说成完全没有介入：这些 Run 之前确实发生过代码修复、配置保存、服务重启和普通 UI 启动操作；多次失败尝试也不能算产品自主通过。因此严格结论是“已完成 Run 内由 Content OS 自主执行”，不是“从开发到成片全程零介入”。

Voice v2 单模块复验使用新项目 `30a15f11-9502-4a8c-91de-e89bb02f2c89`，同稿、同 V60 Profile `29294db2...`、同参考窗口 `28.24–34.24s`。首次 Job `7c635ee9...` 因工作区 320 次调用上限在推理前停止，没有模型调用或音频；只把调用上限最小提高到 322，CNY 449 金额上限与未知费用许可未变，随后新 Job `596bb216...` 由 Voice Worker 完成。

上一版音频 `651458ec-a6a8-4c30-bad5-e5a437426c0a`（38.690 秒，SHA-256 `0c951b...`）技术 QA 通过，但用户指出“这不是跟风是”断句错误、“分开”没有连在前句以及“照明差”误读一声，未通过人工听感。

据此新增 recipe v3 Provider 私有交付控制：把“这不是跟风，”和“步行道分开，”转换为明确收句并拆成独立生成段；“照明差”用 `照明CHA4` 拼音声调控制四声。可见原文、QA 目标和字幕仍保持原稿。新 Voice Job `c0bd95a8...` 与 Voice QA Job `be398c71...` 完成，Audio `baf2aa00-42f4-46cc-85c1-85d090b67cb2` 为 38.950 秒、7 个生成段、24 kHz PCM16；SHA-256 `76f2395f04167b1e229673f7d3ed3d5271d9c3ca3b809ee8c51dcebbd3d350f4`。QA 覆盖 1.0、缺失/重复 0、1 个已容差替换，最长静音 130 ms。

新音频在“跟风”和“是”之间形成独立 ASR 段，词间约 80 ms，PCM 实测静音 120 ms；“骑行道和步行道分开”成为独立识别段，段后 PCM 实测静音 120 ms。用户随后确认这三处均通过。

用户又指出“最主要的原因，是照明”被拆成“最主要的原因是”和“照明现在……”。这不是单段修音问题，而是缺少通用“系词+短名词补语”绑定规则。recipe v4 新增 `copula_complement_break`：识别“主语，是短名词补语。”，Provider 文本去掉主语后的逗号，将“是+补语”绑定为完整谓语，并在补语后的真实句末建立独立生成段；不依赖“原因/照明”等具体词。测试同时覆盖“真正的问题，是成本。”。

新 Voice Job `5a6ee04a...` 与 Voice QA Job `1b109112...` 完成，Audio `16ac63f2-6147-4924-8d51-8c2feb3182b5` 为 39.260 秒、8 个生成段、24 kHz PCM16；SHA-256 `f5312d9f0a742af038da4ae7c5e222629e0a23c21ef4a83e8a04ac004f63d41a`。QA 覆盖 1.0、缺失/重复 0、4 个已容差识别替换。新音频把“最主要的原因是照明”识别为一个完整段，下一句从约 40 ms 后开始，句间 PCM 实测静音 120 ms。

音频：[f5312d9f0a742af038da4ae7c5e222629e0a23c21ef4a83e8a04ac004f63d41a.wav](content-os-data/assets/audio-originals/f5312d9f0a742af038da4ae7c5e222629e0a23c21ef4a83e8a04ac004f63d41a.wav)。证据：[manifest.json](content-os-data/evaluation-evidence/voice-copula-rule-v4-20261107/manifest.json)。用户已确认此确切音频通过；Human Review `user-voice-approval-2026-11-07-v4-copula` 六项均 pass，Profile delivery review 已置为 approved。未启动 Talking、Render 或新的完整正片。

recipe v5 已移除“照明差”的单例 `pronunciation_overrides`，改为通用 `contextual_pronunciation`：根据局部“质量/条件/效果类主语 + 差作谓语”语境选择四声 `chà`，并以时差、温差、差异、差别、出差、参差、差不多为反例测试。首次 v5 Job `8b543cd2...` 在推理后因字节与已批准音频相同、旧注册逻辑拒绝重复而停止；Provider 输出原始哈希 `f8249b...` 与 v4 完全相同。随后补上相同字节的安全复用，Job `9232b500...` 完整执行并把 v5 配方、Provider 文本和语境读音决策记录到既有 Audio 的重执行历史。

当前 Profile 已升级为 recipe v6、delivery schema 1.4。`contrastive_correction_break` 按“不是 A，是/而是 B”的对照修正结构收住 A；`predicate_completion_break` 在动词与结果补语已经完成谓语后建立边界。两条规则只改 Provider 私有断点，不写具体整句，也不改可见文案。当前 Profile 的 `punctuation_overrides` 与 `generation_breaks` 已清空，不再由“这不是跟风”和“步行道分开”两条样例句承担通用能力。

同一整片稿在 v6 通用规则下生成的 Provider 文本 SHA-256 仍为 `c89ddb6b0f2d6792d5df6ba4e46dd9bc8710ebec6d2cadc880b13f6acf4d2c78`，与此前 v5 已执行并复用的人审音频输入完全相同。Content OS Voice Worker Job `148ab359-8ec6-422c-9f5c-7055abd73112` 已完成，Provider call `1b967bc7...` 为本地 OmniVoice/W13；输出音频仍为 `16ac63f2-6147-4924-8d51-8c2feb3182b5`、39.260 秒、SHA-256 `f5312d...`，与用户已批准音频逐字节相同。新执行和 v6 配方已记录到 Audio 重执行历史，Profile delivery review 现为 approved。证据见 [manifest.json](content-os-data/evaluation-evidence/voice-syntax-generalization-v6-20261107/manifest.json)。

验证：Voice 语法、Voice/QA、停顿与 Profile API 定向回归通过；Provider 私有文本不得改写可见原文，缺省/重复/重叠、非法拼音或未覆盖的多音字语境不会静默猜测。v6 已完成真实 Voice Worker 重执行及音频字节核验，未用“输入相同”代替执行结果；修改前数据库备份为 `.tmp/p6-production/before-voice-syntax-v6-20261107.sqlite3`。工作区调用上限 331，CNY 449 金额上限、未知费用许可和 provider 权限未变。

## 核心能力权威文档收口

场景规划、Voice、Talking、生产资源调度分别以 [SCENE_PLANNING.md](docs/product/SCENE_PLANNING.md)、[VOICE.md](docs/product/VOICE.md)、[TALKING.md](docs/product/TALKING.md)、[PRODUCTION_SCHEDULING.md](docs/product/PRODUCTION_SCHEDULING.md) 为唯一行为来源。旧 [VOICE_TALKING.md](docs/product/VOICE_TALKING.md) 只保留跳转，不再承载第二份规则。

[CAPABILITIES.json](docs/product/CAPABILITIES.json) 登记唯一权威文件、marker、关联模块及代码/测试入口；`scripts/sync_capability_docs.py` 自动生成 [产品模块页](docs/product/README.md) 与 [能力权威索引](docs/product/CAPABILITY_INDEX.md)。`scripts/check_docs.py` 会验证权威唯一性、marker、路径和生成文件是否过期。

验证：生成脚本 `--check`、文档治理检查、脚本编译，以及能力文档/场景规划/Talking 池/Talking 调度/自动生产/预检 6 组共 240 项测试通过；相对链接失效或生成索引过期同样会使文档检查失败。没有调用模型，也没有修改产品运行数据。

## Voice 停顿层级收口

普通 Voice Profile `29294db2-d3c6-45c2-b467-6a02e853c826` 已升级为配方 v6：保留 V60 质量锚 `c1346d02-d967-4475-b5f4-4bdf7933b2d3`、W13 runtime 与 `28.24–34.24s` 参考窗口；词组内、逗号、句末、段落、关键观点、片尾六层规则不变，新增通用对照修正与结果补语边界。句末停顿必须高于逗号；全局 speed 明确不是停顿控制器。

普通多段生成现在会在导入前消费 `seam_policy`：只处理已知段落接缝的 `terminal/continuing` 边界，只删除 10 ms、-45 dBFS 实测静音，每侧保留 60 ms，单次最多 300 ms；不改语音帧、不插入未测静音。`no_break`、`forward_binding`、无实测静音或超过上限的接缝保持原声并记录跳过原因。生成 metadata 保存配方、原始/最终哈希、实际 cuts 和最终分段时轴；Voice QA 从原始分段与 cuts 重建 Master，必须字节一致。

证据为 `content-os-data/evaluation-evidence/v60-length-completion/manifest.json`、`content-os-data/evaluation-evidence/v19c-c-punctuation-hierarchy/v19c-c-punctuation-hierarchy.json` 与 `v19b-u-voice-punctuation-hierarchy-review`。本次只完成代码、配方与文档收口，未把此前的整片音频代判为停顿质量通过；下一次普通 Voice 的真实输出仍待人工复核。

技术验证：13 组普通 Voice/QA/预检/Run/Worker/维护运行时回归共 333 项通过；文档检查和 `git diff --check` 通过。对既有真实 Voice `a4481d21-9c77-44d5-84aa-17d1cac50168` 做零付费本地回放，4 个段落接缝均识别为 `terminal`，分别删除 80/150/160/110 ms 实测静音，39,280→38,690 ms；预览 SHA-256 为 `0c951b2434f95167e2176606114daae7b1da68d0396265a9a0bc42212dfc6a23`，仅位于 `.tmp/p6-production/voice-pause-v2-preview-a4481d21.wav`，未登记为产品版本、未代判声音质量。修改前数据库备份为 `.tmp/p6-production/before-voice-pause-hierarchy-v2-20261107.sqlite3`。

另有 `test_maintained_voice_prepared.py` 3 项旧评估测试失败：一项仍尝试启动子进程探测系统版本，两项请求已从普通应用移除的评估/admission 路由；不计入普通产品链路通过数，也不据此恢复旧门禁。

三核心能力本次均有真实产物：场景规划生成 5 场；Voice 为 39.280 秒，metadata 固定 `v60-human-approved-20260925`、W13 runtime 和 `28.24–34.24s` 参考窗口；Talking 为 7.440 秒、1080×1440、25 fps，QA 覆盖 1.0、时长差 20 ms、186 帧，并进入最终 A-roll。最终画面为 1 场 Talking、5 场已核验 Pexels/本地库素材；没有用 AI 生成画面补位。

外部素材体系已完成统一并实际进入调度：Pexels、Wikimedia Commons、Kimi 搜索/Fetch 连接可由 API、手工检索和 Worker 共用；本轮复用已入库 Pexels 资产，没有重复外取。IP、Voice 与 Talking Profile 均绑定同一授权身份，Talking 参考源为 `user_asset`。

本轮另修复两个真实链路缺陷：Voice `asset_pts` 的 Asset 哈希契约导致推理前误拒；Talking 子进程缺少 `WINDIR` 导致 Matplotlib 启动失败。另修复本人可选切镜引用 stock 时未补齐允许来源，以及 Voice capability 按更新时间误选旧 W51 标记的问题。本片音频与 Talking 的 `model_usage` 已纠正为实际 W13 许可证证据；修正前数据库备份为 `.tmp/p6-production/before-w13-provenance-correction-20261106.sqlite3`。相关 Voice、Talking、规划与自动生产回归通过。失败 Run `fcf4f21f-d3d0-4a34-86ca-cf500ab3ba97`、`75d3e9e5-137d-47da-8ac9-a65d31fc34bb` 和项目 `0f07adec-134f-4cef-864b-dfa62a977f81` 均保留证据，未恢复或自动重试。

## 尚未完成与下一步

最终成片已完成技术链路，三核心能力均有真实产物：场景规划 5 场；Voice v6 35.980 秒、独立真实 ASR QA verified；Talking 4.000 秒、100 帧、1080×1440、LatentSync-1.5、QA verified；最终画面为 1 场 A-roll + 4 场本地图解，没有外部 stock 或 AI 画面。场景时长合计 35.980 秒，渲染补齐到 36.000 秒。

验证：P2 Talking/调度/生产 Run/Handler/参考池联合回归 196 项全部通过；能力索引、文档治理、Python 编译和 `git diff --check` 通过。

P2 仍处于优化中：`3be87b3f...` 保持唯一通过基线，`47fc4760...` 已由用户判失败；参考质量 v2 只保留诊断，不再自动排序。单任务时长边界已写入 [TALKING](docs/product/TALKING.md)、持久决定与运行 provenance；下一步才继续研究本轮口型调优，不启动全片。

工作区调用上限通过普通 UI 为 400；P2 完成时 375 次调用，已知金额 50.2332 CNY，CNY 449 金额上限、未知费用许可和 provider 权限未变。首个 Talking 4 秒参考不足失败、两个合格候选和全部 QA 证据均保留。
