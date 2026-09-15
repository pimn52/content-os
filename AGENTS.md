# AGENTS.md — Content OS implementation contract

Read [`START_HERE.md`](START_HERE.md) first. It defines the document hierarchy.

## Product invariant

Content OS is a Local-first / BYOK personal content engine, not a generic AI video generator.

R1 must prove:

> new topic → editable new copy → authorized creator voice → at least one new creator Talking/lip-sync segment → real B-roll/typography/subtitles → 30–60s export.

Imported narration, source-led recuts, old mouth motion, generic TTS or generic avatars may exist as labeled fallback paths, but do not satisfy this gate.

## Engineering invariants

- Local-first; user media first; continuous clips are first-class assets.
- Upload once: derive audio/transcript/keyframes/metadata automatically where supported.
- Provider-neutral core. Never hard-wire a vendor/model into domain contracts.
- Minimum end-to-end path must not require a high-end GPU.
- No Redis/Celery/n8n/Kubernetes/microservices in R1 unless the execution spec is explicitly changed.
- No broad Market Brain/web scraping in R1.
- YouTube account path remains read-only until explicitly changed.
- Voice/face cloning requires explicit rights/consent records.
- Never log secrets.
- Runtime provider calls use the existing durable idempotency/budget/usage boundary; unknown cost is not zero.
- Unknown capability is not verified capability. Never label documentation claims, theoretical limits, or another machine's results as local evidence.
- Fixture, assisted-test and runtime evidence remain distinct. Never promote fixture success to product-quality success.

## Voice / Talking provider policy

Voice and Talking are replaceable provider layers.

### OmniVoice

OmniVoice is approved for **local non-commercial technical evaluation / benchmark** in Content OS.

- Source code license: Apache-2.0.
- Official pretrained weights: currently CC-BY-NC because of upstream training-data constraints.
- Therefore: do not bundle those weights in a commercial release, do not present them as a commercial-safe default, and do not let Core depend on OmniVoice-specific state.
- Install only as an optional extra/provider worker, never as a mandatory base dependency.
- Reuse existing ASR/reference transcript when available instead of re-running Whisper unnecessarily.
- Generated speech must pass QA before render: copy coverage, duration/silence sanity, playable audio, and retry/fallback behavior.

A commercial-safe local provider and a BYOK cloud fallback must remain possible without schema changes.

## Capability-aware routing policy

Treat these as separate layers:

1. **Narrative / Scene intent** — what the content needs editorially.
2. **Hybrid Asset Router** — which visual/content source should satisfy it.
3. **Execution Planner / Compute Router** — which provider/runtime/configuration should execute that capability.

Do not distort narrative structure merely to fit one provider's current limitation. Provider constraints belong in execution/routing.

Capability evidence must be scoped to a concrete provider/model/runtime/machine configuration. When practical, record:

- implemented/configured/available/verified state;
- observed pass/fail operating range;
- quality/continuity evidence;
- relevant resource use and latency;
- local/remote execution mode;
- known/unknown cost;
- license/commercial status;
- verification provenance and time.

## Advanced Settings / parameter overrides

Advanced Settings is an override layer over the same routing system, not a parallel configuration stack.

Parameter precedence is fixed:

1. per-job explicit override;
2. saved user override for the provider + machine/profile;
3. locally verified capability/profile value;
4. provider-known conservative default;
5. unknown.

Rules:

- Do not invent an optimized value when evidence is missing.
- Expose an advanced override when a provider/machine parameter materially affects quality/runtime and the system lacks sufficient evidence.
- Keep provider-specific parameter names behind provider adapters/config schemas; avoid leaking one provider's knobs into universal domain contracts.
- Where practical, surface parameter provenance/status: `verified`, `provider_default`, `user_override`, or `unknown`.
- Allow reset to automatic/verified defaults.
- User overrides may not bypass consent, budget, license, provenance, or hard runtime-safety checks.
- A successful user-tuned run may be promoted to local evidence only after appropriate QA/human acceptance; never auto-promote it to a global default.
- Prefer the narrowest persistence scope that matches intent: job → project → provider+machine profile.

## Model cost policy

Use the cheapest model that can complete the task with the required quality.

### Luna — default worker

Use for:

- isolated UI/CRUD;
- tests and fixtures;
- docs and repository hygiene;
- simple adapters with fixed interfaces;
- mechanical refactors and local bug fixes.

Do **not** let Luna independently redesign core schemas, execution semantics, provider accounting, capability routing or media/timeline architecture.

### Terra — core implementation

Use for:

- media/timeline logic;
- Voice/Talking provider integration;
- planner/router/search semantics;
- capability profiles and Compute Router / Execution Planner;
- multi-file state changes;
- jobs/retry/recovery/idempotency;
- migrations and compatibility-sensitive implementation;
- complex debugging with a reproducible failure;
- bounded capability-search work where provider/runtime behavior and product-quality evidence must be separated.

