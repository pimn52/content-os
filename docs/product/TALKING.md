<!-- capability-authority:talking_generation -->
# Talking 与对口型

本页是 Talking 生成、人物参考、口型 QA 和局部修复的唯一权威来源。Voice 负责独立声线，场景规划负责编辑意图，跨能力调度负责窗口分配；本页不重复它们的规则。

## 当前方案

普通 Talking 使用本机维护的 LatentSync 1.5 双 runtime。Provider 从 `CONTENT_OS_MAINTAINED_TALKING_CONFIG` 指向的版本化 runtime 配置读取 engine/vendor/checkpoint、device、inference steps、guidance、seed、attention、allocator、timeout 和输出边界；实际 provider/model/runtime/machine、参数、参考哈希和输出哈希写入 execution evidence。

当前工作区的 runtime 快照和最近一次真实产物在 [STATUS](../../STATUS.md)。runtime 配置变化不自动继承旧执行证据；不能只凭 provider 名称或已安装权重宣称本次可执行。

## 单次任务时长与机器边界

- LatentSync 的单次口型任务存在本机质量边界。该边界约束“单次完整旁白输入时长”，不是整片总时长、拼接后 A-roll 时长、参考素材时长或渲染场景时长。
- 官方声明最低 VRAM 为 `6.5 GB`。当前验证机器是 RTX 3060 Laptop `6144 MiB`（6 GiB），低于官方声明值，因此必须使用本机实测出的安全上限，不能把官方能否启动当作质量通过。
- 历史本机验证的精确范围是 `LatentSync-1.5 / local-compatibility / asus-rtx3060-laptop-6gb`：单次旁白 `4460 ms` 曾通过；`5120 ms` 技术上可播放且容器/时长 QA 通过，但用户观察到口型质量下降，质量判断为失败。`9740 ms` 是技术请求失败，不作为质量边界的有效样本。
- `4460 ms` 输入对应的输出视频为 `4480 ms`、112 帧，这是 LatentSync 16 帧窗口对齐后的容器时长，不能反过来把 `4480 ms` 写成输入旁白上限。
- 机器级上限只对确切的 provider/model/runtime/machine 组合有效，不继承到另一台电脑、另一 runtime 或其他模型。当前 W52 runtime 在同一台 6 GiB 机器上保存 `max_talking_duration_ms=4460` 作为保守调度上限，状态是“沿用同机历史边界，尚未对 W52 重新做质量验证”，不能写成 verified quality。来源 manifest 为 `content-os-data/latentsync-duration-boundary-20260915/manifest.json`，精确 setting 为 `e6204425-40fc-43c7-bf82-de945c012181`。
- 正常调度按完整 ASR 语句边界拆分长旁白，每个 Talking Job 不超过当前能力 profile 或本机 setting 的上限；不得为了让一个 Job 跑完而在语音词中间切段。未配置可用上限时，参考窗口时长只证明技术覆盖，不证明长任务质量。
- 换机器或更换 runtime 后必须重新做有界 pass/fail 校准，保存输入时长、输出文件、技术 QA 和人工可见口型判断。advanced setting 可按机器调整，但不得成为跨机器通用常量。
- 该边界用于调度和失败恢复，不是普通产品的商业、能力档案或逐次人工审批门槛。Talking Job 的 `slice_max_duration_ms` 同时绑定已解析上限和来源；机器设置来源记录为 `machine-setting:max_talking_duration_ms:<setting_id>`，不能再误标成参考素材时长。
- 普通 UI 的“Provider 与本机高级设置”提供整数参数 `max_talking_duration_ms`。当前运行配置能解析出 provider/model/runtime/machine 时自动选中该精确 scope 并带入机器标识；无法解析时仍显示 `4460 ms` 初始值，并提示填写目标本机标识后调整。默认值会进入实际排队调度，但不写成已验证；用户保存值必须通过后续 Talking QA 和可见口型人审才可能成为本机质量证据。当前配置解析证据见 [verification.json](../../content-os-data/evaluation-evidence/talking-duration-ui-20261108/verification.json)。
- 当前 W52 runtime 已用完整 ASR 语句组做一次有界校准：已批准 Master `19.000–23.170s` 共 `4170 ms`，低于 `4460 ms` 上限；参考 `5.000–9.500s` 共 `4500 ms`，覆盖 `4480 ms` 模型窗口。Asset `8da57bd1-...` 技术 QA 通过（4200 ms、105/105 帧、时长差 30 ms、上游文案 QA verified），但可见口型仍未通过人审。已批准稿件中不存在恰好 4460 ms 的连续完整语句组，因此没有为凑数切词。

