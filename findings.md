# 关键事实

## 2026-11-08 信息损失图节点归一

- 项目 R 的 DeepSeek 完整 6 场方案中，唯一错误是 `diagram_loss` 使用 3 个节点。该图表达“判断→不表达→真实感受”的因果序列，节点本身有效。
- 产品只在 `diagram_loss` 超过两个节点时归一为 `diagram_flow`，保留全部节点与文案；两节点信息损失图和其它图解类型不变。真实响应离线解析为 6 场。

## 2026-11-08 DeepSeek supporting visual 字段错位

- 项目 Q 的 DeepSeek 响应完整 7 场、18.6 秒；唯一契约问题是把“本人补充讲解”错放到一个 explanatory 场景的 `supporting_visual_intent`。该场景已有 stock 主画面及 talking_profile 备用来源。
- 该字段只适用于可替代的 creator_speaking。解析层现清理 explanatory 场景上的错位支持画面注解，不改变主画面、fallback、文案或 Creator Fit；真实响应离线回放为 7 场。

## 2026-11-08 Kimi 429 后的 DeepSeek 规划兼容

- 项目 N/O 均因当前 Kimi 规划额度返回 HTTP 429；调用虽记账但没有方案、素材或媒体。普通运行配置已切到本机已有 DeepSeek 凭据，视觉仍使用 Kimi K2.6，金额上限未变。
- 项目 P 的 DeepSeek 响应在 18.6 秒内完整返回 6 场，但两个已知 Creator Fit 字段被误放进 `supporting_visual_intent`。解析层只移除已知错位注解，其它有值未知字段继续失败；真实响应离线回放为 6 场。

## 2026-11-08 项目 M 的别名与返工叠加

- 项目 M 的规划与首次 Voice 均完成；自动返工在 33 ms 内无模型调用失败，原因是同一个“葛”已由 Voice Profile 的 `斯多葛→斯多格` 同音别名替换，旧 QA 返工清单又要求将它转为拼音并因字符不存在而报错。
- 字符级修复现只在字符数差证明目标已由同音别名覆盖时跳过重复控制；不是全局忽略缺失，其它未定位字符继续失败。文件既有失败 Job 的修复输入现可生成，Voice 配方/修复/生成 102 项通过。

## 2026-11-08 项目 K/L 的两个入口兼容缺口

- 项目 K 的规划已成功并进入 Voice，但读音别名在稿中重复出现时触发 `voice_delivery_adjustment_ambiguous`。读音词条现统一作用于全部出现位置；标点/句法边界仍要求唯一上下文，避免错误修改。
- 项目 L 的 Planning 响应完整但单个 JSON 键写成 `"deviation_risks:[`，缺少键名闭合引号。协议层新增只针对未转义 ASCII 键与 ASCII 冒号的语法恢复；真实响应离线已解析为 6 场，业务 schema 不变。

## 2026-11-08 项目 J 的 Provider 拼音控制边界

- 项目 J 第二次 QA 的有效证据显示，数字拼音序列 `SI1 DUO1 GE2` 在 W13 上仍被识别为“斯多吉”，因此不能继续假设“完整拼音序列一定等价于自然发音”。
- 产品现支持已审核的 Provider 私有同音字别名：目标与别名必须逐字同音、同声调（如“斯多葛”→“斯多格”），别名只进入 Provider 输入，不修改原文、字幕或 QA 目标。该能力由 Voice Profile 词条承载，可追溯和调整，不绑定具体句子。配方/修复/生成 100 项通过。

## 2026-11-08 项目 I 的术语音节边界复验

- 项目 I 已证明普通产品会自动执行一次 Voice 返工，不是停等人工诊断；第二次 Voice 44.96 秒、QA 63.58 秒后仍把重复出现的“斯多葛”识别为“斯多噶”。
- 单字 `GE2` 控制会保留不清楚的音节边界。修复层现识别全稿重复的至少三字 CJK 术语，并对匹配术语位置输出完整拼音序列 `SI1 DUO1 GE2`；这是数据驱动的术语级通道，不绑定具体句子且不改可见文案。项目 I 的第二次失败保留；Voice 修复/生成 88 项通过。

## 2026-11-08 普通 UI 项目 H 的混合字级 Voice 返工缺口

- 项目 H 的规划已自主成功（160.9 秒），素材召回修复让真实 A-roll、图解和应景素材进入执行；Voice 44.0 秒完成，独立 ASR QA 66.7 秒后停止。
- 实际 QA 不是不可定位错误：第 0、2、3 段共有 3 个未解析替换和 1 个可定位缺字，涉及 `葛→噶`、`是→吃`、`只` 缺失及“事/是”等同音异形。旧批量返工拒绝含同音等价记录或缺字的段，也不排除已经同音的字级差异，故未自动执行。
- 批量返工现允许 `copy_missing_tokens + copy_substitution_tokens + copy_phonetic_equivalence` 的可定位组合，但每段所需修复数必须等于未解析缺字/替换数；重复、长静音、不可播放和无法逐字绑定仍停止。同音异形不产生无关拼音控制。既有失败 Audio 的只读重算计划现为 `regenerate_take/whole_take`，Voice 修复/生成 87 项通过。

## 2026-11-08 普通 UI 项目 G 的素材召回缺口

- 项目 G 已正常保存 5 场完整内容方案；素材阶段停在 `s4-tonight-method-part-1`，不是素材库没有候选。现有库中存在明确的“夜晚台灯下在笔记本写字”和“古罗马石柱/遗迹”素材。
- 根因是把整段旁白与短 `visual_intent` 合成一个词法检索串，观点用词稀释了真正的镜头词，精确素材没有进入候选。选材现先按主体和动作召回，只有零命中才使用完整旁白宽召回；最终仍执行真实区间用途核验。素材选择、分辨率和批次相关 84 项通过。

## 2026-11-08 普通 UI 项目 F 的规划 JSON 协议缺口

- 项目 F 的 `kimi-k3` 响应是完整 6 场方案，不是内容或长度不足；失败只因 `deviation_risks` 键后使用中文冒号且缺少闭合引号，属于模型 JSON 语法。
- 规划解析现只对未转义、ASCII 标识符形式的 JSON 键做确定性分隔符修复，兼容 `"key：` 和 `"key"：`；中文正文、字符串值和 schema 规则不受影响。失败响应离线回放得到 6 场：2 场本人 A-roll、3 场应景 stock 需求、1 场真实分叉图解。场景规划/API 231 项通过。

## 2026-11-08 普通 UI 项目 E 的 Voice 段落映射缺口

- 项目 E 规划已按正常短视频策略产出 7 场：2 场本人 A-roll、罗马雕像/遗迹及历史处境 stock、1 场关系图解和沉思氛围 stock，不再全流程卡。
- Voice 在 46.2 秒后报 `voice_output_invalid` 并非模型无输出：10 个真实音频分段已生成。通用 `copula_complement_break` 对“区分它们的，是三个因素。”产生的目标 break 恰好等于已有段落端点，`copy_spans_with_breaks` 把它再次切分后报错。
- 映射规则现忽略“已经是现有段落 start/end”的重复 break，仍拒绝文本外、重叠或无法归属的 break。既有 10 段输出离线回放成功映射到不改字的编辑稿；Voice/生成相关 45 项通过。该失败归产品通用能力，不为项目 E 单句写例外。

## 2026-11-08 裁剪回归与素材 IP 边界

- 裁剪评估回归不是裁剪核验放宽后的真实回退：普通项目创建时自动带 IP，旧测试夹具中的本人素材没有 `ip_profile_id`，新隔离规则因此正确地不把该素材交给项目。
- 现规则为：目标项目有 IP 时，本人/历史/AI 素材必须精确绑定该 IP；目标项目没有 IP 时，只允许未归属素材，仍不能用已绑定其它创作者的素材；stock 可共享。裁剪评估和素材治理 9 项回归通过，未改动裁剪失败关闭和用途核验。

## 2026-11-08 用户判上一片产品不通过

- `render:49561326...` 只是普通 UI 技术链样本，不是产品验收通过。用户指出源素材有十多分钟人物口播却无 A-roll、B-roll 全是同质流程卡、未使用更应景的斯多葛外部影片、Voice 把“痛苦”读成“痛哭”，且普通 UI 的调试总耗时过长。
- A-roll 缺失不是人物容量本身不足：Talking 曾把新生成输出追溯为参考原片区间再次可见使用，旧两次 Talking 耗尽 12 秒干净参考，只剩 566ms。修复后参考只算身份/动作生成输入，派生输出自己的 Asset 哈希占用可见区间；现场人物池恢复 7 条、usable/longest 均 12 秒。
- 普通短视频视觉策略已从隐含模型偏好提升为自动规划上下文：未显式要求全本地图文时，真实画面目标约占 40% 以上、最多连续两场文字/图解；具象历史/科技/自然主题主动请求应景 stock；当前 IP 有可执行人物能力时至少保留一场本人可见讲述。旧全图解来自测试提示词限制，不能代表产品正常策略。
- Voice QA 旧容差在整稿较长时把“痛苦→痛哭”作为少量替换放过。现在未解析替换一律失败，只有上下文默认读音和声调一致的同音差异可记录为等价；自动分段修复用有序 token 对齐，只对失败的目标 CJK 字符生成 Provider 私有拼音（如 `KU3`），不改可见文案、字幕或整句。
- 单次成功 Run 总耗时 `205.397s`：Planning `81.7s`、Voice `58.6s`、Voice QA `34.0s`、Render `28.7s`。约一小时是开发、失败尝试、修复和重启的总体验，不能说成单次产品运行耗时；也不能把 3 分 25 秒写成已经满足“快速自主成片”。

## 2026-11-08 P6 普通 UI 主链与 Voice ASR 通用缺口

