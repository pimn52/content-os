# Content OS — Current Status

Updated: 2026-09-15 (Asia/Shanghai)

## Current truth

- The local media/runtime foundation is in place: persistent IP/draft revisions, provider accounting/idempotency, local media import and continuous clips, ASR/vision/index boundaries, ScenePlan/Hybrid Router, browser/mobile upload, MasterNarration/timeline, Remotion rendering, Jobs/recovery and backup checks.
- The project is still an **internal Alpha**. It has not passed the R1 product gate: `new topic → creator voice → creator Talking/lip-sync → 30–60s new video`.
- Voice and Talking remain separate quality gates. Automated ASR/container QA is necessary but does not prove creator likeness, natural pacing or visible lip-sync.
- **OmniVoice** remains the local non-commercial Voice benchmark. Short samples have been usable after QA, but the 17.44s take exposed weaker perceived likeness, compressed pacing and insufficient breathing. Its official pretrained weights remain CC-BY-NC.
- **MuseTalk 1.5** and **VideoReTalking** are rejected for the mature product path.
- **LatentSync 1.5** is an optional benchmark-only local Talking adapter. A short ordinary-material sample around 3s was acceptable to continue; the 17.44s result was not publishable because visible lip-sync increasingly lagged the narration. The final output reuses the original narration audio, so visual sync drift must be diagnosed separately from Voice quality.
- The current 6GB RTX 3060 Laptop GPU can run the bounded LatentSync benchmark only near its hardware limit and with an evaluation-only attention compatibility fallback. This machine is valid for capability probing, not proof of the minimum product hardware requirement.
- KeySync remains deferred to a later compatible machine. No new Talking provider is being added during the current capability-boundary package.

## Completed work package — Gate D3: shorter fresh-Voice Talking probe

**State: PASS**
**Primary implementation model: Terra**

### Objective

Test whether the visible late-sync degradation in the failed 5.12s combined sample is duration-sensitive. Reuse the first complete natural clause from that exact U-Voice-approved take (about 3.12s including its original lead-in), so Talking duration is the only varied input.

### Task contract

- **Allowed modules:** local evaluation evidence, existing provider/QA paths and this status record. Core interfaces remain unchanged unless a concrete reproducible defect requires a separately scoped repair.
- **Stable interfaces:** provider-neutral Voice/Talking contracts, consent/provenance, durable provider accounting and the existing local-routing evidence.
- **Acceptance:** create and independently QA the complete first-clause audio prefix; run one normal local Talking Job with raw-output coverage guard and automated Talking QA; then stop for U-Talking review of its exact artifact.
- **Non-goals:** no regenerated voice, new provider, paid call, full narration, multi-segment assembly or 30–60s render. The extracted prefix is test evidence, not a final-assembly audio-editing design.

### Current run

- Reuse `gate-d1-natural-short-cuda.wav`, already accepted for Voice likeness, naturalness, pacing and breathing. Its independent medium-ASR QA is verified: 5.12s / 24kHz mono, 100% copy coverage, zero missing/duplicate/substitution tokens and 460ms leading/longest silence.
- The complete first clause ends at the ASR-verified natural sentence boundary at 3.12s: `有了新主题，先保留真实素材。` Its direct QA found 539ms leading silence, so the test input removes only that silent lead (2.58s total) and independently passes QA: 100% coverage, zero missing/duplicate/substitution tokens, 0ms leading silence and 140ms longest silence. Existing authorized creator reference and transcript remain fixed. OmniVoice/LatentSync official weights remain non-commercial benchmarks only.
- The normal local Job `457f0521-f33b-42aa-a043-72238be2db5a` completed. Its H.264/AAC 1280×720 output is 65 frames / 2.60s against 2.58s audio (20ms drift), with full verified Voice-QA coverage and no `freezedetect` interval. The raw-output coverage guard ran before normalization.

### U-Talking decision

