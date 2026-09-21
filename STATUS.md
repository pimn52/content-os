# Content OS — Current Status

Updated: 2026-09-20 (Asia/Shanghai)

## Current truth

- Content OS remains an **internal Alpha**. R1 has not yet passed: `new topic → creator voice → creator Talking/lip-sync → hybrid 30–60s export`.
- Core media/runtime foundations are in place: local media/library, continuous Clips, IP/draft state, ScenePlan/Hybrid Asset Router, MasterNarration/timeline, Remotion/FFmpeg render, durable Jobs/recovery, provider accounting/budget and Voice/Talking QA boundaries.
- **Local-first Data + Hybrid Compute** is now the product architecture direction. Local-first governs creator data/control; heavy inference may route to local or explicitly approved remote compute according to evidence, privacy, cost and runtime capability.
- Voice/Talking remain provider-neutral. **Chatterbox, MuseTalk 1.5 and VideoReTalking are rejected** for the current R1 path.
- **OmniVoice** remains the local non-commercial Voice benchmark. Official pretrained weights are CC-BY-NC and are not a commercial-safe default.
- **LatentSync 1.5** remains an optional benchmark-only local Talking adapter. On the current RTX 3060 Laptop 6GB configuration, fresh-Voice product evidence includes:
  - **2.58s PASS** — U-Voice-approved new OmniVoice speech + LatentSync passed U-Talking;
  - **4.46s PASS** — a natural, full-copy fresh-Voice take passed U-Talking as natural/publishable;
  - **5.12s FAIL** — complete dynamic output, but lip-sync progressively drifted late in the segment;
  - the current fresh-Voice short-Talking evidence bracket is therefore **4.46s PASS / 5.12s FAIL**; this is not long-Talking or multi-segment-continuity proof.
- The LatentSync adapter now rejects a truncated raw model response before duration normalization; it can no longer turn a short raw output into a frozen padded false-positive.
- A terminal Talking job can explicitly request **Content OS face-visible natural closeout** for the bug “speech has ended but the mouth still appears to be speaking.” Content OS owns this product protection; an adapter can implement it using model-only context even if its upstream Provider has never named the bug. The current LatentSync 1.5 adapter maps it to its 600ms `trailing_silence_lookahead_ms` baseline unless a scoped override exists, then delivers only the original narration through the final Voice-QA speech timestamp. This is executable product behavior, while the D6g human-quality pass remains scoped local evidence.
- Local Voice and Talking generation now share one SQLite-backed, machine-named GPU inference lease. Workers on the same resource key serialize model execution, renew ownership with the existing Job heartbeat, release it on every terminal transition and safely reclaim it after expiry. This is local scheduling only, not a distributed GPU system.
- **E1 full R1 export proof failed before Talking/render**: its one 34.12s fresh OmniVoice H.265 narration was playable but independent Voice QA found 679ms leading silence (over the 500ms limit) and one reported missing token. E4 showed the missing-token count includes the comparator's `H.265`/`H265` notation gap; the bounded package still did not crop, retry, generate Talking or render a misleading partial video.
- **E3 sentence-take recovery also failed at its first take**: the 5.840s opening sentence had acceptable 479ms leading silence but independent Voice QA found one reported missing token and three over-limit substitutions. E4 identified the same `H.265`/`H265` comparison gap, while medium/small ASR disagree on the spoken `码率` phrase (`马力` versus `马率`); the take remains failed and the bounded stop rule prevented the remaining three takes, master composition, Talking and render.
- OmniVoice CPU execution on this machine is not a viable default interactive route (a short test ran about 29 minutes without output); the same benchmark on CUDA completed in roughly 10 seconds. GPU scheduling/resource contention therefore matters for future local execution.
- **Multi-short-segment continuity is unproven.** Individually passing 2–3s Talking segments must not be presented as evidence that a long continuous presenter can be created by simple concatenation.
- KeySync remains deferred to a compatible machine. No new provider is admitted merely because it can produce a demo.

## Completed work package — Gate D4: capability profile + routing decision foundation

**State: PASS**
**Primary implementation model: Terra**  
**Luna role: focused tests/docs/UI follow-up only**  
**Sol role: not required unless a concrete architecture conflict remains after Terra implementation**

### Objective

Implement the smallest maintainable foundation that can turn real provider/machine evidence into a deterministic execution decision **without yet adding a remote Talking provider or a full Advanced Settings UI**.

This package establishes the boundary between:

1. Narrative / Scene intent;
2. Hybrid Asset Router (what visual/content route is needed);
3. Execution Planner / Compute Router (which provider/runtime/configuration should execute it).

### Required behavior

1. Add a provider-neutral **capability profile / execution evidence** representation scoped to a concrete provider/model/runtime/machine configuration.
2. Represent at least:
   - capability type;
   - local/remote mode;
   - readiness state (`implemented/configured/available/verified` or equivalent);
   - verified/observed operating bounds where known;
   - continuity/quality evidence status;
   - latency/resource/cost fields when known;
   - license/commercial status;
   - provenance / last verification information.
3. Add a deterministic routing/config resolution function with this precedence:
   - per-job explicit override;
   - saved user/provider+machine override;
   - locally verified profile value;
   - provider-known conservative default;
   - unknown.
4. `unknown` must remain explicit; do not fabricate an optimized value.
5. Existing LatentSync/OmniVoice evidence may be represented as seed/runtime evidence for the current development machine, but must not become global defaults.
6. Expose a machine-readable **routing reason / parameter provenance** so future UI can explain why a route/value was selected.
7. Preserve existing provider-neutral Voice/Talking contracts and durable Job/provider-call accounting.

### Safety / override rules

User overrides may tune quality/runtime parameters, but the resolver must not allow them to bypass:

- consent/identity authorization;
- budget/paid-call gates;
- license/commercial restrictions;
- provenance requirements;
- hard runtime-safety/integrity checks.

### Allowed modules

Prefer small new modules under existing `services/api/app/routing/` or an equally narrow capability/config package, plus focused domain/config types and tests.

Modify `runtime.py` only when needed to expose capability evidence/readiness; do not grow `main.py` with routing logic.

### Acceptance criteria

- deterministic tests cover all five precedence levels;
- unknown remains unknown when no evidence/default/override exists;
- one local profile can differ from another machine/profile without changing global provider defaults;
- a user override is reversible and narrowly scoped;
- hard safety/license/budget constraints can reject an override;
- LatentSync current-machine evidence can be represented without hard-coding LatentSync fields into universal Core contracts;
- routing result exposes reason/provenance;
- existing focused Voice/Talking/provider-accounting tests remain green;
- `python scripts/check_docs.py` passes.

### Non-goals

Do **not** in this package:

- implement or register a new remote/cloud Talking provider;
- build the full Advanced Settings UI;
- change Scene Planner semantics to fixed 2.58s/3s/4s scenes;
- claim multi-segment Talking continuity;
- run paid APIs;
- continue fine-grained duration probing;
- implement a general distributed scheduler/Celery/Redis system.

### Completion

- Added the provider-neutral `app.routing.execution` foundation: concrete provider/model/runtime/machine capability keys; readiness, observed bounds, quality/continuity, resource/latency/cost/license and provenance evidence; scoped reversible overrides; deterministic parameter provenance; and non-bypassable consent/budget/license/provenance/runtime-integrity gates.
- The resolver has no persistence, provider call, Scene Planner or Asset Router side effect. Existing LatentSync development-machine evidence can be represented through generic fields rather than global defaults or LatentSync-specific Core contracts.
- Focused routing, runtime-readiness, provider-accounting, Voice and Talking suites passed (38 tests). D5 is the next ready package; no UI or remote provider work was started here.

### Exit states

This package must close as exactly one of:

- `PASS` — capability profile + deterministic resolver + tests meet the criteria;
- `FAIL` — the bounded design cannot satisfy the criteria without redesigning preserved Core contracts;
- `BLOCKED` — a concrete dependency prevents implementation and the smallest clearing action is documented.

No human U-Voice/U-Talking review is required for this architecture package because it must not generate new subjective-quality evidence.

## Completed work package — Gate D5: Advanced Settings product surface

**State: PASS**
**Primary implementation model: Terra**

### Objective

Build one local Advanced Settings entry for the currently admitted local Voice/Talking benchmark schemas. It must save a provider+machine override at the narrowest durable scope, reset it to automatic, and call the D4 resolver to display the exact effective values and their provenance.

### Task contract

- **Allowed modules:** `services/api/app/routing/`, narrowly related domain/repository/migration/API files, `apps/web/src/main.tsx` and its stylesheet, focused API/routing tests and this status record.
- **Stable interfaces:** provider-neutral Voice/Talking contracts, Job/provider-call accounting, consent/budget enforcement, Asset Router/ScenePlan semantics and the D4 resolver precedence.
- **Acceptance:** provider-specific schemas are rendered by one common UI; values show `verified`/`provider_default`/`user_override`/`unknown`; a provider+machine setting saves and survives reload; reset removes it; API resolution uses D4 provenance and rejects unsafe settings; focused API/routing tests and Web build pass.
- **Non-goals:** no remote/BYOK provider implementation, credentials, paid calls, model execution, global provider defaults, project/job UI overrides, capability-boundary probing or D6 continuity work.

### Current implementation boundary

- D5 exposes the narrow provider+machine scope first. Per-job and project scope remain resolver capabilities, not UI controls, so the surface does not overstate persistence behavior before a dispatch integration exists.
- The local benchmark schemas describe parameters only; they do not make official non-commercial weights commercial-safe or bypass existing pre-dispatch authorization/budget/integrity checks.

### Completion

- The common Web entry reads provider-owned local benchmark schemas, selects a provider/model/runtime/machine key, shows the D4 resolver's effective value plus provenance, saves one exact provider+machine override and resets it to automatic/provider-default resolution.
- Schema v19 persists these settings locally. The API validates provider-owned parameter ranges and does not run a model or persist credentials. The D4 safety gates remain the execution-time authority; this D5 preview is not a dispatch path.
- Focused routing/API/migration/Voice/Talking/provider suites passed (71 passed, 1 skipped); the production Web build passed. D6 is the next ready package.

## Completed work package — Gate D6: short-Talking continuity experiment

**State: FAIL**
**Primary implementation model: Terra**

### Objective

Use the already U-Voice-approved 5.12s natural OmniVoice master take as one continuous narration, then compare two local visual treatments of its complete content: independently generated short Talking clips directly adjacent versus the same narration with a real creator B-roll/typography transition between Talking appearances.

### Task contract

- **Allowed modules:** local evaluation evidence, existing Voice/Talking provider and QA paths, timeline/render utilities and this status record. Core contracts remain stable unless a concrete reproducible defect requires a separately scoped repair.
- **Stable interfaces:** one continuous MasterNarration/audio provenance, consent, provider accounting, existing 2.58s/5.12s duration evidence, Hybrid Asset Router/Execution Planner separation and D4/D5 settings semantics.
- **Acceptance:** use the complete verified master take; independently QA any newly extracted sentence and generated Talking clip; produce one full-content bare-continuity artifact and one full-content hybrid-transition artifact; run container/timing/freeze checks; then stop at an explicit U-Talking comparison review.
- **Non-goals:** no new provider, paid call, new Voice generation, duration-boundary refinement, claim of long continuous Talking, 30–60s product-gate claim or automatic promotion of a successful setting to verified machine evidence.

