# Content OS — Current Status

Updated: 2026-09-23

## Current truth

- Content OS remains an internal Alpha. R1 has not yet passed a complete repeatable new-topic → creator Voice → creator Talking → hybrid 30–60s export flow.
- Creator/IP state, local media/library, ScenePlan/Hybrid Asset Router, Jobs/recovery, cost/accounting, MasterNarration/VideoSpec, TalkingRun admission and local render foundations exist.
- Local-first Data + Hybrid Compute foundations exist. Cross-provider automatic Local↔Remote selection is not required to finish the immediate R1 proof.
- Talking is no longer the main unknown. The reviewed source-forward multi-short route has produced an admitted 17.24s TalkingRun using the verified MasterNarration as final audio.
- The main R1 risk is creator Voice performance: new speech must preserve copy and creator identity while delivering understandable emphasis, pauses and rhetorical rhythm.
- Narration Performance Plan, NarrationPerformanceUnit, VoiceGenerationSpan, reference-window selection and VoicePerformanceComposer now exist as execution foundations. Performance Unit is editorial semantics; GenerationSpan is the provider-call boundary.
- V15 closed U-Voice FAIL: the short local OmniVoice baseline passed automated copy/playability QA but still sounded insufficiently like the creator and like script reading.
- V16 did **not** reach a performance-quality verdict. A baseline passed Voice QA, but B GenerationSpan 0 (`先别急着写文案。选题有没有判断，决定观众会不会继续听。`) produced three 6.48s takes and all three failed independent copy QA. No complete B/C candidate or U-Voice review exists.
- Therefore V16 is closed as **BLOCKED: OmniVoice copy-integrity**, not `LOCAL_PERFORMANCE_FAIL`.
- The current evidence does not yet distinguish true model omission from QA false-negative, reference-window effects, or a short/under-allocated GenerationSpan. Those causes require different fixes and must be attributed before performance work resumes.
- R1 remains **OmniVoice-first / one-provider-first**. Do not open another Voice provider merely because this bounded integrity issue occurred.
- OmniVoice official pretrained weights remain non-commercial evaluation material; R1 product usability and future commercial provider admission are separate decisions.

## Active work package — Gate V17: OmniVoice generation-integrity attribution

**State: READY**  
**Primary implementation model: Terra**

### Objective

Explain and clear the exact V16 B-Span blocker with the smallest number of additional inferences.

Do not optimize rhetorical performance in this package. Establish whether the retained 6.48s failures came from:

1. a Voice-QA / ASR false negative;
2. reference-window choice or reference-text/audio mismatch;
3. GenerationSpan duration/boundary or duration allocation;
4. a reproducible OmniVoice copy-integrity failure under otherwise valid inputs.

### Phase A — forensic existing evidence first, no generation

Use the three retained V16 B Span-0 attempts and the passing A baseline.

For every attempt record/compare:

- exact requested text;
- exact ASR transcript and timed segments;
- missing / duplicate / substitution counts and exact differing token positions;
- leading / internal / trailing silence;
- generated WAV duration and content hash;
- reference Clip/window start/end/transcript;
- provider execution parameters;
- whether the A baseline used the same reference window and relevant execution parameters.

Required conclusions before any new inference:

- Are the three failed WAV hashes identical or different?
- Do all three fail on the same words/positions?
- Are failures concentrated at the end of the Span or inside it?
- Does the audio itself contain speech that the QA ASR omitted/misrecognized?
- Is the selected reference transcript an exact match to the extracted reference audio interval?

If the retained evidence is sufficient to identify a QA false-negative or reference-window defect, fix only that defect, add regression coverage, and rerun QA on the existing audio before generating anything new.

### Phase B — at most two controlled new inference probes

Only if Phase A shows the generated audio truly omits/repeats requested copy.

Probe 1 must keep the **exact failed Span text** and isolate the most likely variable:

- if A and B used different references, use A's proven reference with otherwise unchanged B settings;
- if reference is not implicated and the omission is a trailing truncation, test a modest provider-local duration/speed allowance while preserving exact copy;
- if failures are internal and the same reference/settings are already shared, do not blindly tune speed. Prefer merging this Span with the next natural semantic unit so the provider call approaches the already-passing longer A scale.

Probe 2 is allowed only if Probe 1 cleanly distinguishes the next variable. Do not create a parameter grid.

### Important implementation rule

Do **not** treat “three retries” as useful redundancy unless the retained WAV hashes or provider stochastic evidence show the attempts are materially different. If retries are effectively deterministic, future retry policy must not spend three identical attempts.

### Provider facts to respect

The installed OmniVoice route currently uses its standard generation path. Upstream exposes duration/speed, decoding and sampling controls, and supports reusable voice-clone prompts, but these are provider implementation details rather than product defaults.

Do not change `num_step`, guidance/temperature, speed, duration, post-processing and reference selection together. Change one explanatory variable at a time and persist provenance.

### Acceptance / exit states

- `QA_ATTRIBUTION_PASS` — existing audio was materially correct and the blocker was a bounded QA/comparison defect; regression added and existing evidence clears.
- `REFERENCE_ATTRIBUTION_PASS` — exact failed Span clears with a corrected/known-good authorized reference; record reference integrity as the cause.
- `SPAN_PROFILE_PASS` — exact copy integrity is restored by a larger natural GenerationSpan or justified duration allowance; persist the local OmniVoice generation-profile evidence and return to V16.
- `MODEL_COPY_BLOCKED` — copy omission/repetition remains reproducible after the two controlled probes with valid reference/QA; stop local performance work and return for product-level route review.
- `BLOCKED` — required retained evidence is missing or cannot be interpreted; name the smallest clearing action.

No U-Voice review occurs in V17 unless a complete performance candidate somehow already exists; this package is about copy integrity only.

### Non-goals

- no new Voice provider/model;
- no paid/remote call;
- no 30–60s narration;
- no Talking;
- no new creator recording;
- no performance-plan redesign;
- no parameter sweep;
- no fourth/fifth blind retry;
- no relaxation of copy QA merely to obtain a pass.

## Short queue after V17

1. If V17 clears integrity: reopen the existing V16 A/B/C performance comparison using the verified GenerationSpan/reference profile and stop at `AWAITING_U_REVIEW`.
2. If V17 ends `MODEL_COPY_BLOCKED`: perform a product-level Voice route review before any alternate-provider implementation.
3. Only after Voice quality passes: run the first R1 end-to-end product gate.
4. Repeat on a second topic before claiming R1 core flow.

## Current blockers

- The exact cause of the V16 B Span-0 copy-QA failures is not yet attributed.
- No local Voice route has passed U-Voice for both creator identity and content-aware delivery.
- OmniVoice official pretrained weights remain a non-commercial evaluation constraint for future commercial release.
- No current GitHub Actions status is available for the latest master; local test/build evidence remains tied to recorded implementation runs.

## Documentation rule

STATUS contains only current truth, one active package, blockers and a short queue.

Past package history does not remain here. Current product behavior belongs in the module specs; implementation history remains in Git/evaluation evidence.
