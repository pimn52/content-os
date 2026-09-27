# Creator Voice and Talking

## Product responsibility

Create **new words in the authorized creator's voice and face** without requiring the creator to re-record complete narration for every video.

Voice and Talking are separate provider-neutral capabilities with separate quality gates.

## User outcome

From new copy, Content OS should be able to produce:

1. a verified creator-voice Master Narration; and
2. one or more creator-visible Talking runs that speak the new narration and can be used like normal production media.

## Voice system

For normal Voice generation, a consented `VoiceProfile` can enumerate its
currently authorized, transcript-backed reference windows through
`GET /voice-profiles/{profile_id}/reference-windows`. A caller may carry one
returned source-bound `selection` into `POST /projects/{project_id}/voice-jobs`.
The Job persists that exact window, and the OmniVoice worker rechecks profile
consent, Clip authorization, transcript identity and source-file hash before
using it. The generated AudioAsset records the selected window as provenance;
independent Voice QA and U-Voice still decide technical correctness and
publishability. Omitting `reference_window` keeps the existing provider-default
path. This is an explicit human/application choice, not an automatic acoustic
quality rule. Selected reference and Draft performance-plan application cannot
yet be combined in one normal Job; the API rejects the combination explicitly.

A normal Voice Job may optionally include a provider-facing `delivery_text`
alongside the editorial `text`. This is deliberately narrow: the variant may
only delete punctuation or whitespace already present in the editorial copy,
and its spoken token sequence must remain identical. The API validates it at
submission and the Worker validates it again before provider reservation.
The provider receives the variant, while the generated AudioAsset retains
both strings and independent Voice QA remains anchored to the exact editorial
copy. Omitting it leaves the existing one-text route unchanged. This is not
an automatic punctuation policy, a freeform rewrite, or verified control of
pause/rhythm; a Draft performance plan cannot yet be combined with a delivery
variant in one normal Job.

### Core flow

```text
new copy
→ editable narration performance intent
→ Voice execution
→ generated take(s)
→ independent Voice QA
→ optional take composition
→ verified Master Narration
→ U-Voice
```

Long narration does not have to be generated in one provider call. A provider may be more reliable with bounded natural sentence/phrase takes. A semantic
`NarrationPerformanceUnit` is not itself a provider call: the Voice execution
layer may merge adjacent Units into an adapter/profile-scoped
`VoiceGenerationSpan`, while retaining each Unit's exact-copy and delivery-cue
provenance. Its preferred size is local capability evidence, not a universal
product seconds limit. Content OS may compose only independently verified Span
takes and must QA the resulting master again.

Normal Master composition has an explicit measured-quiet seam option. It
operates only at known joins between ordered, independently QA-verified takes:
for 24 kHz mono PCM16, it may remove the interior of a measured low-energy
interval spanning a join while retaining a margin on both sides. Absent or
ambiguous quiet fails the option instead of inventing a speech boundary.
The unmodified raw hash, final hash, child hashes and exact cuts are retained;
ordinary concat remains the default. This is a small publishability edit at a
known assembly seam, not an intra-take pause detector or proof that words are
correct. The compacted Master remains QA-pending until fresh full-copy QA and
asset-specific U-Voice review.

The composition boundary persists the ordered source-take IDs and provisional
timing on a QA-pending candidate. Its final timing and eligibility are replaced
only by fresh independent master-level Voice QA; the candidate is not eligible
for VideoSpec, Talking or render while pending.

Each GenerationSpan uses bounded generate → independent copy-QA attempts. A
failed attempt remains a distinct provenance record and is never overwritten;
the first complete QA-passing take is the only eligible input to composition.
If the adapter/profile exhausts its configured bounded attempts, the route is a
generation-integrity block, not evidence about U-Voice performance quality.

### Voice QA

At minimum:

- requested-copy coverage;
- missing / duplicate / excessive substitution checks;
- duration and silence sanity;
- playable audio;
- provider/reference provenance.

### Voice performance observation

For a QA-verified narration AudioAsset with its exact requested copy, the
normal audio contract can create and read a source-hash-bound performance
observation. This is **measurement evidence**, separate from the editable
Performance Plan (intent), an adapter application receipt, automated copy QA,
and U-Voice (human quality judgment).