### Fixed experiment inputs

- Master narration: U-Voice-approved `gate-d1-natural-short-cuda.wav` (5.12s; verified copy coverage). The first short Talking appearance reuses the D3 2.58s U-Talking-pass artifact. The only new Talking inference is the remaining complete natural clause from the same master take, after independent Voice QA.
- The comparison uses the same authorized ordinary creator reference. The hybrid treatment may insert only real creator B-roll and readable typography/subtitles while the continuous master narration plays; it may not conceal a failed Talking clip by extending frozen frames.

### Required U-Talking comparison — stop condition

Once both artifacts are ready, review each from start to end and judge: (a) the visible seam/identity/sync of adjacent short Talking clips, (b) whether the hybrid transition makes the total experience publishable, and (c) whether either treatment introduces an audio discontinuity. Stop before further tuning.

### Ready for U-Talking comparison

- `content-os-data/latentsync-duration-boundary-20260915/gate-d6-bare-adjacent-short-talking.mp4` — the two independently generated short Talking clips are visually adjacent at the clause boundary; only a 0.56s real-source pre-roll and 0.20s tail preserve the complete 5.12s master timeline.
- `content-os-data/latentsync-duration-boundary-20260915/gate-d6-hybrid-broll-transition.mp4` — the same continuous master audio, with a 1.36s real creator B-roll transition between the two Talking appearances.
- New second-clause evidence: `gate-d6-fresh-voice-second-clause-1800ms.voice-qa.json` and `talking-d6-second-clause-result.json`; its Voice and Talking QA are verified.
- Both comparison containers are 1280×720, 25fps, 128 video frames and 5.120s audio/video; no `freezedetect` interval or >=0.3s `silencedetect` interval was reported. Their audio is encoded in one pass from the same complete master narration, not concatenated from clip audio.

### U-Talking result

- Audio continuity passes, but both treatments fail visual continuity: the independently generated clips each begin from the same ordinary-reference start frame. The second clip therefore resets the creator's body/gesture motion instead of following the first clip; the reviewer observed an obvious forward-then-backward "time travel" jump which also makes lip-sync judgment unreliable.
- This is a composition/reference-window defect, not evidence that the second 1.80s Talking clip has failed its own automated audio/duration QA. D6 must not be promoted to evidence for multi-short Talking continuity or publishability.

## Ready work package — Gate D6b: ordered-reference short-Talking continuity

**State: PASS**
**Primary implementation model: Terra**

### Objective

Run one bounded replacement continuity comparison in which each independently generated short Talking clip uses an explicitly ordered, non-overlapping source-reference window, and any inserted real creator B-roll follows that same forward source timeline. This isolates motion continuity from the prior reset-to-frame-zero defect.

### Task contract

- **Allowed modules:** local evaluation evidence, existing Talking provider/QA paths, reference-clip/media extraction utilities, timeline/render utilities and this status record.
- **Stable interfaces:** the U-Voice-approved 5.12s master narration, consent/provider accounting, provider-neutral Talking contracts, D4/D5 routing/settings semantics and existing duration evidence.
- **Acceptance:** preserve the exact master audio; make one forward-only reference-window map before inference; independently QA any newly generated Talking output; export one ordered bare and one ordered hybrid artifact; verify container/timing/freeze; then stop at U-Talking review focused first on visual temporal continuity and then lip-sync/publishability.
- **Non-goals:** no new provider/voice/paid call, no duration-boundary experiment, no reference-frame interpolation or optical-flow invention, no claim of long continuous Talking, and no 30–60s product-gate claim.

### Fixed constraint

The D6 direct-concatenation artifacts are retained as failed evidence only. D6b must not reuse their implicit frame-zero reset: every visual splice must advance through the selected creator source timeline.

### Ready for U-Talking comparison

- `content-os-data/latentsync-duration-boundary-20260915/gate-d6b-ordered-bare-short-talking.mp4` — source-ordered visual map: D3 Talking reference window `0.000–2.560s`, unmodified creator-source bridge `2.560–3.120s`, D6b Talking reference window `3.120–4.920s`, then source tail `4.920–5.120s`.
- `content-os-data/latentsync-duration-boundary-20260915/gate-d6b-ordered-hybrid-broll-transition.mp4` — the same continuous master narration and forward source timeline, with a longer `1.200–3.120s` real-creator B-roll transition between the two Talking appearances.
- The first D6b attempt correctly failed rather than padding: a 1.800s reference Clip left the raw model output at 1.280s. The bounded retry extended only the ordered reference input to `3.120–5.040s` to cover LatentSync's required 1.920s 16-frame model window. Its raw output was 1.920s; the verified Talking result is 1.800s with zero duration drift and complete reused Voice QA in `talking-d6b-ordered-second-clause-padded-reference-result.json`.
- Both comparison containers are 1280×720, 25fps, 128 video frames and 5.120s audio/video. No `freezedetect` interval or >=0.3s `silencedetect` interval was reported. A seam-frame contact sheet confirms the visual selections advance forward through the source timeline; it is a technical check, not a substitute for U-Talking review.

**Stop:** Review both from start to end for (a) absence of the prior backward/time-travel motion, (b) visible seam, identity and lip-sync, (c) whether the hybrid treatment is publishable, and (d) audio continuity. Do not tune, promote, or open another package until the user judges these artifacts.

### U-Talking result

- The reviewer confirmed that the ordered-reference artifacts no longer have the backward/time-travel motion and that their visual seams are natural. This passes the bounded D6b reference-window continuity question; it does not prove a longer continuous Talking capability.
- The two variants look intentionally similar because both use the same continuous creator source: the bare version exposes a 0.560s source bridge, while the hybrid version exposes a 1.920s source B-roll transition. No typography treatment was introduced, so the B-roll duration is their only editorial difference.
- A distinct close-out defect remains: after the spoken final syllable `达` in `再补上必要的新表达`, the final Talking frames re-open/purse the mouth. For a sentence with no following speech, that visual ending is not yet publishable as a closed-mouth stop.

## Ready work package — Gate D6c: sentence-final Talking close-out

**State: FAIL**
**Primary implementation model: Terra**

### Objective

Implement and run one bounded, non-frozen sentence-final treatment for the already reviewed ordered timeline. The endpoint must be derived from any verified narration transcript's final speech segment—not the literal word `达`—then transition to an appropriate forward creator-source visual or readable end typography so the visible mouth does not resume speech-like movement after the audio ends.

### Task contract

- **Allowed modules:** existing D6b local evidence, source-frame inspection/extraction, narrowly scoped provider-neutral narration/timeline or render policy utilities and focused tests, plus this status record. No provider/model call is required unless a concrete source-level issue makes the bounded treatment impossible.
- **Stable interfaces:** U-Voice-approved 5.12s master narration, D6b ordered source timeline, consent/provider accounting and all provider-neutral contracts.
- **Acceptance:** expose one reusable, provider-neutral close-out decision based on the final verified transcript segment for arbitrary copy; retain one continuous master audio; use only forward source material or readable typography after that endpoint; do not freeze-pad, loop or synthesize a new mouth state; cover the policy with focused tests; export one close-out candidate; verify timing/container/freeze; then stop for U-Talking review focused on whether the ending reads as naturally complete.
- **Non-goals:** no word-specific rule, no new Voice/Talking generation, no duration probing, no image/video interpolation, no product-gate claim and no long-segment continuity claim.

### Fixed constraint

The D6b second Talking output remains evidence for the sentence itself; D6c is a composition close-out treatment only and must not disguise a Talking failure with cloned or frozen terminal frames.

### Reusable product policy and ready U-Talking artifact

- Added provider-neutral `app.talking.plan_talking_closeout`: it derives a frame-safe close-out interval from the final persisted narration transcript segment for arbitrary language/copy. It returns no plan when speech already reaches the final renderable frame, and rejects missing or overrun timing instead of guessing.
- The master-narration VideoSpec path now applies that plan only to a final verified `AI_VIDEO` Talking visual: it keeps the Talking visual through the final verified speech frame, then emits an explicit local Typography continuation for any remaining narration frames. Hybrid routing remains free to choose an appropriate forward real-source continuation instead; neither path freezes, loops, nor asks a provider to invent a closed-mouth frame.
- Focused tests cover arbitrary final text, 25fps and fractional frame rates, no-tail behavior, invalid timing, and the assembled `AI_VIDEO → TYPOGRAPHY` boundary (37 passed).
- `content-os-data/latentsync-duration-boundary-20260915/gate-d6c-transcript-closeout-typography.mp4` is the concrete D6c candidate. The final verified speech endpoint is 4.920s; its five 25fps frames through 5.120s are Typography rather than the source tail that visibly pursed the mouth. It retains the one 5.120s master audio, 1280×720/25fps/128 frames, and reports no freeze interval or >=0.3s silence interval.

**Stop:** U-Talking review is required. Judge whether the transcript-driven no-mouth close-out reads as a natural ending, and whether the short Typography handoff is preferable to the former mouth movement. Do not tune or promote until the user responds.

### U-Talking result

- The transcript-driven Typography tail removed the post-speech mouth movement, but the reviewer rejected it as the preferred product behavior: the desired capability is a natural closed-mouth Talking end state, not a direct visual cut.
- D6c remains valid generic timeline safety fallback, but it does not satisfy the product-quality close-out requirement and must not be promoted as the preferred route.

## Active work package — Gate D6d: silent-driving closed-mouth Talking end

**State: FAIL**
**Primary implementation model: Terra**

### Objective

Run one bounded normal Talking job in which the already verified second-clause driving take receives a short, explicit silent tail for model inference only. Export keeps the original continuous 5.12s master audio; the experiment tests whether LatentSync naturally settles the mouth after arbitrary final speech without direct pixel editing, freezing or a Typography cut.

### Task contract

- **Allowed modules:** D6b evidence, existing local Voice/Talking QA and provider/job paths, timeline/render utilities, narrowly related close-out policy tests/status.
- **Stable interfaces:** original U-Voice-approved master narration/audio, consent/provider accounting, provider-neutral Talking contracts, D4/D5 routing and D6b ordered reference timeline.
- **Acceptance:** verify the silent-tail model-driving audio's copy/playability/timing provenance; run exactly one normal local Talking job with an ordered source window; use only the post-speech portion of that output as the close-out visual while preserving original master audio; verify container/timing/freeze; then stop for U-Talking review of natural closed-mouth completion.
- **Non-goals:** no pixel-level mouth edit, face inpainting/interpolation, new provider/voice/paid call, duration-boundary search, extra retry after a quality failure, long-Talking claim or product-gate claim.

### Fixed constraint

The silent tail is a model-driving control input, not narration. It must never be substituted for, appended to, or concealed as the master audio. A failed or pucker-like silent result is valid D6d failure evidence.

### Ready for U-Talking review

