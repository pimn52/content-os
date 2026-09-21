# Content OS — Current Status

Updated: 2026-09-21

## Current truth

- Content OS remains an internal Alpha. R1 has not yet passed a complete repeatable new-topic → creator Voice → creator Talking → hybrid 30–60s export flow.
- Creator/IP state, local media/library, continuous Clips, ScenePlan/Hybrid Asset Router, Jobs/recovery, cost/accounting, MasterNarration/VideoSpec and local render foundations exist.
- Capability profiles, deterministic parameter resolution, Advanced Settings and local GPU resource leasing are implemented foundations of Local-first Data + Hybrid Compute. Automatic cross-provider Local↔Remote selection is not yet complete.
- Current benchmark providers remain non-commercial evaluation paths where their model-weight licenses require it; no benchmark result is a commercial-readiness claim.
- Current local Talking evidence has materially improved:
  - fresh-Voice single-segment evidence includes 4.46s PASS / 5.12s FAIL for the reviewed LatentSync configuration;
  - the E21 source-forward series produced a 17.28s joined result from five short jobs, and the user approved the exact joined artifact as natural, continuous and publishable;
  - this proves the reviewed execution strategy, not a universal provider/source/machine guarantee.
- The two important Talking corrections are now durable product rules:
  - terminal face closeout is a Content OS capability request, with provider-specific implementation;
  - multi-short Talking must map all child jobs onto one authorized source-forward continuous performance run before dispatch.
- The successful E21 result is not yet fully productized: its child-series topology is still closer to execution evidence than to a normal first-class product visual consumed by Asset Router/VideoSpec.
- Current Core also keeps sliced Talking and terminal closeout separate; the product rule still needs to allow terminal closeout on the final slice of a TalkingRun while keeping intermediate slices continuous.
- The largest remaining R1 capability risk is fresh long-form Master Narration reliability. Prior local OmniVoice long-form/sentence recovery attempts did not establish a repeatable 30–60s Voice route.
- Documentation governance has been reset around a stable hierarchy: whole-product spec → module specs → architecture → current STATUS → durable decisions → implementation policy.

## Active work package — Gate E22: reviewed TalkingRun productization

**State: READY**
**Primary implementation model: Terra**

### Objective

Convert the already validated multi-short Talking execution path into one normal product-level TalkingRun that downstream Content OS can consume as a first-class generated Asset/Clip.

Do not run another provider benchmark merely to prove what E21 already proved.

### Required product behavior

1. Introduce a provider-neutral TalkingRun product/read model representing:
   - project;
   - MasterNarration asset and master-relative interval;
   - authorized continuous reference Clip/run;
   - provider/execution provenance;
   - ordered child job/output evidence;
   - assembled visual asset;
   - automated QA state;
   - immutable human continuity/publishability decision.
2. Add a bounded TalkingRun assembly path:
   - child outputs contribute the visual stream;
   - the verified MasterNarration interval is the authoritative final audio;
   - child audio joins do not become product semantics.
3. Allow a series final slice only to request terminal face closeout when the selected adapter supports it.
   - intermediate slices remain continuation slices;
   - no per-slice artificial stop/start behavior;
   - no visual concealment/frozen-tail workaround.
4. Admit a TalkingRun as a normal generated Asset/Clip only after required child QA and approved series continuity review.
5. Make Hybrid Asset Router / VideoSpec able to consume that admitted TalkingRun without understanding its child-job topology.
6. Expose enough workspace/API state to identify the resulting TalkingRun, its source/evidence and whether it is admitted for production.
7. Move new orchestration out of main.py when practical; prefer a narrow application service rather than adding another large route block.

### Existing evidence to reuse

Use the already approved E21 series/evidence when exercising the assembly/admission path locally. A new LatentSync inference run is not required for this package unless a concrete implementation defect makes the existing evidence unusable.

### Stable interfaces

- consent/authorization;
- provider-neutral Voice/Talking contracts;
- MasterNarration meaning;
- capability/profile resolution;
- provider-call ledger/idempotency;
- automated QA vs human review separation;
- existing ordinary non-series Talking behavior.

### Acceptance criteria

- TalkingRun is not represented merely as a list of child jobs.
- Assembled output uses one authoritative MasterNarration interval for final audio.
- Source-forward child ordering/provenance is preserved.
- Only the final series slice can compose terminal closeout intent.
- Rejected/unreviewed/incomplete series cannot become a production Asset/Clip.
- Admitted TalkingRun can be selected/assembled through normal Asset/VideoSpec contracts.
- Focused domain/repository/application/API/assembly tests pass.
- Production Web build runs if UI changes.
- python scripts/check_docs.py passes.

### Non-goals

- no new Talking model/provider;
- no paid API;
- no new duration-bound search;
- no optical-flow/frame-interpolation seam repair;
- no full remote Compute Router;
- no broad UI redesign;
- no attempt to solve long-form Voice in this package;
- no claim that E22 alone passes R1.

### Exit states

- PASS — TalkingRun is a first-class product asset and the E21 evidence can exercise the normal admission/assembly path.
- FAIL — the bounded design cannot satisfy the product boundary without breaking preserved Core semantics.
- BLOCKED — a concrete missing artifact/dependency prevents completion; name the smallest clearing action.

## Short queue after E22

1. Voice Master reliability package — establish one repeatable fresh 30–60s MasterNarration route using provider-bounded natural takes + composition, or make the need for another local/remote Voice provider explicit.
2. R1 end-to-end product gate — new topic → new Voice → TalkingRun → real B-roll/typography/subtitles → 30–60s render → one U-Product review.
3. Repeatability gate — repeat on a second topic before claiming R1 core flow.

## Current blockers

- No repeatable fresh long-form MasterNarration path has passed the product gate yet.
- TalkingRun productization is the current bridge between successful E21 evidence and the normal render flow.
- No current GitHub Actions status is available for the latest master; local test/build claims must remain tied to their recorded implementation evidence.

## Documentation rule

STATUS contains only current truth, one active package, blockers and a short queue.

Past package history does not remain here. Current product behavior belongs in the module specs; implementation history remains in Git/evaluation evidence.
