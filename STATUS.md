# Content OS — Current Status

Updated: 2026-09-12 (Asia/Shanghai)

## Current truth

- Latest repository work has completed the main local media/runtime foundation: persistent IP/draft revisions, provider accounting/idempotency, local media import and continuous clips, ASR/vision/index boundaries, ScenePlan/Router, cost controls, browser/mobile upload, `MasterNarration` timeline work, Remotion rendering, Jobs/recovery and backup/release checks.
- The project is still an **internal Alpha**. It has not yet passed the product gate “new creator voice + new creator Talking/lip-sync + 30–60s new video”.
- Imported narration and source-led real-media renders are engineering/fallback paths, not proof that repeated filming has been replaced.
- TTS/voice cloning and Talking/lip-sync remain the next core capability area. Do not resume broad peripheral feature work before this uncertainty is tested.
- The local development machine previously probed as an RTX 3060 Laptop GPU with 6 GiB VRAM. This can justify bounded local experiments, but Content OS must retain a non-high-end-GPU fallback path.
- `MasterNarration` Web build and isolated browser regression are now verified: ScenePlan → candidate persistence → timed SRT binding → aligned master-track selection → three-scene VideoSpec completed through the UI. The regression fixed two actual S0 blockers: asynchronous forms retain their form element before reset, and route persistence strips UI-only `shoot_list` before submitting the strict `DraftRoute` contract. This is UI/timeline evidence only, not Voice/Talking quality evidence.
- Voice now has a provider-neutral persisted request/job boundary: a consented `VoiceProfile` plus credential-free copy/authorization input becomes `GENERATE_VOICE`; the handler reserves/reconciles `tts`, imports the local output as an `AudioAsset`, and persists provider/profile/job provenance. Generated narration is blocked from VideoSpec assembly until recorded QA is `verified`. The optional OmniVoice adapter is lazy and explicitly non-commercial-benchmark-only; no weight, dependency, credential, or paid-provider call has been used.
- Voice QA now consumes a real generated-file transcription/alignment result and persists provider/model evidence, copy coverage, missing/duplicate tokens, timed-segment gaps/long silence and basic local-file playability. Missing or fabricated semantic evidence is failed rather than treated as a fixture pass. It is unit-verified only; no creator sample or model inference has been run.
- U-Voice authorization now covers all four latest MP4 files in `C:\Users\ASUS\Downloads` and the first non-commercial model download. Their 10-second 24 kHz mono reference candidates are isolated under `content-os-data/omnivoice-evaluation/references`; originals remain unchanged. An isolated CPU OmniVoice 0.2.1 environment is installed because the CUDA 12.6 wheel transfer stalled. Official `k2-fsa/OmniVoice` model download then stalled under both Xet and standard unauthenticated Hub transport after small metadata files; no generated audio or semantic/likeness claim exists. Resume with the same environment/cache when a read-only HF token or viable network route is available.

## Active work package

### B — Voice Provider Spike + QA

Goal:

> Produce authorized new narration in the creator's voice through the provider-neutral job boundary, with repeatable QA and no change to core contracts.

Required implementation order:

1. `MasterNarration` Web build and browser regression are verified on the current branch;
2. add a provider-neutral Voice generation service/job if not already present;
3. add an optional OmniVoice adapter for **non-commercial local benchmark only**;
4. keep a schema-compatible commercial-safe local/cloud path;
5. use existing reference transcript/ASR where available;
6. persist generated `AudioAsset`/provider provenance through the existing authorization and budget/idempotency boundaries;
7. run automatic Voice QA before a narration is eligible for final render;
8. collect U-Voice human judgment for likeness/naturalness.

### OmniVoice acceptance for the spike

Test with authorized real samples where available:

- short clean reference (roughly 3–10s);
- reference extracted from historical creator media if suitable;
- Mandarin;
- Chinese/English mixed text where relevant;
- short and longer scene copy;
- numbers/proper nouns;
- repeated generation/retry.

Record:

- generated file and duration;
- target text and QA transcript/alignment;
- copy coverage / missing or duplicated text;
- long silence/clipping failure;
- provider/model/version;
- reference provenance;
- runtime/hardware notes;
- human likeness/naturalness result.

Do not treat a single successful WAV as provider acceptance.

## Next ready task

**Resume the authorized OmniVoice weight transfer with a viable Hub route, then run the non-commercial benchmark against the isolated references; collect actual QA evidence and human likeness/naturalness judgment.**

Default implementation tier: **Terra**, because this crosses provider, job, media asset, runtime readiness and QA boundaries. Luna may handle isolated UI/tests/docs after the interfaces are fixed. Sol is not needed unless a real architecture/security conflict appears.

## Blocking human inputs

Pause only if the next real generation requires information not already explicitly authorized:

- confirm which voice/face belongs to the creator;
- confirm the specific reference clips/audio may be used for voice/face generation;
- approve local model-weight download if it has not already been approved;
- judge voice/talking likeness and naturalness;
- approve first paid cloud/provider use or a budget increase.

Do not infer voice/face-cloning consent merely from general production-media permission.

## Provider/license boundary

- OmniVoice code: Apache-2.0.
- Official OmniVoice pretrained weights: currently CC-BY-NC; use only for non-commercial evaluation/benchmark unless a separate valid commercial path is obtained.
- Do not bundle those weights into commercial releases.
- Commercial-safe provider claims require source **and model-weight/dependency** license review.

## Model-cost routing

`Luna → Terra → Sol`

- Luna: isolated UI/CRUD/tests/docs/simple adapters.
- Terra: current Voice/Talking/provider/media/job work.
- Sol: architecture/security/critical quality gate or unresolved Terra reproduction only.

## Handoff checklist

Before ending an implementation package:

```text
1. Run relevant tests/builds.
2. Run: python scripts/check_docs.py
3. Update this STATUS.md with facts, active package and next ready task.
4. Update DECISIONS.md only if a durable choice changed.
5. Do not create another top-level review/freeze/handoff document.
```

For historical detail, use Git history. Current execution rules are in `CONTENT_OS_EXECUTION_SPEC.md`.
