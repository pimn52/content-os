# Content OS — Durable Decisions

This file records only decisions that should survive individual tasks. Current scope/acceptance is in `CONTENT_OS_EXECUTION_SPEC.md`; current facts and next work are in `STATUS.md`.

## 2026-09-12 — Documentation governance

- Root project control is reduced to six active documents: `START_HERE.md`, `CONTENT_OS_EXECUTION_SPEC.md`, `STATUS.md`, `DECISIONS.md`, `AGENTS.md`, `README.md`.
- Old PRD/plan/freeze/handoff/review files are historical evidence and must not compete with the active hierarchy. Git history is the default archive.
- Normal work must not create a new top-level strategy/freeze/review/handoff document.
- `STATUS.md` is updated after each completed work package; `DECISIONS.md` changes only for durable choices; the execution spec changes only for material scope/acceptance/order changes.
- `scripts/check_docs.py` and CI enforce the active-doc set and stale-reference rules.

## 2026-09-12 — Voice provider strategy

- Voice remains provider-neutral; no model name is allowed to become a core schema dependency.
- OmniVoice is approved as a local **non-commercial technical evaluation / quality benchmark** provider.
- OmniVoice source code is Apache-2.0, but official pretrained weights are currently CC-BY-NC because of upstream training-data constraints; those weights must not be bundled or presented as a commercial-safe default.
- A commercial-safe local path and BYOK cloud fallback remain interchangeable behind the same `VoiceProvider` boundary.
- Voice generation requires automatic QA before final render: copy coverage, missing/duplicate text, duration/silence sanity, playability, provenance and retry/fallback result. Human likeness/naturalness remains an explicit U-Voice gate.
- Local model dependencies remain optional; base Content OS installation must stay lightweight and cannot require a high-end GPU.

## 2026-09-13 — Chatterbox comparison outcome

- Chatterbox was tested only in an isolated environment and remains outside Core contracts and dependencies.
- The local Chatterbox multilingual checkpoint plus runtime occupies roughly 5.376 GB before any Windows cache duplication; its raw outputs also required measured edge handling in the comparison.
- Explicit U-Voice side-by-side judgment found Chatterbox clearly worse than OmniVoice for timbre similarity and naturalness. Chatterbox is therefore not selected for the R1 default voice path.
- Following that rejection, its isolated local runtime, checkpoint and comparison output were removed; Chatterbox remains historical evidence only.
- OmniVoice remains the preferred local technical-benchmark path for this spike, but its CC-BY-NC weights still prohibit treating it as a commercial-safe default. A separate commercial-safe provider must remain possible behind the same provider-neutral boundary.

## 2026-09-14 — Daily-material Talking admission and rejected local candidates

- Mature Content OS Talking paths must accept ordinary, consented creator footage. Special AI-only capture (for example, a deliberately silent or closed-mouth reference clip) cannot be required and cannot by itself satisfy the product-quality gate.
- MuseTalk 1.5 failed the U-Talking product review on ordinary material: visible lip motion did not match the new speech, and its inherited performance/gaze did not fit the delivery. It is not admitted as a mature-product provider.
- The isolated MuseTalk runtime, weights, cache, evaluation outputs and rejected generated media were removed. Its generic provider contract, durable QA/review gates and historical decision evidence remain so a replacement can be evaluated without Core redesign.
- VideoReTalking is the next isolated local candidate. Its source license, model-weight/dependency licenses, runtime feasibility, ordinary-material quality and U-Talking review all remain separate acceptance checks.

## 2026-09-12 — Implementation model cost policy

- Development uses the cheapest model that can reliably complete the task: `Luna → Terra → Sol`.
- Luna is default for isolated UI/CRUD/tests/docs/simple adapters/mechanical fixes.
- Terra owns cross-module media/timeline/provider/planner/router/job/migration work.
- Sol is reserved for architecture/security/critical quality gates or unresolved Terra failures with a concrete reproduction.
- Task importance alone does not justify Sol; cost alone does not justify giving architecture-changing work to Luna.

## Existing architecture retained

- Keep FastAPI + SQLite + local Job/Worker + provider-neutral contracts + Remotion/FFmpeg; no R1 microservice/Redis/Celery/n8n/Kubernetes rewrite.
- Maintain durable provider idempotency, global/project budget enforcement and actual/unknown cost accounting.
- Keep one persistent default creator/IP context for R1 rather than building multi-tenant or multi-IP permissions.
- Continuous media clips are first-class playback assets; keyframes support understanding/search.
- Real creator assets are preferred; optional capture and low-cost static/typography paths precede expensive generation when adequate.
- Successful production usage, not previews/failures/retries, drives reuse history.
- Account Intelligence and broad Market Intelligence remain separate; R1 may use/import the creator's own history while broad market crawling/prediction is deferred.

## Core product gate

- The central R1 proof remains: `new topic → editable IP-aware copy → authorized creator voice → new creator Talking/lip-sync → hybrid real-media timeline → 30–60s export`.
- Imported finished narration, source-led recuts, old mouth motion, generic TTS or generic avatars cannot be used as evidence that this gate passed.
- Consent/rights, first paid usage and subjective creator likeness remain explicit human boundaries.
