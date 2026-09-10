# AGENTS.md — Content OS

> R1 统一执行规格见 [CONTENT_OS_EXECUTION_SPEC.md](CONTENT_OS_EXECUTION_SPEC.md)，本轮工程范围、阶段顺序和验收以它为准；审查证据见 [AUDIT_REPORT.md](AUDIT_REPORT.md)。旧基线文件保留为历史背景。


## Read first

Before changing code, read:

1. `CONTENT_OS_EXECUTION_SPEC.md`、`AUDIT_REPORT.md`、`STATUS.md`、`DECISIONS.md`
2. `STRATEGY_BASELINE.md`、`BASELINE_FREEZE.md`、`LOCAL_HANDOFF.md`、`PRD.md`、`DEVELOPMENT_PLAN.md`
3. The schemas/interfaces for the module you are changing.

## Product invariant

Content OS is not a generic AI video generator.

V0.1 goal:

> Understand the creator, their voice, their talking footage, and their media library; reuse real assets first; minimize reshooting/editing; generate a personalized short video.

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

## Scope discipline

Do not perform unrelated refactors.

Do not change core schemas unless the task explicitly allows it.

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
