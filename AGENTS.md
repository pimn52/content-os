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

Use the cheapest model that can reliably complete the bounded task, including repair, validation and human review cost. This is an implementation-agent policy, not the product's runtime provider router.

Model mapping rechecked 2026-10-03 after the user selected GPT-6.1 Sol, against the current host and [GPT-6.1 Sol guidance](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [selection guidance](https://developers.openai.com/api/docs/guides/model-selection) and [Codex pricing](https://learn.chatgpt.com/docs/pricing). Official model roles inform the mapping; the scope restrictions below are this repository's engineering policy, not a vendor benchmark. API prices do not establish subscription usage.

| Work class | Default model / reasoning | Allowed responsibility |
|---|---|---|
| F — focused | `gpt-6-luna` / low; medium for coordinated bounded changes | frozen-contract UI/CRUD, focused tests, documentation synchronization, mechanical refactors, simple adapters |
| I — integration | `gpt-6.1-sol` / medium; high for demonstrated cross-module uncertainty | schemas, media/timeline, planning/routing, durable jobs, migrations, provider accounting, application orchestration |
| J — judgment/review | `gpt-6-astra` / high, bounded to the unresolved question | architecture conflicts, critical admission-policy design, security/privacy/release review, or a reproduced integration failure after materially different repair attempts |

F must not independently redesign schemas, execution semantics, capability routing, accounting or media architecture. I owns the smallest necessary integration seam, then returns stable follow-on work to F. J returns a decision, threat/gate analysis or minimal failing case to I; it is not the default implementation owner of the whole phase.

The old unversioned Luna → Terra → Sol ladder is superseded. Do not interpret the old top-tier Sol label as today's `gpt-6-sol` assignment. Older models are explicit compatibility fallbacks only when availability or measured task cost justifies them; record the actual model ID.

Do not assume API token prices equal subscription usage/credits. Record actual model/reasoning, attempts, test outcome and observable usage; mark unobservable cost unknown. No automatic Fast/max/ultra setting, repeated model comparison or multi-agent execution merely to follow this table. The host/user must actually select a model; a document assignment does not switch a running chat.

## 3. Escalation

Escalate when:

1. the task is inherently outside the lower tier's allowed scope; or
2. the lower tier has a concrete reproducible failure after a materially different repair attempt.

Do not escalate merely because a task is tedious.

Before escalation retain the failing test/reproduction, attempted different repair, affected interface and a narrow question. Stop an unproductive retry loop; do not spend more on trial-and-error than on a bounded review. After resolution, return to the lowest sufficient work class. Recheck the model mapping when availability changes, not on every package.

## 4. Task contract

Every active implementation package must state:

- Objective
- Allowed files/modules
- Stable interfaces
- Acceptance criteria
- Tests/build commands
- Non-goals
- Exit states
- Work class, actual model/reasoning and escalation trigger
- Evidence level, permitted external calls, experiment/repair budget and expected reduction in manual work

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
7. A documentation/planning request does not authorize starting the next implementation package. Queue readiness and execution authorization are separate.

## 6. Productization discipline

Do not confuse experiment success with product capability.

A capability is productized only when normal product contracts can create/select it, QA/provenance is persisted, downstream normal flows can consume it, and failure/recovery is explicit.

Report maturity separately: contract/code, real runtime, normal UI/workflow, repeatability and automatic decision-making. A package PASS covers its stated acceptance only. Fixture success, an API-only path and an assisted render must never be combined into an end-to-end capability claim.

Every review request must name the decision it enables, exact artifact/version and whether existing evidence already answers it. Keep child technical evidence, but do not add subjective review gates by default. Review-granularity changes require an explicit policy migration and regression tests; never waive current admission checks silently.

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
- docs/implementation/ROADMAP.md — dependency-ordered future package contracts and acceptance/test matrix; no live status, duplicate product specification or model-name policy;
- DECISIONS.md — durable decisions only;
- AGENTS.md — implementation policy;
- README.md — setup/contributor entry.

Completed is not archived. If completed work changes product behavior, update the appropriate module spec. Do not keep the implementation story there.

docs/history/ is only for an entire superseded document with historical reading value. Per-run evidence stays in Git history or local evaluation evidence.

Never create parallel root PRDs/reviews/freezes/handoffs for ordinary work.

Run:

    python scripts/check_docs.py

before handoff.

The documentation checker validates structure, not product truth. Also cross-check plan order, admission rules, model assignment and every changed capability claim against code/evidence.

## 8. Testing and evidence

Media work should validate real timestamps/files where practical, not only mocked return values.

Voice/Talking/Product gates preserve the distinction between:

- automated technical QA;
- human likeness/naturalness/continuity/publishability review.

Routing/settings tests additionally cover deterministic precedence, explicit unknown values, override scoping/reset, hard safety gates and decision provenance.

After a package passes, update STATUS and start only the next explicitly queued package.
