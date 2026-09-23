# Content OS — Current Status

Updated: 2026-09-23

## Current truth

- Content OS remains an internal Alpha. R1 has not passed the repeatable new-topic → creator Voice → creator Talking → hybrid 30–60s export gate. Creator Voice performance is the immediate R1 risk.
- Local-first media, project, routing, jobs, cost, narration, TalkingRun and rendering foundations exist. An admitted 17.24s source-forward TalkingRun exists.
- R1 remains OmniVoice-first. Official OmniVoice pretrained weights are non-commercial evaluation material; commercial release admission is separate.
- `NarrationPerformancePlan` is editorial intent; `NarrationPerformanceUnit` is semantic; `VoiceGenerationSpan` is a provider-call boundary. Current OmniVoice execution has no verified mapping from plan cues to in-take word focus, phrasing or rhythm.
- V17 cleared B copy QA attribution (Git `a3ed505`); V18 produced technical A/B/C without an overall U-Voice verdict (Git `7c029f6`); V19A repaired Composer seams (Git `1fe6f5f`). V19B–E and V20A made bounded C-derived candidates with original media, manifests and independent QA retained (Git `418be1d`).
- User U-Voice accepted V19E's pause/rhythm as “基本可以”; **V19E: PASS for bounded pause repair only**. Its AudioAsset `041ca938-6d0c-4346-8b2c-c1652d326b40` is technical-QA verified. This is not overall Voice approval.
- V20A AudioAsset `03c3cc12-a0a3-4c55-996f-c190babc182a` had 100% technical copy QA. U-Voice found its +3dB `断` / -2dB first `决定` balance unnatural and still unable to make `判断` carry the claim; this gain-based **focus experiment FAILS**. Its `结论落下` landing and tail improved somewhat and sounds reasonably natural. Do not label the whole Voice route failed or claim an OmniVoice plan receipt.
- Physical -45dBFS quiet between `判断，决定` in both V19E and V20A is only about 40ms. The perceived accent jump is from the original contour and the failed level manipulation, not a long inserted silence. Another small silence cut is unlikely to fix it and may make the join harsher. This is a local diagnostic, not a global pause rule.

## Active work package — Gate V20B: focus-gain rollback with retained landing

**State: AWAITING_U_REVIEW**

**Primary implementation model: Terra**

### Objective

Produce exactly one derived C candidate that removes V20A's rejected `判断` boost and first `决定` attenuation, while retaining only the positively reviewed `结论` lift and `下` tail. This isolates whether the useful landing can coexist with a natural first-clause baseline. Keep the V19E pause/rhythm intact and stop for U-Voice.

### Allowed files / stable interfaces

- Allowed: one focused evaluation script/test reusing the fixed V20A audio functions, existing AudioAsset import/Voice QA jobs, `STATUS.md`.
- Stable: exact copy, V19E/V20A and earlier media/QA, authorization/reference lineage, AudioAsset/Voice QA contracts. No rewrite of the V20A source or its failed-focus evidence.

### Bounded method

1. Verify V19E hash and QA. Derive from V19E, not by reversing lossy V20A media. Apply only the unchanged V20A `结论` +1dB smooth window and local `下`-tail pitch-preserving `atempo=0.65` transform. Do not touch `判断`, either `决定`, `结构` or any pause.
2. Retain the user-directed `结论` emphasis / measured `落下` intent in provenance but explicitly mark acoustic work as evaluation-only, not a provider application receipt. Import one new AudioAsset with source hash and exact edit windows.
3. Run one fresh independent full-master Voice QA. If clipping, copy, playability, duration/silence or import fails, set `BLOCKED` and stop without another parameter variant.

### Acceptance / exit

- `AWAITING_U_REVIEW`: one candidate passes technical QA; list it beside V19E and V20A. Ask whether removing the artificial gain restores a natural `判断，决定` transition while preserving the liked final landing. Stop.
- `BLOCKED`: the fixed derivation or fresh QA fails; preserve evidence and stop.

Tests: real WAV sample-preservation/peak/tail tests, fresh Voice QA, `python scripts/check_docs.py`, `git diff --check`.

Non-goals: another pause trim, a second candidate, new OmniVoice generation, alternate provider/reference, semantic word-focus claims, 30–60s, Talking, paid/remote work or product-wide gain/tempo defaults.

### V20B result — stop for U-Voice

- New C-derived AudioAsset `8c7d47a6-480d-48c6-a912-18559ab6e0c1`: `content-os-data/assets/audio-originals/0723ff6a1fe3a815b9eee1f5af2d10bb7d03d2cdb1bed8ad305f48752880ae1a.wav` (9.843s). Compare with pause-approved V19E `041ca938-6d0c-4346-8b2c-c1652d326b40` at `content-os-data/assets/audio-originals/1b154a71df688e2701c491888425d5ee15e0e0fc198f5116c7c5775d0d7408c2.wav` and rejected-focus V20A `03c3cc12-a0a3-4c55-996f-c190babc182a` at `content-os-data/assets/audio-originals/89e6c19228d3ac6c7006ed5cf87964e0a964ad5fc3117e928bf6bbf84e185ab4.wav`.
- This is a clean single-variable rollback: V20B PCM through 8960ms is **byte-identical to V19E**; V20B PCM from 8960ms onward is **byte-identical to V20A**. Thus the 40ms physical `判断，决定` gap and original first-clause contour are retained, while only the liked +1dB `结论` and locally stretched `下` tail remain. No new pause trim or provider inference. All three retained file hashes match.
- Fresh independent full-master Voice QA job `292ba11c-41ac-4562-a597-579297f8b7f3` completed on attempt 1: playable, 100% target-copy coverage, zero missing/duplicate/substituted tokens, QA `verified`; human quality pending. Exact edit and comparison provenance: `content-os-data/evaluation-evidence/v20b-c-landing-only/v20b-c-landing-only.json` and derived AudioAsset metadata. This does not establish phrase-level semantic focus or a provider plan-application receipt.
- U-Voice question: is V20B's restored plain `判断，决定` transition more natural than V20A, while the `结论落下` improvement survives? Does it still need a newly generated phrase-level contour before an overall publishability approval? Stop here; no automatic Voice PASS.

## Next-package boundary

Stop for V20B U-Voice. The next unresolved capability is phrase-level *intonation*: `选题有没有判断` should set up the logical relation and `决定观众会不会继续听` should carry through toward `听`, without loudness tricks. A subsequent bounded OmniVoice delivery experiment would need explicit generation/copy QA and U-Voice; do not silently treat a shortened 40ms physical gap as a substitute. Only after overall creator Voice approval may first R1 end-to-end and second-topic repeatability proceed.

## Current blockers

- No local Voice route has passed U-Voice for both creator identity and publishable content-aware delivery. V19E's pause approval and V20A's partial tail gain are not overall U-Voice.
- OmniVoice semantic plan application and phrase-level intonation remain unverified.
- OmniVoice official pretrained weights remain a non-commercial evaluation constraint for future commercial release.

## Documentation rule

STATUS contains only current truth, one active package, blockers and a short queue. Historical implementation evidence remains in Git or local evaluation records; current product behavior belongs in the module specs.