- 最终普通 UI 项目 `356a2d38...` 使用一次主题输入、单次启动 Job `838cdcfe...` 自主完成 Run `413e0e7d...`；Planning、Voice、Voice QA、Render 均 `attempt=1/completed`，没有恢复、重试或人工代生成媒体。
- 最终成片 `render:49561326...` 为 18.100 秒、1080×1920、30 fps、543 帧、H.264 + 24 kHz AAC，SHA-256 `359f9f...`。HTTP HEAD/Range 可用，FFmpeg 未检出 ≥0.2 秒黑帧或 ≥1 秒、低于 -50 dB 断音。
- 4 场方案为 `text_card/headline` + `diagram_flow` + `diagram_loss` + `diagram_factors`；全部 typography、全部 Creator Fit matched、无新素材请求。外部素材搜索/导入、stock 和 AI 画面均为 0。
- Voice Audio `5bce0e4d...` 为 18.100 秒 recipe v6/W13；独立 Faster-Whisper QA verified，missing/duplicate 0，worker runtime 与 machine 已保存。账目 `406/409`、已知 `50.2332 CNY`。
- 当前只能确认普通 UI 技术主链自主完成；最终整片内容与感官质量待用户对确切 `render:49561326...` 判断。
- 后续干净 Run 在 Voice QA 后又暴露全图解规划缺口：模型正确判断开场只有标题、没有可声明的图关系，返回 `graphic_plan.kind=unsupported`；产品却把它当 Renderer 不支持，而不是按用户显式“全本地文字/图解”约束生成受支持的标题卡。已补通用归一：开场问题→`headline`、对比→`contrast`、其他要点→`key_point`，仅显式本地约束允许，标签不复制整段旁白。
- 第二轮 Voice Run `a4c52439...` 只剩一个故障段；进一步修复了逐段 ASR 与整片 ASR 的合法接缝差异，以及“整稿重音 cue 不在故障段内时可复用前序已执行重点”这一通用返工条件。对旧 Audio 的只读修复计划已从 `stop` 变为 `regenerate_take`，精确指向第 5 段。
- 普通 UI 新项目 `b2f97997-a05d-49a2-9cd4-881e83c74e89` 无 A-roll、无外部实拍/AI 画面，自动 Run `5f7a44dc-21ce-4df6-bb9f-0b24bf9f82c2` 完成 4 场全图解规划并生成 Audio `f64acc18-d9d5-4fd8-b763-98a292b5ef4d`（32.090 秒）。
- Voice QA 原始失败不是漏读：目标“两千年前”被 ASR 写成“2000年前”，比较流把数字书写差异误判为缺词；另有“它/他”“沉/陈”“录/路”同音字形和“葛/噶”专名识别差异。旧 QA 只接受字典唯一读音，导致同音字形没有按声音等价。
- Voice 修复计划另有三个产品缺口：Draft 的自动 performance plan 为 `null`，修复器把“只在 Voice Job/Audio 冻结的自动 plan”误判为意图变化；本地 ASR 仍被 `commercial_safe`、许可证据和 `quality_status=verified` 二次拦截；逐段 ASR 与整片 ASR 的合法接缝差异被要求对象完全同形。
- 已改为通用能力：计数/时间单位前的 spoken quantity 归一到 ASCII；同音字按 pypinyin 上下文默认读音记录等价；Voice QA 持久化实际 ASR runtime/machine/parameters；修复只核对已观察身份和来源，不检查商业档或许可标签；分段修复核对有序 spoken token，不要求 ASR 接缝相同。
- 对原失败 Audio 做零付费离线重判：missing/duplicate 为 0，157 个 token 中 3 个同音字形被记录，1 个有界替换保留，结果为 `qa_state=verified`；没有改写原始 ASR、字幕或音频。
- Voice 相关完整回归 101 项通过；文档治理和能力索引同步通过。失败 Run 未恢复、未重试，下一步需重启服务后在普通 UI 新建干净项目复验。

## 2026-11-08 P3 公开账号与 P4 素材治理

- 已确认并关联账号：毕英杰 B站 `3546799388232614`、YouTube `@bi.johnathan`、抖音 `bi.johnathan`；TIM B站 `946974`、YouTube `@mediastorm6801`、抖音 `superslow`。两账号的公开名称、简介、粉丝/作品指标与代表作品已留证。
- 小红书同名搜索在未登录状态都显示“登录后查看搜索结果”；没有把搜索摘要、笔记提及或同名账号当作官方主页。
- 共导入 30 条历史记录。规划每次至多读取 8 条并跨平台轮转，防止第一个平台的完整历史耗尽预算而隐藏其他平台。历史链接只是来源定位，不等于已推断受众或分区。
- 最新 Profile：毕英杰 v4（西方哲学经典、思想史/宗教学、学者访谈、哲学/AI/创业、遗址行走）；TIM v2（数码科技、影像设备、视频制作与平台分发）。
- 最新真实规划：毕英杰 `2e74295e...` 5 场/46 秒、TIM `45036960...` 5 场/60 秒，均 5/5 matched，并引用 3 账号、8 条跨平台历史。两稿事实仍需人审；毕英杰的匹配支柱合法但偏宽，不把 matched 当作最佳粒度。
- 兼容修复只处理模型输出层：未知/过短 Clip ID 清为空提示，已知被权利阻断的素材仍拒绝；Creator Fit 同义元数据、整数形式时长、非正时长估算不阻断完整内容方案。技术验证不能替代编辑判断。
- P4 现在保证新视频默认绑定当前 IP，本人/历史/AI B-roll 需要 exact IP，stock 可共享。旧库未绑定本人物料保留但不跨 IP 进入新选材。
- 素材分析生成 3–8 帧 dHash；近似重复只在同 IP 查询，距离是提示，不自动删除或合并。active/archived 与引用检查已进入普通 UI；归档保留全部历史引用，当前不提供绕过检查的物理删除。
- 素材治理已成为产品内部 append-only 事件账本：schema 62 的 `asset_governance_events` 记录导入、分类/归属、生命周期、指纹成败和近似重复扫描，schema 63 在数据库层禁止 UPDATE/DELETE。72 个旧 Asset 的迁移快照明确标记为 `snapshot_initialized`；后续事件由 Worker/普通 API 自动写入，普通 UI 可查看事件数、最近事件、引用数和完整治理记录。
- 真实主素材引用数 `e665=23`、`b534=21`、`b98=157`；三者的精确哈希唯一，12 距离阈值内无近似重复。视觉描述覆盖率仍是 80/253，不能写成完整视觉索引。
- 工作区预算剩 10 次（390/400），已知金额 `50.2332 CNY`，63 条未知费用；本轮没有用剩余预算硬刷视觉覆盖率。

## 2026-11-07 核心生产能力二次审计

### 基线判断

- `cf7a9b01... / a301d7d4... / render:f22e0bac...` 的普通 UI 工序链和 `attempt=1` 成果仍成立，不能再重复质疑“是否由产品跑完”。
- 用户对该确切版本给出新的质量结论：Voice 的“最后”“三步”等焦点平铺，Talking 口型仍不够匹配。技术 QA 的通过不能覆盖这两项人审失败。

### Voice 重音缺口

- 产品已有 `NarrationPerformancePlan`、`NarrationPerformanceCue`、emphasis/pace/pause/rhythm 语义和 provider-neutral 编译；但普通自动 Voice 没有生成或消费性能计划。真实 Draft 的 `narration_performance_plan=null`，Voice 仍按 5 个段落生成，metadata 中没有“最后”“三步”的重音 cue。
- 普通派发在发现 `narration_performance_plan` 时直接返回 `voice_performance_plan_dispatch_not_implemented`；Voice Handler 也拒绝 performance plan 与参考窗口或 delivery text 组合。Maintained Voice 同样要求 performance plan 为空。
- `OmniVoiceProvider.performance_intent_support=UNKNOWN`；配置 schema 明确当前适配器未把重音、语速、停顿、节奏映射到运行时。实际本地入口固定 `instruct=None`，没有逐词或逐短语的 emphasis 控制。
- 当前 suggestion 器只可靠识别少量平行否定、列举推进、转折和断言边界；“最后”只作为前向绑定标记，“三步”没有形成焦点 cue。即使生成 plan，也不能因此宣称音频已有重音。
- 现有 recipe v6 的人审 `emphasis=pass` 是 V60 质量锚的历史配方事实；这次新 Audio `dac14f3d...` 的 `human_review_state` 仍为 `pending`，不能复用为当前音频重音通过。
- 结论：上一版“平铺念稿”是模型执行路径的必然结果，不是偶发。需要同时补通用焦点识别、可执行 Provider 映射、韵律 QA/候选比较和普通 Worker 消费。

### P1 重音能力实现与单模块证据

- `voice_emphasis.py` 已实现 `zh-focus-emphasis-v1`：序列收束“最后/最终”、数量框架“记住三步：”、短断言和对照修正肯定项；普通自动 Voice 在 Draft 没有人工 plan 时生成 emphasis-only assisted plan。反例覆盖“最后一天”“最后，终于”“三步并作一步”“一点”以及只有单句前置内容时的“最后”。
- Provider 文本只在精确焦点区间建立独立生成段；实际 PCM 只在 cue 对应段应用有界增益，`strong` 目标 +1.58 dB，首尾 10 ms 渐变，不改时长、不改全局 speed。修改后的焦点 part 另存并更新哈希；Voice QA 从实际 part 和静音 cuts 重建 Master。
- 单模块项目 `d8de1ff5-635d-41b5-afda-a9bbf7515562` 使用同稿 SHA-256 `fe36242b...`。首个 Voice Job `a824a0a0...` 完成；首次 QA `81cc0aad...` 因焦点变声后仍引用原 part 哈希而报 `voice_qa_segment_provenance_invalid`，失败证据保留。
- 修复 part provenance 后，Voice Job `63ac3f28...` 真实重执行并复用同一音频字节；Voice QA `c4828053...` 完成，`qa_state=verified`、copy coverage 1.0、missing/duplicate 0。Audio `9d68deed-884c-4c41-af6f-16350482b7e4` 为 37.850 秒、24 kHz 单声道 PCM16，SHA-256 `bb2b898b5f65e55e7edf45ea20e11caf26c9a1ee266f6824a621e6ae41d1cdc7`。
- 两个焦点均实际应用：`最后，` 位于 segment 3，`记住三步：` 位于 segment 5，分别记录应用前后 RMS 和增益。ASR 将“最後”识别为独立段 `19.000–19.860s`，将“記住三步”识别为独立段 `29.460–30.820s`，两处后均存在生成接缝。
- P1 定向回归 193 项通过，包含重音规则正反例、Provider 边界、PCM 局部增益、焦点 part 哈希重建、Voice QA、Handler、ProductionRun、停顿接缝、语法与多音字；文档治理、能力索引、编译和 `git diff --check` 通过。
- 当前人工状态仍为 `pending`。这证明产品已能生成一个可听的重音候选，不证明“最后/三步”已自然、像真实讲话或达到用户要求；必须由用户对 Audio `9d68deed...` 听审后决定 P1 是否通过。未启动 Talking 或 Render。

