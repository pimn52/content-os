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

## Model cost policy

Use the cheapest model that can complete the task with the required quality.

### Luna — default worker

Use for:

- isolated UI/CRUD;
- tests and fixtures;
- docs and repository hygiene;
- simple adapters with fixed interfaces;
- mechanical refactors and local bug fixes.

Do **not** let Luna independently redesign core schemas, execution semantics, provider accounting or media/timeline architecture.

### Terra — core implementation

Use for:

- media/timeline logic;
- Voice/Talking provider integration;
- planner/router/search semantics;
- multi-file state changes;
- jobs/retry/recovery/idempotency;
- migrations and compatibility-sensitive implementation;
- complex debugging with a reproducible failure.

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
- Update `STATUS.md` after every completed work package.
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
- explicit human U-Voice judgment for likeness/naturalness.

After a work package passes, update `STATUS.md` and directly claim the next ready task. Pause only for explicit consent/identity, first paid use or budget change, irreversible data/scope change, or a human product-quality gate.