## 输入与输出

- 输入必须绑定一条通过 QA 的 Master 音频、确切语句区间、已授权的 Talking Profile、参考 Clip 和同 IP 身份。
- 人像参考只需自然、连续、脸嘴清晰；可以不流利或不说话，声音来自独立 Voice。静音动作参考可入池，但不代表口型或 likeness 已通过。
- Talking 的参考素材只是身份、人物和动作生成输入，不是成片中的可见画面。每个生成的 Talking 输出以自己的 Asset 哈希和输出区间登记可见占用；不能把参考原片的区间也记作成片使用，否则同一段干净人物素材会被派生结果误耗空。
- 输出 `TalkingRun` 保存原始参考、连续片段、实际音频区间、生成文件、帧率、时长、来源和 QA；提供商子任务不直接冒充成片资产。
- 同文案同区间的已认可 TalkingRun 可按本次用途复用；跨 Run 或改动语音的新表演必须核对确切依赖。原样事实引用、梗和对照不能因“生成”标签被机械禁用。

## 人物参考池

- 素材权利在入库登记、人物同意在 Profile 登记；Run 继承，不再逐次索权。池只接受 Profile 与当前 IP 匹配、来源哈希可验证且确有连续合格区间的参考。
- 参考按同一来源的实际连续区间统计，可用总时长先去同源重叠；不同 Clip 的独立短片不拼成未经证明的长连续区间。声音参考不计入可见动作容量。
- 已被其他有效作品占用的区间和未知来源坐标不能冒充分配容量。人物容量只限制可见画面分配，不限制完整旁白文案。
- 本人参考不足时，只使用场景规划原稿已允许的切题实拍投影；保留必要本人出镜、完整原文和证据要求，不能临时改写或降级不可缺表演。

## 生成与 QA

- 一段 Talking 绑定唯一 Master 区间和授权参考窗口。已派发窗口固定，重放不生成新任务；批次整体分配失败时不产生部分结果。
- LatentSync 参考转 25 fps 后按实际帧数覆盖模型窗口。原始输出独立核验画轨帧数与时长；短画轨、末帧复制或帧率不符不得进入装配。
- 输出技术 QA 独立解码画轨，保存实际帧数、时长、帧率、文件哈希和覆盖关系。容器音轨时长、声明帧率或待测平均帧率不能自证同步。
- 配置本地 Talking 运行时时，QA 额外用 MediaPipe 人脸关键点保存参考/输出的脸部可见率、头部稳定性、嘴部活动和音频包络相关性。该观察只用于定位参考质量、模型同步或装配偏移，不成为新的普通 UI 硬门。
- 本次 Run 的技术合格输出可直接进入待审成片，不要求子片 U-Talking、精确预览、连续性审核或运行准入；最终 likeness、自然度、可见口型和整片节奏由用户判断。
- 帧率转换可能重复采样；技术覆盖不等于循环、口型同步或人物自然度通过。短样本成功不外推到长视频或另一参考。

## 尾音收口与末字时序