- The user judged `content-os-data/latentsync-duration-boundary-20260915/talking-runtime-data/assets/originals/b42d200ad5503b3120d07359fc5040622a498728e077e04a43bb2cebb48dc08b.media` **PASS**. This is the first fresh, U-Voice-approved OmniVoice take + LatentSync combination that passes U-Talking on this machine.
- Automated evidence is `content-os-data/latentsync-duration-boundary-20260915/talking-shorter-fresh-voice-result.json`. It supports a currently verified **2.58s** local fresh-Voice Talking segment, not a claim that 3–5s fresh-Voice segments are publishable.

### Next ready task

If the user wants a less conservative router boundary, open a separate adaptive product-routing package to test one natural fresh-Voice sentence between 2.58s (pass) and 5.12s (fail), with fresh Voice QA/U-Voice and then U-Talking. Otherwise route new local Talking hooks at or below the verified 2.58s duration and use B-roll, typography or a replaceable BYOK/remote Talking provider between hooks.

## Completed work package — Gate D2: fresh Voice + short Talking confirmation

- **Result: FAIL — PRODUCT_LIMITED.** The normal Job `55c7aed7-4138-4123-b2cf-c57954adfe96` with the full fresh 5.12s voice completed automated QA (0ms drift, no freeze), but the user found lip-sync progressively less aligned toward its end. It is not publishable and does not authorize assembly.
- Evidence: `content-os-data/latentsync-duration-boundary-20260915/talking-combined-fresh-voice-result.json` and `content-os-data/latentsync-duration-boundary-20260915/talking-runtime-data/assets/originals/4c51f2ba87e140208501d5ee3785b43b0726d7391dcab1bdd2cb41de1f20c902.media`.


## Completed evidence — Gate D1: natural Voice handoff

**Result: PASS**

- The CPU 32-step attempt was stopped after roughly 29 minutes with no WAV despite sustained computation and about 4.5GB private memory use. This is a runtime failure for the CPU route, not a Voice-quality decision.
- The same local model/reference/copy completed on CUDA in roughly 10 seconds. The user accepted its quality after independent Voice QA; it is the input evidence for Gate D2.

## Completed evidence — Gate C2: Talking capability-boundary search

**Result: CAPABILITY_BOUNDARY_ESTABLISHED**
**Primary implementation model: Terra**  
**Luna role: tests/docs/mechanical follow-up only**  
**Sol role: review only if the bounded evidence remains ambiguous after this package closes**

### Objective

Find the **highest practically usable continuous Talking duration** for the current LatentSync/runtime/hardware/material combination with the fewest repeated experiments. The goal is an evidence-backed product capability boundary, not a predetermined 8s/9s answer and not a mathematically exact threshold.

### Known bracket

- Lower bound: one short ordinary-material sample around **3s** was visually acceptable enough to continue.
- Upper bound: the **17.44s** sample failed publishability because visible lip-sync drift accumulated.

Treat this as an initial pass/fail bracket. Exact historical sample lengths remain evidence, not universal product constants.

### Closed runtime evidence

- The first adaptive midpoint probe used the first 9.74s of the existing 17.44s OmniVoice take, the same ordinary authorized Biyingjie reference and fixed LatentSync 1.5 20-step/guidance-1.5 runtime. The normal Job reported automated container/timing QA success, but its output is invalid for visual boundary measurement.
- Frame inspection found the raw LatentSync output was only 128 frames / 5.12s. The adapter's duration normalizer then used `tpad=stop_mode=clone` to extend the video to the 9.74s narration; `freezedetect` finds the final asset freezes from 5.08s. The UI-visible file is `content-os-data/latentsync-duration-boundary-20260915/talking-runtime-data/assets/originals/5fd60c2bbf62a8aa25abb6ad711407e2789ae98db02f9be1fed00ea6830c4aa6.media`.
- User review found the dynamic first part's lip motion basically acceptable, but that partial observation is not a full continuous Talking pass and does not move the known 3s lower bound.
- A separate ASR pass over the clipped prefix observed three substitutions (with zero missing/duplicate tokens), while the existing complete source take remains the verified Voice evidence. This was a visual-duration probe, not a new U-Voice pass.
- Historical runtime blocker: the adapter could mark a frozen, duration-padded output technically verified. The provider now probes raw output duration before normalization and rejects a response that cannot cover the driving narration; the real 9.74s rerun reproduced the upstream short response and was rejected before import.

