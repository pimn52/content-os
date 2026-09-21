# Content OS — Durable Decisions

This file records choices that should remain stable across multiple work packages. It is not a product specification or experiment history. Current product behavior lives in docs/product/.

## D001 — Workspace/IP is the durable product center

Content OS is organized around a persistent creator/brand workspace and reusable context/assets, not around isolated generated videos.

## D002 — Local-first Data + Hybrid Compute

Creator assets, identity/profile, project state, evidence and control remain local-first.

Heavy inference may run locally or through an explicitly approved remote/BYOK provider when quality, privacy, hardware, latency, license or cost requires it.

Local-first does not mean local-inference-only.

## D003 — Provider-neutral product semantics

Core product contracts describe intent and evidence, not vendor/model implementation details.

Provider-specific parameters remain in adapters/settings schemas.

## D004 — Narrative intent stays independent of provider limits

ScenePlan says what the content needs editorially.

Execution Planner adapts that intent to current provider/runtime/machine capability. A temporary model limit must not become a permanent narrative rule.

## D005 — Real media first

Prefer adequate creator-owned/historical media before expensive generation.

Missing media may produce an optional capture task before AI generation.

## D006 — MasterNarration is the authoritative narrated audio timeline

The product should not require manual per-scene narration cutting.

Provider calls may operate on bounded takes/slices, but final narrated assembly maps back to one verified MasterNarration.

## D007 — TalkingRun is the product-level Talking result

Short provider slices/child jobs are execution details.

A continuous creator-visible result becomes a product capability only when it is represented as a reviewed TalkingRun and admitted as a first-class generated Asset/Clip.

For a composed TalkingRun, MasterNarration is the authoritative final audio.

## D008 — Multi-slice Talking requires source-forward continuous reference

Before dispatching a multi-short Talking series, Content OS maps all slices onto one authorized continuous source-performance run.

Children may not independently restart at source time zero, loop unrelated footage or silently choose unrelated reference material.

## D009 — Terminal face closeout is product intent, not a vendor knob

The product may request a face-visible natural mouth close when final speech ends.

An adapter may implement it through provider-native controls or validated Content OS context-and-crop logic.

Intermediate TalkingRun slices are continuation slices; only the final delivered slice may apply terminal closeout.

## D010 — Capability evidence is configuration-scoped

Verified capability belongs to:

provider + model/version + runtime + machine/profile

One machine's result does not silently become another machine's verified default.

Unknown remains unknown.

## D011 — One configuration precedence

Effective provider parameters resolve as:

    per-job override
    > saved provider+machine override
    > locally verified value
    > provider conservative default
    > unknown

Advanced Settings is an override surface over this same system, not a parallel configuration stack.

## D012 — Human quality gates remain explicit

Automated QA may prove copy/timing/playability/integrity.

It does not prove creator likeness, naturalness, visible lip-sync, continuity or publishability.

## D013 — R1 remains a modular monolith

Keep FastAPI + SQLite + local Job/Worker + provider adapters + Remotion/FFmpeg.

Growing orchestration should move into application services before considering distributed infrastructure.

## D014 — Completed capability and archived document are different concepts

When work completes:

- current product behavior belongs in the relevant product/module spec;
- implementation details remain in Git/evaluation evidence;
- STATUS removes the completed package;
- docs/history/ is used only for an entire superseded document with historical reading value.

## Current provider admission state

Provider-specific current state is maintained in docs/product/VOICE_TALKING.md, not duplicated here.
