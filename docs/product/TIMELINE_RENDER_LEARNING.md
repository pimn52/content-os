# Timeline, Render, Review and Learning

## Product responsibility

Turn verified narration and selected visual assets into a recoverable 30–60 second project output, then retain the evidence needed for user review and future improvement.

## Master Narration

Master Narration is the authoritative continuous audio timeline for a narrated project.

Scenes and generated Talking runs map to intervals on that timeline. Visual execution details must not force manual per-scene audio cutting.

## EditPlan / Visual Direction

EditPlan is the product-level bridge between editorial ScenePlan / routed assets and deterministic VideoSpec.

It answers **how the selected material should be presented**, without changing what the scene means or which provider generated an asset.

Per scene it may carry:

- visual role;
- selected asset/clip reference;
- framing policy;
- subtitle/text treatment;
- semantic graphic treatment;
- transition intent;
- explicit fallback;
- visual-style tokens/evidence.

For creator Talking footage, unknown crop safety must not silently become a fixed crop. R1 should prefer a face-safe contain/design treatment unless a static crop is verified across the full used interval. Motion-aware crop can be added later without changing EditPlan semantics.

Graphic scenes are semantic treatments such as headline, key point and contrast—not a single generic text card. Visual styling should come from reusable creator/project style tokens rather than hard-coded renderer CSS.

A visual preflight may reject an EditPlan before render when framing, subtitle interaction or text treatment is unsafe or internally contradictory.

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

An admitted TalkingRun can carry an explicit, evidence-referenced vertical
reframe in VideoSpec when source footage has burned-in text outside the usable
image area. The renderer must not silently crop an unreviewed face. On a
typography scene, a short display title can coexist with separately timed
Master Narration captions; repeating the full paragraph as both title and
subtitles is a visual-QA failure. Neither treatment is automatic evidence of
publishability: the final rendered video still needs human review.

Fixed center cropping can remove burned-in subtitles yet fail when the
speaker moves out of the vertical frame. A production edit needs face-safe
framing over the full selected interval, with an explicit fallback when that
cannot be verified. Simple text-only cards can likewise be technically
readable but still fail the final editorial/publishability gate. Automated
container and sampled-frame QA must not be promoted to U-Product approval.

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

Approved Voice and Talking assets can now enter the normal assembly path. The remaining R1 bridge is a productized EditPlan/visual-direction layer, one 30–60s U-Product pass through that path, product-generated planning rather than assisted scene input, and then second-topic repeatability.
