# Content OS — Current Status

Updated: 2026-09-15 (Asia/Shanghai)

## Current truth

- The local media/runtime foundation is in place: persistent IP/draft revisions, provider accounting/idempotency, local media import and continuous clips, ASR/vision/index boundaries, ScenePlan/Hybrid Router, browser/mobile upload, MasterNarration/timeline work, Remotion rendering, Jobs/recovery and backup checks.
- The project is still an **internal Alpha**. It has not passed the R1 product gate: `new topic → creator voice → creator Talking/lip-sync → 30–60s new video`.
- Imported narration, source-led recuts and old mouth motion are engineering/fallback paths only; they do not prove repeated filming has been replaced.
- Ordinary, consented day-to-day creator footage is the required Talking input. Special silent/closed-mouth AI-only reference capture is not an acceptable product prerequisite.
- After syncing the latest GitHub history through `1afa5f3`, the requested verification was rerun: full pytest passes (`290 passed, 8 skipped`), `scripts/check_docs.py` passes, and `npm --prefix apps/web run build` passes. The two stale candidate-construction tests were aligned to the latest existing-media contract; the contract itself was not weakened.
- The first real VideoReTalking ordinary-material run completed on the authorized Biyingjie Chinese clip plus one continuous new narration take. The output is playable H.264/AAC, 1280×720, 3.48s; independent QA reports 3.48s target duration, 19ms mux drift, 100% target-copy coverage, and no missing/duplicate tokens. The isolated output also preserves the source clip's burned-in subtitles, which do not match the new narration. U-Talking review additionally found severe mouth deformation, blur and local scale/warp artifacts, so VideoReTalking is rejected and is not a publishable product provider.
- The pinned official LatentSync 1.5 benchmark now runs locally on the authorized ordinary Biyingjie reference plus one continuous new narration take. The official 5,072,348,184-byte U-Net, Whisper tiny, SFD and VAE assets are present in an isolated Python 3.10.13 / Torch 2.4.1+cu121 environment. On the 6GB RTX 3060 Laptop GPU, both a 4-step feasibility output and a 20-step / guidance-1.5 output completed at the model's 256px processing size, restoring to a playable 640×360 H.264/AAC clip of 3.2s without OOM. A normal-resolution source/output run also completed at 1280×720 with the same 3.2s output duration and no visible scale/blur failure in sampled frames; the monitored 20-step run peaked at 5,721 MiB used with 276 MiB free out of 6,144 MiB total (about 93.1%), showing that the internal model resolution, not output resolution, dominates GPU use. Windows lacked a usable SDPA kernel, so the evaluation used an explicit math-attention fallback in an eval-only runner; this is not a Core dependency. Automated playability/timestamp checks pass, and the user's U-Talking sample judgment was acceptable to continue (comparable to the prior sample and clearer). The full 30–60s product gate remains open.
- The first real provider-neutral LatentSync `GENERATE_TALKING` Job now completes on the ordinary Biyingjie reference and verified narration. The adapter caught the model's 16-frame duration quantization, padded only the model input, then restored the original 3.48s narration exactly in the final H.264/AAC output; the prior 3.2s-vs-3.48s assembly rejection is resolved. The Job, provider-call ledger and local asset import all complete, with local estimated external cost USD 0.
- The latest formal UI LatentSync Talking Job (`dc2eba7c-9355-4b9e-889e-820716295de1`) completed on the ordinary 72s Biyingjie Clip and the verified 17.44s OmniVoice narration. The production path now selects the LatentSync runtime FFmpeg explicitly because the bundled Remotion FFmpeg lacks the `tpad` filter required for duration normalization. The imported H.264/AAC asset is 1280×720 with video/audio both exactly 17.440s; independent Talking QA is verified with playable output, 0ms duration drift, 100% narration copy coverage, zero missing/duplicate tokens and 2 bounded ASR substitutions. U-Talking review rejected this current long-clip result for product use: perceived voice likeness regressed, the narration is too fast and lacks natural breathing, and visible mouth motion falls increasingly behind the speech as the clip runs. No further Talking/assembly job is being submitted pending Sol review.
- The same real generated asset passed independent automated Talking QA in the evaluation harness and a 6.742s 1080×1920 Remotion smoke render with three scenes: new Talking, authorized real B-roll and editable typography. A burned-subtitle review record propagated an explicit bottom-crop treatment into the vertical render, so stale source subtitles were not carried into the composed result. This is an integrated smoke proof, not the required 30–60s/two-topic R1 product gate.
- Transcript-aware Talking QA now independently probes output video/audio and requires upstream real Voice QA evidence: the latest formal UI LatentSync asset measured 0ms narration drift, 100% copy coverage and zero missing/duplicate tokens. The formal local UI exposes authorized Voice/Talking Job submission; Worker routing is optional and path-explicit for OmniVoice and LatentSync. Real Voice QA for the selected OmniVoice narration is complete and remains explicitly separate from the visual U-Talking gate.
- The selected ordinary Biyingjie reference Clip is now backed by an observed assessment: camera-facing start/end, engaged/neutral expression, burned-in subtitles. The UI only offers transcript-bearing Clips with an observed Talking assessment or explicit candidate flag, so the source subtitle crop is deliberate and traceable.
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
- **LatentSync 1.5 has completed the bounded local benchmark, but the latest 17.44s ordinary-material result is not publishable.** The earlier short/normal-resolution sample was acceptable to continue; the longer UI result exposed a voice-likeness regression, overly compressed delivery without breathing and cumulative lip-sync drift. Its pinned 1.5 README states that inference requires 6.5GB VRAM and uses 256px processing; this machine has only a 6GB RTX 3060, so the run used an eval-only attention compatibility fallback. A minimal optional benchmark-only Core adapter exists behind explicit runtime paths; this is not a commercial-safe default or a completed R1 product admission. Further strategy is paused for Sol review.
- **KeySync is deferred to a later compatible machine.** Its public checkpoints total about 24.6GB, and no KeySync inference has run locally. The local temporary environments and partial downloads have been removed.

