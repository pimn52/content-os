# Media, Asset Intelligence and Hybrid Asset Routing

## Product responsibility

Turn creator-owned media into reusable, searchable production assets and choose the lowest-cost adequate visual route for each content need.

## User outcome

The creator uploads or imports media once. Content OS should preserve the original media, derive reusable clips and metadata, and avoid forcing repeated manual cutting for every new video.

## Core objects

- Asset
- Clip
- AudioAsset
- analysis / transcript evidence
- Candidate
- ShootTask
- Asset usage evidence

## Media model

Original video remains continuous. Clips are first-class playback intervals. Keyframes and derived observations support understanding/search; they do not replace the source video.

### Horizontal-source reuse: contract and missing assessment

A local fixed-crop derivation contract/service exists. It accepts an authorized source interval, a crop matching an explicitly selected portrait output, supplied subtitle rectangles and asserted full-interval clearance, then requests a separate Asset/Clip with parent/transform metadata. Output stays1080px wide; its even `output_height` may be1080–1920, with1920 as the unchanged9:16 default. For example1440 gives9:12 and1680 gives9:14. These are source viewport proportions, independent of the final9:16 VideoSpec. Matching crop/output aspects avoid distortion. The ordinary web media panel exposes9:12,9:14,9:16 and square proportions, exact interval/pixel fields, a bounding rectangle containing all known source subtitles and an explicit full-interval review assertion/reference. It checks interval coverage and geometric subtitle overlap. It does not detect faces, track motion or infer burned-in text.

The panel can load the authorized local source with current-byte SHA-256 verification and display the fixed crop outline over native video playback. The preview is bounded to360px width while retaining its source aspect. The outline helps manual composition assessment; no sampled-frame or automatic positive conclusion follows. Editing the source identity, proportion, crop, interval, subtitle claim or evidence reference clears the prior review selection. An identical submission reuses its idempotency identity; a successful derivative refreshes normal Assets/Clips for later candidate selection. Backend rejection stays visible, and creation followed by refresh failure is distinguished from creation failure. UI handler tests/build, local browser synthetic playback/request checks and actual synthetic geometry remain distinct from a real creator derivative or publishable composition. Automatic choice and drag-to-crop remain unimplemented.

Before the full-interval declaration, the normal panel offers a read-only viewport-feasibility check. It verifies current source bytes, Clip coverage, crop/output geometry, supplied subtitle overlap and exact persisted negative crop evidence. It shows retained pixel area and uniform scaling; these are tradeoffs, not semantic content coverage or recovered detail. Unknown subtitles and known conflicts are explicit stops; clean geometry still leaves face/head clearance, motion and effective quality unknown. The check creates no Asset, Clip, Job or human receipt and does not evaluate execution admission. Geometry/source changes discard the result; stale responses cannot bind another input. Local Asset/Clip playback resolves portable paths against the configured data root, including from a different process CWD, and rejects relative escapes and network paths. Legacy absolute paths retain their existing behavior.

Suitable full-screen portrait footage remains the preferred result when it meets source, framing and quality requirements. A square/wider source viewport or containment is a disclosed compromise for limited input, not the new default quality target. A user accepting one exact constrained composition does not establish a general preference for square footage or approve unrelated crops/sources.

The final canvas must not force every source rectangle to the same aspect.
Supported transforms should prioritize comfortable subject scale/headroom,
retained content/context, subtitle readability and usable resolution; text is
optional supporting information, not mandatory filler for unused canvas.
An input may support a publishable result, a useful result with disclosed
limitations, or no suitable result within current capability. The required
product choice is to show the supported presentation options, their concrete
tradeoffs, evidence/unknowns and any better-input alternative, then let the user
select an outcome. The fixed-crop panel provides a narrow manual choice flow;
automatic planning and normal final-composition choices remain incomplete. Aesthetic compromise does not
relax consent/license/provenance, verified capability or required human reviews,
and acceptance of a limited result is not a blanket R1/publishability pass.