The observation retains the independent ASR source and alignment state,
structural sentence/clause boundaries, decoded PCM quiet intervals, apparent
sentence pace and relative adjacent-sentence pace. A quiet interval is a
waveform measurement, not proof of the spoken words or of a breath; an ASR
segment gap is not automatically physical silence. Text-boundary timing is
unavailable when exact ordered ASR tokens cannot be aligned or when the
boundary falls inside one coarse ASR segment. A punctuation-hierarchy flag is
only a review prompt when both compared pauses are physically measurable.
When measured quiet crosses a contiguous ASR segment edge, it is exposed only
as a low-confidence nearby candidate, excluded from that hierarchy flag.

The observation is idempotently persisted on the AudioAsset with the source
hash and measurement limits. It does not rewrite media, alter Voice QA/U-Voice
eligibility, apply an acoustic correction or claim that emphasis, intonation,
naturalness or publishability were achieved. Rates count normalized copy
tokens for within-copy comparison, not universal words-per-minute targets.
An explicit unsupported/uncertain result is preferable to invented word-level
timing or focus.

When independent local Voice QA supplies word timestamps, they are retained
as separate source-hash-bound ASR evidence. A new observation may use exact
ordered normalized word tokens to locate punctuation between words; malformed,
overlapping, stale or non-matching word streams make that alignment unavailable.
Only measured PCM quiet wholly inside the matched adjacent-word gap is a
definite boundary measurement. Quiet crossing an ASR word edge remains a
candidate, because estimated word timestamps can extend into real silence.
This evidence never upgrades copy QA, human review or edit eligibility, and
older idempotent observations are not silently overwritten.

For older QA-verified assets without retained words, a project-scoped local
Job can backfill this evidence through the configured local ASR Worker. The
request is idempotent and source-hash-bound; it records its ASR ProviderCall,
word stream and boundary alignment separately from the original Voice QA,
U-Voice and performance observation. A normal read endpoint exposes the
result. Missing/mismatched words remain explicit unavailable evidence. This
backfill is not a second copy-verification pass and does not make the audio
eligible for an edit or release. In particular, an ASR implementation that
places adjacent word timestamps at the same instant provides no measured
word-gap duration even when PCM reveals quiet nearby.

### Conservative whole-take pace candidate

For an authorized, same-project narration that has already passed independent
Voice QA, the normal product API can request one explicit `gentle_slower`
candidate. A local Job/Worker applies a pitch-preserving whole-take tempo
factor drawn from a bounded reviewed comparison, never rewriting the source
or pretending to apply the semantic Performance Plan. The derived AudioAsset
retains source ID/hash, exact copy, authorization, profile/factor and Job
provenance. A separate ordinary Voice QA Job is enqueued automatically;
until it completes, the candidate is QA-pending and cannot be admitted.

The setting is a conservative option, not a universal speaking-rate target.
It changes the entire take, including natural pauses, so successful technical
QA cannot establish that the creator still sounds natural or publishable.
The derived asset needs its own U-Voice judgment before Talking or final
assembly. Unsupported, cross-project, stale-source and transform failures
remain explicit; the original AudioAsset and its QA/review records are not
changed. Fine-grained focus and whole-sentence contour are outside this
local pace operation.

The Web workspace can play a QA-verified candidate and record the existing
six-dimension U-Voice decision for that exact AudioAsset. Each dimension and
concrete findings are explicit; all-pass is required for approval, and the
irreversible submission is confirmed by the reviewer. The UI does not turn
the bounded V31 listening response into an automatic approval of another
candidate.

### U-Voice review

Human U-Voice is a first-class, asset-specific quality gate after automated
Voice QA. It records an evidence reference, concrete findings, and one
explicit `pass` / `needs_revision` judgment for all six dimensions:

- creator likeness;
- naturalness;
- emphasis;
- pace;
- pauses; and
- rhetorical rhythm.

An approval is valid only when all six are `pass`. A rejection is immutable
for that exact audio asset; the next attempt must be a new take rather than a
silent overwrite of feedback. Both pending and rejected generated Voice are
ineligible for Talking and final VideoSpec assembly. This records subjective
product judgment without pretending it is provider capability evidence or an
automatic acoustic score.

For this version, `pass` on emphasis and rhetorical rhythm means the actual
speech is clear and publishable, not that every word lands ideally or that the
provider has verified fine-grained focus/whole-sentence contour control.
Materially confusing or unpublishable delivery still fails the exact asset.
Those finer controls remain improvement work, not a separate hard prerequisite
that blocks an otherwise passing Voice asset or R1 solely for lacking an
adapter-level plan receipt. A saved Performance Plan still cannot be claimed
as applied without the explicit adapter receipt described below.

