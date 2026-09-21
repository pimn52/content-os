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
→ Voice execution
→ generated take(s)
→ independent Voice QA
→ optional take composition
→ verified Master Narration
→ U-Voice
```

Long narration does not have to be generated in one provider call. A provider may be more reliable with bounded natural sentence/phrase takes. Content OS may compose only independently verified takes and must QA the resulting master again.

### Voice QA

At minimum:

- requested-copy coverage;
- missing / duplicate / excessive substitution checks;
- duration and silence sanity;
- playable audio;
- provider/reference provenance.

Human U-Voice remains responsible for likeness, naturalness, pacing and breathing.

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

## Product gaps

1. Productize reviewed slice-series output as a first-class TalkingRun Asset/Clip.
2. Apply terminal closeout correctly to the final series slice without forcing intermediate closeouts.
3. Establish a reliable Master Narration route for new 30–60s content.
4. Later add a commercial-safe and/or approved remote provider route without changing Core product semantics.
