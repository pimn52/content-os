# AGENTS.md — Content OS

> 统一执行规格见 [CONTENT_OS_EXECUTION_SPEC.md](CONTENT_OS_EXECUTION_SPEC.md)，本轮工程范围、阶段顺序和验收以它为准；当前复审证据见 [CONTENT_OS_REVIEW_2026-09-10.md](CONTENT_OS_REVIEW_2026-09-10.md)，旧 [AUDIT_REPORT.md](AUDIT_REPORT.md) 仅保留历史起点。不要新建并行的更高优先级规格。


## Read first

Before changing code, read:

1. `CONTENT_OS_EXECUTION_SPEC.md`、`CONTENT_OS_REVIEW_2026-09-10.md`、`STATUS.md`、`DECISIONS.md`
2. `STRATEGY_BASELINE.md`、`BASELINE_FREEZE.md`、`LOCAL_HANDOFF.md`、`PRD.md`、`DEVELOPMENT_PLAN.md`
3. The schemas/interfaces for the module you are changing.

## Product invariant

Content OS is not a generic AI video generator.

V0.1 goal:

> Understand the creator, their voice, their talking footage, and their media library; reuse real assets first; minimize reshooting/editing; generate a personalized short video.

当前核心验收不是“导入旁白”或“裁切旧口播”：在获得明确授权、可用样本和预算后，必须证明“新文案 → 本人新声音 → 本人新的 Talking/口型片段 → 30–60 秒成片”。旧片原声、通用 TTS、旧口型和通用头像都不能冒充这一链路完成；它们只能作为已明确标注的历史/降级路径。

## Core engineering invariants

- Local-first.
- BYOK.
- User media first.
- Continuous video clips are first-class assets; keyframes are for understanding, not final playback.
- Upload once, extract audio/transcript/visual metadata automatically.
- Provider implementations are replaceable.
- Core domain logic must not depend on one vendor.
- Do not require a GPU for the minimum end-to-end path.
- Do not introduce Redis, Celery, n8n, Kubernetes, or microservices in V0.1.
- Do not add Market Brain or broad web scraping in V0.1.
- YouTube account integration is read-only in V0.1.
- Do not implement unconsented voice/face cloning.
- Never log API keys or secrets.
- 外部 Provider 调用必须经持久化的请求身份、预算预留、单一执行 owner、结果/Job 状态和失败对账边界；不得用 Provider 的具体 `isinstance` 分支绕过账本。
- 全局预算是工作区总上限，项目预算是叠加限制；当前预算周期明确为本地账本生命周期，不能标称为月度额度。
- 真实模型辅助测试不能用固定夹具替代语义、声音或口型结果；保存输入、输出、Provider/模型、成本、失败与可播放产物证据。

## Scope discipline

Do not perform unrelated refactors.

Core schema changes may be made when they are needed by the active execution package; include a compatible migration, contracts, callers and regression coverage rather than treating schema work as a separate approval gate.

Do not introduce large dependencies without explaining:

- why needed;
- license;
- runtime impact;
- lighter alternatives considered.

## Testing

Every behavior change must have:

- unit tests where practical;
- integration tests for provider boundaries;
- fixture-based tests for media pipelines.

Media pipeline tests must verify timestamps and produced files, not only mocked return values.

Voice/Talking acceptance must additionally check playability, complete copy coverage, audio/video duration, duplicate or missing sentences and obvious sync failures. Likeness and naturalness remain U-Voice human judgments.

## Agent cost policy

Use the cheapest capable model:

- Luna: isolated implementation, CRUD, UI, tests, docs, simple adapters.
- Terra: media pipelines, cross-file logic, provider integration, planners, routers, concurrency/recovery.
- Sol: architecture/security/critical quality gates only.

Escalate only after a lower-cost agent has a concrete failure or the task is inherently cross-system.

## Required completion report

At the end of each task report:

- Changed files
- Tests run
- Acceptance criteria status
- Known limitations
- New dependency/license concerns
- Recommended next task

After a completed work package, update `STATUS.md` with current facts and directly claim the next ready dependency; pause only for U-Voice, U-Product, explicit budget/permission needs, or irreversible scope changes.