The derivation API requires the caller's full-interval review claim to name the exact crop, source hash, method/version and coverage; the service checks current source bytes even on idempotent replay and probes output before import. These are integrity and provenance guards, **not** independent face tracking or subtitle recognition. A caller's human-review label is still an assertion, not an automatic suitability result. Earlier unsuccessful narrow crops remain negative evidence. One selected square treatment now has exact user comfort/clearance, real normal-API Asset/whole-Clip persistence, byte identity with the reviewed output, idempotent replay and existing downstream Talking/Master/source validation. This establishes one assisted, user-reviewed derivative path; automatic suitability, ordinary UI creation/selection, final composition acceptance and repeatability remain separate unverified capabilities.

The required product boundary is interval-and-transform-scoped eligibility: source identity/hash, exact interval, crop, method/version, coverage, confidence and accept/reject/unknown reasons. Changing any bound input invalidates the conclusion. Useful intervals come from actual material; conversational duration examples are not limits or qualifying thresholds.

Preserve later contradictory visual observations alongside a supplied human clearance claim. The selected square's final-render inspection still found a brief lateral hair boundary cutoff; API acceptance of the claim does not establish independently verified full-head completeness. Aesthetic acceptance as a limited-input result remains distinct from unconditional clearance, final composition quality and publication admission.

A persisted negative-only crop-assessment seam now accepts face-box observations bound to an Asset hash, Clip, exact interval/crop, method/version, coverage, evidence class and reference. An observed face box crossing the proposed crop rejects only that proposal; missing or inside-only observations remain unknown. Fixture-class observations cannot drive routing. An explicit crop proposal lets normal routing/preflight avoid a known-bad choice and use an unaffected alternative, while vertical derivation blocks the matching crop before FFmpeg. Without a proposal, the Router does not reject the entire Clip. This is supplied assisted observation evidence, not an installed automatic detector or a positive eligibility assessment; actual source bytes are rechecked at derivation, not during metadata-only routing.

Acceptable head movement means movement within the composition's quality tolerance, not an immobile face. Face/head clearance, continuity, effective resolution and subtitle interaction must be assessed over the entire used interval. Sparse observations alone cannot prove full-interval safety. Insufficient coverage stays unknown, requiring a bounded further check, a focused judgment or another visual route.

Cropping away a subtitle region can create subtitle-clear material without inpainting, but only if the remaining image clears the quality bar. Unknown subtitle regions are not absence of subtitles. An uncropped portrait panel is another explicit presentation choice, not an automatic quality pass.

The original stays unchanged. Derived outputs must retain source rights/consent and generation admission, resolve portable paths through the data root, and become consumable only after transform/QA persistence succeeds. Cropping never bypasses Voice/Talking review. The current service still relies on a supplied full-interval judgment; do not equate its fixture success with an autonomous decision.

This fixed-crop service excludes source audio explicitly; the transform records
that fact and narration must come from the selected approved audio path where
needed. Real synthetic9:16/9:12 tests establish output dimensions and interval
duration, not face clearance, creator quality or eligibility of an actual clip.

For a video track beginning after the nominal container origin, fixed-crop output uses FFmpeg's native-rate zero-origin frame alignment: the first frame extends over the leading gap. This timing treatment is retained in transform provenance; it does not invent motion or pad a missing ending. The full requested duration check remains strict. Real offset-source and missing-end tests distinguish the two cases. An inspected output is not an admitted derivative without the existing full-interval human declaration and import/provenance checks.

For a source-native portrait Talking reference, a separate exact-Clip assessment contract can retain a supplied full-interval review claim for face/head clearance, motion continuity, subtitle clearance and effective quality, bound to current source bytes/hash, method/version, reviewer and evidence reference. A fixture cannot yield a positive or negative routing decision; partial/unknown evidence remains unknown. A complete assisted claim is labeled `review_claimed_suitable`, not automatically detected suitability or generated Talking quality. Horizontal material cannot pass as source-native portrait through this contract; its transform/crop suitability remains a separate unresolved decision.

