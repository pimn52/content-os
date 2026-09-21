# Content OS — System Architecture

This document is the stable **technical system map**. It explains how product modules become code/runtime boundaries. It is not a work log and does not prescribe implementation-agent model tiers.

## 1. Architectural shape

```text
                         Content OS
                              │
                ┌─────────────┴─────────────┐
                │                           │
          Product / Local Core        Execution Layer
                │                           │
      ┌─────────┼─────────┐         ┌───────┼────────┐
      │         │         │         │       │        │
 Creator/IP   Media    Project     Local   Remote   Resource
 Context      Assets   Timeline   Compute  Compute  Control
      │         │         │         │       │        │
      └─────────┴─────────┴─────────┴───────┴────────┘
                              │
                         Evidence / QA
                              │
                            Render
                              │
                      Review / Publication
```

The architecture is **Local-first Data + Hybrid Compute**:

- creator assets, project state, evidence and control stay local-first;
- inference may be local or explicitly approved remote/BYOK;
- product semantics do not depend on one provider.

## 2. Stable product layers

### Creator / Intelligence

Owns IP Profile, creator context, sourced opportunities, historical account data and feedback evidence.

It answers:

> Who is this creator, what context is trusted, and what should the content express?

### Narrative / Project

Owns Project, Draft, script revisions and ScenePlan.

It answers:

> What is the editorial intent?

It must not encode current model limitations.

### Media / Asset Intelligence

Owns Asset, Clip, AudioAsset, analysis evidence, candidate selection and Shoot Tasks.

It answers:

> What real or generated production material can satisfy the editorial need?

### Hybrid Asset Router

Chooses the **visual/content route**: real creator media, capture, typography/static, generated Talking, future stock/image/video.

It does not choose the execution provider.

### Execution Planner / Compute Router

Owns provider/runtime/machine capability evidence and effective parameter resolution.

It answers:

> How should this requested capability execute under current quality, hardware, privacy, license and cost constraints?

Current implementation includes capability profiles, deterministic parameter precedence, Advanced Settings and local GPU lease. Cross-provider automatic Local↔Remote selection is not yet complete.

### Voice

Produces new creator speech and independent QA evidence.

Long-form generation may be implemented as bounded provider calls + verified composition. The final product object is the verified Master Narration, not the provider call.

### Talking

Produces creator-visible video for new speech.

Execution may use short provider-bounded slices. Slices are not product objects.

The product-level output is a **TalkingRun**: a reviewed continuous visual run mapped to one Master Narration interval and one authorized continuous source-performance run.

### Timeline / Assembly

Owns MasterNarration and VideoSpec.

It maps verified audio and visual Assets/Clips onto one renderable timeline. Provider execution details must disappear before this boundary.

### Render / Review / Learning

Owns Remotion/FFmpeg output, human quality gates, explicit publication records, usage evidence and feedback.

## 3. First-class product objects

```text
Workspace / IP Profile
        │
Project / Draft
        │
ScenePlan
        │
        ├─────────────── Asset / Clip
        │
        ├─────────────── MasterNarration
        │
        └─────────────── TalkingRun
                              │
                              └─ execution-only Talking slices / child jobs
        │
VideoSpec
        │
Render
        │
Publication / Feedback
```

### Execution-only objects

The following support reliable execution but should not leak into product semantics unless needed for diagnosis:

- provider child calls;
- Talking slice plans;
- source reference windows;
- local GPU leases;
- adapter-specific temporal context;
- retry/lease metadata.

## 4. TalkingRun boundary

A reviewed TalkingRun should contain or reference:

- project identity;
- Master Narration asset and master-relative interval;
- authorized continuous reference Clip/run;
- provider/model/runtime/machine provenance;
- ordered child generation evidence;
- assembled **visual** result;
- automated QA result;
- human continuity/publishability decision;
- generated first-class Asset/Clip identity.

For a multi-slice run, the Master Narration remains the authoritative final audio. Child video outputs contribute the generated visual stream.

Intermediate slices are continuation slices. Only the final slice may request terminal face closeout when the selected adapter supports it.

Once the TalkingRun is admitted as a generated Asset/Clip, Hybrid Asset Router and VideoSpec should consume it exactly as a normal eligible production visual; they should not know its child-job topology.

## 5. Provider and capability boundary

Universal Core contracts may describe capabilities and intent, such as:

- Voice;
- Talking;
- terminal face closeout;
- requested continuity;
- execution cost/privacy requirements.

Provider adapters own model-specific controls such as LatentSync silent look-ahead.

Capability evidence is scoped to:

> provider + model/version + runtime + machine/profile

Configuration precedence is:

```text
job override
> saved provider+machine override
> locally verified evidence
> provider conservative default
> unknown
```

Unknown remains explicit.

## 6. Data and provenance

Persistent state lives in SQLite and local media roots unless an explicitly approved remote provider is invoked.

Important generated assets retain enough provenance to answer:

- which source/reference was authorized;
- which master narration interval was used;
- which provider/configuration executed;
- which QA evidence admitted the result;
- whether human review approved the result;
- what project/output used it.

Portable stored media paths must resolve through the configured local data-root boundary; worker process current directory is not a data contract.

## 7. Job and resource model

Heavy work is executed through durable Jobs with:

- idempotency;
- provider-call ledger;
- budget/cost accounting;
- retry/recovery boundaries;
- explicit QA states.

Local Voice/Talking GPU work may hold a SQLite-local named resource lease to prevent same-machine VRAM contention. This is intentionally not a distributed scheduler.

## 8. Quality gates

Automated evidence and human judgment are separate.

### Voice

Automated:
- copy/timing/silence/playability/provenance.

Human:
- likeness, naturalness, pacing, breathing.

### Talking

Automated:
- playable streams, duration/timing, narration-copy inheritance, raw-output integrity.

Human:
- visible sync, identity, mouth/teeth artifacts, source motion/gaze retention, continuity and publishability.

### Product

The final U-Product gate judges the rendered 30–60s output rather than internal job success.

## 9. Current implementation pressure

The codebase should remain a modular monolith for R1:

- FastAPI;
- SQLite;
- local Job/Worker;
- provider adapters;
- Remotion/FFmpeg.

Do not introduce microservices, Redis/Celery/Kubernetes simply because execution complexity grows.

As orchestration grows, move application workflows out of `main.py` into narrow application services rather than changing deployment topology.

Priority application-service seams:

- TalkingRun orchestration;
- Voice master/recovery orchestration;
- execution settings/routing orchestration.

## 10. Productization rule

A capability is not fully productized merely because a controlled experiment succeeded.

It becomes a product capability when:

1. the behavior exists behind a normal product contract;
2. required evidence/QA is persisted;
3. the product can create or select it without experiment scripts;
4. it produces a first-class object consumable by downstream normal flows;
5. the normal UI can expose the outcome at the appropriate abstraction level;
6. failure/recovery is explicit.

This rule is especially important for the current transition from a successful Talking slice-series experiment to a first-class TalkingRun.
