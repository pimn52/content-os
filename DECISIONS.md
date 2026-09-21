# Content OS — Durable Decisions

This file records only decisions that should survive individual tasks. Current scope/acceptance is in `CONTENT_OS_EXECUTION_SPEC.md`; current facts and next work are in `STATUS.md`.

## Documentation governance

- Root project control stays limited to: `START_HERE.md`, `CONTENT_OS_EXECUTION_SPEC.md`, `STATUS.md`, `DECISIONS.md`, `AGENTS.md`, `README.md`.
- Old PRD/plan/freeze/handoff/review files are historical evidence and must not compete with the active hierarchy. Git history is the default archive.
- `STATUS.md` records only current truth, one active bounded package, its explicit state and next work. Per-run paths, timestamps, download/cache history and retired-provider experiment logs stay in Git history or local evaluation evidence.
- `scripts/check_docs.py` and CI enforce the active-doc set and stale-reference rules.
- Work packages must close as pass/fail/blocked/inconclusive or stop at an explicit human-review handoff; they must not remain indefinitely in an ambiguous in-progress state.

## Voice provider strategy

- Voice remains provider-neutral; no model name is allowed to become a Core schema dependency.
- **OmniVoice** is approved as a local non-commercial technical benchmark. Its source is Apache-2.0 but official pretrained weights are currently CC-BY-NC, so those weights are not a commercial-safe default.
- **Chatterbox is rejected** for the current R1 path after real side-by-side U-Voice testing found clearly worse creator timbre similarity and naturalness than OmniVoice.
- A commercial-safe local Voice provider is currently unselected. BYOK/cloud fallback remains interchangeable behind the same `VoiceProvider` boundary.
- Generated voice must pass automatic QA plus explicit human likeness/naturalness review before it can count toward the product gate.
- Automatic copy/timing QA does not establish timbre similarity, pacing or breathing quality; those remain explicit U-Voice concerns.
- A failed generated-Voice take is never silently repaired into a verified master. Content OS records a provider-neutral recovery recommendation beside its immutable QA evidence: a trim/derived-audio retry is eligible only for a **sole** leading-silence failure and must receive fresh independent QA; missing, duplicate or excessive substituted copy requires fresh bounded sentence takes, each independently QA-verified before continuous assembly and master-level QA. Corrupt/no-timing/long-silence results require provider diagnosis. The original failed asset remains preserved and ineligible for render.

## Talking / lip-sync strategy

- Mature Talking paths must accept ordinary, consented creator footage. Special silent/closed-mouth/expressionless AI-only capture cannot be a product prerequisite.
- **MuseTalk 1.5 is rejected** after ordinary-material U-Talking review found visible sync/performance quality below the product bar.
- Rejected model-specific runners/adapters do not remain in the active Core provider surface. Core keeps only provider-neutral contracts, Jobs, QA, reference selection and provenance boundaries.
- **VideoReTalking is rejected** after ordinary-material U-Talking review found severe mouth deformation, blur and local scale/warp artifacts; it has no Core adapter.
- **LatentSync 1.5** remains an optional benchmark-only local adapter. Fresh-Voice evidence on the current 6GB development machine includes a 2.58s pass and a 5.12s late-sync failure. These are configuration-specific routing evidence, not universal model limits.
- Content OS owns a terminal Talking **closeout protector** for the defect “speech has ended but the mouth still appears to be speaking.” It uses the persisted final transcript timestamp as the face-visible audio/video end and never replaces the face with B-roll, black, Typography, or a frozen clone. The Core carries only the request plus that endpoint; an adapter may implement it with a native Provider control or a Content-OS context-and-crop technique even if the upstream Provider has never identified the defect. For the current LatentSync 1.5 adapter, **结束静音前瞻（ms）** (`trailing_silence_lookahead_ms`) is its silent model-input context after speech. `600ms` is a conservative adapter baseline that may run on compatible machines, while the D6g human-quality pass (175ms leading/600ms trailing around a 1.8s segment) remains evidence for its exact provider/machine/take only, not a global quality claim or universal parameter. An unadapted Provider must expose this product request as unavailable/unknown rather than receive LatentSync's setting or a visual workaround.
- Do **not** freeze the product to a user-mentioned duration example. Talking duration capability must be measured from evidence.
- Use a pass/fail bracket and adaptive midpoint testing to locate the useful duration boundary with the fewest runs. Adjust the midpoint only to a nearby natural speech boundary.
- Stop when the remaining interval is precise enough to change product routing/UX/market decisions; do not chase a mathematical maximum when a small residual uncertainty is operationally irrelevant.
- Separate visual Talking duration from Voice duration/prosody and keep multi-segment continuity as a separate product-quality question.
- Individually passing short Talking clips do not prove a continuous-presenter experience.
- A multi-short Talking series must map its complete master-narration timeline to one authorized, source-forward continuous reference Clip **before** Provider execution. Each child receives its own persisted source window at the matching master offset; any Provider frame-alignment runway is input-only and may not alter delivered audio/video cut points. If the selected Clip cannot cover the entire mapped run, dispatch fails rather than restarting at frame zero, looping, or choosing unrelated material.
- KeySync remains deferred to a later compatible machine; do not integrate broad avatar-generation models merely because they produce talking heads.