- `gate-d6d-second-clause-plus-200ms-silence.wav` is the verified model-driving input only: 2.000s, copy coverage 1.0, no leading silence and 340ms terminal silence in `gate-d6d-second-clause-plus-200ms-silence.voice-qa.json`. The delivered composition does not map this audio.
- One normal local LatentSync Job completed and its normalized 2.000s Talking output passed automated Talking QA in `talking-d6d-silent-tail-result.json`. The unnormalized model output was 1.920s, so the D6d composition deliberately uses only its real dynamic silent-window frames `1.800–1.920s`; it does not use the adapter's final 0.080s clone padding as closed-mouth evidence.
- `content-os-data/latentsync-duration-boundary-20260915/gate-d6d-silent-driving-closed-mouth-closeout.mp4` retains the original 5.120s master audio. Its `4.920–5.040s` visual is the dynamic silent-driving tail; only the final `5.040–5.120s` is a deliberate fade-to-black end treatment, not a frozen face or Typography cut.
- The candidate is 1280×720, 25fps, 128 video frames and 5.120s audio/video. No >=0.5s `freezedetect` interval or >=0.3s `silencedetect` interval was reported. Automated checks cannot judge whether the mouth settles naturally; that remains U-Talking evidence.

**Stop:** Review the D6d artifact from start to end. Judge whether the mouth visibly settles after the final word before the fade, whether the fade is a natural non-speaking end, and whether the audio remains continuous. Do not tune or promote until the user responds.

### U-Talking correction

- The reviewer identified that D6d tested the wrong semantic treatment: it exposed the silent-driving tail and then faded to black. That is not the intended look-ahead method. D6d is retained as failed evidence only; neither its black tail nor its normalized clone tail may be used as the product close-out.

## Active work package — Gate D6e: silent-look-ahead Talking close-out

**State: FAIL**
**Primary implementation model: Terra**

### Objective

Run one bounded silent-look-ahead experiment: append a sufficiently long model-only silent suffix to a verified short driving take, generate the longer Talking output, then export **only** the original narration-duration visual portion with the unchanged original master audio. The suffix is temporal context for model prediction and must not appear in the delivered timeline.

### Task contract

- **Allowed modules:** D6b/D6d evidence, existing Voice/Talking QA and provider/job paths, ordered source-window preparation, timeline/render utilities, focused close-out policy tests and this status record.
- **Stable interfaces:** original 5.120s U-Voice-approved master audio, consent/provider accounting, provider-neutral Talking contracts, D6b forward source-time map and D4/D5 routing/settings semantics.
- **Acceptance:** independently QA the longer model-driving audio; ensure its source window covers the model frame-alignment duration without raw-output cloning; run exactly one normal local Talking job; crop the generated visual at the original 1.800s narration boundary while mapping the original master audio; verify no black/typography/silent-tail visual, exact timing/container/freeze and then stop for U-Talking review of the final visible mouth state.
- **Non-goals:** no literal-word rule, pixel mouth edit, face inpainting/interpolation, new provider/voice/paid call, duration-boundary search, extra retry after a quality failure or long-Talking claim.

### Fixed constraint

The appended silence is never exported in either audio or video. It may only influence the model's prediction of frames before the original driving-audio boundary. If the cropped original-duration end still puckers or the output needs clone padding, D6e fails.

### Ready for U-Talking review

- The model-driving input `gate-d6e-lookahead-175ms-prefix-200ms-suffix.wav` passed independent Voice QA (2.175s, copy coverage 1.0). Audio-level silence detection confirms the intentional `0.000–0.175s` prefix and `1.975–2.175s` suffix. Both are model context only.
- One normal local Talking Job completed in `talking-d6e-silent-lookahead-result.json`. Its raw model output is 2.560s/64 dynamic frames, so the experiment has a full source-reference window and does not rely on duration-normalization clone padding for the cropped delivery segment.
- `content-os-data/latentsync-duration-boundary-20260915/gate-d6e-silent-lookahead-cropped-closeout.mp4` takes exactly 45 frames/1.800s from the model output's source-aligned `0.200–2.000s` frame interval. This is the original spoken duration after frame-grid alignment; no separate prefix/suffix visual tail is exported. The visible source map remains forward-only: source bridge ends at 3.145s, cropped Talking advances through 4.945s, and the final 0.175s uses a same-source no-mouth B-roll crop rather than black, Typography or a cloned face frame.
- The delivered audio is the original 5.120s U-Voice-approved master, mapped once. The container is 1280×720/25fps/128 frames/5.120s; no >=0.5s `freezedetect` interval or >=0.3s `silencedetect` interval was reported. Automated checks do not establish natural end-mouth state.

**Stop:** Review the D6e artifact start to finish. Judge whether the final spoken mouth now naturally completes, whether the short no-mouth B-roll handoff is acceptable, and whether lip-sync/audio continuity remain publishable. Do not tune or promote until the user responds.

### U-Talking result

- The reviewer rejected D6e because its final no-mouth B-roll crop hides the face. That is a composition workaround, not evidence that the model has created a natural closed-mouth end state. D6e must not be used as a product close-out route.

## Gate D6f: face-visible speech-complete Talking end

**State: FAIL**
**Primary implementation model: Terra**

### Objective

Validate the actual product behavior without substitution: use the D6e silent-look-ahead output only through its original speech-duration frame boundary, preserve the creator face as the final visible visual, and finish the terminal delivery at the verified final-speech endpoint. The added model context is trimmed; no B-roll, black, Typography, frozen face or unrelated visual may follow it.

### Task contract

- **Allowed modules:** D6e evidence, existing provider-neutral narration/timeline close-out utilities and focused tests, plus timeline/render utilities and this status record.
- **Stable interfaces:** consent/provider accounting, Voice/Talking provider contracts, verified source/master provenance and D4/D5 routing/settings semantics.
- **Acceptance:** expose a reusable terminal-delivery policy that derives a frame-safe end from the final verified transcript segment (not a literal word); export one face-visible candidate ending there with audio/video duration equal; retain only the original speech audio samples, not appended model silence; verify container/timing/freeze and stop for U-Talking review of the final visible mouth frame.
- **Non-goals:** no new model/provider call, no pixel mouth edit, B-roll/black/Typography substitution, cloned/frozen final face, arbitrary word rule or long-Talking claim.

### Fixed constraint

This is a terminal-delivery choice, not a general rule to truncate narration between scenes. It applies only when the narrative itself ends at the final verified speech segment; if the face is not naturally closed at that exact boundary, D6f fails rather than hiding it.

### Result

- Added the provider-neutral, opt-in terminal-delivery policy. It derives the final verified transcript boundary without inspecting a literal word, permits a terminal cut only when that timestamp is exactly frame-aligned, and otherwise refuses to remove a partial final phoneme or add unverified tail audio. The normal master-narration route no longer silently substitutes Typography for an AI Talking tail.
- Exported `content-os-data/latentsync-duration-boundary-20260915/gate-d6f-face-visible-speech-complete.mp4`: the first Talking segment is 1.200s, real-source middle is 1.920s, and the final 1.800s is the D6e silent-look-ahead Talking output cropped to its original-speech interval. No B-roll, black, typography, generated duplicate frame, or model-only silent suffix follows the final face.
- `ffprobe` verifies 1280×720, 25fps, 123 frames, and exactly 4.920s for both video and audio. `freezedetect` found no >=0.5s frozen interval and `silencedetect` found no >=0.3s silent interval. The final six frames are retained at `content-os-data/latentsync-duration-boundary-20260915/gate-d6f-last-six-face-frames.jpg` for audit only; automated checks cannot decide whether the mouth has naturally settled.

**Stop:** Review `content-os-data/latentsync-duration-boundary-20260915/gate-d6f-face-visible-speech-complete.mp4` from start to end, with particular attention to the final spoken mouth frame. It must be a natural, closed-mouth face at the exact speech endpoint. If not, mark D6f FAIL; do not hide or extend it with another visual. Do not tune or promote until the user responds.

### U-Talking result

- The reviewer found the final face still slightly pursed. D6f therefore fails its strict face-visible natural-close criterion. The 200ms model-only silent suffix was insufficient for this take; the terminal cut and no-substitution policy remain valid, but do not establish a usable visual close-out.

## Gate D6g: extended silent-look-ahead Talking close-out

**State: PASS**
**Primary implementation model: Terra**

### Objective

Run one high-information local LatentSync retry with the same verified 1.800s second-clause speech, the same 175ms leading silent alignment context, and a longer **600ms model-only trailing silence**. Export only the original speech interval into a terminal delivery ending on the creator face at the verified 4.920s master-speech boundary.

- **Allowed modules:** D6 evidence and the existing local LatentSync evaluation runner/assets, provider-neutral Talking close-out utility and focused tests, timeline/render utilities, and this status record.
- **Stable interfaces:** consent/provider accounting, Voice/Talking provider contracts, verified source/master provenance, and D4/D5 routing/settings semantics.
- **Acceptance:** one local provider run completes with persisted provenance and automated Talking QA; a single face-visible terminal candidate contains no added silent-audio samples, B-roll/black/Typography/frozen-face tail, or unrelated visual after speech; container durations match at 4.920s; `freezedetect`/`silencedetect` are checked; stop for U-Talking review.
- **Non-goals:** no provider/model change, paid call, mouth pixel edit, multi-value tuning sweep, visual cover-up, generalized long-Talking claim, or promotion without human review.

### Bounded search rule

This is exactly one retry, changing only model-driving trailing silence from 200ms to 600ms. The source reference interval may begin earlier only to supply the required real reference duration; the exported visual still begins at the original speech boundary and ends at it. If the final face is still pursed, close D6g FAIL rather than tuning again.

### Result

- Built `gate-d6g-lookahead-175ms-prefix-600ms-suffix.wav` from the approved original second-clause samples plus model-only 175ms leading and 600ms trailing silence. Waveform `silencedetect` measured 175ms and about 600ms respectively; independent local faster-whisper-medium Voice QA passed copy coverage 1.0, zero missing/duplicate/substitution tokens, playable audio, and 675ms longest detected silence under the explicitly raised 1s input-context limit.
- One local LatentSync-1.5 call completed with durable provider-call evidence (`amount: 0 USD`, no external provider charge), 2.575s driving audio, a 2.705–5.506s real source-reference window solely to fit that input, automated Talking QA passed (playable, 25ms duration drift, copy coverage 1.0).
- Exported `content-os-data/latentsync-duration-boundary-20260915/gate-d6g-600ms-lookahead-face-visible-closeout.mp4`. It has the same strict terminal composition as D6f: 1.200s approved first Talking, 1.920s real-source middle, 1.800s D6g face Talking cropped from 0.200s to 2.000s, and the original master audio trimmed at 4.920s. No look-ahead audio, B-roll/black/Typography, frozen face, or unrelated visual follows the face.
- `ffprobe` verifies 1280×720, 25fps, 123 frames, exactly 4.920s for each audio/video stream. `freezedetect` found no >=0.5s frozen interval; `silencedetect` found no >=0.3s silent interval. Last-six-frame audit sheet: `content-os-data/latentsync-duration-boundary-20260915/gate-d6g-last-six-face-frames.jpg`.

### U-Talking result

- The reviewer judged the 600ms silent-look-ahead terminal close-out natural enough to pass. This is local evidence only for LatentSync-1.5 on the current machine and this short 1.800s terminal segment; it is not a general long-Talking claim or an automatic global parameter default.

## Completed work package — Gate D7: portable terminal Talking close-out profile

**State: PASS**
**Primary implementation model: Terra**

### Objective

Turn the D6g terminal close-out evidence into a provider-neutral, portable capability slice. The Core expresses the editorial need for a face-visible terminal Talking end; each provider schema describes only its own controllable temporal-context parameters. The parameter's semantic scope is provider/model adapter behavior, while human-quality evidence remains scoped to its observed machine/reference conditions.