### Sol — gate/review only

Use only for:

- architecture conflicts with no obvious local resolution;
- security/privacy/release reviews;
- final Talking/voice quality gate design;
- a Terra task that still fails after materially different attempts and has a minimal reproduction.

Task importance alone is not a reason to use Sol.

## Escalation rule

`Luna → Terra → Sol`

Escalate when either:

1. the task is inherently outside the lower tier's allowed scope; or
2. the lower tier has a concrete, reproducible failure after a materially different repair attempt.

Never spend a higher tier merely to avoid writing a precise task boundary.

## Task contract

Every implementation task must specify:

- Objective
- Allowed files/modules
- Interfaces that must stay stable
- Acceptance criteria
- Tests/build commands
- Non-goals

Completion report must include:

- changed files;
- tests/builds run;
- acceptance criteria status;
- known limitations;
- new dependency/license concerns;
- next ready task.

## Work-package lifecycle

Every active package in `STATUS.md` must carry exactly one state:

- `READY` — scoped and safe to start;
- `RUNNING` — execution is actively in progress;
- `AWAITING_U_REVIEW` — the required human-quality artifact is ready; **stop work** until the user reviews it;
- `PASS` — acceptance criteria are satisfied and the package is closed;
- `FAIL` — the bounded experiment/implementation did not satisfy acceptance criteria and is closed;
- `BLOCKED` — a concrete external/runtime/user dependency prevents completion; preserve reproduction/evidence and stop.

Rules:

1. An agent may move `READY → RUNNING` when it starts the package.
2. Before asking the user for subjective Voice/Talking judgment, update `STATUS.md` to `AWAITING_U_REVIEW`, list the exact artifact(s) to review, and stop. Do not keep tuning while waiting.
3. After the user's review, convert the package to `PASS`, `FAIL`, `BLOCKED`, or continue the same package **only when its declared bounded search rule explicitly allows another informative experiment**.
4. `BLOCKED` must name the blocker and the smallest next action that could clear it. Do not use `BLOCKED` for ordinary uncertainty.
5. Do not silently expand a package. A provider change, paid service, architecture change, product-scope change, data-boundary change, or experiment outside the declared search rule requires closing the current package first.
6. A failed bounded experiment is a valid completion. Preserve evidence; do not loop indefinitely to manufacture a pass.
7. `STATUS.md` should contain one active package only. Historical detail belongs in Git history or local evaluation evidence.
8. Numeric examples from the user are not automatically fixed requirements. Infer the underlying product question and prefer evidence-efficient experiments that answer it.
9. Capability-boundary experiments should optimize **information gained per run**. Use bracketing/binary/adaptive search when appropriate; stop when further precision would not change product routing or market decisions.
10. Architecture packages must also be bounded. Build the smallest useful capability-profile/routing slice, close it, then open a separate UI/advanced-settings or continuity package rather than mixing them indefinitely.

## Documentation maintenance

Only these files are active project-control documents:

- `START_HERE.md` — navigation only;
- `CONTENT_OS_EXECUTION_SPEC.md` — current scope and acceptance contract;
- `STATUS.md` — current facts, active package, blockers, next ready task;
- `DECISIONS.md` — durable decisions only;
- `AGENTS.md` — implementation/model policy;
- `README.md` — user/contributor overview and run instructions.

Rules:

- Do not create a new top-level review/freeze/handoff/plan document for normal work.
- Update `STATUS.md` after every completed work package or before a human-quality handoff.
- Update `DECISIONS.md` only for durable choices.
- Update the execution spec only when product scope/acceptance/order materially changes.
- Historical evidence stays in Git history or `docs/history/`; it does not outrank active docs.
- Run `python scripts/check_docs.py` before handoff.

## Testing

Every behavior change needs the smallest meaningful verification. Media tests must validate real timestamps/files where practical, not only mocked return values.

Voice/Talking acceptance additionally checks:

- full copy coverage;
- playable output;
- duration and silence sanity;
- missing/duplicate sentence detection;
- obvious sync failure detection;
- explicit human U-Voice judgment for likeness/naturalness;
- explicit human U-Talking judgment for visible sync/publishability.

Routing/Advanced Settings behavior additionally checks:

- precedence order is deterministic;
- unknown values stay unknown rather than being silently guessed;
- user overrides are scoped and reversible;
- hard safety/consent/budget/license gates still win over overrides;
- routing decisions expose enough reason/provenance to debug why a provider/configuration was chosen.

After a work package passes, update `STATUS.md` and claim only the next task explicitly allowed there. Pause for explicit consent/identity, first paid use/budget change, irreversible data/scope change, or a human product-quality gate.