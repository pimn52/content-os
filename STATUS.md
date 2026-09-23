# Content OS — Current Status

Updated: 2026-09-23

## Current truth

- Content OS remains an internal Alpha. R1 has not passed the repeatable new-topic → creator Voice → creator Talking → hybrid 30–60s export gate. Creator Voice performance is the immediate R1 risk.
- Local-first media, project, routing, jobs, cost, narration, TalkingRun and rendering foundations exist. An admitted 17.24s source-forward TalkingRun exists.
- R1 remains OmniVoice-first. Official OmniVoice pretrained weights are non-commercial evaluation material; commercial release admission is separate.
- `NarrationPerformancePlan` is editorial intent; `NarrationPerformanceUnit` is semantic; `VoiceGenerationSpan` is a provider-call boundary. Current OmniVoice execution has no verified mapping from plan cues to in-take word focus, phrasing or rhythm.
- V17 cleared B copy QA attribution (Git `a3ed505`); V18 produced technical A/B/C without an overall U-Voice verdict (Git `7c029f6`); V19A repaired Composer seam silence (Git `1fe6f5f`). V19B–E made one C-derived candidate each, preserving original media, manifests and independent QA evidence.
- User U-Voice accepted V19E's pause/rhythm as “基本可以” and explicitly authorized moving to emphasis/tail. **V19E: PASS for this bounded pause-repair objective only.** V19E AudioAsset `041ca938-6d0c-4346-8b2c-c1652d326b40` (9.77s) has independent full-copy Voice QA job `89b3d9e5-e6c7-4573-b492-498657d9c321` verified. This is not an overall U-Voice approval, creator-likeness verdict or release admission.
- Local V19E word alignment: `判断` ≈3.14–3.96s, following `决定` ≈3.96–4.46s; their whole-range RMS is about -23.4 versus -16.2dBFS, with a weak extended `断` tail. `结构` and `结论` are already relatively prominent in level. `落下` ≈9.24–9.66s fades quickly. Word timings and acoustic measures are diagnostic estimates, not proof of perceived emphasis.

## Active work package — Gate V20A: content-focus and landing pilot

**State: AWAITING_U_REVIEW**

**Primary implementation model: Terra**

### Objective

Produce exactly one V19E-derived technical candidate that tests whether controlled local focus balancing can make `判断` lead into `决定` without an accent jump, while giving `结论 / 落下` a clearer but publishable landing. Preserve the accepted punctuation rhythm. Stop for U-Voice after fresh QA; do not claim word-level semantic performance capability from DSP alone.

### Allowed files / stable interfaces

- Allowed: one evaluation-only PCM/prosody edit script and real-WAV tests, existing AudioAsset import and Voice QA jobs, `STATUS.md`.
- Stable: exact copy, V19E and prior source media/evidence, authorized creator voice lineage, AudioAsset/Voice QA contracts, current plan's punctuation/rhythm cues. Add user-directed exact-copy cues for `结论` emphasis and locally measured `落下` pace; retain other cues.

### Bounded method

1. Verify V19E source hash, QA, aligned word windows, level/headroom and local FFmpeg `atempo` availability. One explicit candidate only: smooth gain balance on the weak `断` core and over-prominent first `决定`, a modest `结论` lift, and a small pitch-preserving local duration increase for `落下`. No global speed or pitch change. Do not amplify near-clipping `判`; do not indiscriminately boost already-prominent `结构`.
2. Preserve every PCM sample outside declared word windows/edge envelopes except the time shift following `落下`; log exact windows, gains, output hash and plan provenance. Apply conservative peak ceiling, reject invalid/empty transformed media, and keep V19E unchanged.
3. Import one new AudioAsset and run one fresh independent full-master Voice QA. If copy, playability, duration/silence or edit safety fails, set `BLOCKED`; no second DSP settings or provider attempt inside this package.

### Acceptance / exit

- `AWAITING_U_REVIEW`: candidate passes fresh technical QA; list it beside unchanged V19E. Ask whether `判断` now owns the claim without `决定` sounding artificially suppressed, whether `结论` lands and `落下` has a natural controlled tail, and whether creator likeness/publishability survive. Stop immediately.
- `BLOCKED`: safe edit or fresh QA fails; preserve evidence and stop.

Tests: real WAV sample/peak/envelope and duration tests; Performance Plan validation; independent Voice QA; `python scripts/check_docs.py`; `git diff --check`.

Non-goals: a second candidate, new OmniVoice generation, another provider/reference, synthetic breath, 30–60s, Talking, paid/remote work, automatic word-level emphasis claims, or product-wide gain/tempo defaults.

### V20A result — stop for U-Voice

- New derived C AudioAsset `03c3cc12-a0a3-4c55-996f-c190babc182a`: `content-os-data/assets/audio-originals/89e6c19228d3ac6c7006ed5cf87964e0a964ad5fc3117e928bf6bbf84e185ab4.wav` (9.843s). Compare against unchanged, pause-approved V19E AudioAsset `041ca938-6d0c-4346-8b2c-c1652d326b40`: `content-os-data/assets/audio-originals/1b154a71df688e2701c491888425d5ee15e0e0fc198f5116c7c5775d0d7408c2.wav` (9.77s). Both retained hashes match the media.
- Evaluation-only intervention: smooth +3dB on the `断` core (3380–3550ms), -2dB on the first `决定` (3960–4460ms), +1dB on `结论` (8960–9240ms); each has a 20ms gain ramp and remains below the peak ceiling. `判` was not boosted because it already approaches peak headroom; `结构` was left alone because it is already relatively prominent. The `下`-onset-to-tail window (9460–9670ms) received only local pitch-preserving FFmpeg `atempo=0.65` with a 10ms crossfade, increasing master duration by about 73ms. No pause edit, provider call, new reference or speech synthesis. Updated exact-copy plan adds `结论` emphasis and measured `落下` pace; this is intent/provenance, **not** an OmniVoice application receipt.
- Fresh independent full-master Voice QA job `7557e7ea-a1f3-4682-9383-ae523028f270` completed on attempt 1: playable, 100% copy coverage, zero missing/duplicate/substituted tokens, QA `verified`; human review pending. File peak remains about -0.7dBFS. Exact windows, transformed tail frames and source lineage: `content-os-data/evaluation-evidence/v20a-c-focus-landing/v20a-c-focus-landing.json` and derived AudioAsset metadata.
- U-Voice question: versus V19E, does `判断` carry the key claim without `决定` sounding artificially suppressed? Do `结论` and `落下` now land naturally, or does the stretched tail sound processed? Did creator likeness, pauses or publishability regress? Technical QA does not answer these questions. Stop here; do not claim word-level performance capability or overall Voice PASS.

## Next-package boundary

Stop for V20A U-Voice. If level/tail editing cannot deliver true content-aware focus, record that limit and open a separate bounded OmniVoice delivery-generation experiment only after review, not a blind DSP grid. Only after an overall creator Voice approval may first R1 end-to-end and second-topic repeatability proceed.

## Current blockers

- No local Voice route has passed U-Voice for both creator identity and publishable content-aware delivery. V19E's scoped pause approval is not overall U-Voice.
- OmniVoice semantic plan application and word-level focus remain unverified.
- OmniVoice official pretrained weights remain a non-commercial evaluation constraint for future commercial release.

## Documentation rule

STATUS contains only current truth, one active package, blockers and a short queue. Historical implementation evidence remains in Git or local evaluation records; current product behavior belongs in the module specs.
