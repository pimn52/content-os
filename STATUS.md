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
- V16 did **not** reach a performance-quality verdict. A baseline passed Voice QA, but B GenerationSpan 0 (`先别急着写文案。选题有没有判断，决定观众会不会继续听。`) produced three 6.48s takes that the former Voice QA blocked. No complete B/C candidate or U-Voice review exists.
- V17 closes `QA_ATTRIBUTION_PASS`: all three B takes contained the complete requested copy. The apparent copy-integrity block was a QA false-negative caused by treating a late ASR segment timestamp as physical leading silence; PCM waveform onset is 110ms for each take, below the 500ms limit.
- The three takes have distinct hashes, so they were not identical retry replays. The selected B reference window was a valid persisted 240–6240ms authorized interval with an exact transcript match to its adjacent source segments. The historical A baseline used the then-default first 240–2240ms segment instead; its exact runtime speed/step parameters were not persisted, but the reference difference is not implicated in the now-cleared QA blocker.
- V18 has complete A/B/C technical candidates and has completed its user-authorized, read-only pause attribution; no terminal performance verdict has been made. The first review finding is that pause treatment is the primary publishability problem across A/B/C and currently obscures their meaningful differences in likeness, rhythm and emphasis. B/C's relative likeness improvement over A remains useful evidence and must not be discarded as a failure.
- R1 remains **OmniVoice-first / one-provider-first**. Do not open another Voice provider merely because this bounded integrity issue occurred.
- OmniVoice official pretrained weights remain non-commercial evaluation material; R1 product usability and future commercial provider admission are separate decisions.

## Active work package — Gate V18: OmniVoice Performance A/B/C completion

**State: PASS (`PAUSE_ATTRIBUTION_COMPLETE`; no terminal U-Voice quality verdict)**
**Primary implementation model: Terra**

### Objective

The read-only pause-attribution phase is complete. Preserve its evidence and
await user direction on any separately scoped repair; do not create audio or
reach a final U-Voice verdict in this package.

### Allowed modules / stable interfaces

- `services/api/app/voice_performance.py`, Voice QA/import/composition services,
  durable Job/AudioAsset repositories and `STATUS.md` only as required for the
  experiment evidence.
- Keep `NarrationPerformanceUnit` as editorial semantics and
  `VoiceGenerationSpan` as the provider-call boundary. Do not alter Core Voice
  contracts, provider choice, the V16 test copy, Performance Plan, Span
  boundaries, OmniVoice parameters or B's uniform authorized reference.

### Execution

1. Reuse A asset `aa0c666f-f976-44f5-97bc-ba154e9f6b83`; confirm its exact
   V16 copy and existing verified Voice QA remain valid. Do not generate A.
2. Reuse B Span 0 from the first V17 re-QA pass:
   `7b9fd8d1-4ca5-45cb-9290-9e9f422c72f7`. Retain all old failed QA jobs and
   V17 attribution evidence; do not select a take by listening quality.
3. Generate only missing B Span(s), with the established Span boundary,
   unchanged OmniVoice settings and B's single authorized 240–6240ms reference
   window. Independently Voice-QA each child take.
4. Compose B in exact-copy order with `VoicePerformanceComposer`; apply only
   plan-owned pauses, no synthetic breath or time-stretch. Import a new B
   MasterNarration candidate and fresh-QA the complete master.
5. Complete C using exactly B's copy, Performance Plan and Span boundaries.
   Its sole core variable is authorized reference selection per
   GenerationSpan—not per Unit. Independently QA children, compose and fresh-
   QA the C master.
6. **Pause-attribution phase (user-authorized):** inspect only the retained
   A/B/C masters and child-take provenance. Use ASR/copy alignment to locate
   expected textual boundaries, and PCM/waveform only to measure silence at
   those aligned locations. Attribute each material pause as in-take,
   composition-boundary, or indeterminate; assess its *relative* syntactic
   hierarchy (sentence close > comma/forward-binding transition) without
   inventing a universal millisecond target.

### Acceptance / exit

- `AWAITING_U_REVIEW` — A, B and C are complete, independently and
  master-level Voice-QA-verified AudioAssets. List exact assets/files and stop
  for one U-Voice comparison: likeness, naturalness, content emphasis, pause
  placement, rhetorical rhythm, splice/continuity and publishability.
- `PASS` / `FAIL` — only the completed U-Voice review may determine
  `LOCAL_PERFORMANCE_PASS`, `REFERENCE_AWARE_PASS` or
  `LOCAL_PERFORMANCE_FAIL`; preserve the exact candidates and findings, and do
  not tune, regenerate, switch provider or open a follow-on package without
  explicit new scope.
- `BLOCKED` — an ordinary normal-flow QA, composition, import or provenance
  anomaly prevents one candidate; preserve evidence and stop. Do not open V19.
- `PAUSE_ATTRIBUTION_COMPLETE` — existing-asset evidence identifies the
  controllable layer for the material pause findings, or explicitly records
  that it cannot do so. Stop before proposing or generating a repair.

### V18 technical candidates and U-Voice result

All three candidates have the identical V16 test copy and a verified fresh
Voice-QA record (copy coverage 1.0; no missing, duplicate or substitution
tokens). No tuning, regeneration or Talking work may begin until this U-Voice
review is complete.

- **A — baseline:** AudioAsset `aa0c666f-f976-44f5-97bc-ba154e9f6b83`,
  `content-os-data/assets/audio-originals/e23e0fedba3b7096868182189e669e1d4814e88f401625f288f43eda95715aa1.wav`, 12320ms.