### Talking 顺序与口型缺口

- 流程正确：Talking 严格在 Voice Master 和 Voice QA 后执行。真实 Run 绑定 Master `dac14f3d...` 的 `0–3980 ms`，Talking Job 使用同一 `narration_audio_id`、切片区间和授权参考窗口 `7434–11914 ms`。
- 真实 Talking `5867b5c1...` 为 4.000 秒、25 fps、100 帧，Master 期望 3.980 秒，时长差 20 ms；这些是装配和容器事实。
- 当前 `talking_qa.py` 只检查文件可播放、音视频流存在、总时长、画轨帧数、帧率和上游 Voice copy QA；没有任何音素/口型同步、唇音偏移、持续嘴部活动或画面自然度指标。
- 参考池有 face/mouth visibility、motion、subtitles 等最低事实，但不会在多个合格窗口间按持续口型稳定性做质量排序，也不能解释“模型口型差”还是“装配位移”。
- 结论：不能说 Talking 工序不对；应说“输入时序正确，口型质量验证缺失且参考/模型质量尚未被定位”。

### P2 Talking 同步实现与单模块候选

- 普通调度现在最多比较 6 个前向无占用参考窗口；配置本地 Talking runtime 时，用 MediaPipe 关键点测量脸部可见率、头部稳定性、嘴部活动和脸部尺寸，但只保存为诊断，普通产品使用最早未占用有界窗口，不按当前 v2 分数排序，也不新增硬门。
- Talking QA 增加本地运动包络观察：嘴部关键点运动与音频包络做 ±400ms 相关，保存最佳偏移、相关性、参考质量和限制。它不是 SyncNet，也不判断音素级口型，只帮助区分参考质量、模型表现和装配偏移。
- 上一版参考 `7.434–11.914s` 含多次转头与视线移动；本地候选扫描中 `5.000–8.000s` 综合分 `0.7872`，高于旧窗口附近候选。把同一 4 秒开场换到 `5.000–9.500s` 后生成 `963ef90a...`，技术 QA 通过，嘴部运动/音频包络峰值偏移约 `40ms`、相关性约 `0.196`。
- 用已批准 Master `31.050–33.090s`（“先写完整结论和边界”）和 `5.000–8.000s` 参考生成短候选 `3be87b3f...`，2.040 秒、51 帧、技术时长差 0，QA verified；运动包络峰值偏移约 `-80ms`、相关性约 `0.301`。相关性仍不高，不能自动判口型通过。
- 首次 4 秒生成 Job `add27adf...` 使用 4.0 秒参考，低于 3980ms 旁白所需的 4480ms LatentSync 帧对齐窗口，按真实约束失败并保留；随后才使用 4.5 秒参考。
- P2 Talking/调度/ProductionRun/Handler/参考池联合回归 196 项通过；覆盖参考质量选择、无可用窗口回退、motion probe 缓存/边界、Talking Job、QA 和连续窗口调度。
- 用户确认第三条 `candidate-stable-short-5-8s.mp4` / Asset `3be87b3f-eab8-48ea-82f9-992afb5722f2` 可用。Human Review `user-talking-approval-2026-11-07-p2-stable-short-v1` 已写入该确切资产；结论不外推到其他参考窗口或其他 Talking 输出。P2 单模块通过，未启动整片 Render。
- 用户指出 P2 仍有优化空间后，对参考源做 500ms 步长、3 秒窗口的细扫：现有综合分仍以 `5.000–8.000s=0.7872` 最高；`6.500–9.500s` 的头部稳定 `0.8097`、脸型稳定 `0.7492` 更高，但当前公式的嘴部活动权重使其综合分仅 `0.7742`。现有评分并非唯一合理排序。
- 固定已批准 2.040 秒旁白、参考 `5.000–8.000s`，通过现有 Content OS LatentSync 适配器做有限参数实验：`seed=0 / 20 steps` 相关性 `0.293`；默认 `seed=1247 / 20 steps` 基线 `0.301`；`seed=1247 / 28 steps` 为 `0.329`；`36 steps` 降至 `0.280`；`28 steps / guidance=1.7` 为 `0.294`。全部运动峰值仍约 `-80ms`。
- 因此“更多推理步数”不是单调改进，只有 `28 steps` 值得进入视觉 A/B；未改变产品 runtime 默认，也未把这些直接适配器实验冒充产品 Job 资产。实验文件与基线/28-step 左右并排慢放在 P2 evidence 目录。
- 进一步把脸型朝向稳定性纳入参考质量 v2 后，`6.500–9.500s` 得到 `0.7241`，用默认 20 steps 生成 Asset `47fc4760...`；技术 QA verified、时长差 0、51/51 帧，local level correlation `0.4604`、motion correlation `0.3611`，均高于已通过基线 `3be87b3f...` 的 `0.2619 / 0.3012`。
- 用户直接比较后判定 `47fc4760...` 的口型同步比 `3be87b3f...` 更差。因此相关性/技术 QA 不能替代人审，参考质量 v2 自动排序已撤回，只保留为诊断；`3be87b3f...` 仍是唯一通过基线。
- LatentSync 时长基础已确认：历史本机 `4460ms` 单次旁白通过、`5120ms` 技术 QA 通过但用户观察到口型质量下降；当前机器 6 GiB 低于官方 `6.5GB`。`4460ms` 只作为当前 W52 精确 provider/model/runtime/machine 的保守调度上限，来源已写入 manifest 和 machine setting，换机或换 runtime 必须重新校准，不能跨机器继承。
- `max_talking_duration_ms` 现在由 LatentSync Provider 高级设置 schema 提供整数默认值 `4460`，普通 UI 可查看和保存本机覆盖。运行时标签可能与静态 benchmark 不同，因此 schema 允许同 provider/model 的唯一模板投影到当前 runtime；当前运行配置可解析时自动选择该 runtime/machine scope，无法解析时仍显示默认并提示用户填写本机标识。未保存覆盖时产品调度会使用默认 `4460ms` 兜底，并把来源记录为 `default:max_talking_duration_ms`。
- W52 当前 runtime 已通过普通 Talking Worker Job `6a130a5a...` 完成一次有界校准：使用已批准 Master 的完整 ASR 语句组 `19.000–23.170s`，输入 4170ms，低于 4460ms 上限；输出 Asset `8da57bd1...` 为 4200ms、105/105 帧，技术 QA verified，时长差 30ms。参考 `5.000–9.500s` 共 4500ms，覆盖 4480ms 模型窗口。由于没有恰好 4460ms 的连续完整语句组，不能为凑时长切词；当前只证明 W52 可技术完成 4170ms，不证明视觉口型或 4460ms 质量。
- 用户观察的最后两个字实际是“来拆”，不是“来猜”。用已持久化词级时间对齐该输出：`来 3330–3570ms`、`拆 3570–4170ms`；音频峰值约 3600ms，嘴部开口峰值约 3800ms，末字内部存在约 200ms 滞后。嘴在 4160ms 已恢复到闭合基线，最后 30ms 无可测张嘴，因此这不是“说话结束后再次张嘴”，而是末字内 viseme 起落与音高/能量不同步。整段 correlation `0.2737`、lag `280ms` 只能提示问题，不能精细到末字。
- 历史 `terminal_face_closeout` 能力并非没有沉淀：Provider 合约、600ms lookahead、Job 字段、参数来源、测试和 D6g 原始结果/视频/末帧都存在。但此前缺少独立不可变人审记录，D6g 只绑定旧 `local-compatibility / asus-rtx3060-laptop-6gb`；当前 W52 配置为 false，最近候选也未启用，因此不能把历史能力视为当前机器已验证。收口解决语音结束后的嘴巴延时，不会自动修复末字内部 viseme 时序。
- 普通 Talking QA 已接入通用词级局部观察：从 `voice_word_timing` 中按输出切片选择最后 3 个完整词，计算各词音频 RMS 峰值与 MediaPipe 嘴部开口峰值，并记录差值。该能力不依赖具体文案，不增加硬门，只补足整段 correlation 对短末字不敏感的问题。
- W52 已完成匹配参考的收口 A/B：同一派生 4170ms 音频、同一 `5.000–10.500s` 参考。无收口 Asset `673e2593...` 的“来/拆”局部峰值为 `-200/+200ms`；收口 Asset `f5d08673...` 为 `0/0ms`。两者 overall correlation 分别 `0.2837/0.2832`，说明整段相关系数几乎无法区分，而词级指标准确显示末字变化。两种候选在 4170ms 后均无测到张嘴。当前收益仍需人审确认，因此默认关闭，不自动提升。
- 用户对匹配 A/B 的人审结论：收口候选提前回头更符合尾句收束，整体更好；但末字“拆”的嘴形仍不像。收口后“拆”的嘴宽峰值从 3880ms 提前到 3680ms，接近音频能量主体，但仍不能证明 /chai/ 的 viseme 形状正确；皱眉/表情可能保留自参考人物，不能靠单字修补解决。候选保留为“相对更优但未通过”，不硬修、不设默认。
- 同输入、同 reference 的 750ms lookahead 候选技术 QA verified；“拆”的嘴宽峰值从 600ms 的 3680ms 再提前到 3640ms，但 3800ms 的二次开口只略降、宽度维持仍存在。该收益不足，不能视为修复。结合 256 处理分辨率、25 fps 采样和参考表情残留，继续递增 lookahead 或改 steps/seed 很可能只是单样本调参，停止继续硬修。
- 当前 Talking 使用的是 12 秒静音裁片，但 W43 crop proof 保留了原始 Asset `e665fa62...`；原片有 48kHz 双声道 AAC 和转写。参考内 O 型峰值约在 8709–8742ms，small/medium 两套词级时间都落在原句“反而对中华文明有的新认知”的首字“反”，不是目标“拆”。当前窗口把该源动作映射到输出 3709–3742ms，正好重叠目标“拆”的 3570–4170ms。
- 语音学上“拆”为 `chai`，韵母 `ai` 应有较宽非圆撮开口并向 `i` 收拢。候选在 3600ms 同时出现最大开口和相对窄嘴宽，是 O 型；结合源词对应，可把它解释为源“反”动作残留，而不是目标读音形状。该问题在 600ms 和 750ms 收口候选均存在，不是断句或收口时序。
- 同 Voice、同 600ms 收口测试 reference `4.500–10.000s`：目标末词映射区间的源嘴部开口上限从 `0.1706` 降至 `0.0954`，但生成的“拆”嘴部峰值从相对音频 `0ms` 退化到 `+480ms`。整段 level correlation 升到 `0.4025` 仍不代表更好。该候选拒绝，产品默认不变；参考选择必须同时检查源 viseme 冲突和生成局部时序。
- 产品取舍已明确：当前整片可作为“可用基线”继续创作和发布评估，局部嘴形/皱眉保留为已知限制；但该结论不能写成 Talking 技术通过。当前 LatentSync 的 lookahead、steps、seed 和单字特例调优停止，成片可用性与核心能力满分分开记录。
- `f22e0bac...` 的 4 个 B-roll 全部是 `local-typography` Remotion 图解，不是 AI 图片/视频；外部 stock 为 0，AI 生成 B-roll 为 0，渲染 provider 成本为 0。项目真实调用只有 K3 规划、 本地 OmniVoice、Whisper 和 LatentSync，没有图像生成调用。AI 参与的是图解文字/版式规划，不是媒体像素生成。
- 当前全局预算为 400 次/449 CNY，账目 381 次、剩 19 次；已知金额 `50.2332 CNY`，另有 54 条未知费用记录，实际总额未知。默认视觉链路继续优先本地授权资产和有语义理由的图解，外部 stock 服务真实画面需求，AI 图片/视频只能是有明确收益和预算余量的高成本补位。