## Hardware/runtime constraint

- The current development machine has an RTX 3060 Laptop GPU with 6 GiB VRAM.
- This machine is suitable for bounded local experiments, but Content OS must not require comparable GPU hardware for the minimum product path.
- For heavy Talking/lip-sync workloads, low-spec machines should be able to use a BYOK/remote provider once an acceptable provider is selected; local inference remains an optional path when hardware, privacy and quality justify it.

## Active work package — Gate E: U-Product path

Current objective:

> Pause at the U-Talking product gate for Sol review of the latest long-clip failure; do not run additional Talking or 30–60s product topics until the voice-speed/breathing and cumulative lip-sync strategy is decided.

Current candidate/evidence:

1. The isolated **LatentSync 1.5** benchmark is complete on the authorized ordinary Biyingjie material and verified single-segment creator narration. The evaluation evidence is under `content-os-data/latentsync-evaluation-20260915/`.
2. The user accepted the normal-resolution sample as sufficiently clear to continue. This is evidence for the next engineering package, not a claim that a 30–60s export is publishable.
3. The optional adapter preserves the existing `TalkingHeadProvider` contract, stages the authorized Clip interval, normalizes model-window duration without cutting verified narration, uses the durable Talking provider-call ledger and exposes local zero-external-cost plus model hardware constraints through readiness.
4. The real integrated smoke path `GENERATE_TALKING → automated QA → Hybrid timeline → Remotion` now passes on one short topic segment; its evidence includes a new Talking scene, real B-roll, editable typography, audio and explicit source-subtitle treatment.
5. The UI/Worker path is wired for optional local Voice and Talking providers. The selected OmniVoice narration has completed the local real-ASR Voice QA handoff with explicit copy, missing/duplicate, silence and bounded substitution evidence; the separate visual U-Talking gate remains open.
6. The latest UI/Worker Talking path completed after the provider-specific FFmpeg repair and passed independent automated technical QA, but U-Talking rejected the current long-clip result: likeness regressed, delivery was too fast/airless, and lip-sync drift accumulated over the 17.44s duration.
7. Pause here for Sol review. Do not rerun or extend the Talking benchmark until the review addresses whether the fix is provider configuration/segmentation or a candidate-quality failure; preserve this exact output as evidence.
8. After the Sol decision and a new human-approved Talking sample, run two real 30–60s topics through the normal UI/Job path and retain the human U-Voice/U-Talking decisions. The 17.44s run is a failed product-quality sample, not an admission.
9. If the integrated path fails at 30–60s or the local compatibility fallback cannot be made a supported runtime, retain the evidence and defer the **KeySync** benchmark to another compatible machine before expanding to unrelated avatar-generation models.
10. If local candidates fail the product bar or exceed realistic consumer hardware limits, move the default low-spec path to an external/BYOK Talking API rather than continuing indefinite local-model search.

The isolated VideoReTalking runner and output remain local failure evidence; they do not modify Core state or act as a quality proxy. LatentSync's compatibility fallback remains isolated from the supported adapter unless a configured runtime wrapper is explicitly selected.

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

**BLOCKED ON PRODUCT REVIEW: await Sol review of the recorded LatentSync long-clip failure (voice likeness, breathless speed and cumulative lip-sync drift). Do not submit another Talking or 30–60s job until the strategy is decided; then require a fresh U-Talking-approved sample before proceeding.**

## Documentation rule

`STATUS.md` records only current facts, active blocker/candidate and next work. Per-run filenames, timestamps, individual cache/download history and retired-provider experiment logs belong in Git history or local evaluation evidence, not here.