Where supported, one import may derive:

- metadata / ffprobe evidence;
- audio;
- transcript;
- clip boundaries;
- keyframes / visual observations;
- search/index inputs.

The existing `index_clips` asset Job optionally accepts explicit `clip_ids` to
refresh only selected source observations and embeddings. The scope must be
nonempty, unique and belong to the Asset; it is part of Job idempotency. Absent
scope retains whole-asset analysis. The Worker extracts/analyzes/indexes only
those Clips, updates their facts atomically and preserves all unselected Clips
and geometry. Existing per-keyframe Vision and indexing Embedding accounting
remain in force, including completion of the successful Embedding call. This
allows bounded refresh of legacy source facts without repeating an entire
source analysis. It does not grant source suitability or visual acceptance.

## Hybrid Asset Router

The Asset Router answers **what visual/content route should satisfy a scene**. It does not choose the compute provider. The preferences below are product policy; not every branch is implemented in automatic routing.

### TALKING preference

1. original creator Talking when the original words already match;
2. authorized generated/lip-synced creator Talking;
3. authorized digital twin/avatar only as explicit fallback;
4. capture gap when a small new recording is cheaper or safer.

### B-roll preference

1. creator / historical production-authorized media;
2. low-friction capture;
3. screenshot / static / typography;
4. stock when supported;
5. AI image/video only when justified, supported and budgeted.

The decided D028 follow-on applies retained final-render rejection at the source-plus-presentation proposal boundary. An adopted constraint can avoid the same bytes, exact interval and effective treatment for that creator; it does not change the source Asset's quality/admission status. A different interval, derivative or layout remains governed by existing evidence requirements. This configuration-use matching is pending implementation and separate from the implemented negative crop-assessment seam above.

## Capture / Shoot List

A missing visual may create an optional, concrete capture task. Capture is part of the asset strategy, not a failure mode.

An explicitly reasoned explanatory scene may plan typography first and retain
real media as fallback, provided no preferred generated Talking/Talking Profile
requires a person. This is the narrow planner source-priority exception;
mixed preferred-source ordering, creator/action requirements and media
admission are unchanged. It does not certify explanatory classification,
source suitability or final quality.

## Product boundaries

- Asset ranking must retain provenance.
- Unknown price is not treated as zero.
- Real creator media is preferred when it clears the quality threshold.
- Generated Talking assets must pass required QA/human gates before normal assembly.
- A future provider change must not require changing ScenePlan semantics.

## Current capability

Normal production preflight and retained composition consume saved explicit
footage selections before lexical retrieval. Reuse rechecks Asset/Clip identity,
source kind, interval length, configured use, rights reference, project scope
and known crop rejection. An explicit selection is neither a semantic score
nor source-suitability approval; missing indices do not force new capture,
and source, Talking and render gates remain independent.

Local import, hash dedupe, continuous Clips, transcript/analysis boundaries, candidate routing, optional Shoot Tasks, usage records and real-media-first selection are implemented foundations.

In explicit lexical mode, token overlap is weaker retrieval evidence, not semantic or visual suitability verification. The router preserves the combined-score threshold. Below it, explicitly preferred typography or an explicitly declared `explanatory` scene allowing typography fallback can become the recommendation with a planning-intent reason. Preferred AI_VIDEO and declared `creator_speaking`/`action_evidence` block that substitution. Strong eligible video matches still take precedence. Typography appearing only in fallback_sources on an unknown scene does not establish adequacy.