### IP、内容与赛道理解缺口

- `IPProfile` 已保存 positioning、domains、content pillars、platform categories、audience、topics、knowledge、opinions、vocabulary、style、boundaries、avoided expressions、common hooks、公开账号链接和证据引用。
- `creator_planning_brief` 现已投影 positioning、domains、content pillars、topics、audience、knowledge、opinions、vocabulary、hooks、style 和 boundaries；按当前主题做有界相关性筛选、字段截断和联系方式脱敏，不发送身份、同意材料或任意 metadata。
- 用户确认旧 Profile 的克隆形象是毕英杰，本机另有 TIM 长视频。已依据毕英杰的希腊/古典文本素材建立“西方历史、古典文明与思想研究”Profile，依据 TIM 的视频技术长片建立“数码科技、视频技术与影像”Profile；33 个产品测试项目不再作为画像依据。
- 因此旧的 `f22e0bac...` 只能证明普通 UI 技术链，不应作为毕英杰的内容/赛道样片；其“知识短视频结论先行/镜头拆分”主题属于产品测试，与毕英杰真实方向不一致。
- 规划 prompt 和 ScenePlan schema 现已要求每场 `creator_fit`：匹配等级、内容支柱、主题适配理由、受众价值、采用的知识/观点、事实缺口和偏离风险。引用必须逐字来自本次 Brief；无法对应时移除并显式降级，不阻塞出片。
- 普通 IP 表单已补齐 positioning、content pillars、topics、knowledge、opinions、vocabulary、hooks、boundaries 等字段，并修复保存时 topics 未展示导致被清空的问题；每场 ScenePlan 在普通 UI 显示 Creator Fit。
- 合成完整 IP 的双主题测试已验证“同一 IP 稳定身份 + 不同主题选择不同证据”；真实 Profile 已由源素材建立。结构化 Profile 下每场 ScenePlan 必须有内容支柱或知识/观点依据，无依据的赛道不能保存成正常计划。
- 真实双主题规划已验证：毕英杰项目 `828efdd8...` 生成 4 场、约 42.5 秒，全部 `creator_fit=matched`，内容落在古典文本、文明比较、遗址讲解和语言学习；TIM 项目 `2eb0b8aa...` 生成 6 场、约 33 秒，全部 matched，内容落在视频画质、编码和平台压缩。两次都使用各自源素材和 Profile，没有使用产品测试主题。
- 这两条真实 plan 仍是内容候选，不是事实审核通过：毕英杰稿的《伊利亚特》开篇词细节、TIM 稿的平台压缩概括都需人审或来源核验；赛道匹配已通过，事实准确性不能写成已验证。
- 旧提议从测试项目提炼支柱，已标记为 superseded，不再使用。公开账号链接可以保存并关联只读历史；只有实际导入的历史记录进入规划，链接本身不等于已分析。

### 发布、反馈与学习缺口

- 已有 `HistoricalContent`、`PublicationRecord`、`ContentFeedback`、`ContentOpportunity` 和有限 planning preference；但没有自动发布，Runtime 明确为 `publishing=not_developed`。
- 真实库当前 historical content、publication records、content feedback、content opportunities 均为 0；planning preferences 为 1。
- `next-suggestions` 只读取最新一条反馈并生成文字建议；普通规划只消费匹配到的 Opportunity 和已采纳 Preference，不读取历史作品表现或发布指标。
- 目前缺少发布包装（标题、描述、封面/首帧、平台账号）、发布任务/回执/失败证据，以及指标观察窗口到下一轮规划的有界输入。
- 结论：有数据结构，没有可运行、可验证的运营学习闭环。

### 素材治理缺口

- 已有能力：SHA-256 精确去重、Clip/ASR/抽帧、区间用途 QA、跨作品占用、crop/Talking 派生链、有意复用例外、负面反馈约束、外部 Pexels/Commons/网页统一入库。
- 真实库有 64 个 Asset（7 个 user、46 个 ai_video、11 个 stock）、253 个 Clip；80/253 个 Clip 有视觉描述，137/253 有转写分段；相同 SHA-256 重复组为 0。
- 全部 64 个 Asset 的 `metadata.ip_profile_id` 和 `metadata.project_id` 均为空。当前选材逻辑把 `None` 视为所有项目可用，因此本人/历史素材没有默认 IP 隔离。
- 只检测完全相同字节哈希，不能识别转码、缩放、裁切或二次上传形成的近重复；没有素材停用/归档/删除和引用检查的完整生命周期。
- embedding 未配置，当前召回基本依赖词法；CLIP 视觉描述覆盖率只有约 31.6%。
- 结论：入库和用途追溯基础较强，但不满足“IP 资产库”和发布运营所需的完整复用/冲突治理。

### 能力边界结论

- “场景规划、Voice、Talking”是执行核心，不是完整产品全部能力。端到端运营至少还需 Creator/IP 内容策略、素材治理、发布包装与发布回执、指标与反馈学习。
- 当前最新失败不代表普通 UI 不能跑完整链；它说明产品把“工序完成”和“能力质量完成”混在了一起。后续仍应按单模块人审 → 单模块修正 → 普通 UI 全片 → 第二主题验证推进。

## 2026-11-07 自主成片归因与 Voice 单模块复验

