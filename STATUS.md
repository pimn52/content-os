# Content OS — Current Status

Updated: 2026-09-22

## Current truth

- Content OS remains an internal Alpha. R1 has not yet passed a complete repeatable new-topic → creator Voice → creator Talking → hybrid 30–60s export flow.
- Creator/IP state, local media/library, continuous Clips, ScenePlan/Hybrid Asset Router, Jobs/recovery, cost/accounting, MasterNarration/VideoSpec and local render foundations exist.
- Capability profiles, deterministic parameter resolution, Advanced Settings and local GPU resource leasing are implemented foundations of Local-first Data + Hybrid Compute. Automatic cross-provider Local↔Remote selection is not yet complete.
- Current benchmark providers remain non-commercial evaluation paths where their model-weight licenses require it; no benchmark result is a commercial-readiness claim.
- E21 now exercises the normal local TalkingRun admission path: it produced a
  17.24s product Asset/Clip with the verified MasterNarration as final audio,
  retained child provenance and an immutable continuity decision. This is
  provider/source/machine-scoped execution evidence, not a universal claim.
- Sliced Talking preserves the product boundary: every child maps to one
  authorized source-forward continuous performance run; only a final slice may
  request a provider-specific terminal closeout.
- The largest remaining R1 risk is repeatable fresh 30–60s Master Narration.
  No local provider+runtime+machine profile has verified controllable
  emphasis, pace, pauses and rhetorical rhythm.
- The 43.04s composed MasterNarration candidate
  `449868d9-9b57-4f5a-9be8-5490f46fed82` passed technical Voice QA but has a
  durable U-Voice rejection (`u-voice:user-review-20260921-performance-control`):
  likeness passed; naturalness, emphasis, pace, pauses and rhythm need
  revision. It is a specific asset-quality result, not provider capability or
  a global learned preference.
- Narration Performance Plan is a provider-neutral, exact-copy-bound Draft
  object with editable emphasis/pace/pause/rhythm cues, immutable Voice-job
  snapshots and explicit adapter application receipts. Its delivery-plan
  compiler creates no timings, audio or provider parameters.
- The rhetorical delivery assistant returns deterministic, exact-copy-bound
  review suggestions for parallel claims, assertion boundaries, sentence roles
  and enumerations. Suggestions are ephemeral until explicitly edited/saved;
  they make no provider call or acoustic-quality claim.
- Gate V12 passed in an isolated local workspace: a creator can review
  anchored suggestions, explicitly save an editable plan, and safely clear
  both the candidate and saved plan by changing copy. No Voice provider call
  occurs in that flow.
- Gate V13 passed: the rhetorical assistant now uses Chinese, evidence-bounded
  review language for concept landing, claim/turn boundaries, local list
  movement and closing cadence. It still supplies semantic candidates only,
  with no acoustic formula or provider application claim.
- Gate V14 passed: a Voice adapter can preflight full, partial or unsupported
  performance-plan coverage. Partial/unsupported coverage now fails before a
  ProviderCall reservation or synthesis, preserving the existing full-plan
  provider path and preventing a pace/pause-only route from masquerading as a
  full delivery implementation.
- Gate V15 local baseline closed FAIL. The exact 2.12s local OmniVoice take
  `688d9eec-697d-4849-8b76-3a9bedd9907f` passed automated Voice QA but has a
  durable U-Voice rejection (`u-voice:user-review-20260922-local-baseline`):
  pace itself passed, but likeness, naturalness, emphasis, pauses and rhythm
  need revision. Its end has no obvious break. This is one source/profile/
  runtime result, not evidence that the four available source videos are
  insufficient.
- The narrow V10 route review found no documented full-plan route. Google Chirp
  3 Instant Custom Voice is a Chinese-capable, consented, remote/BYOK **partial**
  candidate for pace/pause only; it is allow-list-gated and priced per input
  character. Azure Personal Voice documentation likewise excludes word-level
  emphasis. Neither is admitted or locally verified.
- The installed OmniVoice benchmark exposes only global provider controls; it
  has no safe verified range-scoped mapping for the full performance plan.
  Its capability remains `unknown`, and its model-weight license remains
  non-commercial evaluation only.
