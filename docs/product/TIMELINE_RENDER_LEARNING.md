# Timeline, Render, Review and Learning

## Product responsibility

Turn verified narration and selected visual assets into a recoverable 30–60 second project output, then retain the evidence needed for user review and future improvement.

## Master Narration

Master Narration is the authoritative continuous audio timeline for a narrated project.

Scenes and generated Talking runs map to intervals on that timeline. Visual execution details must not force manual per-scene audio cutting.

The local renderer encodes an admitted Master directly into the final MP4 once,
using its exact selected interval without imposing48kHz/stereo conversion. It mutes the
Remotion visual pass instead of sending the Master through intermediate AAC
mixing/concatenation. Visual trim endpoints use the same frame ceiling as the
timeline, avoiding empty frames at fractional scene boundaries. These technical
properties do not replace listening/viewing or final human quality review.

Render failures retain one private JSON diagnostic per Job attempt under the configured data root: failure stage, exception type, subprocess exit code and a bounded error summary with credentials, URLs and absolute local paths removed. The public failure code and accounting remain unchanged; the first diagnostic is never overwritten. Captured stderr does not reach API errors, and diagnostics do not grant retries or output admission.

A one-shot Composition Render may be replaced once only when its original Job and runtime ProviderCall are both terminal `failed`, the call matches that Job and Run, and no accepted or ambiguous output exists. The replacement uses a new Job and ProviderCall identity; uncertain execution, missing failure evidence, revoked use or an existing output remains blocked.

## EditPlan / Visual Direction

Voice and Talking produce or process audio/person media. A-roll and B-roll are
editorial roles: the primary content view and supporting footage, respectively.
An operation demonstration can be A-roll; generated Talking is not the only
possible primary view. Captions and viewpoint text are graphic treatments,
not automatically B-roll. EditPlan owns their content purpose and presentation;
the renderer executes it.

An explicit diagram visual plan can use the existing graphic route for a data
flow, information-loss illustration or factor relationship diagram. These small
Remotion SVG treatments show conceptual motion, labelled as illustrations rather
than measurements; they do not synthesize comparison evidence or turn a static
card into a product-quality PASS. Revised diagram compositions display the
approved narration copy at verified word positions, leaving raw ASR unchanged;
uncertain alignment receives no invented timed captions. Previously admitted
specs remain reproducible. Shot selection and source burned-in subtitle clearance
remain material editorial inputs; containment alone does not supply safe portrait
cropping, remove source subtitles or prove visual suitability.

Editorial choices should depend on the declared content task and available
evidence. Opinion/interview material may retain the speaker and context;
tutorials may need actual steps/screens/details; cases may need supporting
evidence; stories may benefit from shot/sound continuity. These are planning
directions, not implemented automatic classifiers or guaranteed improvement.
No extra graphic/B-roll is a valid choice. Existing creator/action requirements
cannot be satisfied by decoration, and suitable full-screen portrait footage
remains preferred when available.