- 普通 UI 最终验证项目 `cf7a9b01...` / Run `a301d7d4...` 为 `automatic=1`；Planning、Voice、Voice QA、Talking、Talking QA、Render 六个 Job 都是 `attempt=1` 完成，`waiting_stop_reasons=[]`。启动后没有改稿、选材、恢复、重试或人工生成媒体。
- Voice `dac14f3d...` 35.980 秒、QA 覆盖 1.0、缺词/重复 0，实际命中两处 20 ms ASR 分段重叠并按新规则归一到非重叠时轴。Talking `5867b5c1...` 4.000 秒、1080×1440、25 fps、100 帧，QA 覆盖 1.0、时长差 20 ms。
- 最终 Render `f22e0bac...` 36.000 秒、1080×1920、30 fps、1080 帧、H.264 + 24 kHz 单声道 AAC，SHA-256 `d6ef75ca...`。成片调度为 1 段本人 A-roll（主旁白 0–3980 ms）和 4 段本地图解；没有外部实拍或 AI 新画面。
- 这说明 Content OS 在 Run 内已经能自主串起三核心能力及资源调度；仍不能替代用户对 Voice 自然度、Talking likeness/口型、图解语义、字幕和整片节奏的发布判断。
- 最终联合回归 696 项通过，覆盖 Voice v6、ASR 时间轴归一、素材补缺、实测 speech-part 调度、Talking、Render、生产 Run/Worker 与能力文档。
- Voice recipe v6 将对照修正和结果补语从 Profile 整句数据提升为 `voice_syntax.py` 规则集 `zh-clause-binding-v1`。Provider 只接受句末标点与独立生成段变化，可见原文、字幕和 QA 目标不变。
- 两条规则的通用覆盖是有限的、可审计的：对照结构必须出现“不是 A，是/而是 B”；结果补语当前覆盖常见“动词 + 开/完/成/好/到/掉/清楚/明白/干净”，后接“再/又/才/然后/先”等继续关系时不触发。未知结构仍保留原标点，不静默猜测。
- 同一整片稿在 v6 下 Provider 文本 SHA-256 仍为 `c89ddb6b0f2d6792d5df6ba4e46dd9bc8710ebec6d2cadc880b13f6acf4d2c78`。真实 Voice Job `148ab359-8ec6-422c-9f5c-7055abd73112` 输出 Audio `16ac63f2-6147-4924-8d51-8c2feb3182b5`，SHA-256 `f5312d...`，与用户已批 v4/v5 字节完全相同。
- 因此 v6 复用原独立 QA 与六项人审不是基于历史配方，而是基于同一 runtime 下真实重执行产生相同音频字节；新 Job、Provider call、配方和重执行历史均已保存。
- Voice 语法/生成/停顿/Profile API 定向 78 项和普通生产联合 627 项通过，文档治理及能力索引通过。修改前 DB 备份为 `.tmp/p6-production/before-voice-syntax-v6-20261107.sqlite3`。
- 普通 UI 项目 `fd9cc5af...` 的首次 v6 自动成片在 Voice QA 后停止。Audio `c3eb049b...` 的 QA 文案覆盖完整，但第 5/6 个 ASR 分段为 `15510–18770 ms`，上一段止于 `15600 ms`，产生 90 ms 重叠；`scene_narration_segment_spans` 因此返回空，主旁白时间轴失败并被泛化为 `automatic_production_stopped`。
- 修复规则只在 QA 持久化时间轴时处理小型分段重叠：≤250 ms 时把后段起点归一到前段终点，token/文本完全保留，超过上限明确失败。Scene tokens 与该音频的 QA admitted tokens 均为 164，场景边界 `16/45/81/110/140/164` 都落在 ASR 分段边界上。
- 不同主题项目 `4d016f49...` 已越过时间轴缺陷并完成新 Voice/QA。实测旁白把两个本人段拆成四个 speech parts；调度只分配 3130 ms 人物窗口，把两段投影为说明画面，但投影后的来源只有 `user_asset/historical_asset`，现有历史素材没有满足用途的片段，最终为 `material_scene_unresolved`。
- 这不是“素材不存在”的最终事实，而是投影丢失低成本外部候选：supporting B-roll 只继承 fallback 交集。调度现补 `stock` 候选，本地/历史仍先比较；stock 只是新候选来源，不跳过区间用途核验。
- 后续运行确认外部体系本身确实在工作：`d531f1db...` 执行 2 次 Pexels 搜索、3 次候选导入/分析，以及 Kimi 网页搜索和公开页 Fetch；最终 stock 片段未通过用途核验，因此按真实素材缺口停止。
- `6b72f270...` 的规划遵守“只开头一段本人出镜 + 其余本地图解”，但 Voice 实测把开头问句拆为 4.02 秒和 1.68 秒两个 speech part；两段都被当成必需 A-roll，超过 4.566 秒人物窗口。修复保留第一段 A-roll，后续实测 `--speech-` 子段转本地 `key_point` 字卡，原旁白不删。
- `da2edfad... / d959006e...` 与 `b9b52e65... / d16fe150...` 两条已完成 Render 的 Run 都是 `automatic=1`。对应 Planning、Voice、Voice QA、Talking（有依赖时）和 Render Job 均为 Content OS Job/Worker 的 `attempt=1`，没有 candidate/resume 或手工生成媒体。
- 整体开发并非全程零介入：Run 前有代码修复、运行配置、服务重启和普通 UI 启动；多次失败尝试不能算自主成片成功。严格结论是 Run 内由产品自主执行，不是从开发到成片零介入。
- Voice 单模块使用新项目 `30a15f11-9502-4a8c-91de-e89bb02f2c89`，同稿、同 Profile、同 `28.24–34.24s` 参考窗口。首次 Job `7c635ee9...` 被 320 次调用上限在推理前拦截；仅将调用上限提高到 322，金额和未知费用策略未变。
- 新 Voice Job `596bb216...` 完成，Audio `651458ec-a6a8-4c30-bad5-e5a437426c0a` 为 38.690 秒、SHA-256 `0c951b2434f95167e2176606114daae7b1da68d0396265a9a0bc42212dfc6a23`。V60 v2 执行 4 个 terminal 段间接缝的实测静音删除，39.280→38.690 秒。
- Voice QA Job `70260b00...` 完成且 `qa_state=verified`：copy coverage 1.0、missing 0、duplicate 0、2 个已容差替换；最长静音 100 ms。性能观察因文案到持久 ASR 边界无法逐句对齐而 withheld，不能声称句末/逗号层级的时序已通过。
- 用户听审该音频后指出：“这不是跟风”被连成“这不是跟风是”；“分开”与前面短语不连贯；“照明差”误读一声。技术 QA 不能发现这些问题，单模块判为未通过。
- recipe v3 新增 Provider 私有交付调整：指定标点覆盖、短语独立生成边界和 OmniVoice 拼音声调控制。将“这不是跟风，”与“步行道分开，”各拆成独立生成段；“照明差”发送为 `照明CHA4`。可见原文和 QA 目标不变。
- 最终 Voice Job `c0bd95a8...` 生成 7 段 Audio `baf2aa00-42f4-46cc-85c1-85d090b67cb2`，38.950 秒，SHA-256 `76f2395f04167b1e229673f7d3ed3d5271d9c3ca3b809ee8c51dcebbd3d350f4`；Voice QA Job `be398c71...` verified，coverage 1.0、missing/duplicate 0、1 个容差替换。
- 新音频中“这不是跟风”和“是城市”成为独立 ASR 段，词间 80 ms、PCM 实测静音 120 ms；“骑行道和步行道分开”成为独立段，段后 PCM 静音 120 ms。CHA4 已进入 Provider 文本，但四声与最终听感仍待用户确认。
- 用户确认上述三处通过，但又发现“最主要的原因，是照明”被拆成“最主要的原因是”和“照明现在……”。原测试缺口是只验证了通用标点分类和具体短语调整，没有验证“系词+短名词补语”的句法绑定。
- recipe v4 新增通用 `copula_complement_break`：匹配“主语，是短名词补语。”，Provider 文本去掉主语后的逗号，绑定“是+补语”，并在补语后的真实句末生成独立段；测试覆盖“原因/照明”“问题/成本”以及显式短语调整重叠优先级。
- 新 Voice Job `5a6ee04a...` 与 Voice QA Job `1b109112...` 完成，Audio `16ac63f2-6147-4924-8d51-8c2feb3182b5` 为 39.260 秒、8 个生成段，SHA-256 `f5312d9f0a742af038da4ae7c5e222629e0a23c21ef4a83e8a04ac004f63d41a`；QA verified、coverage 1.0、missing/duplicate 0。
- 新音频把“最主要的原因是照明”识别为一个完整 ASR 段，下一句约 40 ms 后开始，PCM 实测句间静音 120 ms。
- 用户确认该确切 v4 音频通过，Human Review `user-voice-approval-2026-11-07-v4-copula` 六项均 pass。没有启动 Talking、Render 或完整正片。14 组 338 项回归、文档治理和 `git diff --check` 通过。

## 2026-11-07 核心能力文档权威

- 场景规划、Voice、Talking、生产资源调度分别由 `SCENE_PLANNING.md`、`VOICE.md`、`TALKING.md`、`PRODUCTION_SCHEDULING.md` 唯一承载行为规则；`VOICE_TALKING.md` 仅保留兼容跳转。
- `CAPABILITIES.json` 登记 8 个模块的权威 marker、职责边界、代码/测试入口和关联关系；`sync_capability_docs.py` 自动生成产品模块页与核心能力索引。
- `check_docs.py` 已接入注册表校验与生成文件检查，并验证权威唯一性、marker、相对链接、代码/测试路径和索引新鲜度。
- 当前工作区规划配置为 `kimi-k3`、`low`、32768 输出上限、600 秒超时；Talking 为 LatentSync 1.5 双 runtime，参数由版本化 runtime 配置和执行证据保存。模块文档只定义规则来源，不把易变快照复制成第二份 STATUS。

## 2026-11-07 Voice 停顿层级

- 当前 V60 Profile `29294db2-d3c6-45c2-b467-6a02e853c826` 已升级 recipe v2；保留质量锚 `c1346d02-d967-4475-b5f4-4bdf7933b2d3`、SHA-256 `756492b4d61555654884276cde33f27f1cb57f1517181d712eb5b1977f21c480`、W13 runtime、`28.24–34.24s` 参考窗口与六项人审 pass。
- 配方明文包含词组内连续、逗号短边界、句末高于逗号、段落一拍、关键观点思考一拍、片尾自然落下；全局 speed 明确不是停顿控制器。V60 仅对已知段落接缝的实测静音做有界删除，不插入静音、不改语音帧。
- 普通 Voice Job 在导入前执行 seam policy；Voice QA 用原始分段和 cuts 重建最终 Master，保存 raw/final 哈希、边界类型、裁剪样本、跳过原因和最终时轴。无静音或超限时原样保留并记录，不形成新的硬阻断。
- 既有真实 Voice `a4481d21-9c77-44d5-84aa-17d1cac50168` 零付费回放命中 4 个 terminal 段间接缝，删除 80/150/160/110 ms 静音，39,280→38,690 ms；预览未登记、未人工通过。
- 13 组普通链路回归共 333 项通过。`test_maintained_voice_prepared.py` 另有 3 项旧评估测试失败，原因是仍调用已移除的评估/admission 路由或启动旧系统探测；不属于普通产品链。

## 2026-11-05 普通 UI 自主成片现场

