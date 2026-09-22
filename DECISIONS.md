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

## D015 — Narration performance intent is editorial semantics, not a Voice knob

Delivery direction (emphasis, pace, pauses and rhetorical rhythm) belongs to
an editable, exact-copy-bound Narration Performance Plan. It records source
and evidence references, versions with the Draft, and is cleared on a copy
edit rather than silently retargeted.

Provider adapters may apply an immutable snapshot only through an explicit
capability and application receipt. That receipt, automated Voice QA and
U-Voice are separate evidence; none may be substituted for another.

Content OS may compile the plan into exact-copy delivery segments before
execution. The derived structure creates no timing or audio; it remains
separate from any adapter application receipt.

## D016 — U-Voice is an immutable asset-level gate

After automated Voice QA, a U-Voice record must retain concrete findings and
explicit judgments for likeness, naturalness, emphasis, pace, pauses and
rhetorical rhythm. Only an all-pass record approves the exact generated Voice
asset for Talking or final assembly.

A rejected asset cannot be silently re-approved in place. Human feedback is
quality/provenance evidence; it does not verify provider capabilities or
promote a delivery preference to a default.

## D017 — Rhetorical delivery assistance remains transparent editorial input

The product may derive reviewable delivery suggestions from general copy
structure such as sentence role, parallel claims, assertion boundaries and
enumerations. Each suggestion stays exact-copy-bound and records its
structural rationale.

Suggestions are ephemeral assisted input: they are neither automatically saved
nor executed, do not invoke a Voice provider, and do not claim acoustic quality
or provider application. An individual U-Voice judgment cannot silently become
a global learned default; a creator must explicitly edit/save the normal
Narration Performance Plan.

## D018 — Rhetorical guidance is evidence-bounded semantic review, not an acoustic formula

Prosody research and public-speaking practice may inform the editorial language
of Content OS: concept landing in parallel/contrastive claims, a reviewable
claim or turn boundary, and locally driven enumeration within an otherwise
conversational delivery.

They do not license a universal WPM, pause duration, F0, intensity, word-stress
or provider setting. Overall pace, local pace and semantic pause boundaries
remain distinct plan fields. A suggestion is still separate from adapter
application receipt, Voice QA and U-Voice evidence; language, creator, genre,
provider and human review determine any eventual realization.

## D019 — Partial Voice-plan coverage fails closed before execution

A provider may distinguish full, partial and unsupported coverage of a
Narration Performance Plan. Partial coverage is a preflight fact, not an
application receipt and not permission to silently omit cues.

Until the product exposes an explicit, creator-reviewed subset mode, a partial
or unsupported result must reject before ProviderCall reservation and before
the provider receives copy or reference media. A full application receipt,
automated Voice QA and U-Voice remain separate downstream evidence.

## D020 — Provider-neutral does not mean provider-shopping

Provider abstraction is a long-term replacement and routing boundary. It is not a requirement to integrate or benchmark many providers before one route can ship.

For the immediate R1 Voice proof, concentrate implementation and product-quality work on the already integrated OmniVoice route until one bounded performance-rendering experiment either reaches the U-Voice quality bar or establishes a concrete provider-acoustic limitation. Only then should one alternate expressive-cloning provider be benchmarked against the same evidence.

This decision does not convert benchmark-only model weights into a commercial-safe dependency; license admission remains separate from product-quality admission.

## Current provider admission state

Provider-specific current state is maintained in docs/product/VOICE_TALKING.md, not duplicated here.
