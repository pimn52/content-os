# Content OS — Current Status

Updated: 2026-09-23

## Current truth

- Content OS remains an internal Alpha. R1 has not yet passed a complete repeatable new-topic → creator Voice → creator Talking → hybrid 30–60s export flow.
- Creator/IP state, local media/library, ScenePlan/Hybrid Asset Router, Jobs/recovery, cost/accounting, MasterNarration/VideoSpec, TalkingRun admission and local render foundations exist.
- Local-first Data + Hybrid Compute foundations exist through capability profiles, deterministic settings resolution, Advanced Settings and local GPU resource leasing. Cross-provider automatic Local↔Remote selection is not required to finish the immediate R1 proof.
- Talking is no longer the main unknown. The reviewed source-forward multi-short route has produced an admitted 17.24s TalkingRun using the verified MasterNarration as final audio.
- The main R1 risk is now creator Voice performance: generating new speech that not only matches timbre, but also delivers content with understandable emphasis, natural pauses, rhetorical rhythm and continuity.
- Narration Performance Plan already exists as an exact-copy-bound, editable semantic layer for emphasis, pace, pauses and rhythm. It is not yet proven as an audio-rendering capability.
- V15 closed FAIL. The 2.12s local OmniVoice take passed automated copy/playability QA, but U-Voice rejected likeness, naturalness, emphasis, pauses and rhythm; pace alone passed. The take did not apply a Performance Plan.
- V16 is `BLOCKED` on generation integrity, not `LOCAL_PERFORMANCE_FAIL`. A passed fresh Voice QA, but
  several B/C *unit-sized* source takes failed independent copy QA. No complete
  B/C MasterNarration exists and U-Voice has not occurred, so there is no
  evidence that the Performance Renderer's listening quality failed. The
  current bounded issue is OmniVoice generation integrity at the tested
  GenerationSpan boundary; failed take/job evidence remains retained and must
  not be joined.
- The current OmniVoice worker derives authorized 3–10s reference windows from
  persisted transcript evidence. V16 now makes the selected window explicit at
  an adapter-scoped call boundary so B can keep one reference and C can select
  per GenerationSpan, rather than switching per short semantic Unit.
- For R1, the implementation strategy is now **OmniVoice-first, one-provider-first**: keep provider-neutral Core contracts, but do not continue horizontal provider shopping while the installed local route still has a bounded, testable path to product usefulness.
- OmniVoice remains a benchmark/evaluation route with non-commercial official pretrained-weight constraints; production usability for R1 does not equal commercial-release readiness.

## Active work package — Gate V16: OmniVoice-first Voice Performance Renderer feasibility

**State: BLOCKED**
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

   `NarrationPerformanceUnit` is an editorial/exact-copy object, not one
   OmniVoice call. Derive `VoiceGenerationSpan` from adjacent Units for the
   current adapter/profile, retaining every Unit's exact-copy, emphasis, pause,
   pace and rhythm provenance. Prefer a natural roughly 5–10s phrase when
   local adapter evidence supports it; that is not a global product duration
   rule. A semantic pause internal to a Span remains provenance-visible and is
   not falsely claimed as an acoustically applied pause.

2. Upgrade OmniVoice reference selection.
   - Build/select authorized 3–10s VoiceReferenceWindow candidates from the existing source-video/Clip evidence when available.
   - Preserve source Clip/time/transcript provenance.
   - Prefer clean, internally continuous speech windows.
   - Do not default forever to the first transcript segment of the first reference Clip.
   - No new recording is required.

3. Implement a minimal OmniVoice performance renderer.
   - It may use bounded GenerationSpans, provider-supported global/local speed where honestly applicable, semantic pause composition, and a compatible reference window.
   - It may test OmniVoice-native clone `instruct` only where the installed upstream interface actually supports it; do not equate generic style instruction with word-level rhetorical emphasis.
   - Never claim an unsupported cue was acoustically applied.
   - Do not use audio time-stretch as a substitute for delivery.
   - Do not insert artificial breath sounds.
   - Pause insertion, when used, must originate from the Performance Plan and remain provenance-visible.

4. Add a minimal `VoicePerformanceComposer`.
   - Preserve exact-copy order and source-take/reference provenance.
   - Produce one QA-pending MasterNarration candidate.
   - Run fresh master-level Voice QA after composition.

