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

The saved ProductionRun UI now exposes its Voice step using the normal VoiceProfile list and persisted execution capability choices. It requires an explicit profile, provider-matching capability identity and authorization reference, then submits only to the Run's existing guarded Voice dispatch/QA actions. Unknown quality, commercial license and budget evidence remain visible and are rechecked server side; this is no runtime recommendation or capability admission.

When the Run identifies its generated AudioAsset, the UI verifies its project and originating Voice Job before loading metadata or protected media. Preview bytes use the current local access token and an object URL released on candidate/scope changes. U-Voice controls appear only when that exact Run is awaiting review and no prior judgment exists. The six dimensions start unset, each requires an outcome plus evidence reference and findings, and a confirmation names the AudioAsset/hash before calling the existing immutable review endpoint. A saved outcome refreshes the Run but does not auto-resume production. The isolated browser fixture exercises Voice selection, unknown evidence, QA progression, bound candidate playback and review-form rendering; it cannot establish real Voice runtime or subjective audio quality.

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

### Voice recovery boundary

The current Voice recovery classifier exposes recommendations, not an executable ProductionRun repair action. Normal Run dispatch produces a single take; standalone take composition does not make arbitrary intervals of that take independently replaceable. A leading-silence warning may use the first ASR timestamp when waveform measurement is unavailable, so the warning alone never authorizes trimming.