- `terminal_face_closeout` 是独立能力：在模型输入末尾加入受控静音前瞻，让嘴在语音结束后自然收住，再把输出裁回确切口播结束。静音只用于推理上下文，不进入交付音轨。
- 当前配置参数是 `trailing_silence_lookahead_ms=600`，并记录 175ms leading context、实际语音结束和全部参数来源。它解决“话讲完了嘴还继续动”，不解决最后一个音节内部的 viseme 起落。
- 历史 D6g 在 `LatentSync-1.5 / local-compatibility / asus-rtx3060-laptop-6gb` 上有输出视频、末六帧、Voice QA 和 Job 结果；证据整理见 [D6g manifest](../../content-os-data/evaluation-evidence/talking-terminal-closeout-d6g/manifest.json)。现有代码把该记录描述为 U-Talking accepted local evaluation，但没有找到独立、不可变的人审记录文件，因此不能把历史结论扩展到 W52。
- 当前 W52 runtime 默认配置仍为 `terminal_face_closeout=false`；最初的 `4170ms` 时长边界候选也未启用收口。后续已按同输入、同参考补做“启用 / 不启用收口”的匹配 A/B。
- 全局嘴部/音频运动相关性会稀释短音节的局部错误。普通 QA 现在从已持久化 `voice_word_timing` 投影切片内的最后 3 个完整词，比较每个词的音频 RMS、嘴部开口峰值和嘴宽变化，并保存 `word_timing_alignment`。峰值和嘴宽只用于定位与候选比较，不能证明音素级 viseme 形状。
- W52 已补同一 `4170ms` 音频、同一 `5.000–10.500s` 参考的收口 A/B：关闭收口时“来/拆”嘴部峰值分别相对音频峰值偏移 `-200/+200ms`；开启 600ms 收口后两者均为 `0ms`，且两种候选在说话结束后的 30ms 都没有再次张嘴。整体相关性几乎相同（约 `0.284`），说明局部词级观察比整段相关性更贴近本次问题。
- 用户人审认为收口候选提前回头更符合尾句收束，整体优于无收口对照；但“拆”的嘴形仍不像，可能同时受模型保留的皱眉/表情影响。该候选标记为“相对更优但未通过”，保留媒体和证据，不针对“拆”硬加单字规则。收口前“拆”的嘴宽峰值晚于音频，收口后提前到能量主体附近，但形状语义仍只能依赖人审。审阅证据见 [closeout-ab-manifest.json](../../content-os-data/evaluation-evidence/talking-duration-boundary-w52-20261108/closeout-ab-manifest.json)。
- 在相同输入和 reference 下，`trailing_silence_lookahead_ms=750` 相对 600ms 仅把“拆”的嘴宽峰值再提前约 40ms，3800ms 的二次开口/宽度维持仍存在；技术运行成功但没有证明修复。该 750ms 候选只作为待视觉比较实验，不进入产品默认，不再继续递增参数碰运气。
- 当前 12 秒人物参考 Asset `9d968a01...` 本身没有音轨，但其 W43 crop proof 保留了原始 Asset `e665fa62...`。原片有 48kHz 双声道 AAC 和转写；源画面偏 O 型约 `8.709–8.742s`，本地 small/medium 两套词级时间都落在原句“反而对中华文明有的新认知”的首字“反”内。该词只说明参考动作来源，不能被复制成目标语义。
- 当前参考窗口 `5.000–10.500s` 把源 `8.709–8.742s` 映射到输出 `3.709–3.742s`，正好落在目标“拆”的 `3.570–4.170s`。因此候选的 O 型与源“反”的强开口动作同位；它不是“拆”的正确读音形状，也不是单纯尾音没收住。
- 已按同 Voice、同 600ms 收口测试一个通用替代窗口 `4.500–10.000s`：该窗口在目标末词映射区间的源嘴部开口上限从 `0.1706` 降到 `0.0954`，但生成末词嘴部峰值从相对音频 `0ms` 退化到 `+480ms`。整个窗口的包络相关虽从 `0.2832` 升到 `0.4025`，局部时序却更差，故候选拒绝且不改变产品默认。证据见 [reference-shift-4500-10000.json](../../content-os-data/evaluation-evidence/talking-duration-boundary-w52-20261108/reference-shift-4500-10000.json)。
- “拆”（`chai`）进入韵母后应有非圆撮的较宽开口，再向 `i` 收拢。候选在 3600ms 的最大开口仍伴随较窄嘴宽，形成偏 O 型；600ms 与 750ms 收口都保留这一问题。继续调收口、steps 或 seed 已不属于可证明的产品修复，保留 600ms 相对更优成果，等待替代参考/模型或表情控制能力。
- 当局部音频峰值、嘴部开口峰值和嘴宽峰值已经接近，但人审仍认为单词语义口型不对时，应归入模型 viseme 精度、25 fps 采样、256 处理分辨率或参考表情残留问题，而不是继续改断句、时长上限或收口参数。该情况需要替代模型/参考或可分离的表情控制；单个词不得写成产品特例。

## 投入收益与停止条件

- 成片“可用”和 Talking“能力通过”是两个判断：产品可以接受一条整体可用、局部口型仍有瑕疵的成片继续创作，但不能因此把当前 provider 写成音素级口型通过。
- 当前 LatentSync 1.5 路径没有明确 viseme/表情控制。音频与嘴部峰值已经对齐、仍只剩嘴形类别和皱眉残留时，继续调 lookahead、steps 或 seed 的收益很低，应停止该 provider 的局部参数搜索。
- 当前 600ms 收口候选保留为“相对更优但未通过”。是否让一条成片继续进入发布评估，由整片内容价值、可理解性和用户最终判断决定，不由 Talking 子模块技术满分决定。
- 只有新模型、参考预处理或嘴形控制能在普通 Worker 中稳定复用，并按整片收益和预算证明值得接入时才继续投入；不能为单条样片或单个音素启动新的调参支线。