The explicit visual requirement is editorial intent, not a media-quality pass. For `creator_speaking`, normal preflight requires native creator footage with exact transcript/audio evidence or admitted generated Talking; absent it, new Talking is requested only when preferred AI_VIDEO already authorizes that route, otherwise the requirement stays unresolved as capture. An action-evidence scene cannot be replaced by typography merely to fill a retrieval gap. EditPlan assembly independently rejects direct typography selection for explicit creator/action requirements. This does not positively verify that footage actually depicts the necessary action. Legacy plans stay unknown; current real plans are not reclassified. The existing adopted exact-use-avoidance lane retains its legacy unknown-scene behavior, but explicit creator/action requirements now prevent its typography substitution too. General model classification correctness and automatic material-aware replanning remain unproven.

When eligible video candidates are below threshold on lexical evidence, the optional capture proposal retains an unresolved-route explanation. Normal preflight additionally reports `visual_route_unresolved` and remains blocked. Its legacy `capture_required` code/production_need are retained for API compatibility, not proof that filming is necessary. No source is declared visually unusable from a text score. Required Talking, source rights/suitability, crop/subtitle, exact-use constraint, Voice and budget gates remain independent. General automatic fallback adequacy and visual replanning remain missing capabilities.

The normal Router can discover an admitted TalkingRun and its already verified vertical derivatives for the same project and exact reviewed speech, even without its optional search index. The retained-media consumer uses this same discovery instead of separately choosing only the original assembled Clip. Eligible derivatives receive a bounded portrait-fit preference after existing full-interval safety/provenance/byte checks; no transform or new crop is requested. Failed evidence leaves the reviewed complete original as an alternative, with actual selection-specific authorization still required. A shared persisted-Run check also protects explicit VideoSpec selection and rendering, including rejection of a scene that would cut off the last timed word. Pending/rejected/missing-evidence children are not production candidates. This is not general AI_VIDEO eligibility or automatic face/crop/subtitle assessment; new-topic copy must still match eligible speech, and unsupported supplementary-material requirements remain explicit gaps.

EditPlan supports containment, declared static-crop evidence, semantic graphics and portrait-panel presentation. The existing source vision analysis now returns equal left/right comparison layout and normalized burned-subtitle rectangles (including outlines/shadows, excluding evidentiary labels). These sampled facts are persisted in existing Asset metadata with source hash, Clip interval and keyframe reference; stale hashes or unrelated intervals are unknown. They are source-analysis observations, not full-interval crop approval. Normal selection prefers a verified same-source, exact-interval portrait derivative, or an equally relevant native portrait alternative; a comparison must not lose one half to obtain portrait footage. A weaker unrelated portrait source does not displace stronger semantic evidence. Without an appropriate alternative or evidenced safe crop, ordinary footage without editorial graphic content uses the complete frame centered in `full_canvas`, rather than a small top panel with empty space. Comparison intent plus a known equal-half source layout automatically selects synchronized upper/lower viewports even with the default style; the explicit comparison style remains selectable. The current split supports 16:9 sources only. The two viewports touch and preserve proportions while maximizing the caption-safe area. Known bottom-band subtitle bounds determine the minimum clipping strip with a two-source-pixel outline guard; no fixed60px removal is assumed. Unknown layout/subtitle geometry or subtitles overlapping required picture stop planning before Render. Existing EditPlan/VideoSpec crop and evidence fields carry this decision, and Renderer rechecks current source facts. Source-use exclusions cannot silently treat a cropped presentation as complete containment. No extra Provider, Planner, table, model invocation or automatic derivative generation is introduced. Contract/offline regressions establish propagation and layout arithmetic only; sampled detection accuracy, moving subtitles and real full-interval suitability remain unverified until actual source/runtime evidence exists.

Explicitly adopted retained source/presentation-use constraints are evaluated in normal production preflight, separately from asset retrieval/admission. The source remains discoverable; exact same-byte/interval/treatment uses carry the constraint version, original evidence and excluded/unresolved decision. Only independently eligible intent-preserving alternatives can replace the recommendation; otherwise the original requirement remains unmet. Different layouts/intervals or regenerated bytes do not inherit either a ban or an approval. See TIMELINE_RENDER_LEARNING.md for intake, scope and execution coverage. This is not automatic asset-defect detection or learned suitability.