[D027](../../DECISIONS.md#d027--repair-the-supported-dependency-and-preserve-the-review-boundary) defines the next bounded action, not shipped capability: a terminal, unapproved single take with no downstream consumption may receive one explicitly confirmed same-payload replacement for a supported transient generation or independent copy-QA failure. Preserve old evidence and charge records; independently QA the entire new take and require exact U-Voice again. Incomplete QA, unknown failures and missing audio require diagnosis; subjective delivery, reference/configuration changes, approved Masters and multi-take dependencies require replanning rather than this action. Voice/Talking/presentation repairs must share the original production allowance; both TTS and fresh QA ASR operations need declared bounds. No automatic splice, silence normalization or downstream Master substitution is implied.

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

The policy-v1 admission path stores a provider-neutral TalkingRun record and
creates one generated Asset plus one whole-duration Clip only after every child
has automated QA, individual U-Talking approval and an immutable approved
continuity review. Explicit VideoSpec selection and the renderer consume the
admitted Asset/Clip without traversing child jobs. Normal routing now discovers
the admitted whole Run Clip for the same project and exact reviewed speech;
unreviewed children do not become normal candidates. Explicit assembly and
rendering recheck persisted Run identity, review state, project, speech and
whether the selected scene interval reaches the last timed word
instead of trusting copied Asset labels. Generated AI_VIDEO outside this
admission path remains ineligible.

The project-scoped U-Talking API records one immutable human decision on each
exact generated child Asset only after automated QA and completed-Job
provenance. A child approval does not imply that joins between children are
acceptable. An assembled review-only preview can expose those joins with the
Master Narration as audio, but it is not an admitted TalkingRun or routable
production Asset; the separate continuity decision remains mandatory.

The planned-child bridge described next is policy v1. The versioned v2 API lane below retains its exact-preview and provenance boundary with a broader whole-result judgment replacing mandatory child judgment.

The single planned child uses an existing one-element series evidence collection with a versioned planned-origin snapshot; completed Job payloads remain unchanged. After exact technical QA and child U-Talking approval, a guarded product action links the completed output, current plan, Master interval, source admission, consent/license and review evidence to a durable local preview Job. The Worker rechecks inputs and bytes before and after FFmpeg, remuxes the authoritative Master interval, probes the candidate and persists a review-only Asset with its hash and measured streams/timing. A separate immutable continuity decision must explicitly name that exact preview Asset and hash; it also binds origin and technical-report hashes. Approved admission atomically promotes the same Asset bytes, creates one full Clip and existing TalkingRun, and binds ProductionRun without re-encoding. Product and series admission endpoints share the same service; replay and downstream generated-visual gates reopen current source authority, reviews, origin and bytes. Marker-stripped collections and legacy direct assembly remain blocked. ProductionRun exposes preview/review/admitted states. Its narrow resume action can consume all exact bound native-portrait Runs plus deterministic typography, resolve Master timestamps and enqueue the existing render Job. It does not reselect substitutes or silently extend short Talking with a frozen frame, loop, stretch or typography tail. Unknown burned-in subtitles remain unknown; no new caption overlay is added to such Talking. Child QA does not inherit as technical QA of the new encoding, and a single child grants no human-review exemption. D025 owns the boundary. This bridge is fixture/API-integrated, with synthetic real-media evidence for preview preparation only; real planned provider output, human approval, normal UI and repeatability are not proven.

### Versioned aggregate review (planned API lane)

[D026](../../DECISIONS.md#d026--consolidate-talking-judgment-on-the-exact-output-through-a-versioned-policy) is implemented for newly created planned Runs through the API and the normal Web entry point. `POST /projects/{project_id}/production-runs` accepts `talking_review_policy_version: 2`; omission retains v1. The normal Web entry explicitly selects v2 for new Runs; a loaded Run without the field is treated as legacy v1, preserving its child-plus-aggregate controls. Version is immutable and included in request identity; an existing plan bound to another policy returns a conflict. Current execution/source admission and an exact Master-audio preview remain required. Legacy/evaluation and multi-child series retain v1. Unknown/new configurations still need bounded capability validation before normal dispatch; changing the topic alone does not require a new calibration campaign.

The v2 Web review shows child technical QA separately from subjective Run review, the exact preview and all six dimensions, plus persisted local concerns and answers. A local concern may be registered against that preview and answered with the aggregate review, or appended after an existing approval without rewriting it. Existing rejection remains visibly immutable. The isolated same-fixture browser comparison verified the core v1/v2 review path and records required subjective submission actions (v1: child + whole Run; v2: whole Run), not elapsed user time. The concern/answer browser submission branch remains unverified. Real planned-media quality, repeatability and real human-time reduction are not established.

### Bounded planned Talking repair

`GET /projects/{project_id}/production-runs/{run_id}/talking-repair-plan`
requires a current bound scene and reads the existing generation failure or
exact negative aggregate/local review evidence. The plan names the action,
finding range, reused Master, required fresh review, remaining repair allowance
and fixed call/cost boundaries. Only `talking_temporarily_unavailable` or
negative findings restricted to visible sync/artifacts permit regeneration.
Other quality dimensions return replanning; invalid/unknown failures or absent
negative evidence stop. A requested preference change alone cannot be submitted
as verified quality failure. Source/subtitle/crop problems require a suitable
source/presentation decision before any further inference.

`POST .../talking-repair` explicitly confirms that scene regeneration against
the current plan fingerprint. The application reopens consent, source receipt,
Master, exact execution identity, plan and budget gates in a write transaction,
then persists one repair record and new ordinary generation Job while changing
only that scene's active binding. Each scene can be repaired once, within the
Run's original repair allowance. A durable record retains the old binding,
Job/collection predecessor, reason, evidence, budget snapshot, unchanged Master
and other scene identities; `GET .../talking-repairs` also exposes replacement
status and predecessor/replacement accounting records. Replaying an accepted
request returns its existing record and never dispatches again.

The replacement's new QA, collection and exact Master-audio preview keep the
existing review policy. Once prepared, its successor collection is linked to
the repair record without modifying the predecessor collection or review.
Rejected original bytes remain blocked. Changed bytes need a fresh whole-result
judgment; only unrelated approvals are reusable. Worker reservation allows at
most one local provider call per repair and no external charge, independently
of Job attempt numbers, while retaining ordinary budget accounting. Unknown
local resource/time costs remain unknown. API/fixture and synthetic FFmpeg
evidence establish these contracts; normal repair UI, actual inference and
creator-quality improvement are not yet verified.

In that lane, mandatory child technical QA feeds one explicit whole-preview human judgment covering visible sync, identity, artifacts, source-performance retention, continuity and publishability. The existing continuity-review endpoint requires the exact preview Asset/hash, matching `review_policy_version`, and all six `dimensions` (`visible_sync`, `identity`, `artifacts`, `source_performance`, `continuity`, `publishability`). Every outcome is explicit `pass/fail/unknown`; approval requires all pass. An incomplete attempted approval leaves the object awaiting review. Explicit rejection requires `scoped_findings` containing a dimension, reason and optional complete in-range interval; no interval means whole-result scope. Unsubmitted child judgment stays unsubmitted. Exact U-Voice and final U-Product remain required, and source review reuse follows its existing trusted receipt contract.

The planned series review projection lists the policy, required dimensions, blockers and exact-subject concerns. Its `review-concerns` action persists an idempotent human inspection concern, bound to current preview Asset/hash, dimension, evidence and optional interval. The reviewer can answer concerns within the full-review request's `concern_answers`, or through `review-concern-answers` after approval. Answers are immutable and timestamped. Pending concerns block approval/admission and downstream consumption; a positive exact answer restores an otherwise current full approval without re-encoding or rewriting it. Negative answers, child rejections and exact-preview rejections remain vetoes, including across collection aliases. This interface records human concerns; it does not detect visual low confidence automatically.

Old judgments keep their scope and canonical origin hashes; a continuity-only approval cannot become an expanded whole-result approval. V2 origin retains child/QA/media and execution evidence without hashing optional positive child judgment, so recording that judgment later does not invalidate the aggregate. Replacing a dependency invalidates the affected aggregate, retaining unrelated judgments. Collection/review records remain immutable; the bounded repair API creates a distinct successor as described above. API, Worker preparation, admission/replay and Router/Assembly/Renderer share the versioned gate. Synthetic real FFmpeg preview and offline routing/resume/render staging tests cover compatibility; no real v2 provider result, human quality judgment or measured time reduction is established. UI migration and regression criteria belong in [V76a](../implementation/ROADMAP.md#v76a--审核粒度迁移).

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

The ProductionRun backend exposes `GET voice-repair-plan`, `POST voice-repair` and `GET voice-repairs` under `/projects/{project_id}/production-runs/{run_id}/`. This bounded action replaces one terminal, unapproved and unconsumed original single take, not a composed Master or arbitrary audio interval. A known transient generation failure, or a complete timed independent QA report with explicit missing/duplicate/over-tolerance substitution checks, can propose whole-take regeneration. The report is technical evidence of ASR/copy disagreement, not a claim that a human confirmed missing speech. Invalid/unknown errors, absent timing/media and silence-only findings stop for diagnosis; subjective revision replans; approved/consumed Master and composition stop.

Dispatch preserves the exact copy, Job payload, VoiceProfile/consent/reference metadata, admitted capability/runtime/machine and a digest of persisted provider settings. Worker execution receipts retain the configured runtime/machine/parameters from the original reservation. Missing historical snapshots or receipts are unknown, not backfilled permission. Repair compares current reference bytes and frozen evidence, then atomically preserves the predecessor, creates one ordinary replacement Job and clears current AudioAsset/QA bindings. Fresh full-copy QA has a repair-specific identity; old completion callbacks cannot relink the predecessor. Existing audio deduplication/provenance protection rejects identical old failed bytes as a new candidate. U-Voice and explicit downstream resume remain unchanged.

Voice, Talking and presentation repair records consume the same Run creation allowance under a SQLite write transaction; prior usage survives migration. Each Voice action allows at most one locally admitted TTS reservation and one locally admitted QA ASR reservation, with zero external-charge authority. Provider/model/runtime/machine/configuration changes and missing accounting stop before inference. The plan exposes unobserved live Worker readiness: a queued action is not a claim that a Worker is running. A single unambiguous persisted local ASR capability is required; the Worker must match its QA configuration. Unchanged qualified Master/Talking dependencies can be retained by the separate presentation-only render revision, which never copies final approval. Normal Run UI exposes read-only Voice recovery plans, explicit reasoned whole-take replacement, durable lineage/history across generation/QA/review states and the existing fresh QA/U-Voice route. Presentation UI binds a human observation to the exact completed Render hash, displays the backend before/after candidate and retained dependencies, then requires explicit reasoned revision. Component event/effect regressions cover new identities, fresh unknown review defaults, reload/replay, stale input, authorization/allowance denial and confirmation refusal. User-operated isolated screenshots confirm new Voice/QA/history and fresh unselected six-dimension fields; refresh is offline-tested, not independently recorded in these screenshots; these controls do not establish real repair quality or measured user-time savings. Fixture counters report successful workflow requests and zero manual internal binding actions. The old implementation path had no complete ordinary recovery entry; its historical action total and user minutes are unknown, so no numeric reduction is inferred.

The current local configuration has **implemented Voice APIs and exact-asset runtime/human evidence**, not a proven automatic end-user production flow:

- normal project APIs create bounded generated Voice takes;
- independent Voice QA persists copy/timing/silence/playability evidence;
- verified takes can compose one MasterNarration candidate;
- the composed Master requires fresh whole-master QA;
- immutable U-Voice review covers likeness, naturalness, emphasis, pace, pauses and rhetorical rhythm;
- downstream Talking/VideoSpec reject generated narration that has not passed the required gates.

An exact OmniVoice Master passed full-copy automated QA and six-dimension U-Voice; STATUS owns its identity. Retain this evidence and proceed with workflow integration. Perfect generalized emphasis/prosody control is not a separate prerequisite. The Web workspace does not yet orchestrate Master creation and downstream work as one normal creation action.

### Talking

The current local configuration has **implemented Talking APIs and an admitted, human-reviewed Run**, with incomplete normal-flow integration:

- long Talking intent may execute as provider-bounded source-forward children;
- each child retains Master/reference/provider provenance and independent technical QA;
- one failed child may use the bounded explicit recovery path without rerunning passed children;
- exact child assets require immutable U-Talking;
- the assembled result requires a separate whole-run continuity decision;
- only then can TalkingRun admission create one first-class generated Asset/Clip;
- explicit VideoSpec selection and normal routing consume the admitted Asset/Clip for a matching project and reviewed speech, without exposing child-job topology;
- MasterNarration remains authoritative final audio.

An exact reviewed Run passed these gates and is consumable by VideoSpec; STATUS owns its identity. UI-driven source-forward series/Run orchestration and fresh-topic repeatability are not yet proven.

### Review consistency and target granularity

Policy v1 series admission requires each child's human approval plus whole-run continuity. New planned policy-v2 collections instead require exact six-dimension whole-preview approval and current localized-concern evidence, as described above. Direct generated-child selection fails closed under either policy: production requires the admitted Run. The normal Web entry selects v2 for new Runs and preserves v1 controls for legacy Runs; this is fixture evidence, not real-media quality proof.

Target normal production keeps detailed child technical QA while consolidating subjective judgment at the Master, complete Run and final output, with localized exception review. Implement this only through an explicit versioned migration across API, routing and consumption. Existing immutable judgments remain scoped to their exact media; changes invalidate only affected dependencies. Calibration/new-configuration experiments stay separate from routine production.

Human findings are currently asset evidence, not automatically learned behavior. A future scoped rule/preference needs explicit adoption, regression evidence and planning provenance; it cannot silently become a provider default or edit a saved performance plan.

## Current limits

- Voice and Talking human passes remain asset/configuration specific; they are not universal provider capability claims.
- Generalized in-take pause, semantic emphasis and rhetorical-rhythm control remain improvement areas rather than R1 blockers when the exact Master passes U-Voice.
- The current local Talking operating bound remains machine/provider specific; it must not become a narrative rule.
- OmniVoice official pretrained weights and the current LatentSync benchmark remain non-commercial evaluation dependencies. Commercial provider/model admission is separate from R1 product-path quality.
- The immediate bottleneck is production-before-planning drift, source suitability and normal-flow orchestration/review. Additional provider/prosody experiments do not by themselves close these gaps.

## R1 provider focus

[D029](../../DECISIONS.md#d029--admit-execution-for-an-evidenced-purpose-and-preserve-that-scope-downstream) defines a pending migration for explicit, license-evidenced internal evaluation through the normal Run. Existing commercial-safe checks described below remain the current implementation. An old approved Master/Run can support exact configuration-evidence adoption where its provenance is sufficient, but does not fill unknown runtime/settings/bounds or authorize a new copy. Internal use must itself be permitted by the exact provider/model terms; this document does not admit the current benchmark weights. Evaluation purpose will preserve normal QA, U-Voice, the Run's review-policy version and final U-Product; it will not route new planned work through legacy direct evaluation APIs.

In the narrow typography-only ProductionRun flow, a waiting run may dispatch one ordinary Voice generation Job when a consented VoiceProfile, exact verified provider/model capability, commercial-safe license evidence, current budget and explicit authorization all match. The run persists the Job dependency across restart and exposes pending/running/failed states. After durable Voice completion, a Worker also configured for Voice QA automatically attempts the same idempotent product-level advance: it discovers the unique exact-copy generated AudioAsset, rechecks local bytes and budget, and schedules one existing local-ASR Voice QA Job. A Voice-only Worker instead records `voice_qa_worker_not_configured` without dispatching QA. A one-time startup reconciliation on a Voice QA Worker can recover a completed dependency, but separate Workers have no live cross-Worker notification loop. Failed authorization, budget or output evidence leaves a visible last automatic stop reason and no QA Job; after correction the explicit advance endpoint is the bounded retry. The QA Worker resolves portable audio through the configured data root and rechecks bytes before ASR. A single full-copy QA-verified take is a Master candidate awaiting the current exact U-Voice human review; rejected and approved-but-not-resumed states remain distinct. Multi-take composition is not part of this Voice lane. A fixture-qualified execution profile does not admit the evaluation-only OmniVoice weights for commercial use.

An approved-Master plan can retain explicit `new_talking` scene dependencies in a ProductionRun waiting state. Those scene IDs and visual fingerprints do not stand in for a suitable authorized source, consent, a generated TalkingRun or QA/human admission. A narrow, admitted single-scene action can now enqueue one Talking Job; waiting and generated states still cannot render without later QA/Run admission.

A plan requiring new Voice and Talking can remain in one ProductionRun: existing Voice/QA actions create a single-take Master candidate, and exact U-Voice approval permits an atomic transition to Talking waiting. The completed Voice/QA, exact Master identity/bytes, current consent/license and scene-resolvable timing remain required; another approved take is not a substitute. This is fixture/API integration, not real new Voice/Talking production or fewer subjective review gates.

The waiting run may retain one explicit reference candidate for each planned Talking scene after exact speech-interval alignment, profile consent, Clip rights and bytes, recorded reference fit, commercial-safe capability and budget checks. This is a provenance-bearing candidate, not source admission: the existing fit selector does not establish continuous full-interval visual suitability. That state remains `full_interval_unknown`; candidate binding alone cannot generate or render.

An exact source-native portrait Clip can carry a separate full-interval suitability review claim. Its method, coverage, evidence class/reference, source hash/interval and face/head, continuity, subtitle and quality judgments persist independently of the start/end reference selector. The waiting run can consume only a matching assessment after rechecking the candidate and source bytes. `review_claimed_suitable` means a complete assisted assertion was recorded, not that Content OS automatically proved it or that U-Talking/Run admission passed; fixture and incomplete claims remain unknown, while an assisted negative requires replan. A separate current trusted admission receipt is required before a planned Talking Job; that receipt alone does not authorize rendering.

The claim endpoint still accepts caller-supplied reviewer/evidence references; a claim alone is non-executable. A separate local reviewer action resolves an actor from a configured per-reviewer key digest, verifies that a complete assisted positive claim points to intact local evidence bytes inside the data root, rechecks the exact source bytes, and persists one content-bound, revocable admission receipt. Retained evidence can be adopted once and reused for the same exact source review without repeating the subjective decision; this is still a human attestation, not an automatic detector. Missing credentials/evidence, fixture/partial/negative claims, changed bytes and revoked receipts fail closed. A planned single-scene dispatch reopens that exact receipt, current plan/Master, source rights/consent, provider/model/runtime/machine capability, commercial license, budget and a verified provider-scoped duration limit before atomically linking one Job. The Worker repeats those gates before ProviderCall, and planned native-portrait generation cannot silently crop source subtitles. Longer scenes without a verified one-slice bound stop for separate planning; no fixed duration from a conversational example applies. On completed planned generation, an enabled local Talking QA Worker can automatically bind the unique content-verified output and enqueue a separate technical QA Job; a one-time startup reconciliation or explicit product action can recover a missed handoff. QA verifies playable video/audio and compares measured duration to the planned Master slice, while requiring the Master's existing copy QA. The waiting ProductionRun exposes generation/QA pending, failed and technical-verified-awaiting-U-Talking states across restart. This does not prove visible lip-sync, likeness, naturalness or publishability: human review, whole TalkingRun admission and rendering remain separate. Direct Talking Job/series creation and recovery, including previously queued Jobs at the Worker, default to disabled; an explicit local-evaluation-only opt-in retains the legacy experiment path and does not confer commercial or planned-run admission.

The normal API lists exact source reviews and their receipt state per Clip, with protected evidence download bound to the recorded data-root path. It discovers retained assisted claims and trusted receipts without creating another review. A separate reviewer-key action can save an explicit human full-interval inspection as a versioned server-managed report and `assisted_test` assessment; the caller-supplied claim endpoint cannot occupy this managed report namespace. Only a complete positive judgment explicitly marked for reuse receives the existing admission receipt; incomplete or negative judgments remain visible without dispatch authority. The report stores source/interval, actor, four checks and findings, with its bytes bound to the assessment; replay is idempotent and conflicting content is rejected. ProductionRun also exposes a read-only scene setup context with its current Master and exact speech interval when ordered transcript segments have one unambiguous match. That interval is revalidated by source bind and differs from the assembled visual timeline.

The normal ProductionRun Talking panel lists only the selected Talking Profile's authorized reference Clips and persisted Talking capability choices. It displays source dimensions, rights reference, consent, subtitle/reference evidence, capability/license evidence and Run stop reasons without treating unknown as acceptable. For a scene it reads the server-resolved exact Master speech interval; unresolved timing cannot be typed around in the UI. A source bind is an explicit operation and does not dispatch generation. Existing Clip-bound reviews are shown and only a current positive receipt can be applied; when no exact review exists, a separate full-interval human form requires all four judgments, findings and a configured reviewer key kept in component memory and sent only in the existing header. It is cleared after submit, explicit clear, scope change or leaving the panel. The UI does not call horizontal media crop-ready, infer suitability, or auto-select a provider/source.

After explicit source adoption and dispatch, the Run panel separates generation/technical QA from U-Talking on the exact output Asset. It then requires an explicit request for the planned preview, shows its exact Asset/hash, technical QA, planned-origin fingerprint and current blockers, and binds whole continuity review to that preview's identity. Run admission is a further explicit action; backend validation remains authoritative and consumes the same preview bytes. Authenticated preview fetches use ephemeral object URLs which are revoked when the run scope changes or the panel leaves. Stale scopes, unavailable media, server denials and unknown evidence stay visible. No UI action automatically retries, approves, resumes, or renders. The isolated V75b2 fixture exercised profile/Clip/capability selection, server timing, reuse of an existing receipt, child review, exact preview review and admission; its synthetic records do not establish real provider runtime, source quality, subjective media approval or repeatability. Real Talking UI implementation is present, while real planned source/child/Run review remains to be performed through the normal workflow.

For an exact completed-but-rejected preview, generation failure or approved-preview negative concern, the normal panel can read the bounded repair plan and submit a reason-bearing, fingerprint-bound `regenerate_scene` when authorized. It displays durable repair history even while a successor has no preview, including saved predecessor findings and separate predecessor/successor IDs; technical evidence remains distinct from human review. Candidate series, preview bytes, QA and policy define the review cache identity. Changed candidate or concern state remounts the review form and clears stale repair confirmation; responses are fenced to the current Run/media scope. Missing/null cost remains unknown rather than a measured zero. Executed offline fixture-state tests cover pending generation with no output/preview, explicit simulated Worker completion, independent QA, distinct unreviewed successor identities, persisted history, idempotent replay and technical/negative-concern evidence. These tests use in-memory storage, not a browser or provider. Unit tests, fixture TypeScript checking and production build pass. User-operated V76b2r fixture screenshots verify durable pending history, explicit simulated Worker completion, separate QA, distinct successor preview/series, six fresh unknown form dimensions and state/history after browser refresh. Seeded technical-failure and approved-preview negative-concern repair entries were also observed; concern creation/answer submission and real-media playback remain unverified. No actual inference, quality gain or time savings is established.

Reuse the reviewed local route for bounded integration proof where it satisfies the plan. Provider-neutral contracts are replacement boundaries, not a requirement to reopen provider shopping. A demonstrated quality, capability, consent or license blocker may justify a separately scoped provider-admission package; the current route is not an unconditional shipping commitment.

## Product gaps

1. Unify admission and generated-media selection without repeating valid asset-level human review.
2. Plan visual feasibility and required production before dispatch; wire ordinary UI/application orchestration to existing Master/Run APIs.
3. Validate normal UI aggregate-review and bound repair on real authorized media, then consume scoped feedback in later decisions.
4. Prove fresh-topic creation and second-topic repeatability through the normal flow, not an assisted script.
5. Admit commercial-safe provider/model routes separately before commercial release, preserving Core semantics.
