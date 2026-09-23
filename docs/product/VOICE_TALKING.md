# Creator Voice and Talking

## Product responsibility

Create **new words in the authorized creator's voice and face** without requiring the creator to re-record complete narration for every video.

Voice and Talking are separate provider-neutral capabilities with separate quality gates.

## User outcome

From new copy, Content OS should be able to produce:

1. a verified creator-voice Master Narration; and
2. one or more creator-visible Talking runs that speak the new narration and can be used like normal production media.

## Voice system

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

The implemented admission path stores a provider-neutral TalkingRun record and
creates one generated Asset plus one whole-duration Clip only after every child
has automated QA, individual U-Talking approval and an immutable approved
continuity review. Hybrid Asset Router, VideoSpec and the renderer consume the
admitted Asset/Clip without traversing child jobs.

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

## Current evidence and limits

- MuseTalk 1.5 and VideoReTalking are rejected for the current product path.
- LatentSync 1.5 remains a benchmark-only local adapter; its official benchmark/model licensing does not make it a commercial default.
- On the current development machine, fresh-Voice evidence has established a short local operating bound around 4.46s pass / 5.12s fail for the reviewed setup.
- A 17.28s source-forward multi-short LatentSync series has received human approval as natural, continuous and publishable for the exact reviewed configuration/reference/take.
- That evidence proves the execution strategy for the reviewed case; it does not prove every source clip/provider/machine will behave the same.
- Current long-form OmniVoice reliability is still a product risk. Long Master Narration must not be assumed reliable from short-sample success.
- Chatterbox has prior short local QA evidence but was rejected as a Voice route
  by U-Voice for creator similarity and naturalness; it is intentionally not a
  Core adapter or R1 default.
- No delivery-capable Voice provider is admitted. The narrow documentation
  review found Google Chirp 3 Instant Custom Voice as a possible **partial**
  remote/BYOK experiment: its documentation lists Chinese (`cmn-CN`), required
  consent/reference audio, pace control and experimental pause control, but not
  range-scoped emphasis or rhetorical-rhythm control. It is allow-list gated
  and the published price is per input character, so it needs explicit access,
  transfer and budget approval before any use. [Google capability and consent
  requirements](https://docs.cloud.google.com/text-to-speech/docs/chirp3-instant-custom-voice)
  and [published pricing](https://cloud.google.com/text-to-speech/pricing)
  are provider documentation—not local capability evidence.
- Azure Personal Voice is likewise not a documented full-plan route: its
  current SSML matrix supports rate and break for the listed personal-voice
  base models, but marks word-level emphasis unsupported. [Azure Personal Voice
  SSML matrix](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/personal-voice-how-to-use)
  is documentation evidence only.

## R1 provider focus

For the immediate R1 proof, Voice follows an **OmniVoice-first / one-provider-first** strategy.

Provider-neutral architecture remains mandatory, but it is a replacement boundary rather than a reason to keep shopping providers before one route is product-usable. The installed OmniVoice path already has the strongest local integration and real creator evidence in this repository, so R1 should first exhaust a bounded, evidence-driven attempt to make it production-usable through Content OS-owned performance planning, reference-window selection and composition.

Open another provider benchmark only when a bounded OmniVoice performance experiment shows that the remaining quality gap is provider-acoustic capability rather than product orchestration, reference selection or composition. If that happens, benchmark one alternate route against the same copy/performance evidence; do not start a provider tournament.

This focus does not change the licensing fact: OmniVoice official pretrained weights remain non-commercial evaluation material, so R1 production usability is not a commercial-release decision.

## Product gaps

1. Close the local OmniVoice performance-rendering gap: convert the existing semantic Narration Performance Plan into bounded semantic units, deliberate pause/continuity composition and better authorized reference-window selection, then judge the actual audio by U-Voice. Do not assume the semantic plan is already acoustically applied.
2. Establish a reliable fresh 30–60s Master Narration route with an explicitly
   selected local execution profile or approved provider. The 43.04s composed
   CUDA candidate passed automated QA but is formally U-Voice-rejected for
   naturalness, emphasis, pace, pauses and rhythm; it is not a reliability
   claim and cannot be re-approved in place.
3. Later add a commercial-safe and/or approved remote provider route without changing Core product semantics.