### Narration performance intent

Copy tells the system **what** to say. A Narration Performance Plan tells it
how the speech should land: its delivery goal, overall pace, and editable cues
for emphasis, pace, pauses and rhetorical rhythm.

This is a provider-neutral editorial layer. It uses semantic values such as
`measured` pace, a `beat` pause, or a `land` rhythm cue; it is not SSML, a
vendor `speed` parameter, punctuation rewriting, or audio time-stretching.

- Every cue is anchored to a Unicode character range or boundary in the exact
  current editable copy. Content OS does not guess how character anchors move
  after a copy edit: the current plan is cleared, while the prior draft
  revision keeps it for audit.
- A plan records who supplied it (`user`, `assisted` or `imported`) and any
  evidence references. An assisted plan is editable input, not proof that a
  speech will perform well.
- A Voice job can explicitly snapshot only the matching current Draft plan.
  The snapshot makes delivery direction reproducible even if the Draft changes
  later.
- An adapter must explicitly advertise support and return an application
  receipt before a plan is passed to it. `adapter_applied_pending_quality_review`
  means only that the adapter reports applying the semantic plan; it still
  needs normal Voice QA and U-Voice review. Unknown or unsupported adapters
  fail the plan-bearing job explicitly instead of silently dropping cues.
- A provider may preflight a plan as full, partial or unsupported. A partial
  result is **not** an application receipt. Until Content OS has an explicit,
  creator-visible subset mode, partial or unsupported coverage fails before
  ProviderCall reservation and before the provider receives any copy or voice
  reference. The system never silently removes unsupported cues to make a
  route pass.

The current OmniVoice benchmark adapter has no verified mapping for this
semantic plan. Its numeric `speed` setting remains a provider-local Advanced
Setting, not a substitute for performance intent or evidence of rhetorical
control.

Google Chirp 3 Instant Custom Voice remains a future remote/BYOK **partial**
candidate only: its documented global speaking rate and pause tags cannot
honestly stand in for full concept emphasis, local pace or rhetorical rhythm.
It remains unconfigured and unadmitted until its consent/reference-transfer,
allow-list, credential and budget requirements are explicitly authorized and
then locally tested.

### Rhetorical delivery suggestions

Content OS can return an ephemeral assisted plan for the current Draft copy.
It recognizes only transparent editorial structure: sentence role,
contrast/parallel claims, assertion boundaries and repeated enumerations. The
suggestion explains its structural reason and may recommend, for example,
concept landing in a parallel claim, a `beat` after an important point, or a
locally driven list within otherwise conversational pacing.

This is a review surface, not an automatic rewrite or acoustic optimizer:

- it is exact-copy-bound and deterministic for the same persisted Draft;
- it is neither saved to the Draft nor included in a Voice job until a user
  explicitly edits/saves the suggested plan through the normal plan contract;
- it makes no provider call and supplies no provider parameters, milliseconds,
  inserted silence, punctuation edits or audio claim; and
- it does not learn a global preference from an individual U-Voice judgment.

The patterns are intentionally general but limited. A suggestion says that a
copy structure is a useful review opportunity; it never claims that the cue is
the uniquely correct performance or that an available Voice provider can apply
it.

### Evidence-bounded editorial basis