- Documentation governance has been reset around a stable hierarchy: whole-product spec → module specs → architecture → current STATUS → durable decisions → implementation policy.

## Active work package — Gate V15: local existing-source Voice baseline

**State: FAIL**
**Primary implementation model: Terra**

### Objective

Use the existing authorized local creator reference, with no new recording and
no cloud transfer, to prove the normal local Voice worker can make one new
short baseline take. The result is a route-readiness and quality-check step
before any 30–60s MasterNarration attempt.

### Allowed modules

- local OmniVoice worker/reference-path boundary, its focused tests and
  evaluation evidence; active Voice module/control documents only.

### Stable interfaces

- existing NarrationPerformancePlan / NarrationDeliveryPlan contracts;
- V14 full/partial/unsupported preflight boundary and Voice job API;
- Draft copy invalidation, provider-neutral Voice jobs and U-Voice evidence;
- durable provider-call ledger/budget, consent and no-secret persistence.

### Acceptance criteria

- The existing consented VoiceProfile reference resolves beneath the local data
  root and the explicitly configured local worker starts without an inference
  request.
- One new short take has complete-copy Voice QA and is presented for an
  explicit U-Voice judgment; stop at that human review.
- Record the route as local OmniVoice benchmark evidence only. It makes no
  claim of 30–60s reliability or verified emphasis, pace, pause or rhythm
  control.

### Non-goals

- no remote media transfer, account/allow-list setup, credential storage or
  paid use;
- no automatic reduction of a full performance plan to a provider subset;
- no 30–60s MasterNarration, R1 complete-flow or repeatability claim.

### Review artifact

- `AudioAsset 688d9eec-697d-4849-8b76-3a9bedd9907f` — new local OmniVoice
  benchmark take for `选题先做判断，文案再搭结构。`; `Voice QA job
  ed054d11-9150-452e-8458-1073df697e33` verified complete copy coverage,
  playability, zero missing/duplicate tokens, 0ms leading silence and 120ms
  longest silence. U-Voice record
  `u-voice:user-review-20260922-local-baseline` is `needs_revision` for
  likeness, naturalness, emphasis, pauses and rhythm; pace is `pass`. It has
  no Narration Performance Plan and is ineligible for Talking or final
  assembly.

### Exit states

- PASS — one local baseline take is QA-complete and receives an explicit
  U-Voice judgment; the next separate package may then decide whether a longer
  local reliability experiment is warranted.
- FAIL — the local reference/worker/take cannot clear the bounded quality
  check; preserve evidence and close it.
- AWAITING_U_REVIEW — the exact short take and its QA evidence are ready for
  the user; stop until the user judges it.

## Short queue after V15

1. Reference-aware local Voice experiment — a separate bounded package may
   inventory and compare the existing authorized source-video candidates before
   any new local retake. It must not treat V15 as evidence that all four are
   inadequate or that an unverified performance plan works.
2. R1 end-to-end product gate — new topic → new Voice → TalkingRun → real
   B-roll/typography/subtitles → 30–60s render → one U-Product review.
3. Repeatability gate — repeat on a second topic before claiming R1 core flow.

## Current blockers

- No repeatable fresh long-form MasterNarration path has passed the product gate yet.
- No local provider+runtime+machine profile has verified controllable emphasis,
  pace, pauses or rhythm. The current semantic plan must remain distinct from
  provider application until evidence exists.
- The local OmniVoice benchmark remains non-commercial evaluation only and has
  no verified full performance-plan mapping. The active baseline is testing
  local reference/worker execution and audio quality, not delivery readiness.
- Any remote/BYOK route remains blocked until separately authorized with its
  consent/reference transfer, allow-list access and declared budget.
- The 43.04s candidate is formally U-Voice-rejected and cannot be retried in
  place.
- No current GitHub Actions status is available for the latest master; local test/build claims must remain tied to their recorded implementation evidence.

## Documentation rule

STATUS contains only current truth, one active package, blockers and a short queue.

Past package history does not remain here. Current product behavior belongs in the module specs; implementation history remains in Git/evaluation evidence.
