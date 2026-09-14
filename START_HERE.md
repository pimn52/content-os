# Content OS — Start Here

这是项目唯一的人类/实施 Agent 入口。**不要从旧 PRD、审查报告或历史冻结文件开始。**

## 1. 阅读顺序

1. [`STATUS.md`](STATUS.md) — 现在做到哪里、真实阻塞、下一项 ready 任务。
2. [`CONTENT_OS_EXECUTION_SPEC.md`](CONTENT_OS_EXECUTION_SPEC.md) — 当前产品承诺、R1 范围、验收和执行顺序。
3. [`AGENTS.md`](AGENTS.md) — 工程约束、实施模型成本分层、文档维护规则。
4. [`DECISIONS.md`](DECISIONS.md) — 只有需要理解长期不可轻易反转的取舍时再读。
5. [`README.md`](README.md) — 面向使用者/贡献者的产品概览与启动方式。

模块开发时再读取对应代码、schema、测试；不要把整个历史文档树塞进上下文。

## 2. 当前产品主线

长期方向：

> **Know what to create → Create it as you → Learn what works**

R1 当前先攻克 `Create it as you`：

> 已授权创作者输入新主题后，系统生成可修改的新文案，合成本人的新声音，生成至少一个说出新台词的本人 Talking/口型片段，再结合真实 B-roll、排版、字幕输出 30–60 秒新视频。

旧片裁切、导入现成旁白、旧原声或通用数字人只能作为明确标注的辅助/降级路径，不能冒充上述核心验收通过。

## 3. 当前 Provider 策略

### Voice

- `VoiceProvider` 永远可替换。
- **OmniVoice** 是当前本地声音克隆技术 benchmark；代码为 Apache-2.0，但官方预训练权重当前为 CC-BY-NC，因此不能作为未来商业版默认权重。
- **Chatterbox 已经真实测试并因本人音色相似度/自然度不足被淘汰**，不再作为当前候选。
- 商业可用本地 Voice Provider 目前**未选定**；后续候选必须分别通过真实质量、代码/权重许可证和运行成本 Gate。
- Cloud/BYOK Voice Provider 保持可插拔，用于无合适本地能力或质量不足的机器。

### Talking / Lip-sync

- `TalkingHeadProvider` 永远可替换。
- **MuseTalk 1.5 已被产品质量 Gate 淘汰**，不属于当前受支持 Provider。
- **VideoReTalking** 正在隔离实测；实测通过 U-Talking 前不得进入 Core adapter。
- 若 VideoReTalking 不达标，优先继续 benchmark KeySync，再考虑 LatentSync 1.5；不要同时安装大量 Avatar 模型。
- Local-first **不等于所有模型必须本地推理**。对于重型 Talking/lip-sync，低配电脑可以优先使用用户明确授权、可计费、可替换的外部/BYOK API；本地推理仅在硬件、隐私、质量与运行成本合适时启用。

模型名称不是产品架构。Content OS 保存 Profile、参考来源、授权、质量证据、成本和 Provider metadata，允许替换底层实现。

## 4. 实施模型成本原则

保证质量前提下使用最低成本的可胜任模型：

`Luna → Terra → Sol`

- **Luna**：边界清晰的 UI、CRUD、测试、文档、简单 adapter、机械修复。
- **Terra**：媒体链路、Provider 集成、跨文件状态、Planner/Router、Job/恢复、Voice/Talking 实现。
- **Sol**：架构冲突、安全审查、关键质量 Gate，或 Terra 有具体最小复现后仍无法解决的问题。

不能因为任务重要就默认用 Sol；也不能为了便宜让 Luna 独立改变核心协议。

## 5. 每个任务结束后的固定动作

1. 跑与改动相符的测试/构建；
2. 更新 `STATUS.md` 的**当前事实 / 当前工作包 / 下一项 ready 任务**；
3. 只有产生长期取舍时才更新 `DECISIONS.md`；
4. 只有产品承诺、范围或验收发生变化时才改 `CONTENT_OS_EXECUTION_SPEC.md`；
5. 不为一次审查、一次失败或一次阶段交接新建顶层 Markdown；
6. 通过 `python scripts/check_docs.py` 后再交付。

历史状态、旧方案、单次实验日志和审查证据以 Git history 或本地 evaluation evidence 为准，不参与当前优先级解析。
