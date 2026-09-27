# Content OS — Current Status

Updated: 2026-09-27

## Current truth

- Content OS remains an internal Alpha. The first normal product-generated 30–60s new-topic export and second-topic repeatability gate have not passed.
- **Voice productization for the current R1 path is accepted.** The normal project flow can generate bounded OmniVoice takes, independently QA them, compose a MasterNarration, persist immutable U-Voice review, and gate downstream use. Exact 32.560s Master AudioAsset `c1346d02-d967-4475-b5f4-4bdf7933b2d3` passed full-copy QA and six-dimension U-Voice.
- **Talking productization for the current R1 path is accepted.** The normal flow supports provider-bounded source-forward children, independent QA, bounded single-child recovery, immutable U-Talking, whole-run continuity review, TalkingRun admission, and first-class Asset/Clip consumption by VideoSpec. Exact 32.400s TalkingRun `d6d03f20-ccd0-4d4b-8f35-9a47f51878e6` passed its human gates and normal admission.
- These passes are product-path evidence for the reviewed local configuration and exact assets. They do not prove generalized prosody control, universal Talking reliability, commercial-safe model licensing, or second-topic repeatability.
- V66 proved the next bottleneck is **visual direction / edit quality**, not Voice or Talking. The exact rendered MP4 failed U-Product because fixed vertical crop let the moving face leave frame, text-only scenes were visually weak, and the overall edit was too rough.
- Current ScenePlan/new-copy evidence is still assisted; automatic product-generated narrative/visual planning remains an R1 gap.
- Historical V17–V66 run detail stays in Git and local `content-os-data/evaluation-evidence/`; retrieve only named evidence when needed.

## Active work package — V67: EditPlan / Visual Direction foundation

**State: READY**  
**Primary implementation model: Terra**

### Objective

Introduce the smallest product-level visual-direction layer between editorial ScenePlan / routed assets and deterministic VideoSpec.

Do not repair V66 by tuning another fixed crop. Build reusable product semantics for how a scene should be framed and visually treated.

### Required product behavior

1. Introduce a provider/render-neutral `EditPlan` (or equivalent first-class contract) that carries, per scene:
   - visual role;
   - selected/routed asset reference;
   - framing policy;
   - subtitle/text treatment;
   - graphic treatment/template;
   - transition intent;
   - explicit fallback.

2. Add bounded Talking framing policies:
   - `face_safe_contain` — full source remains visible inside a designed vertical canvas;
   - `verified_static_crop` — allowed only with explicit full-interval face-safe evidence;
   - motion-aware crop may remain a future capability, not a V67 requirement.
   If crop safety is unknown, fail closed to `face_safe_contain`; sampled-frame approval is insufficient.

3. Replace the single hard-coded typography fallback with at least three semantic graphic treatments:
   - headline;
   - key point;
   - contrast.
   Rendering must consume reusable visual-style tokens rather than hard-coded per-video styling.

4. Add a preflight boundary that can reject an EditPlan before render for:
   - unsafe/unsupported framing;
   - conflicting burned-in/new subtitles where known;
   - duplicate full-paragraph + timed-caption treatment;
   - missing visual treatment/fallback.

5. Compile the accepted EditPlan into existing VideoSpec without leaking renderer-specific implementation into ScenePlan or Hybrid Asset Router.

### Stable interfaces

- V60 approved MasterNarration;
- V64 admitted TalkingRun and its QA/human provenance;
- Hybrid Asset Router candidate semantics;
- VideoSpec as deterministic render contract;
- existing Voice/Talking gates and assets.

### Acceptance criteria

- EditPlan/visual-direction semantics exist behind normal product contracts, not only an evaluation script.
- V66's Talking source would resolve to a face-safe fallback unless a full-interval crop is explicitly verified.
- Graphic treatments are semantic and style-token driven, not one hard-coded text card.
- Invalid visual plans fail before render.
- focused tests, Web production build (if renderer/UI changes), `python scripts/check_docs.py`, and `git diff --check` pass.

### Non-goals

- no new Voice or Talking generation;
- no Voice/Talking provider change;
- no final 30–60s render in this package;
- no full face-tracking/CV subsystem;
- no AI image/video generation;
- no unrelated B-roll;
- no automatic product ScenePlan generation yet;
- no commercial-release claim.

### Exit states

- `PASS` — EditPlan foundation and preflight are normal product capabilities and ready for one bounded final-render package.
- `FAIL` — the proposed boundary conflicts with stable ScenePlan/Router/VideoSpec semantics; preserve evidence and stop.
- `BLOCKED` — a concrete missing dependency or asset prevents completion; name the smallest clearing action.

## Short queue after V67

1. **V68 — one bounded U-Product render:** reuse V60 Master + V64 TalkingRun through the normal EditPlan path; preflight the complete visual plan before exactly one fresh render, then stop for U-Product.
2. **V69 — product-generated planning:** move from assisted Scene/EditPlan input to the normal topic → ScenePlan → EditPlan → routing flow.
3. **Second-topic repeatability:** repeat the normal product flow before claiming R1.

## Current blockers

- Final visual direction/edit quality has not passed U-Product.
- Product-generated ScenePlan/EditPlan from a new topic is not yet proven.
- OmniVoice official pretrained weights and the current LatentSync benchmark remain non-commercial evaluation dependencies; commercial release clearance is separate.

## Documentation rule

STATUS contains only current truth, one active package, blockers and a short queue. Closed package detail belongs in Git/evaluation evidence; durable product behavior belongs in module specs.