The output-integrity repair is now complete: focused provider/QA tests pass, and a real rerun of the same 9.74s input reproduced the short raw response but failed before import/normalization. It produced no frozen asset. The current 5.12s raw-response ceiling is a runtime observation, not yet a visual product boundary. The next informative probe is the 4.78s prefix, the closest existing narration boundary below that observed ceiling.

### Reviewed artifact

- Review `content-os-data/latentsync-duration-boundary-20260915/talking-runtime-data/assets/originals/0aec97b33d361da95df3c99f6707b89d27b1a6785366d2b54c1d5632191a56a1.media`: a full 4.78s ordinary-material LatentSync 1.5 output using the same authorized reference, existing OmniVoice-take prefix and fixed 20-step/guidance-1.5 runtime.
- The normal local Job and automated QA passed: H.264/AAC, 1280×720, 20ms duration drift, zero missing/duplicate tokens, no freeze interval found by `freezedetect`, and estimated external cost USD 0. The raw-output coverage guard ran before normalization.
- U-Talking decision: the full segment was basically acceptable, with visibly weaker naturalness; it is recorded in the closure below.

### Closure — observed product-design range

- U-Talking judged the full 4.78s sample **basically acceptable**, with noticeably weaker naturalness. Treat it as a short-segment technical capability, not a claim of polished or long-form local Talking quality.
- For this exact authorized ordinary material, existing OmniVoice take, LatentSync 1.5 20-step/guidance-1.5 compatibility runtime and 6GB RTX 3060 Laptop GPU, use **about 3.5–4.8s per generated Talking segment** as the currently verified local design range. The raw runtime response reaches only about 5.12s; requests beyond it are now rejected before a frozen output can enter the product.
- The 17.44s sample remains a separate non-publishable failure; no claim is made for continuous local Talking beyond the short observed range. Naturalness remains a product-quality limitation even inside that range.

### Next ready task

Gate D/E may use the observed 3.5–4.8s range as Router/Scene Planner evidence, splitting longer narration into shorter Talking hooks with real B-roll, typography or capture between them. It must still obtain a fresh U-Voice decision before any product-gate claim and must not present OmniVoice/LatentSync official non-commercial weights as a commercial-safe default.

### Runtime-repair task contract

- **Objective:** reject a truncated raw LatentSync response before duration normalization can turn it into a technically valid frozen clip.
- **Allowed modules:** `services/api/app/providers/latentsync.py`, its focused provider tests, this status record and operator documentation only if configuration changes.
- **Stable interfaces:** provider-neutral `TalkingHeadProvider`, Job/provider-call accounting, portable Core media paths and configured LatentSync runtime selection.
- **Acceptance:** focused tests prove a full raw response reaches normalization, a truncated raw response fails before normalization, and one real local rerun either creates a continuously dynamic candidate or preserves a reproducible upstream-truncation failure.
- **Non-goals:** no new provider, Core-schema redesign, paid service, Voice re-generation, 30–60s render, or another duration-boundary claim until output integrity is established.

### Search rule

Use an **adaptive interval search**:

1. Keep provider/runtime/source material/inference settings fixed unless a technical blocker makes that impossible.
2. Reuse the existing narration when testing visual Talking duration so Voice generation is not a new variable.
3. Choose the next duration near the midpoint of the current known pass/fail bracket, adjusted only to a nearby clean speech boundary.
4. Run the normal provider path and automated Talking QA, then request U-Talking review.
5. If the sample passes visual U-Talking, move the lower bound upward to that duration.
6. If it fails visual U-Talking, move the upper bound downward to that duration.
7. Repeat only while another sample is likely to change a product decision.

### Stop precision

Stop the visual boundary search when **any** of these is true:

