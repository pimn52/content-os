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

A local fixed-crop derivation contract/service exists. It accepts an authorized source interval, 9:16 crop, supplied subtitle rectangles and asserted full-interval clearance, then requests a separate vertical Asset/Clip with parent/transform metadata. It checks interval coverage and geometric subtitle overlap. It does not detect faces, track motion or infer burned-in text.

The derivation API now requires the caller's full-interval review claim to name the exact crop, source hash, method/version and coverage; the service checks current source bytes even on idempotent replay and probes output before import. These are integrity and provenance guards, **not** independent face tracking or subtitle recognition. A caller's human-review label is still an assertion, not an automatic suitability result. A bounded local FFmpeg transform has produced real vertical bytes, but its sampled frames lost the moving face and retained burned-in captions, while output timing fell short of the requested interval. That transform was not admitted as a production Asset. Automatic suitability and a qualifying real derivative remain unverified.

The required product boundary is interval-and-transform-scoped eligibility: source identity/hash, exact interval, crop, method/version, coverage, confidence and accept/reject/unknown reasons. Changing any bound input invalidates the conclusion. Useful intervals come from actual material; conversational duration examples are not limits or qualifying thresholds.

A persisted negative-only crop-assessment seam now accepts face-box observations bound to an Asset hash, Clip, exact interval/crop, method/version, coverage, evidence class and reference. An observed face box crossing the proposed crop rejects only that proposal; missing or inside-only observations remain unknown. Fixture-class observations cannot drive routing. An explicit crop proposal lets normal routing/preflight avoid a known-bad choice and use an unaffected alternative, while vertical derivation blocks the matching crop before FFmpeg. Without a proposal, the Router does not reject the entire Clip. This is supplied assisted observation evidence, not an installed automatic detector or a positive eligibility assessment; actual source bytes are rechecked at derivation, not during metadata-only routing.

Acceptable head movement means movement within the composition's quality tolerance, not an immobile face. Face/head clearance, continuity, effective resolution and subtitle interaction must be assessed over the entire used interval. Sparse observations alone cannot prove full-interval safety. Insufficient coverage stays unknown, requiring a bounded further check, a focused judgment or another visual route.

Cropping away a subtitle region can create subtitle-clear material without inpainting, but only if the remaining image clears the quality bar. Unknown subtitle regions are not absence of subtitles. An uncropped portrait panel is another explicit presentation choice, not an automatic quality pass.

The original stays unchanged. Derived outputs must retain source rights/consent and generation admission, resolve portable paths through the data root, and become consumable only after transform/QA persistence succeeds. Cropping never bypasses Voice/Talking review. The current service still relies on a supplied full-interval judgment; do not equate its fixture success with an autonomous decision.

For a source-native portrait Talking reference, a separate exact-Clip assessment contract can retain a supplied full-interval review claim for face/head clearance, motion continuity, subtitle clearance and effective quality, bound to current source bytes/hash, method/version, reviewer and evidence reference. A fixture cannot yield a positive or negative routing decision; partial/unknown evidence remains unknown. A complete assisted claim is labeled `review_claimed_suitable`, not automatically detected suitability or generated Talking quality. Horizontal material cannot pass as source-native portrait through this contract; its transform/crop suitability remains a separate unresolved decision.

Where supported, one import may derive:

- metadata / ffprobe evidence;
- audio;
- transcript;
- clip boundaries;
- keyframes / visual observations;
- search/index inputs.

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

Local import, hash dedupe, continuous Clips, transcript/analysis boundaries, candidate routing, optional Shoot Tasks, usage records and real-media-first selection are implemented foundations.

In explicit lexical mode, token overlap is weaker retrieval evidence, not semantic or visual suitability verification. The router preserves the combined-score threshold. Below it, explicitly preferred typography or an explicitly declared `explanatory` scene allowing typography fallback can become the recommendation with a planning-intent reason. Preferred AI_VIDEO and declared `creator_speaking`/`action_evidence` block that substitution. Strong eligible video matches still take precedence. Typography appearing only in fallback_sources on an unknown scene does not establish adequacy.

The explicit visual requirement is editorial intent, not a media-quality pass. For `creator_speaking`, normal preflight requires native creator footage with exact transcript/audio evidence or admitted generated Talking; absent it, new Talking is requested only when preferred AI_VIDEO already authorizes that route, otherwise the requirement stays unresolved as capture. An action-evidence scene cannot be replaced by typography merely to fill a retrieval gap. EditPlan assembly independently rejects direct typography selection for explicit creator/action requirements. This does not positively verify that footage actually depicts the necessary action. Legacy plans stay unknown; current real plans are not reclassified. The existing adopted exact-use-avoidance lane retains its legacy unknown-scene behavior, but explicit creator/action requirements now prevent its typography substitution too. General model classification correctness and automatic material-aware replanning remain unproven.

When eligible video candidates are below threshold on lexical evidence, the optional capture proposal retains an unresolved-route explanation. Normal preflight additionally reports `visual_route_unresolved` and remains blocked. Its legacy `capture_required` code/production_need are retained for API compatibility, not proof that filming is necessary. No source is declared visually unusable from a text score. Required Talking, source rights/suitability, crop/subtitle, exact-use constraint, Voice and budget gates remain independent. General automatic fallback adequacy and visual replanning remain missing capabilities.

The normal Router can discover an admitted TalkingRun Asset/whole Clip for the same project and exactly matching reviewed speech, even when its optional search index is empty. A shared persisted-Run check also protects explicit VideoSpec selection and rendering, including rejection of a scene that would cut off the last timed word. Pending/rejected/missing-evidence generated children are not production candidates. This is not general AI_VIDEO eligibility or proof that the selected source is visually suitable; automatic face/crop/subtitle assessment remains open.

EditPlan supports containment, declared static-crop evidence, semantic graphics and portrait-panel presentation. These selectable treatments are not automatic suitability judgments. Source orientation, burned-in text, motion/crop quality and capability evidence are not yet fully fed into normal planning/ranking. The transform contract above is not completed automatic vertical-media production; a real transform that visibly clips the head and retains subtitles is explicit negative evidence, not a successful product path.

Explicitly adopted retained source/presentation-use constraints are evaluated in normal production preflight, separately from asset retrieval/admission. The source remains discoverable; exact same-byte/interval/treatment uses carry the constraint version, original evidence and excluded/unresolved decision. Only independently eligible intent-preserving alternatives can replace the recommendation; otherwise the original requirement remains unmet. Different layouts/intervals or regenerated bytes do not inherit either a ban or an approval. See TIMELINE_RENDER_LEARNING.md for intake, scope and execution coverage. This is not automatic asset-defect detection or learned suitability.
