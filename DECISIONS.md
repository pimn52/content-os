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