- the pass/fail bracket is within about **2 seconds**;
- the next midpoint would not materially change Scene Planner/Router behavior or market fit;
- two nearby durations produce inconsistent human results, indicating material/sample variance is larger than the duration difference;
- runtime/hardware variability prevents a clean duration comparison;
- the user explicitly judges the current evidence sufficient for product routing.

Do not chase a 0.1s or 1-frame theoretical maximum. This is a product capability estimate.

### Human-review focus

For every candidate duration, U-Talking should judge:

- lip motion alignment from start to end;
- identity retention;
- mouth/teeth artifacts;
- original motion/gaze/background retention;
- visual-quality retention;
- whether the result is publishable apart from any already-known Voice-quality concern.

### Product-path confirmation

After the visual search establishes a useful approximate upper bound:

1. Generate a **fresh OmniVoice take** at a natural sentence/phrase boundary near that capability range; do not force speech speed merely to hit a numeric target.
2. Run existing automatic Voice QA.
3. Require U-Voice review for timbre similarity, naturalness, pacing and breathing.
4. If U-Voice fails, close as `VOICE_LIMITED` and move the next package to Voice duration/prosody; do not keep tuning LatentSync.
5. If U-Voice passes, run one combined Talking job near the verified visual range, then automated QA and U-Talking.
6. If the combined result passes, record the observed range as the **currently verified product capability**. It is not a permanent global maximum and does not require every Talking scene to use that duration.
7. If it fails, close as `PRODUCT_LIMITED` with the concrete cause.

## Mandatory package exit states

The package must end in exactly one of these states:

- `CAPABILITY_BOUNDARY_ESTABLISHED`
- `VOICE_LIMITED`
- `PRODUCT_LIMITED`
- `BLOCKED_RUNTIME`
- `INCONCLUSIVE_VARIANCE`
- `AWAITING_U_REVIEW`

`AWAITING_U_REVIEW` is not an invitation to keep working. Once a requested output/evidence artifact is ready, update this file to that state, list the exact artifact(s), and **stop** until the user gives the human-quality decision. After that decision, update the bracket or final state and continue only if the current bounded search rule still allows another informative experiment.

## Acceptance / evidence

Before handoff or closure:

- preserve the existing short passing evidence and 17.44s failed evidence;
- record tested duration(s), current pass/fail bracket, provider/runtime settings and output references in local evaluation evidence or Git history, not as a long run log here;
- run the relevant automated Voice/Talking QA;
- if code changes are genuinely needed, run the smallest relevant pytest set plus `python scripts/check_docs.py`; run the Web build only if Web code changed;
- do not add another provider, redesign Core contracts, or start the 30–60s two-topic U-Product gate in this package.

## Next work after closure

- `CAPABILITY_BOUNDARY_ESTABLISHED`: Gate D/E integration should use the observed range as routing evidence, while allowing shorter scenes whenever editorially better.
- `VOICE_LIMITED`: open a Voice duration/prosody package.
- `PRODUCT_LIMITED`: review whether shorter Talking, hybrid editing, another provider or remote compute best matches market needs.
- `INCONCLUSIVE_VARIANCE`: improve test material/benchmark design before more duration probing.
- `BLOCKED_RUNTIME`: preserve the reproduction and stop; do not convert a runtime failure into a quality conclusion.

## Product quality gate

A Talking provider/path is admitted only when all are true:

- accepts ordinary authorized creator footage;
- speaks genuinely new verified narration;
- preserves creator identity sufficiently for publication;
- lip sync is visibly acceptable across its claimed capability range;
- does not unnecessarily replace original body motion/gaze/background;
- output quality is acceptable in the normal 9:16 Content OS assembly;
- runtime/cost/license constraints are explicitly known;
- human U-Talking review says the result is publishable.

## Documentation rule

`STATUS.md` records current truth, the one active package, its explicit state and next work. Per-run filenames, timestamps, cache/download history and retired-provider experiment logs belong in Git history or local evaluation evidence.
