# Timeline, Render, Review and Learning

## Product responsibility

Turn verified narration and selected visual assets into a recoverable 30–60 second project output, then retain the evidence needed for user review and future improvement.

## Master Narration

Master Narration is the authoritative continuous audio timeline for a narrated project.

Scenes and generated Talking runs map to intervals on that timeline. Visual execution details must not force manual per-scene audio cutting.

## EditPlan / Visual Direction

EditPlan is the product-level bridge between editorial ScenePlan / routed assets and deterministic VideoSpec.

It answers **how the selected material should be presented**, without changing what the scene means or which provider generated an asset.

The target lifecycle starts before production: ScenePlan and preliminary visual intent identify reusable material, missing production, subtitle treatment and fallback; suitability/capability/cost preflight determines whether to dispatch. After production, measured timing and admitted assets resolve the same plan into VideoSpec. Unfulfilled production needs are not fake Asset IDs or renderable selections. A read-only draft preflight returns a fingerprinted scene/action preview and a preliminary EditPlan where all scenes have a selection. A narrow ProductionRun takes a fresh, ready plan with an approved reusable Master and typography-only visuals through automatic VideoSpec assembly into a durable render Job. For the same deterministic visual lane, it may wait without a Job for an approved Master, then explicitly resume through current U-Voice admission and refreshed preflight before dispatch. Guarded Voice dispatch and independent QA can produce a single-take Master candidate; neither is self-approval. Missing Talking/capture remains a named need, not a renderable selection. The same planned Talking lane can start without an approved Master, use existing Voice/QA actions, then explicitly resume after bound U-Voice approval into Talking waiting without a second ProductionRun. It preserves generation provenance and Voice/QA references, requires actual scene timing and rechecks current Voice authority downstream. A narrow waiting Talking lane can then resume after all exact native-portrait planned Runs are independently reviewed and bound: it pins those Clips and the original Master, resolves actual narration timestamps, and persists one existing render Job atomically. Other scenes must be deterministic typography. Replay/restart recheck current plan, reviews, source authority, file hashes and the render snapshot; short Talking cannot silently become a typography tail. Unknown source subtitles remain unknown and receive no new caption overlay. This is fixture/API and local-file-staging proof, not a genuine planned render or U-Product pass; the general dependency graph, normal UI and automatic suitability remain gaps.

Per scene it may carry:

- visual role;
- selected asset/clip reference;
- framing policy;
- subtitle/text treatment;
- semantic graphic treatment;
- transition intent;
- explicit fallback;
- visual-style tokens/evidence.

For creator Talking footage, unknown crop safety must not silently become a fixed crop. Containment/design is a conservative geometric option, not automatic editorial acceptance; if it fails the quality bar, replan, choose another eligible interval or stop. Motion-aware crop may be added later without changing EditPlan semantics.

For authorized horizontal Talking footage with known burned-in subtitles, EditPlan can use a source-preserving `portrait_panel`: the complete, uncropped source sits inside a styled vertical panel, while optional semantic text remains outside its bounds. This is a presentation treatment, not subtitle removal, inpainting or a mutation of the source Asset. Timed captions over that panel remain disallowed when source subtitles are known.

The target media-derivation boundary yields a separate Asset/Clip for an eligible interval/transform; source duration imposes no fixed eligible length or count. Current derivation accepts supplied assertions and geometry, not independently verified crop safety. Real-media success and automatic selection remain unverified; see MEDIA_ASSET_SYSTEM.md. EditPlan must not reintroduce conflicting captions. Cropping outside subtitle regions is not inpainting.

Graphic scenes are semantic treatments such as headline, key point and contrast—not a single generic text card. Visual styling should come from reusable creator/project style tokens rather than hard-coded renderer CSS.

