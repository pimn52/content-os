# Content OS — Durable Decisions

This file records only decisions that should survive individual tasks. Current scope/acceptance is in `CONTENT_OS_EXECUTION_SPEC.md`; current facts and next work are in `STATUS.md`.

## Documentation governance

- Root project control stays limited to: `START_HERE.md`, `CONTENT_OS_EXECUTION_SPEC.md`, `STATUS.md`, `DECISIONS.md`, `AGENTS.md`, `README.md`.
- Old PRD/plan/freeze/handoff/review files are historical evidence and must not compete with the active hierarchy. Git history is the default archive.
- `STATUS.md` records only current truth, active blocker/candidate and next work. Per-run paths, timestamps, download/cache history and retired-provider experiment logs stay in Git history or local evaluation evidence.
- `scripts/check_docs.py` and CI enforce the active-doc set and stale-reference rules.

## Voice provider strategy

- Voice remains provider-neutral; no model name is allowed to become a Core schema dependency.
- **OmniVoice** is approved as a local non-commercial technical benchmark. Its source is Apache-2.0 but official pretrained weights are currently CC-BY-NC, so those weights are not a commercial-safe default.
- **Chatterbox is rejected** for the current R1 path after real side-by-side U-Voice testing found clearly worse creator timbre similarity and naturalness than OmniVoice.
- A commercial-safe local Voice provider is currently unselected. BYOK/cloud fallback remains interchangeable behind the same `VoiceProvider` boundary.
- Generated voice must pass automatic QA plus explicit human likeness/naturalness review before it can count toward the product gate.

## Talking / lip-sync strategy

- Mature Talking paths must accept ordinary, consented creator footage. Special silent/closed-mouth/expressionless AI-only capture cannot be a product prerequisite.
- **MuseTalk 1.5 is rejected** after ordinary-material U-Talking review found visible sync/performance quality below the product bar.
- Rejected model-specific runners/adapters do not remain in the active Core provider surface. Core keeps only provider-neutral contracts, Jobs, QA, reference selection and provenance boundaries.
- **VideoReTalking is rejected** after ordinary-material U-Talking review found severe mouth deformation, blur and local scale/warp artifacts; it has no Core adapter.
- The pinned **LatentSync 1.5** benchmark passed the user's sample-level continue judgment. Keep its Core adapter optional and benchmark-only behind explicit runtime paths; do not treat it as a commercial-safe default or mature R1 admission until the integrated 30–60s gate passes. Defer KeySync to a later compatible machine and do not integrate broad avatar-generation models merely because they produce talking heads.

## Talking runtime routing on consumer hardware

- **Local-first does not mean local-inference-only.** Local-first governs creator data ownership, local media/library/control and replaceable providers; compute-heavy inference may run remotely when that produces a better product.
- For low-spec PCs, the default mature Talking path may be an external/BYOK API once the user explicitly approves media transfer and provider cost.
- Local Talking remains an optional route when the machine passes provider-specific readiness and the output passes the same product-quality gate.
- Do not hard-code one universal VRAM threshold. Runtime readiness is provider-specific and must report implemented/configured/available/verified states.
- A remote provider must still pass the same identity retention, lip-sync, original motion/gaze, visual-quality and publishability gates as a local provider.
- Provider cost, data transfer, authorization and provenance must be visible; no hidden cloud fallback.

## Implementation model cost policy

- Development uses the cheapest model that can reliably complete the task: `Luna → Terra → Sol`.
- Luna: isolated UI/CRUD/tests/docs/simple adapters/mechanical fixes.
- Terra: cross-module media/timeline/provider/planner/router/job/migration work.
- Sol: architecture/security/critical quality gates or unresolved Terra failures with a concrete reproduction.
- Task importance alone does not justify Sol; cost alone does not justify giving architecture-changing work to Luna.

## Existing architecture retained

- Keep FastAPI + SQLite + local Job/Worker + provider-neutral contracts + Remotion/FFmpeg; no R1 microservice/Redis/Celery/n8n/Kubernetes rewrite.
- Maintain durable provider idempotency, global/project budget enforcement and actual/unknown cost accounting.
- Continuous media clips are first-class playback assets; keyframes support understanding/search.
- Real creator assets are preferred; optional capture and low-cost static/typography paths precede expensive generation when adequate.
- Account Intelligence and broad Market Intelligence remain separate; R1 may use/import the creator's own history while broad market crawling/prediction is deferred.

## Core product gate

- The central R1 proof remains: `new topic → editable IP-aware copy → authorized creator voice → new creator Talking/lip-sync → hybrid real-media timeline → 30–60s export`.
- Imported finished narration, source-led recuts, old mouth motion, generic TTS or generic avatars cannot be used as evidence that this gate passed.
- Consent/rights, first paid usage and subjective creator likeness remain explicit human boundaries.
