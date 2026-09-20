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

## Active work package — Gate D10: local GPU inference lease

**State: RUNNING**
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

## Queued packages after D10

These are **not active simultaneously**. Open one only after an explicit scope is recorded.

### Future — GPU resource lease / scheduler

After routing behavior is stable, add a lightweight local GPU resource lease/queue if concurrent Voice/Talking jobs can contend for VRAM. Reuse the existing JobRunner; do not introduce infrastructure meant for distributed systems.

## Product routing principles

- Content intent is not rewritten to match a provider limitation. Execution adapts the intent.
- Existing real media remains cheapest/preferred when adequate.
- A constrained local provider may satisfy only short Talking hooks; a future capable local/remote provider may satisfy longer continuous requirements without changing ScenePlan semantics.
- Automatic routing uses verified evidence when available and conservative defaults otherwise.
- When evidence is insufficient, users must have an Advanced Settings path instead of receiving a falsely precise automatic choice.
- Successful user tuning may become local evidence after QA/human acceptance; it does not automatically become a global default.

## Documentation rule

`STATUS.md` contains current truth, exactly one active package and a short queued sequence. Detailed completed experiment logs remain in Git history or local evaluation evidence. Every package must close as `PASS`, `FAIL`, `BLOCKED`, or an explicitly required human-review state before another package becomes active.