- 项目 `f4ab1d52...` / Run `a7cdf9e0...` 的数据库一致快照保存在 `.tmp/p6-production/visual-route-a7cdf9e0-20261105.sqlite3`。Draft 仍是 Voice 前的估算版，Run 停在 Voice QA 后。
- 实测 Master 为 45.52 秒，四个场景实际需要 12.74 / 1.88 / 14.30 / 16.60 秒；Draft 绑定的候选 Clip 约 11.6 秒，因此预检把三个变长场景降级为 capture。短的第二场正常通过。
- 对实测场景按既有标点拆分后，普通采访片段重新满足时长；唯一剩余缺口是已有 `diagram_flow` 的场景，其旧 stock 候选只有 11.6 秒，小于实测 14.3 秒。
- 当前规划草稿已明确该场景有可执行 `diagram_flow`，但丢了 `typography` 备选。正确路径是先在片段失败后采用已经声明的图解，而不是外取无关素材或绕过 QA。
- 已修复共享素材解析：实测分句后若显式 `diagram/text_card` 的原 stock 候选不再满足时长或用途，则保留原声明并自动转为本地 `typography`，同时清除不可执行的补取请求。定向回归和同快照完整回放均通过。
- 普通 UI 新建干净项目 `6f39aaeb-49fa-4d46-befd-256a15eb6ba3`，启动前 `/auto-produce` 为 `null`。只点击一次“开始制作短视频”后得到 Start Job `7c27821c-500b-4ef9-89b5-d436b8dee192`；当前 running，responsible stage 为 planning。
- `6f39aaeb` 的规划真实完成：`kimi-k3` 返回 4 场、约 31.5 秒方案并已记账；随后源视频用途核验连接 `api.moonshot.cn` 失败，Job 在 43.4 秒以 `material_video_analysis_failed` 停止。未生成 Run/Voice/Talking/Render，不恢复该 Job。
- 失败后只读检查 DNS 与 TCP 443，`api.moonshot.cn` 可通过代理地址解析且连接成功；该次属于短暂连接故障，不是配置或许可门。预算仍为 449 CNY / 320 calls，当前账目 279 calls、20.2620 CNY、38 次未知费用，未超限。
- 通过普通 UI 另建干净项目 `da2edfad-f926-494c-b7c2-65abc40b2af6`，启动前自动制作记录仍为空；单次启动 Start Job `72c16fa7-8c88-4853-ab00-9ccd4ce47967`。截至 41.9 秒仍在 planning，无停止原因。
- `da2edfad` 的规划在 178.9 秒完成并创建 Run `d959006e-b91a-4ab6-9bbc-22668cb8e9fb`；自动进入 Voice Job `1bdd8c2f-9939-4977-8b0d-d75655fc0111`。当前 `voice_running`，无停止原因。
- `da2edfad` 的 Voice 在 44.97 秒完成 Audio `3d7da8d3-1328-4105-aa65-8d0e70eaf034`，Voice QA Job `641ada14-f489-4c9c-b73b-73e2d21754d9` 在 43.88 秒完成且无停止原因。
- Voice QA 后自动执行了一次新的源区间视觉核验，并成功穿过此前失败的实测时长/画面路由段；没有出现 `visual_route_unresolved` 或 `capture_required`。项目方案无需 Talking 依赖，Run 直接进入 Render Job `5a53f20f-ae06-4841-b56a-38304461d067`，当前 `render_running`。
- Render 69.1 秒真实完成，输出 `content-os-data/renders/da2edfad-f926-494c-b7c2-65abc40b2af6/a971ec16-6ffc-4b7f-a95f-679c25314853.mp4`。ffprobe 确认 37.433 秒、1080×1920、30 fps、1123 视频帧、AAC 24 kHz 单声道；8 帧联系表可见两段采访画面和三段本地图解，无空白或黑帧。
- 本片没有 creator-speaking 场景，`talking_dependencies=[]`，因此没有生成 Talking 或对口型结果；不能据此声称 Talking 质量通过。普通 UI 已显示确切 `render:a971ec16-...` 和“成片已输出，待完整质量审核”。
- 最终文件 SHA-256 为 `FED0F340E12D83A65AC6DB44890F58C614327B37F93D7805D4351E493344FBFC`；FFmpeg 解码未检出 ≥0.2 秒黑帧或 ≥1 秒、低于 -50 dB 的断音。来源署名文件列出 Pexels `7644024 — MART PRODUCTION`。
- 该产品的 6 次 Provider 调用全部 completed、无 failed/running；工作区账目 285/320 calls、已知 CNY 22.7555、未知费用 39 次，未超 449 CNY。

## 三大核心能力与调度核查

- 产品规格明确把三大核心能力定义为：场景规划、语音克隆、对口型；生产资源调度是贯穿三者的执行能力，不是第四种内容能力。
- 本次 `da2edfad` 验证了场景规划、Voice 和 Voice QA；最终 `talking_dependencies=[]`，没有派发 Talking，因此没有验证“新内容本人 Talking/对口型”。按 R1 的“至少一段说新内容的本人 Talking”条件，这次仍不是三能力全部验收。
- 历史产品对象与执行能力仍在当前库：1 个已授权默认 Voice Profile、2 个已授权 Talking Profile；87 个带 Voice Generation 元数据的音频中 79 个 QA verified，1 个历史音频为人工 approved；4 个 TalkingRun 都记录为 admitted + automated_qa verified，其 4 个 assembled asset 均存在且 SHA-256 匹配；另有 35 个 completed `generate_talking` Job 和 5 个 completed `verify_talking` Job。当前项目 `/talking-pool` 返回 7 个合格条目、12 秒容量。
- 常规自动路径保留这些产品对象：`VideoProductionService` 统一推进 Planning→Voice→Voice QA→实测画面配时→按需 Talking→Render；普通 Worker 注册 Voice/Talking/QA/Render handler。自动 UI 隐藏子片 U-Voice/U-Talking、预览和连续性准入，但保留其手动/历史入口及底层产物，不把旧式中间审批重新变成常规门禁。
- 资源调度的设计是有界分层约束优化：先做画面可行性，再用完整文案生成唯一 Master；以真实时长重排 A/B-roll；人物窗口按整批回溯分配，保留连续区间和长窗口；普通素材按表达匹配、时长、字幕、来源占用、重复、横屏过度使用和创作者素材价值联合比较，最多保留 64 个组合；Talking 整批分配最多 50,000 状态，失败不产生部分结果；预算、幂等、租约和已完成证据复用贯穿工序。
- 这不是穷举求数学全局最优。媒体选择是有界束搜索，Talking 是有界深度优先回溯，都是产品可接受的确定性近似最优；仍可能出现高图解占比或同源重复，需要整片人审和后续反馈约束。
- 本次最终 5 场为 2 场 stock + 3 场本地图解，图示时长占成片约 66%；两场 stock 均来自 Pexels 同一源资产并用 `repeatable=true` 记录。当前调度没有把它判为合格质量结论，也没有用硬门禁阻止，说明重复最小化目前是软排序而非硬保证。

## 2026-11-05 Voice、外部素材与 IP 核查