For an explicitly declared `GraphicCardPlan`, normal EditPlan resolution uses
the declared treatment and every bounded point, joined with line breaks, for
both primary graphic text and fallback. VideoSpec/Remotion props preserve this
text and the renderer displays the line breaks. A supplied plan cannot silently
drop/reorder points or change the treatment/fallback. This is static typography,
not diagram, timeline, waveform or animated-state realization. An explicit
unsupported graphic need stops normal preflight with
`graphic_realization_unsupported:<scene_id>` and blocks resolved assembly;
it cannot silently survive as a typography fallback even when footage is chosen.
Replanning must explicitly settle that need before production. Legacy null
graphic intent retains old selection behavior and remains unverified for effect
coverage. Full-paragraph/subtitle duplication and required person/action guards
still apply; a bounded point list does not confer publishability or human QA.

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

Required policy: generated Voice/Talking cannot enter final assembly before their QA/human gates pass. Policy-v1 TalkingRun admission enforces child approval and whole-run continuity; newly created planned v2 Runs retain child technical QA and require exact six-dimension whole-preview approval with current concern evidence. Routing, explicit assembly and rendering share that versioned persisted Run boundary. Unadmitted children cannot be consumed directly as production visuals, and Run reuse requires the matching project and reviewed speech.

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

[D029](../../DECISIONS.md#d029--admit-execution-for-an-evidenced-purpose-and-preserve-that-scope-downstream) requires a future internal-evaluation output to retain purpose/admission lineage through composition, preview, TalkingRun and Render. Local preview/download is allowed only within the evidenced evaluation use, with a visible scope and retained manifest; quality approval never changes commercial eligibility. New IDs, derived bytes or reimport cannot erase known restrictions, and unresolved lineage cannot confer commercial clearance. Existing publication records describe events rather than grant rights; linked restrictions must remain visible. These are pending migration requirements, not current export enforcement or a claim that the app can prevent out-of-product copying.

The normal output path remains local Remotion + FFmpeg for vertical video.

R1 target:

- 30–60 seconds;
- playable vertical output;
- creator voice;
- at least one new creator Talking segment/run;
- real creator B-roll where appropriate;
- readable typography/subtitles.

## Review model

Evidence should stay fine-grained while user interaction becomes coarse. This is a target, not current uniform behavior.

- child Voice/Talking jobs keep detailed QA/provenance;
- low-confidence failures may require local review;
- the normal product should prefer reviewing the composed Voice master, TalkingRun and final video rather than asking the user to approve every internal slice.

Today, generated Voice requires exact-asset U-Voice. Legacy/evaluation policy-v1 Talking series require individual child U-Talking plus whole-run continuity; the normal new planned Run entry selects v2, retaining child technical QA while consolidating subjective judgment on the exact preview across all six dimensions and current concerns. Its fixture action-count comparison is not measured human-time or real-quality evidence. Calibration/new-provider experiments remain separate from ordinary production. Do not waive current rules merely because the target asks for fewer review actions.

Approved exact assets need not be repeatedly reviewed unless dependencies changed. Replacing a child invalidates affected aggregate evidence, not unrelated work. Human likeness/naturalness and final publishability remain explicit.

The planned Talking API can explicitly replace one failed scene using persisted evidence: a known transient execution error, or exact negative preview/concern findings confined to visible sync or face/mouth artifacts. Other quality dimensions require replanning; missing judgments, invalid requests and unknown errors stop. The action preserves the old Job/collection/rejection, original finding intervals, unchanged Master and other scene bindings. The current execution unit is the scene's single child, so a localized finding does not imply arbitrary frame-range splicing. A replacement receives fresh technical QA, a new immutable preview collection and policy-required whole-result review. Each scene gets at most one replacement, within the Run's predeclared repair allowance; Worker reservation also caps it at one local provider call with zero authorized external charge and rechecks project/global budgets. Local compute cost and user time remain unmeasured. Normal repair controls retain durable history without a current preview and cache review by exact candidate/QA/policy identity. Executed offline fixture-state tests now clear old bindings, separate pending generation from fresh QA/preview and preserve successor state on replay; user-operated V76b2r fixture screenshots also verify successor controls, fresh unknown six-dimension form defaults, history after refresh and technical/approved-preview negative-concern plan entries. These are synthetic state-flow checks, not real-media approval. V76b6 adds normal Run UI controls for bounded single-take Voice replacement with fresh QA/U-Voice, and exact-render-observation presentation revision with immutable history and a pending final review. Build/state tests do not replace user-operated UI/action-count fixture review or creator-media approval. Real repair quality remains unverified.

### Presentation recovery boundary

[D027](../../DECISIONS.md#d027--repair-the-supported-dependency-and-preserve-the-review-boundary) limits presentation recovery to supported, deterministic corrections on exact completed render inputs. The Run-scoped backend now records an explicit scene observation bound to the render SHA-256, proposes a fingerprint-bound plan, applies an immutable revision and exposes durable history. An observation is `human_observation`, not automatic QA or a complete U-Product rejection. Candidate before/after fields preserve readable narrative copy: duplicate full graphics can use a saved short emphasis while retaining timed captions; known source subtitles can suppress only the newly rendered captions without deleting semantic narration; a qualified unchanged Talking source can use the existing 1080×1920 uncropped portrait panel with text outside it. Unknown subtitles, unsupported scene-fragment mapping, source/crop/interval changes or missing short emphasis stop/replan, not typography fallback.

Apply atomically consumes the original shared Voice/Talking/presentation allowance, creates a new render Job and retains the entire predecessor Job/spec/hash in history. Current draft, complete EditPlan, Master/source hashes and existing renderer admission gates are rechecked before a single durable local render reservation and again before output evidence is persisted. Voice/Talking inference calls are zero; authorized external charge is zero, local compute and user time unknown. New output technical evidence binds its hash, video frames/duration, dimensions/fps, audio duration and full decode. AAC encoder padding is measured separately from the exact video timeline. Changed/missing output bytes invalidate the displayed technical evidence. No prior final approval is copied; `final_review_state=not_submitted` is an explicit pending requirement, not an implemented U-Product submission API. Normal Run UI exposes the observation/candidate/confirmed-apply/history path. Editing an observation discards its old candidate, switching Run/inputs clears forms and fences late work, and backend denial removes the executable plan. Reloaded observation references are checked against the server's exact record before applying. Component event tests cover the synthetic successor/hash/pending-final-review chain; user-operated isolated screenshots confirm successor/hash/history and pending review display; refresh is offline-tested, not independently recorded in these screenshots. Real-quality acceptance remains separate.

Face loss, removing original subtitles, changing source/interval, or unknown suitability requires an evidence-named replan/stop, not another generation attempt or a silent low-quality fallback. Changes to reviewed Talking-preview bytes or Master dependencies require their own fresh candidate/review scope. New render revisions retain their predecessors and share the original production repair allowance; they cannot reset it. Source duration examples do not define transform eligibility. No detector, crop/subtitle-removal automation or automatic final approval is claimed. The recovery panel fetches only the exact current completed Render Job, computes its byte hash, and records a reason/evidence-bound human observation before requesting a plan. It displays the candidate presentation delta and retained Voice/Talking identities; render completion remains technical evidence and final U-Product review is still pending. User-operated isolated screenshots confirm the new render/hash/history/technical-verified and final-pending display, not real creator-quality acceptance.

## Publication and learning

The product records only explicit publication/feedback actions. Previewing or retrying is not publication.

[D028](../../DECISIONS.md#d028--retained-render-rejection-constrains-an-adopted-use-not-the-whole-source) separates an exact final-render rejection from a separately adopted future-use constraint. The ordinary backend now discovers retained rejection manifests under the configured data-root evaluation-evidence directory, confirms/imports exact negative evidence, and versions explicit scope adoption/disable. Discovery is read-only and limited to 500 manifest files / 2 MB per file; it is not confirmation, QA or policy authority. Intake rechecks project, source and output hashes, preserves original findings/document/interpretation, and does not fabricate Jobs, ProductionRuns or publication. Fixture evidence cannot authorize a policy. A whole-render finding does not independently fail every scene or the entire source; the recovered scene uses are proposed future avoidance scopes, not new per-scene human judgments.

The independent `configuration_use_limit` rule matches creator, same source bytes, exact source interval and reconstructable effective presentation, not Clip IDs. It does not generalize to overlapping/subinterval uses, parents, regenerated descendants or materially different layouts. Current normalization supports full-frame contain/portrait panel with known source-text state and none or measured timed captions; crop geometry, unknown text and unmeasured/legacy caption fallback remain unresolved. Conflict with existing source text is not automatically semantic mismatch or removable-overlay evidence. Changed treatments are not thereby fixed, admitted or publishable. Existing Master/Talking approvals remain separate.

Normal production preflight evaluates these uses without globally removing the asset from retrieval. An excluded recommendation chooses an explicitly allowed deterministic graphic fallback or an independently admitted native-portrait planned Run, otherwise names an unmet replan/capture requirement; required Talking never silently becomes typography. Rule versions, disable tombstones and changed evidence fence preflight/planning results and planned Talking dependencies. Newly queued ordinary production Render Jobs additionally snapshot policy context and recheck before/after rendering; unsnapshotted legacy/direct Jobs are not covered by this new hook. API/offline choice and invalidation tests exist. Normal UI, actual failure-scope adoption and applicable subsequent real decision/quality evidence remain pending; this backend seam does not close the feedback loop.

The regular project feedback panel now lists configured-root retained rejection documents and shows the original whole-render review separately from the proposed future-use intervals and presentation. The user confirms the document source and evidence class, selects reconstructable intervals, supplies a reason, and confirms adoption; fixture and held/unresolved evidence cannot enable. Disable and every write refresh current server state, including after replay or conflict. Component tests and production bundle build pass. Live browser interaction and actual creator adoption have not been recorded.

Feedback records generate next-suggestion text. Exact retained presentation duplication observations can now become explicitly adopted creator-scoped planning preferences with revision/hash guards, version history and disable; see CREATOR_INTELLIGENCE.md. Voice/Talking likeness findings, asset defects and configuration limitations do not become global routing rules. Persisting a review is not learning: the narrow offline next-topic regression is not real quality/repeatability proof. R1 does not claim autonomous growth optimization.

## Current capability

MasterNarration, VideoSpec, local rendering, subtitles, publication/feedback records and usage evidence exist.

Approved Voice and admitted TalkingRun can enter explicit assembly; a matching Run is also discoverable in normal asset routing. The draft preflight exposes tentative route/presentation, unknowns and stale-plan rejection without provider calls. It distinguishes exact reviewed native-portrait planned Runs from unknown media; that classification reopens the persisted admission gate and does not claim automatic visual suitability or publishable composition. The project UI exposes whole-plan preflight/start, durable status/resume/cancel, Run-bound Voice/Talking configuration/QA/review, plus bounded Voice and presentation recovery controls. Browser evidence is isolated fixture coverage; Voice review submission and a real end-to-end workflow remain unverified; the corrected Talking replacement transition has user-operated isolated fixture screenshot evidence, not real production proof. Offline component events now count four Voice workflow requests (plan, repair, QA, one fresh U-Voice) and three presentation requests (observation, plan, revision); internal Job/Audio/Render binding inputs are zero. Form edits, confirmations, refreshes and Worker simulation are outside that counter, and final U-Product remains required. The historical same-task total and user minutes are unknown; browser acceptance remains pending. Bounded repair is not yet real quality improvement or a measured manual-time reduction. Feedback consumption remains incomplete. R1 proof must follow normal planning before dispatch, then fresh-topic output and repeatability; another manually scripted render alone does not close this gap.