- **B — plan-driven / uniform reference:** AudioAsset
  `a4a9c9a5-6df4-47f2-94f2-aa185cb99a4b`,
  `content-os-data/assets/audio-originals/3b0ce5427fe9408a40804c1212efba3ef8563d85a07bfe9f2adf90860fa59b84.wav`, 12900ms.
  It reuses the first V17 re-QA passing Span 0, generates only missing Span 1,
  and composes with plan-owned 420ms boundary pause.
- **C — per-GenerationSpan reference:** AudioAsset
  `abcaf04e-6b04-41c8-9a08-b2ca6d697437`,
  `content-os-data/assets/audio-originals/7eb036b0184580756881ba71919b61b02ea5dc50f73832eb2cdadffe9f3045fb.wav`, 14450ms.
  It uses B's exact copy, plan and two-Span boundary; only authorized reference
  selection differs per Span (240–6240ms, then 6240–12240ms).

Required U-Voice comparison: creator likeness, naturalness, content emphasis,
pause placement, rhetorical rhythm, mechanical splice/continuity and
publishability. Only that review may determine `LOCAL_PERFORMANCE_PASS`,
`REFERENCE_AWARE_PASS` or `LOCAL_PERFORMANCE_FAIL`.

**U-Voice feedback is being attributed — no terminal verdict.** The technical
QA pass does not establish publishability. The review has found:

- **A:** the landings in “会不会继续听”、“这句话能不能被听懂” and “让结论落下”
  were too flat; the delivery did not make the intended point audible.
- **B:** remained flat and added pauses without a clear hierarchy. The pause
  after “选题” was longer than the preceding full-stop boundary, and the pause
  after “最后” made it unclear whether that transition belonged to the prior or
  following clause.
- **C:** used pauses that were too long for outward-facing short-form speech
  and sounded discontinuous, like unrehearsed thinking rather than an edited,
  publishable delivery.

The current review priority is pause hierarchy, not a final A/B/C ranking:
punctuation establishes the default syntax boundary (full stop stronger than
comma); a generated pause must clarify that hierarchy, never compete with it.
For this copy, “选题 / 判断 / 听”, “结构 / 听懂” and “结论落下” are the semantic
landings; transition and summary words such as “但是” and “最后” introduce the
following point rather than replace it. Once pause treatment no longer masks
the delivery, U-Voice can assess the remaining rhythm and emphasis/semantic
landing differences and rank publishability. For short-form production,
publishability—clear, rehearsed, efficiently paced delivery—outranks imitation
of everyday disfluency.

### Pause-attribution result — `PAUSE_ATTRIBUTION_COMPLETE`

Scope was the seven retained A/B/C master and child WAVs only. A local
word-timestamp ASR pass located approximate spoken boundaries; PCM at -45dB
in 10ms windows measured physical silence only. Neither PCM nor the diagnostic
alignment was used to re-decide copy correctness, and no asset or QA record was
changed.

- **A:** no internal physical silence of 100ms or more was found. Its perceived
  pause/flatness issue is therefore timing or prosodic landing, not a removable
  long PCM silence.
- **B:** the designed sentence-end seam after “继续听。” contains 720ms of
  physical silence: 190ms trailing silence in Span 0 + the composer's 420ms +
  110ms leading silence in Span 1. B Span 0 has no internal physical silence
  of 100ms or more, so the reported “选题” break is not a Composer join. B
  Span 1 has a 380ms in-take silence whose approximate ASR boundary overlaps
  “最后 → 让”; it is provider-take behavior, not composition behavior.
- **C:** the same designed seam is 710ms (190ms trailing + 420ms composed +
  100ms leading). C Span 0 contains a 210ms in-take silence before its final
  clause. C Span 1 contains 950ms and 560ms in-take silences; the latter is
  before its final “最后，让结论落下” sentence, while the former occurs in the
  opening lead-in region. These cannot be repaired by the Composer.

Therefore the Composer's additive seam silence is a concrete, controllable
pause-hierarchy defect, but it is not the only cause. Current OmniVoice
execution has no verified control for internal provider-take pauses, word-level
focus, sentence landing or rhythm; silence trimming alone would not repair A
or the material B/C findings.

### Tests

- focused Voice performance, Voice QA, generation and master-narration tests;
- `python scripts/check_docs.py` and `git diff --check` before handoff.

### Non-goals

- no fourth version, retry sweep or parameter tuning;
- no new Voice Provider/model, paid/remote call, 30–60s narration, Talking or
  creator recording;
- no `instruct` experiment or Performance Plan redesign;
- no second U-Voice revision cycle for these assets;
- waveform/PCM onset may establish physical leading silence only; ASR/copy
  alignment remains required for spoken-copy integrity.

## Next-package boundary

V18 is closed after its user-authorized, read-only pause attribution. It does
not claim a U-Voice quality pass or fail. The user has explicitly authorized a
separate, bounded V19A repair focused only on deterministic composition seams;
its result must not be used to claim control of in-take provider prosody.

## Current blockers

- No local Voice route has yet passed U-Voice for both creator identity and
  publishable content-aware delivery; V18's human review remains open.
- Composer seam silence and provider-take pause behavior are now separately
  attributed; no approved execution route yet controls the latter.
- OmniVoice official pretrained weights remain a non-commercial evaluation
  constraint for future commercial release.
- No current GitHub Actions status is available for the latest master; local
  test/build evidence remains tied to recorded implementation runs.

## Documentation rule

STATUS contains only current truth, one active package, blockers and a short queue.

Past package history does not remain here. Current product behavior belongs in the module specs; implementation history remains in Git/evaluation evidence.
