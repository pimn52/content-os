# Content OS — Current Status

Updated: 2026-09-22

## Current truth

- Content OS remains an internal Alpha. R1 has not yet passed a complete repeatable new-topic → creator Voice → creator Talking → hybrid 30–60s export flow.
- Creator/IP state, local media/library, ScenePlan/Hybrid Asset Router, Jobs/recovery, cost/accounting, MasterNarration/VideoSpec, TalkingRun admission and local render foundations exist.
- Local-first Data + Hybrid Compute foundations exist through capability profiles, deterministic settings resolution, Advanced Settings and local GPU resource leasing. Cross-provider automatic Local↔Remote selection is not required to finish the immediate R1 proof.
- Talking is no longer the main unknown. The reviewed source-forward multi-short route has produced an admitted 17.24s TalkingRun using the verified MasterNarration as final audio.
- The main R1 risk is now creator Voice performance: generating new speech that not only matches timbre, but also delivers content with understandable emphasis, natural pauses, rhetorical rhythm and continuity.
- Narration Performance Plan already exists as an exact-copy-bound, editable semantic layer for emphasis, pace, pauses and rhythm. It is not yet proven as an audio-rendering capability.
- V15 closed FAIL. The 2.12s local OmniVoice take passed automated copy/playability QA, but U-Voice rejected likeness, naturalness, emphasis, pauses and rhythm; pace alone passed. The take did not apply a Performance Plan.
- The current OmniVoice worker already derives a bounded reference window from persisted transcript evidence rather than always feeding the whole source clip, but the selection policy is still mechanical: it takes the first valid transcript segment from the first VoiceProfile reference Clip.
- For R1, the implementation strategy is now **OmniVoice-first, one-provider-first**: keep provider-neutral Core contracts, but do not continue horizontal provider shopping while the installed local route still has a bounded, testable path to product usefulness.
- OmniVoice remains a benchmark/evaluation route with non-commercial official pretrained-weight constraints; production usability for R1 does not equal commercial-release readiness.

## Active work package — Gate V16: OmniVoice-first Voice Performance Renderer feasibility

**State: READY**  
**Primary implementation model: Terra**

### Objective

Close the missing bridge between the existing Narration Performance Plan and actual local OmniVoice audio.

Determine, with one bounded A/B/C experiment, whether Content OS can make the current installed OmniVoice route materially more publishable by combining:

1. semantic performance units;
2. deliberate pause/continuity composition; and
3. reference-window selection from the creator's already authorized source material.

Do not test another Voice provider in this package.

### Product principle

The product owns **how the narration should be delivered**; the provider owns the acoustics it can actually synthesize.

Do not define a fictional perfect Voice contract that no current provider can execute. Implement the smallest useful performance layer that can be honestly realized with OmniVoice, preserve unsupported semantics explicitly, and judge the result by U-Voice.

### Required implementation

1. Introduce a derived execution object such as `NarrationPerformanceUnit` / `VoicePerformanceRenderPlan`.
   - Units follow semantic/rhetorical/breath-group boundaries, not fixed seconds.
   - Preserve exact-copy character provenance.
   - Carry sentence/claim role, applicable emphasis cue, inherited/local pace, pause-after intent and rhythm role.
   - Do not invent pitch/F0/loudness or fake precise timing.

2. Upgrade OmniVoice reference selection.
   - Build/select authorized 3–10s VoiceReferenceWindow candidates from the existing source-video/Clip evidence when available.
   - Preserve source Clip/time/transcript provenance.
   - Prefer clean, internally continuous speech windows.
   - Do not default forever to the first transcript segment of the first reference Clip.
   - No new recording is required.

3. Implement a minimal OmniVoice performance renderer.
   - It may use bounded semantic units, provider-supported global/local speed where honestly applicable, semantic pause composition, and a compatible reference window.
   - It may test OmniVoice-native clone `instruct` only where the installed upstream interface actually supports it; do not equate generic style instruction with word-level rhetorical emphasis.
   - Never claim an unsupported cue was acoustically applied.
   - Do not use audio time-stretch as a substitute for delivery.
   - Do not insert artificial breath sounds.
   - Pause insertion, when used, must originate from the Performance Plan and remain provenance-visible.

4. Add a minimal `VoicePerformanceComposer`.
   - Preserve exact-copy order and source-take/reference provenance.
   - Produce one QA-pending MasterNarration candidate.
   - Run fresh master-level Voice QA after composition.

### Bounded experiment

Use one new 10–15s Chinese script containing:

- setup;
- important claim;
- contrast/turn;
- conclusion/landing.

Generate exactly three candidates:

- **A — baseline:** current OmniVoice path, no Performance Plan.
- **B — plan-driven:** semantic Performance Units + one suitable reference window.
- **C — reference-aware:** same plan, but Units may select the best suitable authorized reference window from the existing source set.

No fourth candidate. No 30–60s generation in this package.

### Human review

After technical QA succeeds, present A/B/C together and stop at `AWAITING_U_REVIEW`.

Review dimensions:

- creator likeness;
- naturalness;
- whether semantic emphasis is understandable;
- pause placement;
- rhetorical rhythm;
- continuity / mechanical splice audibility;
- publishability.

### Acceptance / exit

- `LOCAL_PERFORMANCE_PASS` — B reaches publishable U-Voice quality and materially improves the failed baseline.
- `REFERENCE_AWARE_PASS` — only C reaches the quality bar, establishing reference-role selection as a required product capability.
- `LOCAL_PERFORMANCE_FAIL` — B and C both still sound materially like script reading; close OmniVoice performance tuning for R1 and only then open one alternate-provider benchmark.
- `BLOCKED` — a concrete local implementation/material/runtime issue prevents the comparison; name the smallest clearing action.
- `AWAITING_U_REVIEW` — all three QA-complete artifacts are ready; stop until the user judges them.

### Non-goals

- no new Voice provider/model install;
- no paid/remote Voice call;
- no 30–60s MasterNarration;
- no Talking generation;
- no ScenePlan redesign;
- no new creator recording;
- no universal acoustic formula;
- no automatic learning of a creator preference from one review;
- no commercial-readiness claim for OmniVoice official pretrained weights.

## Short queue after V16

1. If V16 passes: use the verified OmniVoice performance route to build one fresh 30–60s MasterNarration and run U-Voice.
2. If V16 fails: run **one** alternate expressive-cloning Provider benchmark using the same copy/performance evidence; do not start a provider tournament.
3. After Voice passes: R1 end-to-end product gate.
4. Repeat on a second topic before claiming R1 core flow.

## Current blockers

- No local Voice route has yet passed U-Voice for both creator identity and content-aware delivery.
- OmniVoice's installed adapter has not yet proven range-scoped emphasis/rhythm control; V16 must distinguish Content-OS-owned composition from provider-native synthesis honestly.
- OmniVoice official pretrained weights remain a non-commercial evaluation constraint for future commercial release.
- No current GitHub Actions status is available for the latest master; local test/build evidence remains tied to recorded implementation runs.

## Documentation rule

STATUS contains only current truth, one active package, blockers and a short queue.

Past package history does not remain here. Current product behavior belongs in the module specs; implementation history remains in Git/evaluation evidence.