- 当前自动 Voice 确实复用了历史 W51 运行参数：`voice_execution_receipts` 中当前 25 个参数、runtime `omnivoice-existing-maintained-personal-w51` 和 machine ID 与历史人审执行规格完全一致；历史 QA/人审/精确音频及证据文件也仍存在。
- 但历史人审结论只对确切旧音频有效。当前新音频 `3d7da8d3...` 的 `human_review_state=pending`；历史 W51 音频使用 `28.24–34.24s` 参考，当前自动生产改用了 `0.24–7.24s` 参考窗口，且自动细分生成 5 段。产品和历史并非同一次声音输入。
- 历史审批本身还明确保留“W51 输出比此前最终版差”的负面比较，因此默认产品继续使用 W51 参数不能证明已达到历史最佳音质。`VoiceProfile` 只保存身份/provider/语言/参考 Clip，没有版本化的最佳参考窗口、质量偏好或人审后参数配方；普通自动派发遇到 `narration_performance_plan` 还会以 `voice_performance_plan_dispatch_not_implemented` 停止。
- 外部素材完整体系代码包含 Pexels、Pixabay、Coverr、Wikimedia Commons、Kimi/DeepSeek 网页搜索与 Fetch、公开页 JSON-LD VideoObject 解析、统一下载/入库/切镜/用途核验/复用；AI 生成画面是独立高成本通道，不是默认外取来源。
- 实际证据仍不完整：数据库只有 Pexels 6 次搜索和 6 次导入、Wikimedia 1 次搜索、Kimi 1 次网页搜索成功；Pixabay、Coverr、DeepSeek 无真实调用，public-page 解析有 2 次失败、无成功导入记录。本次 `da2edfad` 没有 stock/web 搜索调用，只复用了已入库 Pexels 素材和本地图解。
- API 的素材连接状态与 Worker 运行设置存在分裂：普通 API `/external-media/providers` 当前把 Pexels 报为 `missing_credentials`，但 Worker 使用数据库运行设置已经完成过 Pexels 真实搜索与导入。原因是 API/手工外取读取进程 `os.environ`，而 Worker 启动时另行应用了保存的 `runtime_environment`。外部体系已接通主调度，但状态观测与手工外取尚不一致。
- 当前项目已绑定 IP `65ed92be...`，默认 Voice/Talking Profile 也都绑定该 IP，人物池可用 7 条/12 秒；但最终 5 场 `creator_speaking` 为 0，`talking_dependencies=[]`，A-roll 时长为 0。画面是外部 stock 人物采访 + 本地图解。
- 自动模式的 planner prompt 明确允许“creator speech is not mandatory”；只有项目 `presentation_style=creator_led` 才强制至少一场本人讲话。素材联合排序对 creator-provided 素材只有软偏好，不能弥补规划阶段没有 A-roll 场景。因此这次虽满足 IP 数据绑定，却没有实现观众可见的 IP 辨识度。
- 重启后的 `/runtime/readiness` 返回 `runtime_ready=true`；规划 `kimi-k3`、视觉 `kimi-k2.6`、本地 Voice、Voice QA、LatentSync Talking、Remotion Render 均 ready。仅 embedding 为 `provider_not_configured`，本地词法 retrieval 为 ready，不阻断当前普通主链。
- 普通页面保留配置失败的旧项目 `ac155664-6126-4ad3-a0b9-6028d35b9777`；其一次启动在规划请求前即停止，继续作为失败证据。本次另建干净项目，避免把配置修复后的执行混入该失败尝试。
- 普通页面允许选择已有 IP `Local Research Creator`、填写主题和自动规划，并由“开始制作短视频”进入 ProductionRun。页面仍展示大量高级/手动区域，但本次不操作这些区域，只观察自动过程与最终整片复核。
- 第二个干净项目 `8d173d9e-5767-4ff3-ae88-bb9f0e06fc82` 的首次启动也停止于 planning，Job `0759df14-29d4-49fc-856d-2bc276ce7308` 用时 424 ms。失败并非模型、商业状态或产品准入，而是素材关键帧提取调用 `ffmpeg` 时缺少 `fps` 滤镜。
- 当前没有系统 `ffmpeg`；`resolve_local_executable` 因此回退到 `apps/renderer/node_modules/@remotion/compositor-win32-x64-msvc/ffmpeg.exe`。该文件可服务 Remotion，但滤镜集不完整，不适合作为通用媒体提取工具。工作区已有完整 `content-os-data/ffmpeg-gyan-9.0.2-r1-w45/.../ffmpeg.exe`，包含 `fps`；现已改为优先选择这组完整工具并通过标准启动注入。
- 第三个干净项目 `69baa8d8-1314-4142-8938-ef6809dfeda0` 已从普通 UI 单次启动 Job `99762330-9acf-4420-840a-e8b94f7296f6`，状态为 `plan_content` running；这次已越过此前的媒体工具启动断点，后续是否完成仍以 Run、Provider 调用和确切渲染文件为准。
- 该运行的素材目录分析真实执行了两批 `kimi-k2.6` 视频用途核验，均 completed，并识别出人物讲话、场景切换、中英文字幕和保护区；随后 `kimi-k3` 场景规划才开始。账目从 212 增至 214 次调用（另有规划 1 次 reserve），已知推算成本由 2.0358 增至 2.1692 CNY，未知费用调用 22 次，未超预算。
- `kimi-k3` 本次规划使用 `prompt` 输出模式和 8,192 输出上限；提供方返回 `finish_reason=length`，总 completion 8,195 Token，其中 reasoning 7,851 Token，JSON 正文尚未结束，因此被正确拒绝为不完整响应。规划的 5,497 输入 / 8,195 输出已记账，但该模型价格未配置，实际费用仍未知。
- 当前 `planner_thinking_type` 与 `planner_reasoning_effort` 都留空；之前把 K2.6 的 thinking 参数用于 K3 会被拒绝，因此本次不做未经实测的思考参数切换。最小修复是把输出容量提高到能容纳推理加最终 JSON，而不是绕过完整性校验。
- 普通 UI 已保存 `planner_max_output_tokens=32768`，模型、600 秒超时、思考和推理档位保持原值；标准服务重启后创建项目 `5ea78efe-ce32-405d-bdfa-a3852ba93ebe`，以 Job `e039833b-3238-4c2c-bf66-61c259d48f9a` 开始新一次真实执行。
- K3 第二次完整响应的失败来自旧契约：`creator_speaking` 已有可执行的可见讲解，模型却为可选切题 B-roll 填写新 `stock` 请求，`ScenePlan` 将两者组合直接拒绝。当前主链并不使用该请求来替代本人出镜，因此这是把可选质量增强误设成硬前置。
- 解析层现在对 `creator_speaking` 清除非必需新素材请求，保留 `supporting_visual_intent`，供后续人物容量调度按需投影；实际必须出现的外部素材场景仍照常保留请求、获取与用途核验。相关 204 项测试通过，并用失败响应离线验证为 4 场。
- 契约修复加载后，最终交付项目 `0d18b51e-577c-4525-9c43-b188a7ac13b5` 已通过普通 UI 启动 Job `7db3fc54-6548-4e99-ae6a-9414820ba6f4`；此前三次失败证据均保留，新项目没有复用失败任务。
- `0d18b51e` 的规划成功并保存了 5 场、约 40.9 秒方案，随后因 `creator_talking_reference_not_configured` 停止。原因是两份旧 Profile 有明确授权但尚未绑定当前 IP，而旧素材的 metadata 也没有 `ip_profile_id`。
- 数据库已备份后完成一次身份绑定：默认 IP 的默认声音设为现有毕英杰 Voice Profile，两个同主体 Talking Profile 也绑定该 IP。人物池选择器现接受“Profile 明确绑定当前 IP、旧素材 metadata 缺失”的组合；双方均未绑定时仍拒绝，避免跨身份猜测。
- 重启后项目人物池为 available，7 个合格候选，usable/longest 均为 12 秒。该容量足以支持至少一段短人物口播；较长人物段需要按原规划的可替代切题画面或真实旁白后再分配。
- 新项目 `3541da38-07a4-4699-ba8d-038faa316801` 已以当前修复后的代码和 12 秒人物池启动 Job `f8a111a6-19b1-4a4f-9172-49c594e2a265`，不复用前一次的失败任务。
- 该次 `kimi-k3` 调用在 600.9 秒超时，未返回响应或用量；Provider 账目将本次记为未知费用调用，预算仍为 217/240 次、2.1692 CNY，未超限。超时诊断仅含错误类型和阶段，没有响应正文。
- 普通 UI 已应用 `low` 推理强度并保留 32768 输出上限。新项目 `e63100e9-c673-47c2-9f86-caf5724def11` 在人物池 available、无历史自动制作记录的条件下单次启动 Job `defd729a-6a49-454c-a875-afee8e616bb2`。
- 该项目 `low` 模式 48 秒收到完整 3 场响应，说明速度问题解决。响应仅因 `visual_intent` 中多余的 null `framing_note` 和 Creator 段不可执行的可选切出意图被旧严格契约拒绝。
- 新兼容只忽略无值画面注解，并剥离没有 footage fallback 的可选切出；未知字段有值、必需画面、人物出镜及事实证据规则均保留。真实响应离线回放已通过为 10.2 秒三场方案。
- 工作区预算现为 CNY 449 / 320 次调用，未知成本仍允许；原始 218 次、2.1692 CNY 账目未改写。新项目 `66833a9c-1c3c-4f17-84e5-905bb65cd97f` 已在无自动制作记录、人物池 12 秒可用条件下启动 Job `244929e5-...`。
- `66833a9c` 的自动链路完成了规划、视觉修订、Pexels 检索/导入和视频区间用途核验；最终仅因一个已有流程图的场景附带可选 stock 请求被预检误判为必需品而停止。处理链路本身没有要求人工选材或修改文案。
- 生产预检现允许“可执行 diagram/text_card + typography source”独立完成场景；有值的附加素材请求仍保留为证据，但不再阻塞主画面已经可执行的方案。52 项相关回归通过。
- 新项目 `b6213a2b-f487-46c4-852c-d163e9d199f3` 已在修复后的代码下启动 Job `a0a37e2e-...`，继续验证完整生产链而非复用上一条失败任务。
- `b6213a2b` 已真实完成规划、外部素材获取和 4 批视觉核验，但在 Voice 推理前失败：Profile 引用了 71 秒授权素材，却没有 5–10 秒的可执行参考窗口。该失败不是模型或外部费用。
- 自动派发现在从 Profile 明确授权的源 Clip 和同一 ASR 时间轴选择确定性 5–10 秒窗口，最大 24 个候选，并把首个窗口的 Clip/时间/哈希写入 Job；这不扩大来源范围，并保留精确证据。
- 新项目 `8225304c-d59d-4376-8018-a85f95e6cbb5` 已以 Job `e8cf8592-...` 单次启动，验证 Voice 后的完整生产链。
- `8225304c` 的真实 Voice 已成功：本地 OmniVoice 生成 51.13 秒 WAV，耗时约 69 秒；参考窗口固定在 `6.24–14.24s`，源与生成证据完整。随后 Voice QA 未进入 ASR，原因只是完整 WAV 相对路径被重复加上 `content-os-data` 目录。
- Voice QA 路径解析已修复，新增带 `content-os-data/` 前缀的主 WAV 测试；实际分段哈希和 PCM 契约未放宽。
- Voice QA 随后真实发现局部污染：两段开头多出参考素材尾句“说几句吧”。这不是文字覆盖误报，生成的音轨确实多出内容，因此不能绕过 QA。
- 自动参考窗口现从授权源的最早可用区间排序，优先选择更稳定的开头而不是任意中段；同一真实 Profile 的首个窗口已改为 `240–7240 ms`，不再包含污染尾句。
- 新项目 `381d12f5-e697-4ca5-a9d5-443ef9f2b4ad` 已以 Job `c3e3079d-...` 单次启动，继续验证修正参考后的 Voice/Talking/Render。
- `381d12f5` 的外部获取真实执行了两个查询和三次导入；最后一次视觉证据把剪辑界面候选判为严重失焦、无法辨认操作，因此保持 `material_source_unavailable` 是正确的质量结论，而非可放行的形式门禁。
- 短版主题项目 `b6bdd8b2-2c98-460d-b5d4-5294e16a8819` 已单次启动 Job `0ab4da09-...`，用于验证更短内容在现有 12 秒人物池、图解与可用外部素材下能否独立成片。
- `b6bdd8b2` 的 K3 响应本身完整，但少了一个 `visual_intent` 对象闭合括号。解析层仅在该已知位置补一个 `}`，其它 JSON 损坏继续失败；真实响应回放通过为 4 场、31.5 秒。
- 新项目 `f4ab1d52-f17e-4149-9938-8c1d0630a5af` 已以 Job `8d313775-...` 单次启动，继续复用有效短主题验证完整生产链。

## 2026-11-06 三核心整片收口

- Voice 失败根因不是模型内容，而是 `asset_pts` 参考窗口的 Asset 哈希与选择记录 `asset_content_hash=null` 契约不一致；实际源哈希在 `source_content_hash`。修复后真实 `28.240–34.240s` 窗口抽取为 144,000 帧并完成推理。
- Talking 失败根因是受限子进程环境只用 `SystemRoot`，未给 Windows Python/Matplotlib 所需的 `WINDIR`；补齐后同一真实参考源完成 LatentSync 推理。
- 规划可选切镜可能引用已观察 stock，却未在 `fallback_sources` 声明 stock；现从已观察 clip/source-kind 证据补齐允许来源，仍由正常用途 QA 决定是否可用。
- 最终 Run `d16fe150-e5fd-4b62-81d6-6d284e118435` 有三能力真实产物：Planning、V60/W13 Voice、Talking 7.440 秒及 QA、Render 39.300 秒。最终视频场景为 1 场 Talking + 5 场已核验素材，来源署名列出 Pexels `39286036`、`34740179`、`19123755`。
- 资源调度优先复用了已入库 Pexels 素材，本轮没有新外取调用；完整外部连接仍由 API/Worker 共用保存设置。A-roll 来自绑定当前 IP 的本人 Talking Profile 和授权参考素材。
- Voice 的实际配方和推理均为 W13，但 capability 选择原先按更新时间取到 W51，导致产物 `model_usage` 指针错误。现仅按当前 provider/runtime/machine 精确匹配；本片音频与 Talking 来源标记已纠正为 W13，修正前数据库有独立备份。
- 当前停在人工整片判断。技术 QA 不能替代 Voice 自然度、口型肉眼同步、画面语义、字幕可读性与节奏判断。