Presentation reasoning should retain its source, scope and uncertainty.
[Descript's first-draft workflow](https://help.descript.com/hc/en-us/articles/44651483515917)
supports scene/layout choices and optional B-roll;
[OpusClip's B-roll modes](https://www.opus.pro/ai-b-roll) support automated and
sentence/custom-media selection. These demonstrate product workflows, not
objective superiority. [W3C caption guidance](https://www.w3.org/WAI/media/av/captions/)
supports synchronized access to spoken/non-speech information, not decorative
text density. [Signaling research](https://www.sciencedirect.com/science/article/pii/S1747938X17300581)
studies learning outcomes; learning retention is not social-video watch-time
retention and does not prove that large text improves opinion shorts. Claims
about our own output need declared content/audience/outcome and bounded
comparisons before becoming a default. These references do not impose a new
compliance gate or provide evidence of this product's effectiveness.

For limited-input final edits, the renderer supports optional sparse viewpoint
callouts in the unused upper margin of the assisted square-in-portrait
composition. The source stays centered at full width; narration and timed
captions remain unchanged. The assisted helper restricts cue bounds, overlap,
text size and density, leaving most frames without callout text. These cues
are evaluation props, not persisted EditPlan fields or normal UI/automatic
editorial selection. The continuous editorial-panel contract is separate;
neither layout proves better quality or generalization.

Normal production preflight/start exposes subject-first and editorial choices.
Both reuse the existing larger contain panel for supported AI Talking entries;
editorial requires a saved semantic scene headline instead of invented filler.
Headers exceeding the existing panel margin, or text over a full portrait
without a safe margin, stop explicitly. Full portrait media without graphics
keeps the full canvas. The inspected style is sent again at start,
bound by the preflight fingerprint and persisted on the Run. Exact-use
avoidance compares that same proposed presentation before selection. Changing
the UI choice clears its prior preview; it does not alter a previously created
Run or grant source suitability. Existing reviewed vertical derivatives can now
reach the retained-media path through the shared Router. Their existing source
bytes, full-interval crop/subtitle clearance and derivative identity are
revalidated without creating media. Invalid derivatives fall back to the
reviewed original complete frame and require authorization for that actual
selection. Source viewport creation remains separate in the ordinary media
panel; no automatic crop, detection or new evidence is inferred.

Source geometry is now consumed during ordinary planning, before Render. For
semantic comparisons, current source-analysis layout and subtitle rectangles
provide candidate geometry. Only full selected-interval coverage can select
adjacent, proportion-preserving upper/lower viewports and bottom clearance,
including under the default style. A sampled keyframe cannot authorize an
interval-wide crop; Renderer rechecks the same coverage before execution. The explicit comparison
style remains available. Unknown source geometry stops the affected scene;
fixed60px trimming and a gap between comparison panes are not used. Ordinary
footage with no suitable portrait alternative or useful editorial graphic stays
complete and centered. The existing source-selection and EditPlan/VideoSpec
contracts carry these decisions; see the media module for exact selection and
evidence limits. Sampled source analysis and offline geometry tests do not
establish full-interval visual quality or acceptance of a revised video.

Comparison layout fits the two proportion-preserving panes to the full canvas
height, leaving only centered side margins and a zero-pixel seam. It does not
shrink the footage to reserve a bottom caption band. The active caption keeps
its measured line wrapping on an opaque black plate at the bottom of the lower
pane; no source pixels show through the plate. Unused canvas and letterbox
regions around contained camera footage are pure black.

The ordinary draft scene editor now exposes existing creator-speaking,
action-evidence and explanatory requirements, their reasons and source
references, plus legacy/static-card/unsupported graphic intent. An additive
`GraphicCardPlan.kind=none` records an explicit reason to omit additional
graphics while retaining captions. It requires a media main visual: selected
typography, supplied graphic overrides and automatic typography tails for a
short continuous source fail explicitly. The legacy null declaration retains
existing behavior. Explicit static-card text can also supply a bounded Talking
panel headline; it does not replace the speaker or become a full-copy overlay.
Ordinary editorial cards reuse the common semantic card renderer instead of
the former interview-specific duplicate template. Timed editorial presentation
cues remain evaluation props. Unsupported diagrams/animation remain
unsupported. No classifier or universal stylistic default is introduced.

Changes save through the existing draft endpoint and retained revisions;
server input identity invalidates old routes, choices and VideoSpec. Unsaved
copy/scene intent disables new production preflight/start/resume, and a saved
revision requires fresh preflight. Existing Runs keep their inspected inputs
and source/QA/purpose gates. Requirement/graphic reasons and source references
are declarations, not independently verified evidence. Component/API tests and
the built ordinary UI on a production-database copy verify edit/save/reload
and a real blocked preflight; they do not prove a positive normal render or
generalized editorial effectiveness. Source/copy/admission readiness remains
necessary before an actual normal render.

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

Final9:16 output and source/presentation proportions are independent. A person-led
composition may use an eligible9:12,9:14 or square crop instead of forcing a
full-height9:16 source. Within supported geometry, prioritize comfortable subject
scale and useful footage; keep headings compact, captions clear and graphics
optional. A constrained input should receive the best supported adaptation with
specific limitations and alternatives for the user's choice. Do not infer that
the entire source is unusable from one rejected fixed crop or text-heavy panel.
Normal conditional-choice/automatic layout remains unimplemented; known rights,
crop suitability and human quality gates remain distinct from aesthetic tradeoffs.

The opt-in `content-os-subject-first` portrait panel gives eligible Talking media
and authorized USER/HISTORICAL B-roll a styled contain viewport, with captions
outside it. B-roll support reuses the existing panel and does not crop, stretch,
alter source captions or add text. It preserves source aspect; wide footage stays
wide. This presentation choice does not supply crop evidence, tracking, higher
effective resolution or suitability approval. Existing default profiles retain
their former layout, and the revised output still needs real visual review.

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

VideoVisual also supports explicit `bottom_crop_contain`: positive bottom-strip
geometry and a nonblank evidence reference are required on local video media.
FFmpeg stages a separate file retaining the full source width; Remotion contains
that file without applying the crop a second time. Source bytes remain immutable,
and different treatments of the same source use separate staging entries. This
can appear in a Talking portrait panel, with all existing Run/Clip/copy/QA and
content-scope checks unchanged. It is an assisted VideoSpec seam, not normal
EditPlan selection, automatic subtitle detection or independent face-safety
verification. The opt-in `content-os-editorial` style profile adds distinct
headline, process and contrast layouts and a designed caption area; old profiles
retain their presentation. Compilation/fixture evidence does not confer
publishability.

An older assisted VideoSpec that references arbitrary sub-Clips of an admitted
TalkingRun cannot be replayed under current whole-Run identity/copy/speech
admission. The exact assembled Clip or an eligible provenance-preserving
derivative is required. Substituting IDs alone does not authorize partial speech;
replan or use the existing derivation boundary with its actual evidence.

An assisted evaluation can keep an admitted Run as one continuous visual/speech
scene and attach explicit timed layout cues to the retained Remotion props.
Headline, process and contrast content changes outside the source panel without
creating arbitrary sub-Clips or changing its admission/copy/duration. These
evaluation-only cues are preserved with the exact spec and render evidence;
they are not yet a persisted normal EditPlan feature or automatic editorial
decision. A source-short audio tail uses an explicit graphic hold rather than
extending the approved Clip, while the complete Master and timed captions stay
intact. Final human quality review remains separate.

Real assisted rendering now proves complete-width source staging, removal of a
known bottom strip, preserved approved Master/captions, explicit semantic layout
cues and full MP4 decode/timestamp integrity. That evidence does **not** prove a
publishable person-led vertical composition: the reviewed text-heavy panel
output was rejected for billboard-like text and insufficient footage area.
Complete-width containment can be technically safe while failing this product
goal. Automatic face-following/reframing, subtitle-region discovery and reliable
visual suitability selection remain unverified; current evidence cannot label
the horizontal source intrinsically impossible to adapt.

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

The operator primarily supplies hard-defect findings; evidence-backed routine
presentation choices belong to the implementation/editorial process. Material
defects include meaning/context loss, missing required evidence, harmful
framing, subtitle conflicts/readability and audio/video artifacts. A missing
complaint is not a positive judgment. This collaboration allocation does not
change current exact-asset Voice/Talking or final U-Product admission, nor
silently remove naturalness/likeness/continuity/publishability criteria. Any
runtime review-granularity change needs its own versioned migration/tests.

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

Normal production preflight/start now consumes approved same-project retained
Voice/Talking using the existing `evaluation-composition-v1` consumption rules. It
selects an AudioAsset directly, maps exact current scene speech into one or
more original TalkingRuns, and emits a full-Master VideoSpec. Historical
EvaluationMaster/child-operation bindings, generation status/runtime versions
and policy2 generation history are not consumption prerequisites. Current
consent, retained independent or legacy quality review, original file hashes,
copy/timing, source-use exclusions and applicable third-party terms remain
mandatory. An operator adopts the existing exact composition-use review and
one local-render allowance; a purpose label or old provider grant is insufficient.
Generation admission remains separate and does not accept these consumer grants.

The same consumer also accepts a newly QA-verified and human-approved project
Master with routed original footage and declared static graphics. It reuses the
normal Router selection rules, exact speech timing, EditPlan, ProductionRun and
Render Worker; an intermediate EvaluationMaster Run or a TalkingRun is not
required for non-speaking scenes. Original footage keeps its complete frame and
receives no overlay when subtitle status is unknown. Exact source rights/use
review, source hashes, project isolation, exclusions and the one-render allowance
remain required; unmatched or too-short evidence footage stays unresolved.
Saved-plan Voice generation and QA keep their existing separate admission paths.

The ordinary Render Worker executes both ordinary and scoped retained renders
in `RenderVideoJobHandler`, sharing completion accounting and failure handling.
An operator-authorized short-preview request also reuses this executor. Its
exact composition-use receipt, separate authorization and exclusive frame range
(at most ten seconds, shorter than the full timeline) bind one `preview` slot
in the existing batch, a new Job and an independent `assisted_test` ledger key.
It preserves the completed full-render slot and all history. Duplicate requests
return the same preview; changed grants cannot replenish that slot and a failed
preview cannot retry. One separately authorized `preview_correction` slot can
reference that exact completed preview only when its output is still verified,
the composition fingerprint and VideoSpec have changed, and the new authorization
differs. It preserves the original preview, uses a new Job and independent call,
and cannot be replenished by more grants. Current use, budget, frame bounds and
revocation are still checked; a failed or uncertain original cannot enter it.
The original VideoSpec, subtitle timing and Master remain
unchanged: Remotion renders the authorized frames and FFmpeg takes audio from
the same Master interval. Technical QA checks that short output; visual quality
and full-video acceptance remain separate. This is an internal operator preview,
not a new ordinary creator approval gate or a second rendering implementation. Private
output persistence and once-only allowance remain scoped. Normal render GET/HEAD/Range access checks the owned Run, current
permission, project and actual result hash under explicit internal purpose.
Selected generated input/output bytes retain their noncommercial restriction across
aliases. Unknown source subtitles receive no added overlay, reviewed full
frames are contained without a new crop, and a terminal non-speaking interval
uses an explicit text-free neutral background. Reservation and settlement reuse
the admitted VideoSpec estimate; unpriced local compute stays unknown, subject
to the existing unknown-cost policy and currency/call ceilings. Normal
ProductionRun resume reconciles a saved result without rendering again. A failed
Job with no reserved call, saved result or output may be replaced once through
explicit resume, preserving its history and consuming the original unspent
allowance. A reserved call is never replayed; pending cancellation uses the
existing Render Job cancellation.
Run status and playback use normal production/render APIs; separate Composition
selection/status/reconcile/media actions are removed. The read-only history list
is retained because normal ProductionRun has no equivalent list. Operator use/allowance
adoption, its read-only permission preflight, and necessary generation-recovery
APIs remain. Existing persisted records are retained. This consumption integration
does not establish real new-topic generation, automatic editorial suitability,
user-operated end-to-end acceptance or the30–60s R1/repeatability endpoint.

MasterNarration, VideoSpec, local rendering, subtitles, publication/feedback records and usage evidence exist.

Approved Voice and admitted TalkingRun can enter explicit assembly; a matching Run is also discoverable in normal asset routing. The draft preflight exposes tentative route/presentation, unknowns and stale-plan rejection without provider calls. It distinguishes exact reviewed native-portrait planned Runs from unknown media; that classification reopens the persisted admission gate and does not claim automatic visual suitability or publishable composition. The project UI exposes whole-plan preflight/start, durable status/resume/cancel, Run-bound Voice/Talking configuration/QA/review, plus bounded Voice and presentation recovery controls. Browser evidence is isolated fixture coverage; Voice review submission and a real end-to-end workflow remain unverified; the corrected Talking replacement transition has user-operated isolated fixture screenshot evidence, not real production proof. Offline component events now count four Voice workflow requests (plan, repair, QA, one fresh U-Voice) and three presentation requests (observation, plan, revision); internal Job/Audio/Render binding inputs are zero. Form edits, confirmations, refreshes and Worker simulation are outside that counter, and final U-Product remains required. The historical same-task total and user minutes are unknown; browser acceptance remains pending. Bounded repair is not yet real quality improvement or a measured manual-time reduction. Feedback consumption remains incomplete. R1 proof must follow normal planning before dispatch, then fresh-topic output and repeatability; another manually scripted render alone does not close this gap.