- **Allowed modules:** existing `app.routing` capability/override/settings schemas, the provider-neutral Talking close-out planner, narrowly related API/persistence/UI modules and focused tests, plus active control documents.
- **Stable interfaces:** consent, budget, license, provenance and runtime-integrity gates; provider-neutral Voice/Talking contracts; ScenePlan/Asset Router semantics; durable provider-call accounting; existing D4 precedence and D5 saved provider+machine override behavior.
- **Acceptance:** express terminal face-closeout capability separately from any vendor parameter; support a provider-owned adjustable trailing look-ahead setting and a declared support state; retain one verified D6g profile value only for the current LatentSync machine key with provenance; resolve `job > saved provider+machine > local verified > provider default > unknown`; expose effective value, provenance and reset in Advanced Settings; tests prove cross-machine quality-evidence isolation, provider substitution/unknown behavior, override reset and hard safety gates.
- **Non-goals:** no new provider/model installation, no paid call, no general claim that every ending is solved, no automatic promotion of a user override, no new subjective media evaluation, no ScenePlan duration distortion and no distributed scheduler.

### Bounded implementation rule

D7 may record the D6g 600ms setting as **verified only** for the exact current LatentSync-1.5/local-compatibility/current-machine profile and its evidence reference. The same 600ms value may be exposed as the current LatentSync-1.5 adapter's conservative `provider_default` on another compatible machine, but that does not copy the human-quality pass. A provider that cannot declare/support temporal look-ahead must resolve as unsupported/unknown, not silently substitute a visual workaround.

### Completion

- Added provider-neutral `terminal_face_closeout` feature support states (`verified`, `available`, `unsupported`, `unknown`) to capability resolution. `available` means an admitted Content OS adapter has a safe mechanism, never that a new machine has passed visual quality; absence remains `unknown`.
- Added schema v20 and a persisted provider+model+runtime+machine capability-profile record separate from editable Advanced Settings. Only `talking:local:latentsync:LatentSync-1.5:local-compatibility:asus-rtx3060-laptop-6gb` seeds the D6g U-Talking evidence: `trailing_silence_lookahead_ms=600`, terminal-face-closeout `verified`, provenance and artifact reference. Other machine keys start with no human-quality evidence.
- LatentSync owns the adjustable `trailing_silence_lookahead_ms` setting, shown as **结束静音前瞻（毫秒）**: it is the silent model-input context after speech, used to eliminate the visible bug “speech has ended but the mouth still appears to be speaking.” Core never exposes this Provider name or parameter as universal. The current LatentSync-1.5 adapter exposes 600ms as its conservative `provider_default`; a saved other-machine override is labeled `user_override` and returns to the provider baseline after reset. The D6g U-Talking result itself remains current-machine evidence only.
- Focused routing/settings/migration/Talking tests passed (56 passed, 2 pre-existing dependency warnings). Production Web build passed. No Provider was installed, changed, called or promoted; no user override was converted to evidence.

## Completed work package — Gate D8: terminal close-out execution handoff

**State: PASS**
**Primary implementation model: Terra**

### Objective

Connect the D7 resolution to the existing Talking job boundary without leaking LatentSync parameters into Core. The product-level intent is to solve the visible bug **“speech has ended but the mouth still appears to be speaking.”** A terminal Talking request carries that intent and the final verified Voice-QA speech timestamp; the chosen adapter receives validated provider-owned temporal context, lets the model see post-speech silence, then returns only the creator-face video and original narration through that speech endpoint. Missing support/evidence must be a recoverable non-claiming outcome, never a visual substitution.

- **Allowed modules:** existing Talking job payload/handler/provider protocol, LatentSync adapter, D7 routing/settings APIs and focused tests, plus active control documents.
- **Stable interfaces:** consent/budget/license/provenance/runtime-integrity gates, provider-call ledger/idempotency, core ScenePlan/Asset Router contracts, generated-asset QA, and existing ordinary Talking generation behavior when no terminal intent is requested.
- **Acceptance:** terminal intent and the final verified speech timestamp are explicit and provider-neutral; LatentSync maps its resolved leading/trailing context inside its adapter, requires sufficient continuous reference duration, crops model context from the delivered video, preserves only original narration audio through that speech endpoint, and rejects raw truncation before normalization; the feature is never auto-enabled on an ordinary job, unknown/unsupported providers are rejected, and an unverified machine keeps its quality status unverified; deterministic tests cover normal generation, verified execution, unknown/unsupported rejection and no frozen-tail normalization.
- **Non-goals:** no new model run, no new provider, no visual workaround, no universal Core parameter default, no paid call, no claim that a user override is verified, and no long-Talking capability claim.

### Bounded implementation rule

Implement the adapter and job-boundary contract only. Do not run an additional D6-quality experiment: D6g remains the sole human-quality evidence. An ordinary Talking job never activates close-out silently; an explicitly requested LatentSync-1.5 job may resolve to its 600ms adapter baseline or a scoped user override, then still requires the existing QA/U-Talking gates. Another machine does not inherit the D6g pass.

### Completion

- The durable Talking Job now records explicit `terminal_face_closeout` intent, the **final verified speech endpoint**, resolved provider parameters and their provenance. Ordinary Talking Jobs preserve the prior provider call shape and do not enable the feature implicitly.
- The terminal Job API requires Voice-QA persisted timing, resolves the selected Content OS adapter protection and provider-owned parameters through the existing precedence rules, refuses unknown/unadapted providers, and records the scope used. The Web Talking form exposes the intent in plain language: the model sees post-speech silence but the delivery ends at the original speech endpoint, with no black frame, frozen frame or text cover-up.
- The LatentSync 1.5 adapter owns the actual temporal treatment: its internal 175ms leading alignment and resolved **结束静音前瞻（毫秒）** are model input only; it trims the original narration to the final speech timestamp before inference, requires a sufficiently long continuous reference, rejects a short raw result before normalization, trims away all model-only context, and remuxes the original audio only through that timestamp. The terminal branch never uses cloned-frame padding.
- Focused routing/settings/repository/closeout/assembly/Talking tests passed (**58 passed**, two pre-existing TestClient dependency warnings); the production Web build and documentation check passed. No model run, paid call, Provider change, or new quality claim was made.

### Known limitation

- D6g is still the only U-Talking evidence for a natural visible closeout: short 1.800s terminal LatentSync-1.5 on the recorded local configuration/reference conditions. A compatible new machine may execute the adapter baseline but must not be labeled human-quality verified until it passes QA and review. A selected reference clip must also be long enough for the spoken endpoint plus model context.

### Next ready task

Gate D3b is active below. Do not treat its single result as proof of long Talking or multi-segment continuity.

## Completed work package — Gate D3b: refine fresh-Voice duration bracket

**State: PASS**
**Primary implementation model: Terra**

### Objective

Run exactly one high-information natural fresh-Voice Talking midpoint to determine whether the current 2.58s PASS / 5.12s FAIL bracket can safely expand the short-Talking routing interval. The existing 3.12s file is excluded because Voice QA found 539ms leading silence; it is neither a valid natural midpoint nor a passed Voice input.

### Task contract

- **Allowed modules:** D1 approved fresh-Voice audio and its local evaluation evidence, existing FFmpeg/Voice QA/Talking Job/QA paths, one new local evidence script/artifact and this status record.
- **Stable interfaces:** D8 terminal-closeout execution path, existing ordinary Talking job behavior, consent/provider accounting, D4/D5 resolution, and the 2.58s/5.12s product-evidence bracket.
- **Fixed input:** trim only the verified 460ms pre-speech silence from the D1 5.12s fresh OmniVoice take, retain the complete two-clause natural sentence through its existing 4.92s speech endpoint (4.46s delivery), and use the same ordinary authorized creator reference. This is the closest available natural endpoint to the 3.85s midpoint; no new copy, Provider or parameter is introduced.
- **Acceptance:** independently re-run Voice QA on the trimmed audio; execute one durable ordinary LatentSync Talking Job; run playable/duration/copy/freeze technical checks; preserve an artifact and evidence; then stop at explicit U-Talking review. The review asks only whether this 4.46s natural segment is visibly in sync and publishable from start to finish.
- **Non-goals:** no D8 closeout tuning, no multiple duration values, no new Provider/model, no paid call, no continuity splice, no visual concealment, no long-Talking claim and no automatic capability-profile promotion.

### Bounded search rule

This is one run only. If automated Voice/Talking QA or U-Talking fails, record a 2.58–4.46s usable bracket and close D3b; if U-Talking passes, record a 4.46–5.12s bracket and close D3b. Do not run another midpoint within this package.

### Result and U-Talking review

- The D1 U-Voice-approved full natural take was trimmed only by its independently observed 460ms pre-speech silence, producing `gate-d3b-fresh-voice-natural-4460ms.wav`. Fresh local faster-whisper-medium Voice QA passed: full copy coverage, zero missing/duplicate/substitution tokens, zero leading silence, 140ms maximum silence, playable output, and a persisted final speech timestamp of 4.320s.
- One ordinary (non-D8-closeout) durable LatentSync-1.5 Job completed in about 4m56s with the same authorized ordinary creator reference, one local ASR call and one local Talking call (both recorded as 0 USD local execution). Its automated Talking QA passed: playable video+audio, 20ms duration drift, inherited verified copy QA; `freezedetect` found no >=0.5s frozen interval.
- Review artifact: `content-os-data/latentsync-duration-boundary-20260915/gate-d3b-fresh-voice-natural-4460ms.mp4` — 1280×720, 25fps, 4.480s container duration. Evidence: `talking-d3b-natural-midpoint-result.json`.

The reviewer judged the complete 4.46s natural delivery **natural and publishable**. D3b closes PASS under its one-run search rule. The usable fresh-Voice bracket is now 4.46s PASS / 5.12s FAIL. This does not promote a long-Talking claim or prove multi-segment continuity.

## Completed work package — Gate D9: Content OS terminal-closeout protector semantics

**State: PASS**
**Primary implementation model: Terra**

### Objective

Correct the product boundary for the visible defect “the narration has ended but the mouth still appears to be speaking.” Content OS must own the request for a natural, face-visible closeout and may implement it in an adapter even where the upstream Provider has never recognized or documented the defect.

### Task contract

- **Allowed modules:** existing execution capability/schema resolution, terminal Talking dispatch, LatentSync adapter-facing configuration/UI copy, focused routing/API tests, and active control documents.
- **Stable interfaces:** provider-neutral Core Talking intent and final speech timestamp; D4 precedence; consent/budget/license/provenance/runtime-integrity gates; durable Job/provider-call accounting; no visual workaround policy.
- **Acceptance:** distinguish an admitted `content_os_adapter` implementation from a `provider_native` implementation; route the product-level closeout request through an admitted adapter even without a vendor claim; retain `trailing_silence_lookahead_ms` as a LatentSync-only advanced mapping; describe an unadapted Provider as needing Content OS adapter work rather than as having silently solved/no bug; preserve explicit `unknown`/`unsupported` and reject unsafe execution; cover semantics with focused tests, Web build and docs check.
- **Non-goals:** no new Provider/model install or call, no paid API, no universal numeric parameter, no claim that every Provider or source ending is solved, no visual/audio concealment, no new human-quality run, and no upstream GitHub Issue/PR.

### Bounded implementation rule

