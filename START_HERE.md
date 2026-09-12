# Content OS — Start Here

这是项目唯一的人类/实施 Agent 入口。**不要从旧 PRD、审查报告或历史冻结文件开始。**

## 1. 先看什么

按这个顺序即可：

1. [`STATUS.md`](STATUS.md) — **现在做到哪里、真实阻塞、下一项 ready 任务**。
2. [`CONTENT_OS_EXECUTION_SPEC.md`](CONTENT_OS_EXECUTION_SPEC.md) — **当前产品承诺、R1 范围、验收和执行顺序**。
3. [`AGENTS.md`](AGENTS.md) — **实施模型的工程约束、模型成本分层、文档维护规则**。
4. [`DECISIONS.md`](DECISIONS.md) — 只在需要理解长期不可轻易反转的取舍时阅读。
5. [`README.md`](README.md) — 面向使用者/贡献者的产品概览与启动方式。

模块开发时再读取对应代码、schema、测试；不要把整个历史文档树塞进上下文。

## 2. 当前产品主线

长期方向：

> **Know what to create → Create it as you → Learn what works**

R1 当前先攻克 `Create it as you`：

> 已授权创作者输入新主题后，系统生成可修改的新文案，合成本人的新声音，生成至少一个说出新台词的本人 Talking/口型片段，再结合真实 B-roll、排版、字幕输出 30–60 秒新视频。

旧片裁切、导入现成旁白、旧原声或通用数字人都只能作为明确标注的辅助/降级路径，不能冒充上述核心验收通过。

## 3. Voice 策略修正

Voice 永远通过可替换 `VoiceProvider` 接入：

- **OmniVoice**：进入本地 Voice Clone 技术验证/质量 benchmark。其代码为 Apache-2.0，但官方预训练权重当前受 **CC-BY-NC** 约束，因此**不得作为未来商业版默认权重或被打包为商业能力**。
- **商业可用本地 Provider 候选**：保留 Chatterbox 等路线，接入/发布前逐项复核代码与模型权重许可证。
- **Cloud Provider**：作为无合适本地算力或质量不足时的 BYOK fallback；首次付费/超预算必须经过用户边界。

模型名称不是产品架构。Content OS 保存 Voice Profile、参考来源、授权、质量证据和 Provider metadata，允许后续替换底层模型。

## 4. 实施模型成本原则

保证质量前提下使用最低成本的可胜任模型：

`Luna → Terra → Sol`

- **Luna**：边界清晰的 UI、CRUD、测试、文档、简单 adapter、机械修复。
- **Terra**：媒体链路、Provider 集成、跨文件状态、Planner/Router、Job/恢复、Voice/Talking 实现。
- **Sol**：架构冲突、安全审查、关键质量 Gate，或 Terra 有具体最小复现后仍无法解决的问题。

不能因为任务重要就默认用 Sol；也不能为了便宜让 Luna 独立改变核心协议。

## 5. 每个任务结束后的固定动作

实施 Agent 完成任务后：

1. 跑与改动相符的测试/构建；
2. 更新 `STATUS.md` 的**当前事实 / 当前工作包 / 下一项 ready 任务**；
3. 只有产生长期取舍时才更新 `DECISIONS.md`；
4. 只有产品承诺、范围或验收发生变化时才改 `CONTENT_OS_EXECUTION_SPEC.md`；
5. 不为一次审查、一次失败或一次阶段交接新建顶层 Markdown；
6. 通过 `python scripts/check_docs.py` 后再交付。

历史状态、旧方案和审查证据以 Git history 为准，不再占据根目录或参与当前优先级解析。
