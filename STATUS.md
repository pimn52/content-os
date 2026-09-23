# Content OS — Current Status

Updated: 2026-09-23

## Current truth

- Content OS remains an internal Alpha. R1 has not passed the repeatable new-topic → creator Voice → creator Talking → hybrid 30–60s export gate.
- Local-first media, project, routing, jobs, cost, narration, TalkingRun and rendering foundations exist. An admitted 17.24s source-forward TalkingRun exists, but creator Voice performance is the immediate R1 risk.
- R1 remains OmniVoice-first. Official OmniVoice pretrained weights are non-commercial evaluation material; commercial release admission is separate.
- `NarrationPerformancePlan` is editorial intent; `NarrationPerformanceUnit` is a semantic unit; `VoiceGenerationSpan` is a provider-call boundary. Current OmniVoice execution has no verified mapping from plan cues to in-take word focus, phrasing or rhythm.
- V17 cleared the apparent B Span copy-integrity failure as a Voice-QA ASR-timestamp false negative; all three retained takes covered the copy. The PCM-onset repair and evidence are in Git `a3ed505` and local QA jobs.
- V18 produced technically QA-verified A/B/C candidates, but the user withheld an overall U-Voice verdict. Read-only pause attribution is preserved in Git `7c029f6` and the AudioAsset/Job records.
- V19A repaired the Composer's additive seam silence. Its limited human review found that overall pause hierarchy and final-sentence delivery still need revision. B/C's creator-likeness gains and C's promising individual sentences remain valid evidence; no `LOCAL_PERFORMANCE_FAIL` or publishability pass is recorded.

## Active work package — Gate V19A: Pause-first structural composition

**State: PASS (`SEAM_ADDITIVITY_FIXED`; limited U-Voice review: `NEEDS_REVISION`)**

**Primary implementation model: Terra**

### Objective and scope

Derive exact-copy structural boundaries and keep Composer silence at a total seam budget. Recompose the existing B/C verified child takes only. Preserve the copy, plan, Span boundaries, authorized references and previous audio/QA evidence. Stable interfaces: `NarrationPerformancePlan`, `NarrationPerformanceUnit`, `VoiceGenerationSpan`, AudioAsset and Voice QA. Allowed files: narration/voice performance implementation, focused tests, this status and the Voice/Talking product spec.

Non-goals: new OmniVoice inference, Provider or reference change, in-take trimming, time-stretch, synthetic breath, 30–60s narration, Talking, paid/remote work, and claims of word-level focus or rhythm control.

### Technical result and review assets

The boundary map distinguishes terminal sentence close, continuing clause, forward-binding lead-in (`但是，` / `最后，`) and no-break positions. Composer accepts only a terminal seam and fills only the difference between the local profile's 420ms total `BEAT` budget and measured native PCM edge silence. It does not use PCM as copy evidence.

All listed candidates use the exact V16 copy and passed full-copy Voice QA. A is unchanged. B/C use their retained V18 child takes; no new Voice inference was run.

| Candidate | AudioAsset | Local WAV | Duration | Fresh QA |
|---|---|---|---:|---|
| A baseline | `aa0c666f-f976-44f5-97bc-ba154e9f6b83` | `content-os-data/assets/audio-originals/e23e0fedba3b7096868182189e669e1d4814e88f401625f288f43eda95715aa1.wav` | 12320ms | Verified existing QA |
| B pause-first | `8c85917b-0074-4013-98e2-72fe28428374` | `content-os-data/assets/audio-originals/a5f171390258bd614ac8cab38ebff9a763b9ec0f4632689d9f7fe0f83a0d0ba5.wav` | 12600ms | `7b980e03-3ee3-4f0c-b57a-e27c266eb2e3`, verified |
| C pause-first | `b31ee4eb-6c26-48a7-904c-2ccffe75f29e` | `content-os-data/assets/audio-originals/360f96540855f832dd9f3e1a7a48fa472bc87c1fe49d2ae88eaa344617236e26.wav` | 14160ms | `96289b95-96fc-49cb-9091-a2e308a71282`, verified |

B/C's designed seam after `继续听。` now measures 420ms rather than 720ms/710ms. The change does not alter any silence within a Provider take. Focused narration/Voice tests: 34 passed; documentation and diff checks passed at the original V19A handoff.

### Limited U-Voice finding

The user reports that A/C make `选题有没有判断，` feel more separated from `决定…` than the preceding full stop after `写文案。`. B makes the unpunctuated `选题` feel detached. C's sentence-to-sentence spacing remains too long, although `但是，结构决定这句话能不能被听懂` and `最后，让结论落下` sound comparatively good in isolation. All three need `最后，` to lead into the final claim, with `结论` prominent and `落下` given a controlled closing tail. No overall A/B/C quality ranking or terminal U-Voice decision has been made.

The waveform/ASR attribution constrains the remedy: A has no internal physical silence ≥100ms; B Span 0 has none after `选题`; C has a 210ms in-take gap near the comma after `判断`, plus 950ms/560ms gaps in Span 1. The C sentence seam is still 420ms. Perceived separation also depends on syllable duration, pitch contour, energy and the following phrase's onset; a silence-only rule cannot guarantee sentence > comma > unpunctuated word-group hierarchy. Exact text placement from diagnostic word timestamps remains approximate; PCM only establishes physical silence.

### Acceptance and exit

V19A passes only its scoped structural-boundary and additive-seam repair. The limited human review is `NEEDS_REVISION`; these candidates are not approved for Talking or publication. Historical failed QA and the original A/B/C files remain intact. No next package is active.

## Next-package boundary

The next bounded proposal should use the user's prosodic grouping: `先别急着写文案。` / `选题有没有判断，决定观众会不会继续听。` / `但是，结构决定这句话能不能被听懂。` / `最后，让结论落下。`. Boundary strength is perceptual, not a global silence threshold. Preserve C's good sentence delivery while assessing safe removal of verified excess silence at sentence joins; treat `最后` as a connected lead-in, `结论` as the principal focus and `落下` as a modest final cadence. An in-take edit or new inference requires a separate bounded package, fresh full-copy QA and U-Voice review. Do not infer word-level OmniVoice control from the semantic plan.

Only after creator Voice receives U-Voice approval may the first R1 end-to-end product gate start; repeat on a second topic before claiming R1.

## Current blockers

- No local Voice route has passed U-Voice for both creator identity and publishable content-aware delivery. V18/V19A have no terminal overall Voice verdict.
- V19A's seam repair cannot correct generated in-take phrasing or focus; an evidence-bounded product route for that capability remains to be established.
- OmniVoice official pretrained weights remain a non-commercial evaluation constraint for future commercial release.

## Documentation rule

STATUS contains only current truth, one active package, blockers and a short queue. Historical implementation evidence remains in Git or local evaluation records; current product behavior belongs in the module specs.
