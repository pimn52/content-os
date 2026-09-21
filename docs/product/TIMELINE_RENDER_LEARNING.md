# Timeline, Render, Review and Learning

## Product responsibility

Turn verified narration and selected visual assets into a recoverable 30–60 second project output, then retain the evidence needed for user review and future improvement.

## Master Narration

Master Narration is the authoritative continuous audio timeline for a narrated project.

Scenes and generated Talking runs map to intervals on that timeline. Visual execution details must not force manual per-scene audio cutting.

## VideoSpec

VideoSpec is the render contract that binds:

- project identity;
- Master Narration or explicit scene narration;
- scene frame intervals;
- selected visual Assets/Clips;
- narration intervals;
- captions/subtitles;
- required visual treatment and provenance.

Generated Voice/Talking cannot enter final assembly before their required QA gates pass.

## Render

The normal output path remains local Remotion + FFmpeg for vertical video.

R1 target:

- 30–60 seconds;
- playable vertical output;
- creator voice;
- at least one new creator Talking segment/run;
- real creator B-roll where appropriate;
- readable typography/subtitles.

## Review model

Evidence can stay fine-grained while user interaction stays coarse.

- child Voice/Talking jobs keep detailed QA/provenance;
- low-confidence failures may require local review;
- the normal product should prefer reviewing the composed Voice master, TalkingRun and final video rather than asking the user to approve every internal slice.

Human gates remain explicit for creator likeness/naturalness and final publishability.

## Publication and learning

The product records only explicit publication/feedback actions. Previewing or retrying is not publication.

Feedback and observed metrics may drive evidence-backed next suggestions. R1 does not claim autonomous optimization.

## Current capability

MasterNarration, VideoSpec, local rendering, subtitles, publication/feedback records and usage evidence exist.

The critical missing bridge is to let an approved TalkingRun become an ordinary production visual in VideoSpec, followed by a fresh end-to-end R1 render and one U-Product review.