5. For each B/C GenerationSpan, run at most three total generate → independent
   copy-QA attempts. Retain every failed attempt's job/audio/provider/reference
   provenance; select the first complete QA-passing take and never overwrite a
   failure. If any Span exhausts three attempts, close V16 `BLOCKED` as
   OmniVoice copy-integrity with the retained evidence — never as U-Voice or
   Performance quality `FAIL`.

### Bounded experiment

Use one new 10–15s Chinese script containing:

- setup;
- important claim;
- contrast/turn;
- conclusion/landing.

Generate exactly three candidates:

- **A — baseline:** current OmniVoice path, no Performance Plan.
- **B — plan-driven:** semantic Performance Units grouped into GenerationSpans
  + one suitable, uniform authorized reference window.
- **C — reference-aware:** same plan and Span boundaries, with reference
  selection per GenerationSpan from the existing authorized source set.

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
- `LOCAL_PERFORMANCE_FAIL` — only after U-Voice, B and C both still sound
  materially like script reading; close OmniVoice performance tuning for R1 and
  only then open one alternate-provider benchmark.
- `BLOCKED` — a GenerationSpan exhausts its three independent copy-QA attempts,
  or a concrete local implementation/material/runtime issue prevents the
  comparison; name the smallest clearing action.
- `AWAITING_U_REVIEW` — all three QA-complete artifacts are ready; stop until the user judges them.

### V16 generation-integrity evidence / blocker (not a performance-quality exit)

- A baseline Voice QA job `v16-qa-0-20260922` completed.
- B unit QA job `v16-qa-1-20260922` failed; C unit QA jobs
  `v16-qa-7-20260922` and `v16-qa-8-20260922` failed with
  `voice_qa_failed`.
- Those failures are retained source-take provenance, but the former one-Unit
  provider-call boundary is not a valid A/B/C candidate. No B/C candidate
  exists for U-Voice yet.
- The new B GenerationSpan 0 (`先别急着写文案。选题有没有判断，决定观众会不会继续听。`)
  was generated three times with the same qualified authorized reference
  window. Every generated take was 6480ms and failed its own independent
  Voice QA: generation/QA pairs `2927ed77-94e0-41a5-bf8a-c55e62a64e1a` /
  `b41c79a5-ac9c-4752-bdf2-f3c36e3577b8`,
  `7705889b-b1d7-4573-9c72-bde111f7e1e2` /
  `9de5dc33-ea28-4bb2-bf61-319149e9e6d2`, and
  `626b2e5e-7a19-43be-b31c-75ed146e1a5e` /
  `75d04c09-78f2-4674-a94b-611b0f31486f`. The failed audio assets and job/
  provider-call provenance remain distinct; no attempt was overwritten.
- The bounded three-attempt rule is exhausted for this Span. V16 therefore
  stops `BLOCKED: OmniVoice copy-integrity at the current GenerationSpan
  boundary`. No B/C composition or U-Voice occurred, so it provides no
  conclusion about `LOCAL_PERFORMANCE_PASS`, `REFERENCE_AWARE_PASS`, or
  `LOCAL_PERFORMANCE_FAIL`. The smallest clearing action is a separately
  scoped local generation-integrity package that can reproduce and address the
  failed Span's copy/silence QA while preserving independent QA; it must not
  reopen provider selection or infer a listening-quality result from this run.

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
2. Only after a valid V16 U-Voice `LOCAL_PERFORMANCE_FAIL`: run **one** alternate expressive-cloning Provider benchmark using the same copy/performance evidence; do not start a provider tournament.
3. After Voice passes: R1 end-to-end product gate.
4. Repeat on a second topic before claiming R1 core flow.

## Current blockers

- No local Voice route has yet passed U-Voice for both creator identity and content-aware delivery.
- OmniVoice's installed adapter has not yet proven range-scoped emphasis/rhythm control; V16 must distinguish Content-OS-owned composition from provider-native synthesis honestly.
- V16 has a bounded generation-integrity dependency: every B/C GenerationSpan
  must produce one independently copy-QA-passing take within three attempts.
- V16 exhausted that dependency for B GenerationSpan 0. It is blocked pending
  the separately scoped local generation-integrity clearing action recorded
  above; do not continue B/C generation or start U-Voice in this package.
- OmniVoice official pretrained weights remain a non-commercial evaluation constraint for future commercial release.
- No current GitHub Actions status is available for the latest master; local test/build evidence remains tied to recorded implementation runs.

## Documentation rule

STATUS contains only current truth, one active package, blockers and a short queue.

Past package history does not remain here. Current product behavior belongs in the module specs; implementation history remains in Git/evaluation evidence.