## 修复与复用

- 已知素材错误回到媒体调度；时长错误重新配时并保留合格 Voice；口型错误只修相关人物段；字幕、布局和剪辑错误修改装配后重渲染。
- 保留前后来源、调用和判断。已发生或状态不明的推理不能当作未发生；未定位根因不触发整片人物重生。
- 独立 Talking QA 定位的短画轨、帧率不符或明确无效媒体可沿普通 Worker 做一次有界局部修正，保留合格旁白与其他人物段。
- 成片中的口型不同步绑定确切版本、场景和证据；生成画面与装配均可能造成症状，未区分责任前保持未知，不自动重生。

## 参考窗口质量选择

- 存在多个同源前向窗口时，普通调度最多比较 6 个无占用候选；本地运行时可测量脸部可见率、头部稳定性、嘴部活动和脸部尺寸。
- 普通产品选择使用最早的未占用有界前向窗口；探针不可用也不改变这一保守回退。观察指标随 Job 保存，但默认不参与窗口排序。
- 实验性参考质量 v2 为 `0.20*脸部可见率 + 0.30*头部稳定性 + 0.20*脸型朝向稳定性 + 0.20*嘴部活动 + 0.10*脸部尺寸`。它保留为诊断指标，不是普通产品默认排序规则。分数高不能替代 likeness、自然度或可见口型人审。
- 有原始来源链路时，后续参考选择必须能关联源音频/转写，在目标词映射区间检查源 viseme 占用；但“源嘴更安静”不能单独成为排序规则。`4.500–10.000s` 已证明局部开口降低仍可能让生成末词时序更差。候选必须以同一输入同时比较源动作冲突、生成词级时序和最终人审，不能按整段相关性或单一 mouth-activity 分数放行。
- 当前唯一获用户认可的 Talking 基线为 Asset `3be87b3f-...`，使用已批准 Master `31.050–33.090s`、参考 `5.000–8.000s`。参考 v2 候选 Asset `47fc4760-...` 使用 `6.500–9.500s`，虽然技术 QA 通过且包络相关性更高，用户比较后确认口型同步更差，已判失败，不得自动复用或继续以“分数更高”作为默认依据。人审记录见 [user-decision.json](../../content-os-data/evaluation-evidence/talking-sync-p2-20261108/user-decision.json)。
- 本地运动包络只解释候选差异：已通过短基线的运动相关约 `0.301`、峰值偏移约 `-80ms`；`47fc...` 的相关性更高但视觉更差，说明现有指标不能自动裁决口型质量。它不是 SyncNet 置信度，也不证明音素级口型。

### 有限参数实验

- 当前产品 runtime 仍保留已通过的 `seed=1247`、`inference_steps=20`、`guidance_scale=1.5`；实验候选不自动覆盖产品默认。
- 在 `5.000–8.000s` 参考上，`seed=0 / 20 steps` 的包络相关性约 `0.293`；`seed=1247 / 28 steps` 约 `0.329`；`36 steps` 约 `0.280`；`28 steps / guidance=1.7` 约 `0.294`。简单增加步数不是单调提升。
- `28 steps`、`6.500–9.500s` 及两者组合只保留实验证据，不进入普通产品默认。任何后续调参必须先修复可解释的通用缺口，再对确切候选做用户视觉 A/B，不得把自动相关性或单条样片直接升级为规则。

## 验证与关联

代码入口：[maintained_talking.py](../../services/api/app/providers/maintained_talking.py)、[settings_schema.py](../../services/api/app/routing/settings_schema.py)、[production_runs.py](../../services/api/app/production_runs.py)、[main.tsx](../../apps/web/src/main.tsx)、[talking_recipe.py](../../services/api/app/domain/talking_recipe.py)、[pool.py](../../services/api/app/talking/pool.py)、[scheduling.py](../../services/api/app/talking/scheduling.py)、[sync_probe.py](../../services/api/app/talking/sync_probe.py)。测试入口见[能力权威索引](CAPABILITY_INDEX.md)。

Voice 配方见[Voice](VOICE.md)，场景中的可替代意图见[场景规划](SCENE_PLANNING.md)，跨能力资源顺序见[生产资源调度](PRODUCTION_SCHEDULING.md)，成片装配见[时间线与渲染](TIMELINE_RENDER_LEARNING.md)。当前能力与最近确切成片只看 [STATUS](../../STATUS.md)。