This package changes ownership/provenance semantics and messages only. It must not alter the proven LatentSync temporal algorithm or trigger another media run. A future Provider receives the same product request only after its adapter is shown to implement a safe native or Content-OS context-and-crop route; until then the product must say so explicitly.

### Completion

- The provider-neutral feature schema now records its implementation owner. Current LatentSync terminal closeout is explicitly `content_os_adapter`: the adapter’s context-and-crop route is Content OS behavior, not an assertion that upstream LatentSync knows this defect or exposes a matching control.
- The product-level Talking request remains **让人脸在声音结束时自然收口**. The Web explanation and API rejection now say that an unadmitted Provider is **not yet adapted to Content OS terminal face-closeout protection**. It is neither sent LatentSync’s `trailing_silence_lookahead_ms` nor treated as solved; no black frame, frozen face, text or audio concealment route was added.
- `trailing_silence_lookahead_ms` remains a LatentSync-only Advanced Settings mapping, with its existing fixed precedence and 600ms baseline. It is not a generic provider or machine-speed parameter. Future adapters may expose a native Provider mapping or another validated Content OS technique under the same product protection.
- Focused execution/dispatch/closeout/LatentSync tests passed (**30 passed**, two pre-existing TestClient dependency warnings); the production Web build and documentation governance check passed. No model run, paid API, Provider installation or new human-quality claim was made.

## Completed work package — Gate D10: local GPU inference lease

**State: PASS**
**Primary implementation model: Terra**

### Objective

Prevent concurrent local **Voice generation** and **Talking generation** workers on the same GPU resource from executing model inference simultaneously and contending for VRAM.

### Task contract

- **Allowed modules:** existing SQLite migrations, `app.jobs` claim/heartbeat/transition logic, local worker CLI configuration/assembly, focused worker/job-store tests, and active control documents.
- **Stable interfaces:** persisted Job contract and idempotency, existing JobRunner lease/recovery semantics, provider-call accounting/budget boundaries, provider-neutral Voice/Talking contracts, and all non-GPU job routing.
- **Acceptance:** one named local GPU resource is held atomically with a claimed Voice/Talking job; a second worker skips locked GPU inference work without consuming its attempt budget and can still claim unrelated eligible CPU work; ownership is renewed with the Job heartbeat and released on completion/failure/recovery; a crash/expired lease becomes reclaimable; a worker has a deterministic, overrideable resource key; focused migration/job/worker tests and docs check pass.
- **Non-goals:** no Redis/Celery/distributed scheduler, no GPU discovery/autotuning, no new Provider/model or model run, no general CPU/ASR/render serialization, no queue-priority product UI, and no cross-machine resource sharing.

### Bounded implementation rule

Only `generate_voice` and `generate_talking` are gated, and only when a local worker explicitly supplies the same GPU resource key. The lease is SQLite-local to the project database; a different computer must use a different key. This prevents observed same-machine VRAM contention without claiming a cluster scheduler.

### Completion

- Added schema v21 `local_resource_leases` and integrated its atomic acquisition into the existing JobStore claim transaction. A resource-blocked Voice/Talking job remains pending with its attempt count unchanged; the same worker can continue to a later eligible CPU job.
- The existing Job heartbeat renews any held GPU resource, and successful completion, retryable failure, terminal failure, crash recovery and attempt exhaustion release or replace the lease safely. No handler runs inside the SQLite write transaction.
- Local workers now map `generate_voice` and `generate_talking` to a deterministic default `gpu:<hostname>` key. `--gpu-resource-key` (or `CONTENT_OS_GPU_RESOURCE_KEY`) supports a stable machine/GPU-specific override such as `gpu:asus-rtx3060-laptop-6gb`; other Job types remain ungated.
- Focused migration/job-store/runner/worker tests passed (**79 passed, 1 skipped**); adjacent worker/Voice/Talking/closeout regressions passed (**50 passed**, two pre-existing TestClient dependency warnings); documentation governance check passed. No model run, paid API, Provider or quality claim was added.

## Completed work package — Gate E1: one bounded R1 end-to-end export proof

**State: FAIL**
**Primary implementation model: Terra**

### Objective

Create one 30–60s vertical product sample for the existing user-provided project **“U1候选 4｜H.265与码率画质”**: editable new copy, authorized creator’s new OmniVoice narration, one new short creator Talking segment, authorized real source clips, typography and time-aligned subtitles.

### Task contract

- **Allowed modules/data:** the existing H.265 project and its user-provided local source assets, current authorized Voice/Talking profiles, normal durable Job/API/renderer paths, current local GPU lease, active product UI/API code, local evaluation evidence and control documents.
- **Fixed route:** create one new natural H.265/bitrate script; create one full narration and one matching opening Talking phrase no longer than 4.46s; use the same narration timeline over real B-roll for the remaining duration. The Talking clip is a short hook only, never repeated or presented as continuous long Talking.
- **Acceptance:** full narration passes independent Voice QA; the short new Talking job passes technical QA and uses the approved route; output is 30–60s vertical, playable, contains the new voice, one face-visible new Talking segment, real authorized B-roll, readable typography and aligned subtitles; preserve job/render evidence and stop for a single explicit U-Product review.
- **Non-goals:** no additional Talking duration probe, no second model/provider, no paid API, no unlicensed media, no manual per-scene audio cutting, no silent filler or concealed Talking ending, no claim of a second repeatable R1 topic, and no automatic promotion of this artifact to general capability evidence.

### Bounded search rule

Use exactly one full-Voice generation/QA run and one short Talking generation run. If either technical gate fails, close E1 FAIL with its evidence; do not tune or retry inside this package. If render succeeds, set `AWAITING_U_REVIEW` and stop for review.

### Completion

- The one durable local CUDA OmniVoice Job generated the new H.265/bitrate script as a 34.120s WAV and recorded a 0 USD local Voice provider call. The subsequent independent faster-whisper-medium QA Job recorded its own 0 USD local ASR call.
- Voice QA failed: playable output and 0.99296 copy coverage, but **one missing token** and **679ms leading silence** exceeded the product 500ms limit. The generated audio remains marked `qa_state: failed`; it was not admitted as a master narration.
- Per the one-run rule, no trim, retry, parameter change, short Talking request, output render or U-Product review followed. Evidence is the durable `content-os-data/r1-e1-20260920/e1-voice.sqlite3` ledger and its generated local audio.
- This is a valid product-path boundary result: the short D1/D3b Voice/Talking evidence must not be extrapolated into reliable 30–60s one-shot OmniVoice narration.

## Completed work package — Gate E2: QA-failed long-Voice recovery decision

**State: PASS**
**Primary implementation model: Terra**

### Objective

Make a QA-failed generated narration expose one deterministic, product-owned recovery route before another media run. The route must distinguish an independently verified copy failure from a leading-silence-only defect; it must not quietly treat a trim as a repair for missing speech.

### Task contract

- **Allowed modules:** `app.voice_qa`, a narrow provider-neutral Voice recovery module, generated-audio metadata/API serialization already used by the Voice flow, focused Voice QA/recovery tests, active control documents.
- **Stable interfaces:** `VoiceProvider`, generated-audio and `VoiceQaJobPayload` contracts, provider-call accounting, consent, existing independently timed QA evidence, `VoiceTakeComposer`, Talking admission and assembly gates.
- **Acceptance:** every persisted Voice QA result carries a deterministic recovery recommendation; leading-silence normalization is eligible only when it is the sole blocking issue and must still receive fresh QA after a derived, provenance-preserving audio transform; any missing/duplicate/over-limit substitution content chooses bounded sentence-take regeneration and is never cleared by a trim; unrecognized/no-timing/audio failures remain blocked for provider/manual diagnosis; verified takes recommend no recovery. The recommendation is visible through the existing audio asset surface and has focused tests.
- **Tests/build:** focused Voice QA/recovery and Voice-generation Job/API tests; `python scripts/check_docs.py`.
- **Non-goals:** no new model/provider, no paid API, no actual Voice/Talking run, no automatic trimming or re-generation, no modification/deletion of the failed source audio, no claim that sentence composition is visually continuous Talking, and no human quality gate.

### Bounded design rule

The E1 result contains both leading silence and missing copy, so this package must recommend sentence-level recovery rather than normalization. The existing `VoiceTakeComposer` remains the only permitted continuous-audio assembly mechanism: it accepts only individually QA-verified takes and requires a fresh master QA before final admission.

### Completion

- Added provider-neutral `app.voice_recovery`. Every persisted generated-Voice QA result now records a visible, non-executing recovery recommendation in `voice_generation.recovery`, returned by the existing Audio Asset API with the QA evidence.
- The deterministic safety rule is now executable: verified takes require no recovery; only a sole leading-silence failure may request a **new derived audio asset plus a fresh independent QA**; missing, duplicate or excessive substituted copy always selects `regenerate_sentence_takes`; missing timing, corrupt audio or long-silence failures require provider diagnosis. The original failed audio and its QA evidence are never mutated or admitted.
- E1 therefore has the correct next action: `regenerate_sentence_takes`, not leading-silence normalization. Existing `VoiceTakeComposer` remains the continuous-audio assembly guard: every take must independently pass QA, and the composed master must pass QA again before Talking or render.
- Focused Voice QA/recovery, Voice Job/API and take-composition tests passed (**14 passed**, with two pre-existing TestClient dependency warnings); documentation governance check passed. No model was run and no provider cost, license state or quality evidence changed.

## Completed work package — Gate E3: bounded sentence-take long narration

**State: FAIL**
**Primary implementation model: Terra**

### Objective

Test the E2 `regenerate_sentence_takes` recovery route once for the failed E1 H.265 topic without disguising any failed take as a usable master narration.

### Fixed execution plan

Use this exact four-part natural-copy partition, in order, with one CUDA OmniVoice generation and one independent QA per part:

1. `很多人看到 H.265 体积更小，就以为码率可以随便压低。`
2. `其实编码格式解决的是压缩效率，不会凭空创造细节。画面里有纹理、有快速运动，或者还要经历平台二次压缩时，过低码率会先丢掉最难恢复的信息。`
3. `正确顺序不是先问用哪种格式，而是先看素材、平台和交付场景，再为画质留出足够码率。`
4. `把编码和码率分开判断，上传后的画面才不会突然发糊。`

### Task contract

- **Allowed modules/data:** the existing authorized E1 project/profile/reference, normal durable Voice/QA Job path, `VoiceTakeComposer`, local evaluation evidence and active control documents.
- **Stable interfaces:** Voice/Talking provider-neutral contracts, consent, provider-call accounting/budget, E2 recovery metadata, generated-audio admission gate and final assembly contracts.
- **Acceptance:** exactly four new Voice takes and at most four corresponding individual QA Jobs; assemble only if all four verify; one fresh independent QA of the PCM master; preserve per-take/master provenance, real durations and QA results. If the master verifies, stop for U-Voice review; Talking/render remains a separate package.
- **Tests:** media-file/playability and persisted timing/copy/silence evidence for each executed take/master, plus `python scripts/check_docs.py` on handoff.
- **Non-goals:** no retry of a failed take, no normalizing/cropping a failed take, no fifth take, no new provider/paid call, no Talking, no render, no claim of an R1 export, and no automatic quality-evidence promotion.

### Bounded stop rule