The assistant's cues are editorial candidates, not a text-to-acoustics model.
Prosody research connects information structure and focus, while also finding
that the mapping from meaning to acoustic realization is many-to-many and
language-dependent. That supports a creator reviewing a semantic “concept
landing” in a parallel or contrastive claim; it does **not** support deriving a
unique word stress, pitch, loudness or duration from the copy. See
[Cole, *Prosody and Information Structure*](https://doi.org/10.1146/annurev-linguistics-011724-121524),
[Yan & Calhoun on Mandarin focus](https://doi.org/10.3389/fpsyg.2019.01985)
and [Ip & Cutler's cross-language focus study](https://www.isca-archive.org/speechprosody_2016/ip16_speechprosody.html).

Perceived rate combines articulation rate with pause placement and duration;
it is not one preferred WPM. Breathing and silence also interact with discourse
boundaries and listener processing, but neither result supplies a universal
pause length or proves that more pauses are better. Therefore Content OS keeps
overall pace, local list pace and semantic pause boundaries separate, and calls
a pause a reviewable “breathing/thinking boundary” rather than a physiological
or timing promise. See [Grosjean & Lane](https://doi.org/10.1037/0096-1523.2.4.538),
[Hird & Kirsner](https://doi.org/10.1006/brln.2001.2613), and
[MacGregor, Corley & Donaldson](https://doi.org/10.1016/j.neuropsychologia.2010.09.024).

Mainstream delivery teaching reaches compatible practical advice—vocal variety,
strategic pauses, repetition and parallelism—without providing a universal
formula. It is useful for review vocabulary rather than product-quality proof:
[O'Hair et al., *A Pocket Guide to Public Speaking*](https://store.macmillanlearning.com/US/product/A-Pocket-Guide-to-Public-Speaking/p/1319102786),
[Lucas & Stob, *The Art of Public Speaking*](https://www.mheducation.com/highered/product/The-Art-of-Public-Speaking-Lucas.html),
and [Anderson, *TED Talks*](https://www.ted.com/read/ted-talks-the-official-ted-guide-to-public-speaking).

These sources do not verify the current machine, Voice provider or generated
audio. Suggestion, adapter application receipt, automated Voice QA and U-Voice
remain distinct evidence layers.

### Delivery-plan compiler

Before any Voice call, Content OS can deterministically compile the current
plan into ordered delivery segments. Each segment retains the exact source
characters, inherited/overridden pace, applicable emphasis and rhythm role,
and a semantic opening/inter-segment/closing pause where requested.

The compiler is deliberately not a speech synthesizer: it creates no
milliseconds, provider parameters, punctuation changes, silence, or audio.
It exists so a future capable adapter and a human-directed workflow consume the
same editorial structure. A compiled plan is not an adapter application
receipt, QA pass, or U-Voice approval.

### Structural pause boundaries

In addition to editorial cues, Voice execution derives an exact-copy structural
boundary map. A terminal sentence close, continuing clause, forward-binding
lead-in and ordinary no-break position are distinct. A transition or summary
lead-in such as “但是，” or “最后，” binds to the following claim; it is not an
eligible synthetic-pause or composition-seam position.

When independently verified Span takes are composed, a semantic pause is a
local profile's **total seam budget**, not automatic extra silence. The
Composer measures existing PCM edge silence and supplies only the deficit at an
eligible terminal boundary; it never uses PCM to decide which words were
spoken. This controls composition seams only. It does not claim to repair
provider-take-internal pauses, word focus, sentence landing or rhetorical
rhythm, all of which still require an adapter application receipt and U-Voice.

For the current OmniVoice route, exact-copy QA cannot certify natural phrasing.
Punctuation-safe delivery text and measured seam compaction have improved
individual candidates, but they cannot reliably remove a hesitation or long
break *inside* a generated take. Repeated listening feedback on that same
uncontrolled region is not a product control loop. Accept a publishable exact
asset with a recorded pause caveat when the creator explicitly approves it;
keep the remaining in-take pause problem visible as a capability gap rather
than imposing perfect prosody as a release gate or claiming generalization.

## Talking system

### Product intent

Prefer:

> existing authorized creator video + new audio → minimal necessary lip/face retargeting while preserving identity, source motion, gaze, background and quality.

Ordinary creator footage must remain a valid product input. Special AI-only silent/expressionless capture cannot be a prerequisite.

### Short-capability providers

A provider may have a verified short operating bound. That limit belongs to a concrete provider + model + runtime + machine evidence profile, not to the narrative.

Execution may split a longer Talking intent into provider-bounded slices while the Scene remains one editorial intent.

### Continuous Talking Run

A long creator-visible run may be realized from multiple short provider jobs only when all of these are true:

- slices come from one verified Master Narration;
- slice boundaries follow complete persisted speech intervals;
- all slices are mapped **before dispatch** onto one authorized, source-forward continuous performance run;
- provider-only alignment/context may overlap, but delivered boundaries remain owned by the Master Narration;
- child outputs pass technical QA;
- continuity is reviewed on the ordered result.

A passing short clip does not by itself prove a continuous Talking run.

### TalkingRun product object

The product-level result should be a **TalkingRun**, not a set of implementation slices.

A TalkingRun represents:

- project and Master Narration interval;
- authorized continuous reference run;
- provider/execution provenance;
- ordered child-generation evidence;
- one assembled visual result;
- continuity/quality decision;
- a first-class generated Asset/Clip usable by Hybrid Asset Router and VideoSpec.

Provider slices and child jobs remain execution details.

If one child fails while the other children have unique QA-verified outputs,
the series can explicitly replace that failed child once without rerunning the
passed children. The replacement inherits the exact failed child's source,
Master interval and execution payload. The original failed Job and ProviderCall
remain durable provenance in the series recovery history; a replacement is not
a QA pass or human approval. Recovery is unavailable after continuity review or
TalkingRun admission. Further failure needs a new bounded product decision,
not an automatic retry loop.

The implemented admission path stores a provider-neutral TalkingRun record and
creates one generated Asset plus one whole-duration Clip only after every child
has automated QA, individual U-Talking approval and an immutable approved
continuity review. Hybrid Asset Router, VideoSpec and the renderer consume the
admitted Asset/Clip without traversing child jobs.

The project-scoped U-Talking API records one immutable human decision on each
exact generated child Asset only after automated QA and completed-Job
provenance. A child approval does not imply that joins between children are
acceptable. An assembled review-only preview can expose those joins with the
Master Narration as audio, but it is not an admitted TalkingRun or routable
production Asset; the separate continuity decision remains mandatory.

### Audio authority

For a composed TalkingRun, the verified Master Narration is the authoritative final audio track. Child Talking outputs primarily contribute the generated visual track. This avoids turning codec boundaries or child-audio joins into product semantics.

### Terminal face closeout

The product may request:

> keep the creator face visible and let the mouth naturally settle when the final speech ends.

This is a provider-neutral capability request. The implementation may be provider-native or Content-OS adapter logic.

For a multi-slice TalkingRun:

- intermediate slices are continuation slices and must not independently perform a terminal closeout;
- only the final delivered slice may apply terminal closeout when the selected adapter supports it.

Provider-specific lookahead parameters remain inside the provider settings schema.

## Current productization status

### Voice

The current R1 Voice production path is **accepted as productized for the reviewed local configuration**:

- normal project APIs create bounded generated Voice takes;
- independent Voice QA persists copy/timing/silence/playability evidence;
- verified takes can compose one MasterNarration candidate;
- the composed Master requires fresh whole-master QA;
- immutable U-Voice review covers likeness, naturalness, emphasis, pace, pauses and rhetorical rhythm;
- downstream Talking/VideoSpec reject generated narration that has not passed the required gates.

An exact 32.560s OmniVoice Master has passed full-copy automated QA and six-dimension U-Voice. This is sufficient to move R1 forward; perfect generalized word-level emphasis or automatic prosody control is not a separate prerequisite.

### Talking

The current R1 Talking production path is **accepted as productized for the reviewed local configuration**:

- long Talking intent may execute as provider-bounded source-forward children;
- each child retains Master/reference/provider provenance and independent technical QA;
- one failed child may use the bounded explicit recovery path without rerunning passed children;
- exact child assets require immutable U-Talking;
- the assembled result requires a separate whole-run continuity decision;
- only then can TalkingRun admission create one first-class generated Asset/Clip;
- Hybrid Asset Router / VideoSpec consume the admitted Asset/Clip without knowing child-job topology;
- MasterNarration remains authoritative final audio.

An exact 32.400s reviewed TalkingRun has passed these gates and is consumable by normal VideoSpec.

## Current limits

- Voice and Talking human passes remain asset/configuration specific; they are not universal provider capability claims.
- Generalized in-take pause, semantic emphasis and rhetorical-rhythm control remain improvement areas rather than R1 blockers when the exact Master passes U-Voice.
- The current local Talking operating bound remains machine/provider specific; it must not become a narrative rule.
- OmniVoice official pretrained weights and the current LatentSync benchmark remain non-commercial evaluation dependencies. Commercial provider/model admission is separate from R1 product-path quality.
- The immediate product bottleneck has moved downstream to visual direction/edit quality and product-generated planning, not additional Voice/Talking provider experimentation.

## R1 provider focus

Keep the current **OmniVoice-first / one-provider-first** Voice route and the admitted local Talking path while finishing R1. Provider-neutral contracts remain replacement boundaries, not a reason to reopen provider shopping after the reviewed product path has passed.

## Product gaps

1. Finish the visual-direction/EditPlan layer and obtain one 30–60s U-Product pass using already approved Voice/Talking assets.
2. Replace assisted Scene/EditPlan input with the normal product-generated planning flow.
3. Run second-topic repeatability.
4. Later admit commercial-safe Voice/Talking provider/model routes without changing Core product semantics.
