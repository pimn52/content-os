# AGENTS.md — Content OS Implementation Contract

Read START_HERE.md first.

This file owns implementation behavior only. Product scope belongs in the product specification/module docs; current work belongs in STATUS.

## 1. Engineering invariants

- Preserve provider-neutral Core contracts.
- Keep the R1 modular monolith: FastAPI + SQLite + local Job/Worker + provider adapters + Remotion/FFmpeg.
- Do not hide missing capability, cost, license, consent, provenance or runtime failures.
- Unknown capability is not verified capability.
- Fixture, assisted-test and runtime evidence remain distinct.
- Media paths persisted as portable paths must resolve through the configured data-root boundary, not process CWD.
- Runtime provider calls use durable idempotency/budget/usage accounting.
- Every behavior change gets the smallest meaningful test.

## 2. Implementation model cost policy

Use the cheapest model that can reliably complete the bounded task:

Luna → Terra → Sol

### Luna

Use for isolated UI/CRUD, focused tests, docs, mechanical refactors and simple adapters with stable interfaces.

Do not let Luna independently redesign schemas, execution semantics, provider accounting, capability routing or media/timeline architecture.

### Terra

Use for cross-module media/timeline/provider/planner/router/job/migration work, capability profiles, application orchestration and compatibility-sensitive Voice/Talking debugging.

### Sol

Use for unresolved architecture conflicts, security/privacy/release review, critical quality-gate design, or a Terra task that still fails after materially different attempts with a minimal reproduction.

Task importance alone does not justify Sol.

## 3. Escalation

Escalate when:

1. the task is inherently outside the lower tier's allowed scope; or
2. the lower tier has a concrete reproducible failure after a materially different repair attempt.

Do not escalate merely because a task is tedious.

## 4. Task contract

Every active implementation package must state:

- Objective
- Allowed files/modules
- Stable interfaces
- Acceptance criteria
- Tests/build commands
- Non-goals
- Exit states

A completion report must state:

- changed files;
- tests/builds run;
- acceptance status;
- known limitations;
- dependency/license changes;
- next ready task.

## 5. Work-package lifecycle

Exactly one active package belongs in STATUS:

- READY
- RUNNING
- AWAITING_U_REVIEW
- PASS
- FAIL
- BLOCKED

Rules:

1. Before subjective Voice/Talking/Product review, update STATUS to AWAITING_U_REVIEW, list exact artifacts and stop.
2. A failed bounded experiment is a valid completion; preserve evidence and stop.
3. Do not silently expand scope. Provider changes, paid calls, architecture changes, product-scope changes or experiments outside the declared rule require a new package.
4. Numeric examples in conversation are hypotheses/constraints, not automatic fixed requirements.
5. Capability experiments optimize information gained per run and stop when more precision would not change product decisions.
6. Architecture work should build the smallest useful seam, close it, then open a separate package.

## 6. Productization discipline

Do not confuse experiment success with product capability.

A capability is productized only when normal product contracts can create/select it, QA/provenance is persisted, downstream normal flows can consume it, and failure/recovery is explicit.

Prefer product-level objects over leaking execution internals upward. For example:

- MasterNarration, not a list of Voice provider calls;
- TalkingRun, not a list of Talking slice jobs.

As orchestration grows, move workflow logic from FastAPI route handlers into narrow application services. Do not respond by introducing microservices.

## 7. Documentation governance

Current docs have distinct ownership:

- CONTENT_OS_EXECUTION_SPEC.md — whole-product map and R1 gate;
- docs/product/*.md — current product-module specifications;
- docs/architecture/SYSTEM_ARCHITECTURE.md — stable technical map;
- STATUS.md — current facts + one active package + short queue;
- DECISIONS.md — durable decisions only;
- AGENTS.md — implementation policy;
- README.md — setup/contributor entry.

Completed is not archived. If completed work changes product behavior, update the appropriate module spec. Do not keep the implementation story there.

docs/history/ is only for an entire superseded document with historical reading value. Per-run evidence stays in Git history or local evaluation evidence.

Never create parallel root PRDs/reviews/freezes/handoffs for ordinary work.

Run:

    python scripts/check_docs.py

before handoff.

## 8. Testing and evidence

Media work should validate real timestamps/files where practical, not only mocked return values.

Voice/Talking/Product gates preserve the distinction between:

- automated technical QA;
- human likeness/naturalness/continuity/publishability review.

Routing/settings tests additionally cover deterministic precedence, explicit unknown values, override scoping/reset, hard safety gates and decision provenance.

After a package passes, update STATUS and start only the next explicitly queued package.