If any individual take fails technical QA, do not generate remaining takes or a master; close E3 `FAIL` with the durable evidence. If all four individual takes pass but the assembled master fails, close `FAIL` without Talking/render. Only a passed master may enter `AWAITING_U_REVIEW` for U-Voice.

### Completion

- The first local CUDA OmniVoice take was generated as a playable 5.840s WAV and recorded a 0 USD local Voice call. Independent faster-whisper-medium QA rejected it with **one missing token** and **three substitutions above the allowed ratio** (`copy_coverage: 0.95455`). Its 479ms leading silence was within the 500ms product limit, so leading-silence normalization was neither selected nor sufficient.
- Per the fixed stop rule, the remaining three takes were not generated; no take composition, master QA, Talking job, render or U-Voice review occurred. The E3 runner's final evidence-summary property access was corrected after the stop; it did not rerun inference or alter the durable Job/Audio/Provider-call records.
- Evidence is the isolated durable ledger `content-os-data/r1-e3-20260920/e3.sqlite3`, its first generated local WAV and `run_e3_sentence_takes.py`. This is a second independent indication that current local OmniVoice cannot yet be claimed as a reliable long-form fresh-Voice route, even when Content OS uses product-owned sentence composition.
- Focused E2 Voice QA/recovery, Voice Job/API and take-composition tests remained green (**14 passed**, with two pre-existing TestClient dependency warnings); documentation governance check passed before the media run. No paid call, new provider, Talking quality claim or export was made.

## Completed work package — Gate E4: long-Voice reliability decision

**State: PASS**
**Primary implementation model: Terra**

### Objective

Use the already-downloaded local `faster-whisper-small` snapshot to perform one read-only alternate-ASR check of the two failed OmniVoice long-form candidates (E1 full take and E3 first sentence take), without overwriting their medium-model QA or pretending that a second decoder is human review.

### Task contract

- **Allowed modules/data:** the immutable E1/E3 generated local WAVs and durable ledgers, the already-installed local faster-whisper runtime/model snapshot, local evaluation evidence and active control documents.
- **Stable interfaces:** original generated AudioAssets, their existing QA metadata/provider-call ledgers, QA thresholds, Voice/ASR provider contracts and all admission gates.
- **Acceptance:** run exactly one alternate local ASR transcription per failed candidate; compare it to the same requested copy using the existing QA comparator; record decoder/model/timing/copy evidence without mutating the original records. Concordant blocking copy evidence closes current OmniVoice long-form as unsupported; any disagreement retains the technical failure and identifies ASR adjudication uncertainty rather than promoting an audio take.
- **Tests:** real playable-input transcription evidence for both candidates and `python scripts/check_docs.py` at handoff.
- **Non-goals:** no new Voice generation, no retry, no Voice/Talking Provider installation, no paid call, no changed QA threshold, no update to an existing AudioAsset, no Talking/render and no U-Voice claim.

### Bounded stop rule

The existing small snapshot is the sole alternate decoder. Do not download or install another model. This is corroboration only: neither an alternate pass nor a decoder disagreement can make a medium-QA-failed narration eligible for composition or render.

### Completion

- One read-only faster-whisper-small transcription was run for each existing failed audio; no Voice/Talking inference, provider installation, original QA mutation or paid call occurred. Evidence: `content-os-data/r1-e4-20260920/e4-alternate-asr.json`.
- Both decoder sizes kept E1 and E3 technically failed. E1 independently retained excessive leading silence (640ms under small) and reported one missing token. E3 independently retained the missing-token report and blocking substitutions.
- The alternate evidence found a specific comparator defect: both ASR models emit `H265` where the requested copy writes `H.265`; the current token stream splits the latter into `h` + `265`. It also exposed an unresolved Mandarin recognition/Voice ambiguity: medium reported `马力`, while small reported `马率`, for requested `码率`. Neither result can be promoted to passed copy coverage or human Voice quality evidence.
- E4 therefore preserves the current OmniVoice long-form route as unsupported, but opens a narrow QA notation-integrity repair before future evidence is interpreted. Documentation governance check remains required after that repair.

## Completed work package — Gate E5: Voice QA technical-notation normalization

**State: PASS**
**Primary implementation model: Terra**

### Objective

Repair the deterministic copy-comparison defect in which an alphanumeric technical identifier separated by punctuation, such as `H.265`, is treated as different from the identical spoken/ASR form `H265`.

### Task contract

- **Allowed modules:** `app.voice_qa`, focused Voice QA tests and active control documents.
- **Stable interfaces:** persisted historical QA evidence, ASR transcript storage, VoiceProvider/ASR contracts, copy/missing/duplicate/substitution thresholds, recovery routing and all admission gates.
- **Acceptance:** the ephemeral comparison stream normalizes punctuation only when it separates adjacent ASCII letters/digits within one technical identifier; it does not remove CJK punctuation generally, equate Chinese homophones, alter stored text/transcripts or mutate historical QA. Tests cover `H.265`/`H265`, retain ordinary token boundaries and retain blocking `码率` versus `马力` evidence.
- **Tests:** focused Voice QA suite and `python scripts/check_docs.py`.
- **Non-goals:** no Voice/ASR model call, no reclassification of E1/E3 persisted records, no homophone relaxation, no QA-threshold change, no Talking/render/provider work or human-quality claim.

### Completion

- `comparison_tokens` now normalizes `.` / `_` / `-` only when each separates an ASCII letter/digit from a following digit in a technical identifier. `H.265` and `H265` therefore compare as the same token, while ordinary prose punctuation and CJK content remain unchanged.
- The repair does not reclassify the immutable E1/E3 records. It deliberately does not equate Mandarin homophones: `码率` versus medium-ASR `马力` remains a blocking substitution, so neither failed narration becomes eligible.
- Focused Voice QA/recovery, Voice Job/API and take-composition tests passed (**15 passed**, with two pre-existing TestClient dependency warnings). No model call, provider or human-quality claim was added.

## Completed work package — Gate E6: verified-master Talking slice planning

**State: PASS**
**Primary implementation model: Terra**

### Objective

Implement the first executable product step of the confirmed workflow: plan a short Talking audio slice from a QA-verified Master Narration using only complete persisted speech intervals and a provider-supplied duration limit. It must never give a full 30–60s master to a short-capability Talking adapter.

### Task contract

- **Allowed modules:** a narrow `app.talking` planning module, provider-neutral domain/audio types, focused Talking-slice tests and active control documents.
- **Stable interfaces:** Voice/ASR/Talking provider contracts, persisted QA evidence, MasterNarration/timeline semantics, Execution Planner/provider settings, consent/budget/accounting and existing Talking Jobs.
- **Acceptance:** a plan requires a QA-verified generated master with real timed transcript evidence; it selects only whole contiguous transcript intervals; it rejects out-of-range/noncontiguous/unverified requests; it returns exact master asset provenance, speech start/end, requested text and duration; a caller supplies the provider-scoped maximum, and an over-limit plan is rejected. Tests cover valid short selection and every rejection class.
- **Tests:** focused slice/Voice QA tests and `python scripts/check_docs.py`.
- **Non-goals:** no media extraction, no Job/API payload change, no provider/model call, no hard-coded LatentSync duration in universal contracts, no auto-selection of narrative scenes, no Talking/render and no claim of long-form Voice availability.

### Completion

- Added provider-neutral `app.talking.slices`. A `TalkingAudioSlicePlan` can be created only from a QA-verified generated master narration with persisted independent transcript timing; its boundaries are whole, adjacent transcript intervals, never caller-supplied arbitrary milliseconds.
- The plan retains master AudioAsset identity, transcript provenance, exact speech endpoints, text and source interval indices. Its maximum duration is injected by the selected provider/execution profile, so the universal contract contains no LatentSync duration value.
- Invalid master QA/timing, out-of-range or noncontiguous selections, and provider-over-limit requests reject deterministically. This is the first executable layer of `verified master → bounded Talking slice`; it does not yet extract media or dispatch a provider.
- Focused Talking slice/closeout and Voice tests passed (**28 passed**, two pre-existing TestClient dependency warnings). No model run, Provider call, new quality evidence or export was made.

## Completed work package — Gate E7: master-slice extraction

**State: PASS**
**Primary implementation model: Terra**

### Objective

Materialize an E6 bounded Talking slice as temporary local PCM while retaining complete master-relative provenance and never changing the verified master audio.

### Completion

- `extract_talking_audio_slice` invokes local FFmpeg only after validating that the slice belongs to the supplied QA-verified master. It exports a PCM WAV for exactly the transcript-derived interval, retains generated-Voice QA state, rebases the selected transcript timing to the slice, and records the master AudioAsset ID plus absolute endpoints in transient metadata.
- Extraction rejects another master's plan, unavailable FFmpeg, timeout and invalid output. The master asset is neither edited nor re-imported, and the temporary slice is not promoted as a master narration.
- Focused slice extraction/planning and terminal-closeout tests passed (**17 passed**). No model/provider call, Talking job, render or quality claim was made.

## Completed work package — Gate E8: capability-bounded Talking slice limit

**State: PASS**
**Primary implementation model: Terra**

### Completion

- Added `resolve_verified_operating_limit` to the provider-neutral execution layer. It returns an automatic ceiling only from a matching locally verified operating pass bound; failed bounds, provider defaults, documentation and other machines remain explicitly unknown.
- Focused execution-routing and Talking-slice tests passed (**19 passed**). No provider/model call or quality claim was made.

## Completed work package — Gate E9: capability-bounded Talking slice worker dispatch

**State: PASS**
**Primary implementation model: Terra**

### Objective

Persist a capability-resolved Talking slice plan, then make the Worker revalidate and extract that short PCM before provider execution while preserving master-relative provenance.

### Task contract

- **Allowed modules:** Talking Job/API payloads, `app.talking.slices`, Talking Job handler/worker wiring, execution-capability resolver, focused tests and active control documents.
- **Stable interfaces:** existing unsliced Talking Jobs, Voice/Talking provider contracts, QA/consent/budget/accounting, terminal closeout, MasterNarration semantics.
- **Acceptance:** ordinary Talking requests stay unchanged; sliced requests persist only transcript indices plus a resolved provider+machine limit/provenance; Worker revalidates before extracting; Provider receives a transient short asset; output records master ID/absolute slice bounds; unknown limits reject.
- **Non-goals:** no model run, no user-supplied duration limit, no hard-coded global LatentSync value, no automatic scene selection, no Talking/render/human-quality claim.

### Completion

- `TalkingGenerationJobPayload` now has an all-or-nothing persisted slice execution plan: complete transcript indices, resolved duration ceiling and provenance are required together. A slice cannot be combined with terminal face closeout until a later explicit composition rule exists.
- The Worker re-plans from the stored master AudioAsset and indices, extracts E7's transient PCM, sends that short asset to the provider, then records original master ID, absolute slice endpoints and limit provenance on the generated Talking asset. Normal unsliced Jobs retain their prior provider call path.
- Focused Talking Job/slice/execution-routing tests passed (**27 passed**, with two pre-existing TestClient dependency warnings); documentation governance check passed. No model call or quality claim was made.

## Completed work package — Gate E10: capability-resolved Talking slice API

**State: PASS**
**Primary implementation model: Terra**

### Objective

The API accepts only sentence boundaries for a short Talking slice. It resolves the duration ceiling from the concrete provider + model + runtime + machine capability profile; callers cannot submit a duration.

