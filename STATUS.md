# Content OS — Current Status

Updated: 2026-09-14 (Asia/Shanghai)

## Current truth

- The local media/runtime foundation is in place: persistent IP/draft revisions, provider accounting/idempotency, local media import and continuous clips, ASR/vision/index boundaries, ScenePlan/Hybrid Router, browser/mobile upload, MasterNarration/timeline work, Remotion rendering, Jobs/recovery and backup checks.
- The project is still an **internal Alpha**. It has not passed the R1 product gate: `new topic → creator voice → creator Talking/lip-sync → 30–60s new video`.
- Imported narration, source-led recuts and old mouth motion are engineering/fallback paths only; they do not prove repeated filming has been replaced.
- Ordinary, consented day-to-day creator footage is the required Talking input. Special silent/closed-mouth AI-only reference capture is not an acceptable product prerequisite.
- After syncing `origin/master` at `e19efdf`, the requested verification was rerun: full pytest passes (`342 passed, 7 skipped`), `scripts/check_docs.py` passes, and `npm --prefix apps/web run build` passes. The two stale candidate-construction tests were aligned to the latest existing-media contract; the contract itself was not weakened.
- The first real VideoReTalking ordinary-material run completed on the authorized Biyingjie Chinese clip plus one continuous new narration take. The output is playable H.264/AAC, 1280×720, 3.48s; independent QA reports 3.48s target duration, 19ms mux drift, 100% target-copy coverage, and no missing/duplicate tokens. The isolated output also preserves the source clip's burned-in subtitles, which do not match the new narration. U-Talking review additionally found severe mouth deformation, blur and local scale/warp artifacts, so VideoReTalking is rejected and is not a publishable product provider.
- KeySync has not been inferred locally: its temporary Python environments, partial model downloads and the 378MB WavLM component were removed after confirming that the current 6GB GPU cannot run its native path. Its source/evaluation decision remains available for a later compatible machine.

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
- **VideoReTalking is rejected for the mature product path.** Runtime and automated QA passed, but ordinary-material U-Talking review found severe mouth deformation, blur and local scale/warp artifacts. It is not admitted to Core.
- **LatentSync 1.5 is the active next isolated benchmark candidate** in a compatible CUDA environment. Its official README states an 8GB minimum for inference; this machine has only a 6GB RTX 3060, so no local run is planned. Do not install a broad list of avatar/talking models before this gate is evaluated.
- **KeySync is deferred to a later compatible machine.** Its public checkpoints total about 24.6GB, and no KeySync inference has run locally. The local temporary environments and partial downloads have been removed.

## Hardware/runtime constraint

- The current development machine has an RTX 3060 Laptop GPU with 6 GiB VRAM.
- This machine is suitable for bounded local experiments, but Content OS must not require comparable GPU hardware for the minimum product path.
- For heavy Talking/lip-sync workloads, low-spec machines should be able to use a BYOK/remote provider once an acceptable provider is selected; local inference remains an optional path when hardware, privacy and quality justify it.

## Active work package — Gate C: Talking provider evaluation

Current objective:

> Determine whether an existing ordinary creator video plus QA-verified new creator narration can produce a publishable new Talking segment without materially damaging identity, original motion/gaze or visual quality.

Current candidate:

1. Run an isolated **LatentSync 1.5** benchmark in a compatible CUDA environment with at least the provider's stated 8GB inference minimum, using the same authorized ordinary reference material and verified single-segment creator narration; the first derived input is the Biyingjie Chinese clip at `819,000–824,500ms` plus `gate-d-take-02.wav`. If the environment is remote, media transfer must be explicit.
2. Record output playability, audio-copy coverage, duration, peak/runtime feasibility and dependency/license evidence.
3. Run explicit U-Talking human review for:
   - lip sync;
   - creator identity retention;
   - original motion/gaze retention;
   - visual-quality retention;
   - mouth/teeth artifacts;
   - ordinary-material suitability;
   - "would publish directly" judgment.
4. If LatentSync 1.5 fails, retain the evidence and defer the **KeySync** benchmark to another compatible machine before expanding to unrelated avatar-generation models.
5. If local candidates fail the product bar or exceed realistic consumer hardware limits, move the default low-spec path to an external/BYOK Talking API rather than continuing indefinite local-model search.

The isolated VideoReTalking runner and output remain local failure evidence; it does not modify Core state or auto-approve the human gate. The next benchmark must use a separate isolated runner and must not reuse VideoReTalking quality as a proxy.

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

**Run LatentSync 1.5 only in a compatible CUDA environment; the current 6GB laptop cannot satisfy its stated 8GB inference minimum. Do not add a VideoReTalking, KeySync or LatentSync Core adapter before the U-Talking gate passes.**

If LatentSync 1.5 passes:

- add a minimal provider-neutral `TalkingHeadProvider` adapter;
- keep model/runtime dependencies optional;
- add runtime readiness and cost metadata;
- run the normal `GENERATE_TALKING → QA → Hybrid timeline → Remotion` path.

If LatentSync 1.5 fails:

- retain the failure evidence;
- defer KeySync to a later compatible CUDA machine without changing Core contracts.

## Documentation rule

`STATUS.md` records only current facts, active blocker/candidate and next work. Per-run filenames, timestamps, individual cache/download history and retired-provider experiment logs belong in Git history or local evaluation evidence, not here.
