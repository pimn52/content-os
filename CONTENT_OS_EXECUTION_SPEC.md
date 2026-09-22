# Content OS — Product & R1 Specification

Updated: 2026-09-21

This is the authoritative whole-product map and R1 acceptance contract. Module details live under docs/product/. Technical layering lives in docs/architecture/SYSTEM_ARCHITECTURE.md. Current execution state lives in STATUS.md.

## 1. Product thesis

Know what to create → Create it as you → Learn what works.

Content OS is a Local-first / BYOK content operating system for creators, professionals and small teams that have useful knowledge but insufficient time or production capacity to repeatedly turn it into short-form content.

The primary durable object is the creator/brand workspace, not an isolated video.

## 2. Product system

    Workspace / Creator / Brand
    ├─ IP / Persona / Strategy context
    ├─ Knowledge and historical content
    ├─ Voice identity and authorization
    ├─ Visual identity and production assets
    ├─ Platform/account evidence
    └─ Performance / feedback evidence

    New topic or sourced opportunity
            ↓
    IP-aware editable copy + narration performance intent
            ↓
    ScenePlan / editorial intent
            ↓
    Hybrid Asset Router
            ↓
    Execution Planner / Compute Router
            ↓
    Voice / Talking / real media / typography / capture
            ↓
    MasterNarration + TalkingRun + Assets
            ↓
    VideoSpec
            ↓
    30–60s render
            ↓
    Review / publication / feedback

## 3. R1 product gate

R1 is complete only when an authorized creator can, through the normal product flow:

1. retain a persistent creator/IP profile and reusable media library;
2. enter a new topic and obtain editable IP-aware copy;
3. create a new verified Master Narration in the creator's authorized voice;
4. create at least one new creator-visible Talking segment/run speaking new words;
5. combine it with authorized real media, optional capture, typography/static material and subtitles;
6. export a playable 30–60 second vertical video;
7. repeat the flow for another topic without re-recording the entire narration or manually cutting all media from scratch.

Imported finished narration, source-led recuts, old mouth motion, generic TTS or generic avatars may remain explicit fallback paths but do not prove the R1 core gate.

## 4. Product modules

### 4.1 Creator / IP / Intelligence

Owns persistent creator context, sourced opportunities, historical account/content evidence and feedback.

Spec: docs/product/CREATOR_INTELLIGENCE.md

### 4.2 Media / Asset Intelligence

Owns local media, continuous Clips, analysis evidence, candidate selection, capture gaps and asset usage.

Spec: docs/product/MEDIA_ASSET_SYSTEM.md

### 4.3 Voice / Talking

Owns new creator speech, reviewable rhetorical delivery suggestions, editable narration performance intent, Master Narration admission, creator-visible generated Talking, continuous TalkingRun semantics and human likeness/publishability gates.

Spec: docs/product/VOICE_TALKING.md

### 4.4 Execution / Hybrid Compute

Owns capability evidence, provider/runtime/machine resolution, parameter provenance, Advanced Settings, local resource guards and future explicit remote/BYOK routing.

Spec: docs/product/EXECUTION_COMPUTE.md

### 4.5 Timeline / Render / Learning

Owns MasterNarration timeline use, VideoSpec, render, final review, publication records and feedback.

Spec: docs/product/TIMELINE_RENDER_LEARNING.md

## 5. Product invariants

- Local-first Data + Hybrid Compute. Creator assets, identity/profile, project state, evidence and control remain local-first; heavy inference may use approved local or remote compute.
- Provider-neutral product semantics. A model/vendor may be replaced without changing ScenePlan, Asset, MasterNarration, TalkingRun or VideoSpec meaning.
- Narrative intent is not a hardware setting. Execution adapts to capability; editorial structure is not hard-coded to one provider's limits.
- Real media first. Existing creator material is preferred when it clears the quality bar.
- Continuous media is first class. Keyframes support understanding; they do not replace source clips.
- No hidden downgrade or cloud fallback. Cost, privacy/data transfer and missing capability remain explicit.
- Consent and rights are hard gates.
- Unknown cost is not zero; unknown capability is not verified.
- Evidence and human judgment are separate. Automated QA does not claim likeness or publishability.

## 6. First-class product outputs

The normal product pipeline should converge on these objects:

- IP Profile / creator context;
- Project / Draft / ScenePlan;
- Asset / Clip / AudioAsset;
- verified MasterNarration;
- reviewed TalkingRun;
- VideoSpec;
- Render / Publication / Feedback.

Provider child calls, short Talking slices, alignment runways and resource leases are execution details, not the user-facing product model.

## 7. R1 quality gates

### Voice

Automated evidence must cover copy/timing/silence/playability/provenance. One
asset-specific U-Voice record after automated QA must cover likeness,
naturalness, emphasis, pace, pauses and rhetorical rhythm. Pending or rejected
generated Voice cannot drive Talking or final assembly; this human judgment
does not substitute for a provider capability/application receipt.

### Talking

Automated evidence must cover playable/timing/raw-output integrity and narration provenance. Human review covers visible sync, identity, mouth artifacts, original performance retention, continuity and publishability.

### Product

The final U-Product review judges the normal 30–60s rendered result.

A single successful experiment is not enough: R1 requires one full pass and then a second-topic repeatability pass.

## 8. R1 architecture boundary

Keep the current modular-monolith direction:

- FastAPI local API;
- SQLite persistent state;
- durable Job/Worker execution;
- provider-neutral adapters;
- local FFmpeg/ffprobe media operations;
- Remotion + FFmpeg rendering;
- browser workspace;
- provider budget/idempotency/accounting;
- backup/recovery and private-network boundaries.

Do not introduce Redis, Celery, Kubernetes, n8n or microservices merely because the media pipeline is complex.

## 9. Current R1 status at a glance

The product has strong foundations in creator/project state, local assets, routing, execution evidence, Advanced Settings, jobs, timeline and rendering.

Talking has moved beyond isolated short demos: the current local benchmark has produced an approved source-forward multi-short continuous result. The remaining Talking work is to convert that validated execution path into a normal first-class TalkingRun consumed by Asset Router/VideoSpec.

The largest unresolved R1 capability risk is reliable fresh Master Narration generation for normal 30–60s content. Current local Voice evidence is not yet sufficient for a repeatable long-form claim.

See STATUS.md for the active bounded task.

## 10. Deferred beyond the immediate R1 proof

- broad autonomous Market Brain / competitor crawling;
- automatic platform publishing;
- automatic cross-provider remote selection before a remote provider is admitted;
- large distributed scheduling infrastructure;
- broad avatar-generation product modes;
- claims of commercial readiness for benchmark providers with incompatible model-weight licenses.