### Allowed files/modules

`services/api/app/main.py`, Talking/routing adapters, API tests, and active control documents.

### Acceptance criteria

- A known, locally verified LatentSync profile resolves the recorded conservative ceiling and persists the resulting execution plan.
- A profile without local verified evidence is rejected rather than guessed.
- Normal non-slice Talking jobs retain their existing behavior.
- No model inference is run.

### Tests/build commands

Targeted Talking/API/routing tests; `python scripts/check_docs.py`.

### Non-goals

Changing the verified operating range, running an additional provider experiment, or exposing the setting in the product UI.

### Completion

- The Talking API accepts only transcript sentence indices for a short slice. It does not expose or accept a caller-supplied duration.
- For the exact locally verified LatentSync profile, it resolves the conservative 4.46s boundary and writes E9's complete persisted slice plan (`start`, `end`, resolved duration and `local_verified` provenance).
- A different or otherwise unverified machine has no synthetic fallback: the API returns 422 before creating a job. Normal unsliced Talking requests remain unchanged.
- Focused API/worker/slice/routing tests passed (**27 passed**, with two pre-existing TestClient dependency warnings). No model call or quality claim was made.

## Completed work package — Gate E11: capability-aware Talking slice-series planning

**State: PASS**
**Primary implementation model: Terra**

### Objective

Build the smallest provider-neutral planning seam for an ordered series of E10-compatible Talking slices.

### Allowed files/modules

Talking planning/adapters, API tests, and active control documents.

### Acceptance criteria

- The planner creates complete, ordered, non-overlapping sentence slices against one QA-verified master narration.
- The series retains one externally resolved provider+machine duration bound and explicit master-relative boundaries.
- It rejects an unknown capability and a sentence that cannot fit the verified bound.
- No model inference, visual stitching, or long-form quality claim is made.

### Tests/build commands

Targeted Talking/API/routing tests; `python scripts/check_docs.py`.

### Non-goals

Video generation, Job dispatch, image/timeline stitching, continuity-quality verification, and automatic user-facing routing.

### Completion

- Added a provider-neutral series planner that greedily packs only whole, adjacent transcript sentences within one already resolved duration bound. It never cuts through a sentence; a single over-limit sentence fails rather than being altered or silently clipped.
- The returned plan is exhaustive and ordered against the same QA-verified master narration. It carries master-relative slice boundaries and explicit consecutive handoffs (including any source-audio gap) for a later continuity layer.
- A missing duration bound is explicitly rejected as unverified. Resolution of that bound remains E10's provider+model+runtime+machine evidence path; this planner does not invent one.
- Focused Talking/API/routing tests passed (**29 passed**, with two pre-existing TestClient dependency warnings). No model call, video stitch, or long-form quality claim was made.

## Completed work package — Gate E12: durable ordered Talking slice-series dispatch

**State: PASS**
**Primary implementation model: Terra**

### Objective

Persist an E11 series and its E9-compatible child jobs atomically, with a durable parent identity, idempotent replay, and ordered execution eligibility.

### Allowed files/modules

Talking planning, domain/database/job-store/API modules, focused tests, and active control documents.

### Acceptance criteria

- One request atomically persists a parent series and its complete ordered child-job set, or neither.
- Repeating the same parent idempotency key returns that same set; a differing request is rejected.
- Only the next child whose predecessors completed is eligible to run.
- No model inference, visual stitching, or long-form quality claim is made.

### Tests/build commands

Targeted Talking/API/job-store/routing tests; `python scripts/check_docs.py`.

### Non-goals

Visual continuity implementation, child-result composition, paid/provider execution, or changing E10's evidence bound.

### Completion

- Added a durable TalkingSliceSeries parent record and a migration-backed repository. Its parent idempotency key and deterministic request fingerprint distinguish a valid replay from a different request using the same key.
- The Talking slice-series API resolves E10's exact local evidence bound, obtains E11's full sentence plan, and writes the parent plus all E9-compatible child jobs inside one immediate SQLite transaction. A write failure rolls back the entire set.
- Child payloads retain one master narration ID, resolved limit/provenance, series ID, ordinal and series size. Worker output provenance now retains those series fields too.
- Job claiming excludes a series child while any earlier child is not completed; a failed or cancelled earlier child consequently stops the sequence for explicit recovery rather than silently skipping continuity risk.
- Focused Talking/API/job-store/repository/routing tests passed (**48 passed**, with two pre-existing TestClient dependency warnings); documentation governance check passed. No model call, video stitch, or long-form quality claim was made.

## Completed work package — Gate E13: Talking slice-series result registry and human continuity gate

**State: PASS**
**Primary implementation model: Terra**

### Objective

Add a parent-level read model that reports each child result in source order and identifies the exact prerequisites for an explicit U-Talking continuity review.

### Allowed files/modules

Talking QA/read-model modules, API tests, and active control documents.

### Acceptance criteria

- The result registry reports every durable child in the parent order, including missing/duplicate output and QA state.
- It is ready for human continuity review only after every child completed, has one output, passed automated per-clip QA, and received an approved individual human Talking review.
- Any missing, failed, duplicate, or unreviewed child is represented as a reason; nothing is automatically composed or approved.
- No model inference, visual stitching, or quality promotion is performed.

### Tests/build commands

Targeted Talking/API tests; `python scripts/check_docs.py`.

### Non-goals

Generating a series, applying child QA, accepting the final human continuity judgment, or composing clips.

### Completion

- Added a read-only parent series assessment and API. It returns the durable child order, job status, one matching generated output (if present), automated Talking QA state, individual human Talking-review state, and every concrete blocker.
- A series is ready only after every child completed, has exactly one output, has automated QA marked verified, and has an approved individual human review. Missing/duplicate outputs, failed/cancelled jobs, provenance mismatch, or any unreviewed child prevents readiness.
- The ready state is deliberately only an invitation to a separate U-Talking continuity judgment. It does not compose clips, infer visual continuity, or promote a series to publishable.
- Focused Talking/API tests passed (**22 passed**, with two pre-existing TestClient dependency warnings). No model call or quality claim was made.

## Completed work package — Gate E14: series-level human continuity decision contract

**State: PASS**
**Primary implementation model: Terra**

### Objective

Persist one explicit reviewer decision for an E13-ready series, including the exact ordered child evidence it reviewed and the reviewer findings.

### Allowed files/modules

Talking review/domain/database/API modules, focused tests, and active control documents.

### Acceptance criteria

- A decision is accepted only when the E13 registry is ready.
- Its approved/rejected outcome, evidence reference, findings, time, and ordered child IDs are durable.
- Repeating an identical decision is safe; a different decision for that series is rejected instead of overwriting review evidence.
- No model inference, visual stitching, or automatic approval is performed.

### Tests/build commands

Targeted Talking/API/repository tests; `python scripts/check_docs.py`.

### Non-goals

Reopening/replacing a prior decision, executing a provider, composing clips, or publication.

### Completion

- Added an immutable, migration-backed parent continuity-review record with outcome, evidence reference, findings, review time, project/series identity, and the exact ordered child-job IDs that were reviewed.
- The review endpoint first evaluates E13 readiness. It rejects any series with incomplete work, ambiguous/missing outputs, failed automated QA, or unapproved individual Talking review.
- An identical retry safely returns the existing decision; any differing outcome or evidence for the same series returns a conflict rather than overwriting the audit trail. The decision also has a read-only retrieval endpoint.
- Focused Talking/API/repository/job-store tests passed (**41 passed**, with two pre-existing TestClient dependency warnings); documentation governance check passed. No model call, visual stitch, or automatic quality approval was made.

## Completed work package — Gate E15: series decision visibility in the project workspace

**State: PASS**
**Primary implementation model: Luna**

### Objective

Expose E13 readiness and E14 immutable decisions in the local project workspace, including ordered child evidence and blockers.

### Allowed files/modules

Workspace/API read models, focused UI/API tests, and active control documents.

### Acceptance criteria

- A project can list its durable Talking slice series without provider execution.
- Each workspace entry visibly distinguishes readiness, blockers, per-slice evidence, and absent/approved/rejected series decision.
- The workspace adds no playback stitch, provider execution control, publication action, or synthetic quality status.

### Tests/build commands

Focused workspace/API tests; `python scripts/check_docs.py`.

### Non-goals

Changing review rules, generating or composing video, or adding external services.

### Completion

- Added a project-scoped, read-only Talking series list API. Each entry exposes E13 readiness, exact blockers, source-ordered child job/output/QA evidence, and E14's absent/approved/rejected immutable decision with its findings.
- Added a local Workspace section for selecting a project and reading this evidence. The page explicitly does not offer playback stitching, Provider execution, publication, or a fabricated continuity status.
- Focused workspace/API tests passed (**25 passed**, with two pre-existing TestClient dependency warnings). No model call or quality claim was made.

## Completed work package — Gate E16: first bounded end-to-end series runtime trial

**State: FAIL**
**Primary implementation model: Terra**

### Objective

Run exactly one E12 series on the verified local LatentSync profile using the available multi-sentence OmniVoice master narration, then stop at E13 for human evidence review.

### Input evidence

- Master narration: AudioAsset 838f6eb9-2eb6-4ac4-b692-5b8c1cc64b0f; 17.44s, OmniVoice-generated, Voice QA verified, 11 persisted transcript sentences (each 0.84–2.10s).
- Talking profile: c47d4a97-2ff0-457e-bc41-8fcd744365e4; consent confirmed; reference clip e3547414-ae03-5f1d-a2be-1b959fc75a58.
- Execution evidence: exact local LatentSync 1.5 / local-compatibility / asus-rtx3060-laptop-6gb profile; conservative 4.46s short-Talking bound.

### Acceptance criteria

- E12 creates one ordered, atomic series only; each child uses the verified short bound.
- The worker executes children in order and records normal provider/output provenance.
- Every generated child receives normal automated Talking QA; no inferred continuity pass is recorded.
- Before subjective review, update this package to AWAITING_U_REVIEW with exact artifacts and stop.

### Tests/build commands

E12 API/worker runtime evidence plus media timestamp/playability QA; python scripts/check_docs.py.

### Non-goals

Generating a new Voice take, changing the verified duration bound, video stitching, automated continuity approval, or publication.

### Failure evidence

- E12 created one atomic five-child series (a4a71f8e-794b-4be6-9a02-e3b061e64b7f) from the declared 17.44s verified narration. No child reached provider inference.
- The first child failed at local reference construction with talking_reference_invalid: the database's portable content-os-data reference path was evaluated relative to the Worker process directory instead of the configured data root. The remaining four children remain pending behind ordered-execution protection.
- This is a reproducible local path-resolution defect, not an audio, consent, duration-bound, or visual-quality result. Do not retry this failed series; preserve it as failure evidence.

## Active work package — Gate E17: portable Talking reference path resolution

**State: PASS**
**Primary implementation model: Terra**

### Objective

Resolve a portable reference-asset source path against the configured local data root before the Worker constructs a TalkingReference, without changing the stored asset path or bypassing its file/interval validation.

### Acceptance criteria

- A portable stored reference path works from a non-workspace Worker working directory.
- Absolute reference paths retain existing behavior; missing references still fail safely.
- The repair is covered by a focused Worker test. No provider/model call is made.

### Non-goals

Retrying E16, changing Talking reference consent/selection policy, or altering media path persistence.