## 2026-11-05 收口审查

- P6 当前仍不能称为“全链路完成”：`STATUS` 明确普通 UI 自主成片尚未通过，第二主题未复验；只能证明若干离线交接和既有真实工序。
- 普通自动入口已是主题输入→`PLAN_CONTENT`→`ProductionRun/Job`→Voice→Talking QA→Render；无能力的第三方评估路由未挂入普通 API，普通代码只借用其错误类型。
- 商业/许可状态在普通预检、Run、Worker 和 Talking 池中只用于来源或用量记录，未发现新的硬拦截。
- 仍然存在形式性残留：同一 Run 的 Talking QA 后仍写入 `talking_exact_preview_review_required` / `talking_u_review_run_not_implemented`，自动决策再过滤；这会造成 UI 和状态载荷中的无效复核提示。
- 普通 UI 默认展开自动制作；手动预检、Voice、Talking 和旧复核流程位于折叠区，但 Talking 面板仍暴露预览/连续性审核/准入按钮，容易重复制造“形式文书”阻塞。
- `TalkingGenerationJobHandler` 的错误文案仍称需要 U-Voice 人工通过，尽管同 Run 已按技术 QA 放行；当前文案与实际行为不一致。
- 已确认自动 Run 不应保留子片/整段预审核准入：自动面板只负责启动和轮询，手动 Voice/Talking/恢复面板现仅在非自动 Run 显示；最终整片审核仍保留。
- 多人 Talking 批派发原先会在同一事务内为每个场景再次绑定并重跑预检。现已复用本批次刚完成的绑定校验，仍保留 Worker 派发前的最终身份/预算/来源检查。
- 正常预检的“无图形必须有媒体”错误曾被泛化成路线未解析；已恢复具体原因，且对应正常回归通过。
- 旧 `talking/source_review.py` 及其 HTTP 测试在普通 app 已无路由或调用者；其测试还引用已删除的夹具，导致全量收集失败。已删除该旧服务与旧测试，但保留数据库表、历史评估记录和读取旧来源准入所需的数据结构。
- `Test local_generation` 仍在验证已删除的能力/许可布尔门；已改为验证配置运行时可幂等登记、无需证据或商业标记。
- 完整后端测试集规模很大且包含已从普通 app 移除的旧评估 HTTP 路由；这些旧用例不属于普通产品回归。普通自动制作、预检、Voice/Talking/Render、素材、恢复与预算相关联合集已单独通过。

- 产品真实账目项目 178/217、工作区 410/449；入口预留不等于完整制作成本。
- 曾有真实渲染，但经人工介入并被打回；尚无普通 UI 一次输入自主成片通过。
- 外部素材可自动补位；本轮尚未真实检验新分句的外部补画和用途，不能断言有或没有可用素材。
- 合格人物参考池 3.2 秒；已有语音按词级时间的只读模拟可安排人物与补画，真实新生成和质量尚未复验。
- 整片语义、声音自然度、口型同步的自动诊断与局部返工仍有缺口。
- `visible_sync` 当前责任为未知；缺少区分原人物片段和装配造成错位的音画联合诊断。技术缺陷和人工已定位问题的局部返工已有夹具测试，不能当作真实自动质量闭环。
- 已修复自动 QA 返工准备异常被吞、无限显示质检中的问题；停止原因保留在原状态契约中，Run 和 Job 变化时不覆盖新状态。此项已有夹具回归，真实运行未验证。
- 素材导入完成复制、探测和哈希；本地分析可切镜和抽帧，可选视觉索引是抽帧描述。生产用途核验依赖支持视频的远端视觉适配器，以素材哈希、区间和具体表达要求绑定证据；分句可能带来新用途和重复核验。目前尚无接入的本地视频语义模型。
- 普通上传或路径导入已自动派发本地切镜、抽帧与有界视觉索引；真实接口尚未验证。单帧观察不等于整段视频事实或具体用途通过。
- 同 Clip 和同用途的覆盖区间可以复用；已修复候选召回遗漏已核验 Clip 导致重复核验。不同子句或不同来源子区间不能无条件继承整段用途结论。
- 开源候选：Qwen3-VL-2B 用于视频理解、Qwen3-VL-Embedding-2B 用于素材召回；效果、显存和接口均待本机验证，不代表已接入。
- 工作区账目和普通 Worker 视觉索引已接通：新上传/导入最多 4 个单 Clip 抽样任务，受全局预算约束；外取素材按生产需求核验后应复用结果，不能再为统一流程追加独立扫描。真实调用待验证。
- 索引已逐 Clip 保存并保护已有完整或子区间用途证据；完成的视觉响应及可得用量已记账，普通 Worker 可在租约过期后安全复用已完成结果。
- 切镜后中断可幂等补派；租约重领不重复发送状态不明的付费请求。生产与后台共用同步 Worker，单次视觉请求不可抢占，仍可能延迟新生产。
- 预算设置保存与未发起调用任务的补派在同一事务内完成；当前进程缺视觉配置时界面提示检查配置，不能据此推断独立 Worker 的配置。
- 外部素材重复入库不再重新切镜覆盖旧证据；自动切镜在写事务内检查并发写入。外取由规划声明的 stock 需求或现有候选不足触发；声明需求不等于已经证明本地素材无法胜任。
- 真实库只读核对：409 条总调用记录，目标项目上限 183，未配置工作区预算；工作区视觉的未知费用按默认策略会被拒绝。原库复制后迁移至 61，409 条记录保留且外键检查通过。
- 标准启动缺规划配置时 Worker 退出的断链已修；普通产品运行参数此前依赖临时脚本。已有本机 LatentSync 真实任务完成记录，提供商声明的 6.5 GB 并非本机 6 GB 的拦截；新作品口型结果尚未产生。
- 2026-11-05 普通 UI 自动制作在规划失败：模型把已提供的人物动作参考填入普通画面选择，校验器误拒。共用规划层已将该参考交给动作池调度，未知引用仍拒绝；真实失败响应在副本离线重放通过，不代表成片通过。该次响应 6599/1614 Token，旧账目未写入；后续调用记账已修复。

## 2026-11-09 用户可见样片归因

- 用户评价的成片是项目 `356a2d38-b495-47b3-9062-5c224d13f8cf` / Run `413e0e7d-6cce-4926-a77b-fe9e71b74611` / Render `49561326-c355-4fe2-8fbc-15886604bfef`，不是最新一次普通输入。
- 该项目保存的主题明确写着“不要本人出镜，不使用任何外部实拍或 AI 画面，全片只用本地流程图解和文字要点”。四条 ScenePlan 的 `visual_requirement_reason` 也都记录了这一明确约束。因此 0 A-roll、0 stock、4 场流程卡是产品服从测试约束的结果；它不能作为内容 OS 自主视觉策略或 IP 理解的产品验收，也不能反证规划器不会使用 A-roll/stock。
- 最新普通输入项目 `944e0696-dbc3-4bc8-8d7a-6daf55cc1fef` 已去除这些测试限制，规划实际生成两场本人讲话、一场本人辅助切镜和一场今晚实践；说明修正后的规划方向已把 A-roll 与 IP 可见性纳入。该次在素材用途核验阶段因 `VisionRateLimitError` 停止，24.324 秒后停在 `material_video_analysis_failed`，尚未成片。
- 旧成片 Voice Audio `5bce0e4d-c818-41d2-8271-a5304641b925` 的 ASR 明确为“痛哭来自判断”；独立 QA 当时因旧长稿小比例替换容差仍标为 verified。当前代码已改为未解析替换一律失败并定位到目标字做 Provider 私有纠音，但该修复尚未在这条真实整片音频上得到新产物验证。用户所说的“走音/方言”至少这一处可由“痛苦→痛哭”直接解释，不是把完整声线克隆成方言。
- 旧成功 Run 的产品内分阶段耗时为 Planning 81.694 秒、Voice 58.604 秒、Voice QA 33.974 秒、Render 28.728 秒，合计 205.397 秒。用户感知的一小时包含开发中多次规划失败、代码修复、服务重启和新项目重试；它不是一次成功 Run 的耗时，但当前 18 秒视频仍需约三分钟，说明运行体感尚未达到“简单输入后很快完成”的目标。
- 只按主题原词召回会让已核验 stock 缺席：`斯多葛` 不会直接命中“旧书翻页”“夜间书写”“罗马遗址”的描述，规划器只能再次外取。现从 Creator Brief 的内容支柱与常讲主题推导有界视觉母题，只提示已核验 stock，不直接选材；否定描述不会形成假命中。
- 已核验连续区间仍被重复送视觉 Provider，因为同一素材换了镜头用途就缺少当前 `scene_key` 的问答结果。若规划器明确选择确切 Clip，且已核验描述与镜头主体/动作至少有两个非泛化词义相交，调度可本地记录 `local_description_matches`；未核验、冲突和弱匹配仍调用 Provider。
- 项目 `36277a3d...` 证明修正后的自然输入规划会主动安排 A-roll：5 场中 2 场本人讲述，另外 3 场分别使用已核验的古希腊/罗马遗址、旧书翻页、夜间书写 stock；不再重复视觉分析或触发 429。
- Voice 严格 QA 已真实拦截整片首轮与一次局部返工。返工成功修复“边界是→边界使”，但“斯多葛→斯多哥/斯多噶”仍 unresolved；产品没有把修复额度耗尽后的音频推进 Talking/Render。
- 无歧义同音别名“斯多阁”生成的 5.760 秒单模块音频仍被 ASR 写作“斯多噶”。这证明当前剩余问题集中在术语声学/识别边界，而不是 W51 配方；是否通过必须由用户对确切音频判断，不能仅凭 ASR 文本代判。
