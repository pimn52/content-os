# Content OS — Current Status

Updated: 2026-09-14 (Asia/Shanghai)

## Current truth

- The local media/runtime foundation is in place: persistent IP/draft revisions, provider accounting/idempotency, local media import and continuous clips, ASR/vision/index boundaries, ScenePlan/Hybrid Router, browser/mobile upload, MasterNarration/timeline work, Remotion rendering, Jobs/recovery and backup checks.
- The project is still an **internal Alpha**. It has not passed the R1 product gate: `new topic → creator voice → creator Talking/lip-sync → 30–60s new video`.
- Imported narration, source-led recuts and old mouth motion are engineering/fallback paths only; they do not prove repeated filming has been replaced.
- Ordinary, consented day-to-day creator footage is the required Talking input. Special silent/closed-mouth AI-only reference capture is not an acceptable product prerequisite.

## Provider decisions

### Voice

- **OmniVoice** is the current local technical benchmark and has produced basically acceptable creator-voice samples after QA/retry work.
- OmniVoice official pretrained weights remain **CC-BY-NC**, so this is not a commercial-safe default.
- **Chatterbox is rejected** for the current R1 path after side-by-side creator-voice testing found clearly worse timbre similarity/naturalness than OmniVoice.
- Voice contracts, QA and generated-audio provenance remain provider-neutral.

### Talking

- **MuseTalk 1.5 is rejected** for the mature product path. On ordinary creator footage, visible lip movement/performance quality did not meet the product bar.
- The rejected MuseTalk runtime, model assets and generated evaluation media are not part of the active product path.
- Core retains only provider-neutral Talking contracts, Job/QA/reference-selection boundaries and human review gates. A candidate does not enter Core merely because it is being tested.
- **VideoReTalking is the active local candidate under isolated evaluation.** Runtime feasibility, dependency/model licenses, ordinary-material quality and U-Talking judgment are still unproven.
- Next external benchmark candidates, if needed after VideoReTalking: **KeySync**, then **LatentSync 1.5**. Do not install a broad list of avatar/talking models before these gates are evaluated.

## Hardware/runtime constraint

- The current development machine has an RTX 3060 Laptop GPU with 6 GiB VRAM.
- This machine is suitable for bounded local experiments, but Content OS must not require comparable GPU hardware for the minimum product path.
- For heavy Talking/lip-sync workloads, low-spec machines should be able to use a BYOK/remote provider once an acceptable provider is selected; local inference remains an optional path when hardware, privacy and quality justify it.

## Active work package — Gate C: Talking provider evaluation

Current objective:

> Determine whether an existing ordinary creator video plus QA-verified new creator narration can produce a publishable new Talking segment without materially damaging identity, original motion/gaze or visual quality.

Current candidate:

1. Finish **VideoReTalking** isolated installation/inference.
2. Use the same authorized reference material and the same verified creator narration used by the existing Talking benchmark.
3. Record output playability, audio-copy coverage, duration, peak/runtime feasibility and dependency/license evidence.
4. Run explicit U-Talking human review for:
   - lip sync;
   - creator identity retention;
   - original motion/gaze retention;
   - visual-quality retention;
   - mouth/teeth artifacts;
   - ordinary-material suitability;
   - "would publish directly" judgment.
5. If VideoReTalking fails, benchmark **KeySync** before expanding to unrelated avatar-generation models.
6. If local candidates fail the product bar or exceed realistic consumer hardware limits, move the default low-spec path to an external/BYOK Talking API rather than continuing indefinite local-model search.

## Product quality gate

A Talking provider is admitted only when all are true:

- accepts ordinary authorized creator footage;
- speaks genuinely new verified narration;
- preserves creator identity sufficiently for publication;
- lip sync is visibly acceptable;
- does not unnecessarily replace original body motion/gaze/background;
- output quality is acceptable in the normal 9:16 Content OS assembly;
- runtime/cost/license constraints are explicitly known;
- human U-Talking review says the result is publishable.

Automated ASR/playability checks are necessary but cannot substitute for the visual/human gate.

## Next ready task

**Finish VideoReTalking isolated evaluation. Do not add a VideoReTalking Core adapter before the U-Talking gate passes.**

If it passes:

- add a minimal `TalkingHeadProvider` adapter;
- keep model/runtime dependencies optional;
- add runtime readiness and cost metadata;
- run the normal `GENERATE_TALKING → QA → Hybrid timeline → Remotion` path.

If it fails:

- retain the failure evidence;
- remove isolated runtime/cache if no longer useful;
- move to KeySync benchmark without changing Core contracts.

## Documentation rule

`STATUS.md` records only current facts, active blocker/candidate and next work. Per-run filenames, timestamps, individual cache/download history and retired-provider experiment logs belong in Git history or local evaluation evidence, not here.