### Completion

- The Talking Worker now resolves a portable reference path relative to the configured data-root boundary before local TalkingReference validation. The stored Core asset path remains unchanged.
- Absolute paths keep their existing behavior; an unresolved path still reaches the normal safe missing-reference failure rather than being silently accepted.
- Focused Talking Job/slice/job-store tests passed (**34 passed**, with two pre-existing TestClient dependency warnings). No provider/model call was made.

## Active work package — Gate E18: repaired bounded end-to-end series runtime trial

**State: FAIL**
**Primary implementation model: Terra**

### Objective

Create one new, independently idempotent E12 series from E16's verified master narration and run it on the exact verified local LatentSync profile after E17's path repair.

### Acceptance criteria

- The new series passes local reference preparation and executes its ordered children without retrying E16's failed series.
- Every generated child receives normal automated Talking QA.
- Before subjective review, update this package to AWAITING_U_REVIEW with exact artifacts and stop.

### Non-goals

Changing the input, duration bound, reference authorization, visual stitching, or automatic continuity approval.

### Failure evidence

- E18 created one independent five-child series (`fb71a7d3-6c77-4985-99a0-feab13ea87e9`) with the same verified input and conservative 4.46s bound. Its first child (`897d63e8-2372-429e-a943-48f65e8d7298`) stopped before provider inference with `talking_narration_slice_invalid`; the ordered remainder is still pending.
- Root cause is distinct from E16: the Worker fixed the portable reference-video path in E17 but still passed the portable master-narration path to the FFmpeg slice extractor relative to the Worker directory. No generated video or quality conclusion exists for E18. Do not retry this failed series.

## Active work package — Gate E19: portable master-narration path resolution and one fresh runtime series

**State: FAIL**
**Primary implementation model: Terra**

### Objective

Resolve the portable QA-verified master narration at the configured data-root boundary for both whole and sliced Talking provider input, then run one new independently idempotent ordered series on the exact verified local LatentSync profile.

### Acceptance criteria

- A portable master narration path becomes a concrete local path before slice extraction or provider execution; the stored Core path remains portable and unchanged.
- A focused Worker test proves the provider receives the resolved narration path; existing slice/order tests remain green.
- One new five-child series runs only after the repair; every generated child receives normal automated Talking QA.
- Before subjective review, update this package to `AWAITING_U_REVIEW` with exact artifacts and stop.

### Tests/build commands

Focused Talking Worker/slice/order tests; real media timestamp/playability QA for each generated child; `python scripts/check_docs.py`.

### Non-goals

Retrying E16 or E18, changing input/consent/duration bound, video stitching, automatic continuity approval, or publication.

### Failure evidence

- E19 created a new independent five-child series (`8279ac52-3645-488e-8354-6fbe8cb1ac84`). Its first child (`cd79b3d2-d330-4179-b00a-e934d8efc6f1`) again stopped before provider inference with `talking_narration_slice_invalid`; the other ordered children remain pending.
- The portable narration path repair was present, but the slice extractor still defaulted to a system `ffmpeg` executable. This workstation intentionally uses the Worker-resolved bundled FFmpeg, which exists, while plain `ffmpeg` is not on PATH. Direct reproduction confirms the resolved source exists and the underlying error is `Talking slice FFmpeg executable is unavailable`.
- No provider inference or video-quality evidence was produced. Do not retry this failed series.

## Active work package — Gate E20: Worker media-tool injection and one fresh runtime series

**State: FAIL**
**Primary implementation model: Terra**

### Objective

Inject the Worker-resolved local FFmpeg command into provider-neutral Talking audio slicing, then run one new independently idempotent ordered series on the unchanged exact verified local LatentSync profile.

### Acceptance criteria

- A Talking slice uses the Worker media tool rather than relying on process PATH; the Provider adapter remains independent of the slice implementation.
- Focused Worker, Talking-generation, slice, and ordered-series tests pass.
- One new five-child series runs after this repair; every generated child receives normal automated Talking QA.
- Before subjective review, update this package to `AWAITING_U_REVIEW` with exact artifacts and stop.

### Tests/build commands

Focused Talking Worker/generation/slice/order tests; real media timestamp/playability QA for each generated child; `python scripts/check_docs.py`.

### Non-goals

Retrying E16, E18, or E19; changing input/consent/duration bound, video stitching, automatic continuity approval, or publication.

### Human review artifacts

The following are the exact source-ordered child outputs from independent series `2583a470-f1da-424e-953c-fad9654569d8`. Each is a real local LatentSync 1.5 output and has passed automated container/audio/timing plus inherited QA-verified narration-copy checks. None has an individual human U-Talking decision or a series continuity decision yet.

| Order | Master interval | Output asset | Local artifact | Output duration | Automated QA |
| --- | --- | --- | --- | ---: | --- |
| 1 | 0–2940ms | `70120f73-dd22-4f7d-8147-008611c14b20` | `content-os-data/assets/originals/080ee27df00551d9ab15fa17933d31537ab42ad35b341de4578469f0b8076220.media` | 2960ms | pass; 20ms drift |
| 2 | 2940–6880ms | `bf40f307-b1b6-473c-bce2-662692e4e152` | `content-os-data/assets/originals/5b40e40ff56051b6b532362f609d5d13c98783aa93416247bd8868a6ac7f8244.media` | 3960ms | pass; 20ms drift |
| 3 | 6880–11320ms | `63d14726-6379-42dd-b9c3-2cc8eea74a55` | `content-os-data/assets/originals/d40849301a215911e089ca3a495bbbd97384f54ea2c451965ebe2f8008e602b6.media` | 4440ms | pass; 0ms drift |
| 4 | 11320–13800ms | `3f471be0-9538-4f77-a83f-0b9ce954dc71` | `content-os-data/assets/originals/1dc9ac608a07564c113abdfdaa17a594c39951d626f642c20c6666b46fef81f1.media` | 2480ms | pass; 0ms drift |
| 5 | 13800–17240ms | `e32f07f6-4a29-48c6-a7fd-1de107a2eaf8` | `content-os-data/assets/originals/b8805c7dc2fd9c4f698d869a6c4226af29e54953b2e9cfa3d7fccf09998d59e3.media` | 3440ms | pass; 0ms drift |

### Required U-Talking judgment

Review each clip for visible lip-sync, likeness/naturalness, gaze/performance and whether the non-terminal clips are independently usable. Then review source-order joins at 2940ms, 6880ms, 11320ms and 13800ms for continuity. Do not infer approval from technical QA. Stop here until the reviewer records approve/reject findings.

### U-Talking result

- The reviewer accepts each child on its own, but rejects the series as a continuous Talking result. The series scheduler preserved job order and audio boundaries but reused the same long reference Clip for every child, so every Provider invocation stages that Clip from its own start. Individual technical QA is therefore not continuity evidence.
- Direct concatenation would reintroduce the previously observed backward/time-travel motion and make visible sync at joins unreliable. E20 must not be stitched, published, or promoted as continuity evidence.

## Active work package — Gate E21: source-forward reference-window series planning

**State: PASS**
**Primary implementation model: Terra**

### Objective

Productize the D6b rule: before any short Talking child is dispatched, map every complete narration slice onto an explicitly source-forward, continuous reference window from one authorized reference Clip. The Provider receives each mapped window rather than independently restarting at the reference Clip's beginning.

### Allowed modules

Provider-neutral Talking slice/reference planning, typed Talking job/series payloads, dispatch and Worker reference construction, focused API/Worker/slice tests, `STATUS.md`, and `DECISIONS.md` if the rule is completed as a durable product decision.

### Interfaces that must stay stable

Voice/Talking consent and provider contracts; QA and human-review boundaries; provider accounting; verified 4.46s short-Talking bound; existing non-series Talking jobs; durable idempotency and ordered child execution.

### Acceptance criteria

- One planned series derives a source-forward map before child jobs exist: child `n` starts at the exact source time reached by its master-audio slice, and no child may silently restart at the parent reference start.
- The map reserves the Provider-declared frame-alignment context without exporting that context or moving a delivered audio boundary.
- Dispatch rejects a reference Clip too short to cover the whole mapped source run; it does not fall back to unrelated clips or loop/freeze frames.
- The Worker uses the persisted window and records it in output provenance; old non-series jobs retain their existing whole-Clip behavior.
- Focused tests cover forward mapping, boundary/context coverage, insufficient reference rejection, payload validation, Worker construction and idempotency.
- Then create one fresh, independent runtime series from the unchanged verified 17.44s narration and stop at explicit U-Talking review with both individual outputs and a real source-order joined artifact. No automatic continuity approval.

### Tests/build commands

Focused Talking slice/Worker/API/series-review/provider tests; real per-child plus joined-container timestamp/playability/freeze checks; `python scripts/check_docs.py`.

### Non-goals

Retrying E20; pixel-level seam repair, frame interpolation, optical flow, reusing or looping reference frames, changing Voice/copy/consent/bound/provider, automatic continuity approval, or publication.

### Human review artifact

- `content-os-data/generated/talking/e21-source-forward-joined.mp4` is the real source-order concat of five new LatentSync outputs from series `4c84fdea-ebba-4f16-b2c5-1ad4a6b07756`. The persisted input source windows are `0–3580ms`, `2940–7520ms`, `6880–11960ms`, `11320–14440ms`, and `13800–17880ms`; delivery remains the corresponding original narration slices, so only model input overlaps at joins.
- Every child completed and passed automated Talking QA: playable video+audio, duration drift of 20ms/20ms/0ms/0ms/0ms, and inherited Voice QA copy coverage 1.0 with no missing or duplicate tokens. The joined artifact is 1280×720, video+audio, 17280ms. It is a review artifact, not a continuity approval or publication output.

### Required U-Talking judgment

Review the joined artifact start to finish, especially the joins at 2940ms, 6880ms, 11320ms and 13800ms: confirm that body/gesture/source time advances rather than resets, then judge visible mouth sync, seam naturalness and whether this is publishable. Stop here until approve/reject findings are recorded.

### U-Talking result

- The reviewer approved every child and the exact joined artifact as natural, continuous and publishable. The immutable individual U-Talking records and the series continuity review are stored with evidence reference `u-talking:e21-user-approved-20260921`.
- E21 passes the bounded product question: a verified multi-short LatentSync series can use one mapped continuous creator source run without the earlier reference-reset/time-travel failure. This is scoped to the current provider/model/runtime/machine, reference source and reviewed take; it is not proof that every source Clip or Provider will be continuous.

## Product routing principles

- Content intent is not rewritten to match a provider limitation. Execution adapts the intent.
- Existing real media remains cheapest/preferred when adequate.
- A constrained local provider may satisfy only short Talking hooks; a future capable local/remote provider may satisfy longer continuous requirements without changing ScenePlan semantics.
- Automatic routing uses verified evidence when available and conservative defaults otherwise.
- When evidence is insufficient, users must have an Advanced Settings path instead of receiving a falsely precise automatic choice.
- Successful user tuning may become local evidence after QA/human acceptance; it does not automatically become a global default.

## Documentation rule

`STATUS.md` contains current truth, exactly one active package and a short queued sequence. Detailed completed experiment logs remain in Git history or local evaluation evidence. Every package must close as `PASS`, `FAIL`, `BLOCKED`, or an explicitly required human-review state before another package becomes active.