## Local-first Data + Hybrid Compute

- **Local-first does not mean local-inference-only.** Creator assets, IP/profile, project state and control remain local-first; heavy inference may execute locally or through an approved remote provider.
- Hybrid Asset Router and Compute Router are separate concerns: the Asset Router chooses the visual/content route; the Compute Router chooses the provider/runtime/configuration used to execute it.
- Narrative/Scene Planner expresses editorial intent independent of current model limits. Execution Planner adapts that intent to verified local/remote capability instead of contaminating narrative structure with one provider's constraints.
- Low-spec machines may use external/BYOK compute when quality, runtime or hardware make local inference unsuitable. Remote media transfer, cost and privacy implications must be explicit; there is no hidden cloud fallback.
- Local and remote providers must pass the same product-quality and provenance gates.

## Capability profiles and parameter evidence

- A capability belongs to a concrete **provider + model/version + runtime/machine configuration**, not merely to a model name.
- Capability profiles may record readiness, observed operating range, continuity/quality evidence, resource use, latency, cost, license status and verification provenance/time.
- Results from one machine/configuration do not silently become global defaults.
- **Unknown capability stays unknown.** Provider documentation, another user's result or a theoretical requirement may inform a conservative default but cannot be labeled locally verified.
- Successful user-tuned runs may become local evidence after QA/human acceptance; they do not automatically become global product defaults.

## Advanced Settings / overrides

- Production parameter precedence is: `per-job explicit override > saved provider+machine/user override > locally verified capability/profile value > provider-known conservative default > unknown`.
- Advanced Settings is an override layer, not a separate configuration system.
- When the system lacks sufficient empirical evidence, expose a transparent Advanced Settings path instead of pretending the automatic router knows the optimum.
- Advanced Settings must show the source/status of important values where practical (`verified`, `provider default`, `user override`, `unknown`) and allow reset to automatic/verified defaults.
- Advanced overrides may tune provider-specific quality/runtime parameters but cannot bypass consent, budget, license, provenance, or hard runtime-safety checks.
- Overrides should be saved at the narrowest useful scope: one job, project, or provider+machine profile.

## Implementation model cost policy

- Development uses the cheapest model that can reliably complete the task: `Luna → Terra → Sol`.
- Luna: isolated UI/CRUD/tests/docs/simple adapters/mechanical fixes.
- Terra: cross-module media/timeline/provider/planner/router/job/migration work, capability profiles, Compute Router and compatibility-sensitive Voice/Talking debugging.
- Sol: architecture/security/critical quality gates or unresolved Terra failures with a concrete reproduction.
- Task importance alone does not justify Sol; cost alone does not justify giving architecture-changing work to Luna.
- Numeric examples in user conversation are not automatically engineering requirements; implementation agents must infer and preserve the underlying product objective.

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
