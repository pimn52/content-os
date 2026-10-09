# Creator, IP and Intelligence

## Product responsibility

Maintain the durable creator context that lets Content OS decide **what is worth creating and how it should sound like this creator**.

The primary product object is the creator/brand workspace, not an isolated video.

## User outcome

A creator should be able to upload/confirm their own information once, then repeatedly start new content from a topic or evidence source without restating identity, positioning and historical context every time.

## Core objects

- Workspace / creator or brand context
- IP Profile and revisions
- Project / Draft
- Opportunity / sourced topic signal
- Account Connection
- Historical Content
- Feedback / publication observations

## Functional system

### IP Profile

Stores confirmed creator context and evidence. Drafts retain the IP-profile version used so later changes can invalidate stale downstream outputs.

### Topic / Opportunity intake

R1 supports explicit, sourced inputs such as:

- manual notes;
- the creator's own historical content;
- read-only account signals.

Broad competitor crawling, market prediction and autonomous trend scraping are not R1 requirements.

### Account Intelligence

Creator-owned historical content may inform topic selection and IP-aware drafting. Account Intelligence is distinct from future broad Market Intelligence.

### Selected short-video presentation direction

The creator has selected a clear, restrained knowledge/explanation style as the default direction: paced spoken-copy captions, optional semantic hook/point text rather than a second full-paragraph copy layer, meaningful supporting visuals, and text outside faces/source subtitles/platform controls. This is an editable direction, not a universal short-video requirement, platform growth guarantee or fixed word/duration threshold. Deliberate full-text cards remain a distinct presentation choice, subject to the existing caption-duplication preflight. These design expectations do not claim implemented automatic face detection, subtitle removal, crop qualification or layout safety detection.

Explicit creator selection is preference authority, not evidence of an observed media defect. The selection must remain separate from Render-bound feedback adoption and its retained-case regression; chat approval cannot be encoded as a fabricated `duplicate_text` observation or fixture-based runtime approval.

Normal project UI/API now supports direct creator selection without a defect observation: save a candidate against the current creator/IP revision, then explicitly enable it using version-bound state changes. Stored authority is `creator_selection`, with no Render observation or retained-case quality claim. Explicit selection takes precedence over the same feedback-derived rule; feedback cannot silently supersede it. Disable restores original future planning behavior, without automatically re-enabling a superseded older rule. IP changes hold old selections; renewing the selection creates a new candidate and requires fresh adoption. Legacy profiles lacking all revision rows receive a migration-time current baseline, explicitly labeled `migration_snapshot_not_historical_revision`; this is not reconstructed historical creator evidence and does not rewrite the profile.

### Learning loop

Publication/feedback records produce evidence-linked next-suggestion text. Ordinary project UI also exposes exact Render-bound presentation observations and explicit planning-preference classification, candidate validation, adoption and disable. Asset defects and configuration limitations remain non-executable classifications; they are not promoted into creator defaults.

One bounded creator preference is supported: `semantic_graphic_not_full_copy_v1`. An exact retained `duplicate_text` observation, intact render bytes/spec and a retained scene with an existing semantic short emphasis alternative are required. The candidate records a before/after regression without changing voice copy. Explicit adoption binds the creator/IP revision and rule version. Fresh ScenePlan requests consume it in planning context and deterministically choose an already supplied short emphasis instead of a first emphasis duplicating the full voice paragraph. No alternative means no invented replacement: normal presentation preflight still rejects the duplication. Hard admission/permission/cost gates are unchanged.

At most one version of this rule is active per creator; explicit newer adoption supersedes the older rule with retained version history. Disable restores the original behavior for future planning, while existing saved drafts/production and NarrationPerformancePlan remain untouched. Changed evidence bytes/spec or IP revision holds consumption/adoption; cross-creator consumption is prohibited. Planning checks for changed context before persisting a provider result. Offline service/API and component-event regressions establish this seam, not actual creator adoption, browser interaction, cross-topic quality improvement or learning from Voice/Talking likeness judgments.

The target is an explicit, scoped chain: finding → asset defect/configuration limitation/creator preference → candidate rule → retained-case validation → confirmed adoption → changed next planning decision. Keep creator/configuration scope, version, evidence and disable/rollback controls. Do not silently promote one person's judgment into a global default or alter saved narration intent. R1 does not claim autonomous growth optimization.

For retained final-render failures, D028 specifies a separate, explicitly adopted source/presentation avoidance constraint. The creator confirms an exact future-use scope; the original human finding remains unchanged. It can exclude a particular recommendation while leaving the Asset and its independent approvals intact. It cannot generalize an entire rejected composition into frame-level defects or certify a changed source/interval/layout. Legacy evidence intake and this configuration-use rule are pending implementation; current executable planning preferences remain limited to the short-emphasis rule described above. An already-ineligible candidate provides no evidence that a new constraint changed routing.

## Product boundaries

- Never invent creator facts merely to complete a profile.
- Evidence and uncertainty remain visible.
- Editing script/topic/IP invalidates stale dependent plans or renders.
- Market Intelligence must remain separable from creator/IP intelligence.

## Current capability

ScenePlan now carries `visual_requirement` (`unknown`, `creator_speaking`,
`action_evidence`, `explanatory`) and `visual_requirement_reason`. Non-unknown
declarations require a nonblank editorial reason. The normal provider output
schema/prompt requests both fields, and ordinary Draft save/read preserves
them. Legacy stored plans and legacy complete provider output without both
fields remain unknown; partial declarations are rejected. Explanatory means
graphics can preserve the point without losing required person/action
evidence, not simply that footage is unavailable or expensive. This is
planner-declared editorial intent, not independently verified classification
or media suitability. Current real scenes are not retrospectively classified.
Routing consumption belongs in MEDIA_ASSET_SYSTEM.md. Full automatic
material-aware replanning and real classification accuracy remain unverified.

Fresh normal planning receives a read-only `production_feasibility` brief:
recorded Voice/Talking readiness and quality, commercial-evidence status,
current execution-license uncertainty, global/project budget policies and
pre-reservation usage observations. No profile is an execution authorization;
prices, local compute and manual time remain unknown. Missing policies and
profiles remain explicitly unknown. Typed status fields are exposed, not
free-form evidence/provenance or runtime secrets. Normal preflight/dispatch
must recheck all gates after the planning call.

Both provider protocols distinguish creative preference from irreplaceable
visual necessity. Trust, branding, hooks and emotional closure alone do not
establish required visible exact speech; action topics alone do not establish
required action evidence. Reasons should identify the supplied/content basis,
not invent creator requirements from basic IP metadata. Adequate explanatory
narration/semantic graphics can reduce new capture without suppressing genuine
evidence requirements. Unresolved necessity stays unknown. This is a prompt
contract, not independently validated necessity authority: no new schema,
classification proof, automatic rewrite or waiver of saved creator/action gates.

Live usage is excluded only from provider retry identity and never added to
content-evidence refs, so the call's own reservation/completion cannot invalidate
its result or prevent same-key replay. Capability summaries and budget policies
remain in request identity; changed policy requires a new explicit request.
Replays return the accounted earlier plan, not a new feasibility authorization.

New ScenePlan provider output also requests nullable `graphic_plan`. If
typography is a proposed source/fallback, the new output must declare either
`text_card` with treatment/all required points, or `unsupported` with a reason
and no executable card. Supported static layouts are headline (one point),
key_point (one to four points), contrast (two points); points are nonblank,
single-line, at most 24 characters each and 72 total. These are conservative
engineering layout bounds, not editorial duration or quality thresholds.
Required timelines, node links, waveform changes and animation are not text
cards. An adequate static alternative must be described as that actual card;
classification does not independently prove adequacy.

The parser accepts complete legacy output, complete visual-requirement output,
or complete new graphic output; partial new shape is rejected. Legacy plans
retain null graphic_plan and old behavior, with undeclared effect coverage
unverified rather than retrospectively asserted. Existing ordinary JSON Draft
contracts preserve the additive field; no database migration is needed.

The provider request separates `audience_brief` (title/requested topic/user
script), `background_evidence` (creator/material/opportunity evidence) and
`production_controls` (format, preferences, feasibility, exact-use constraints).
Evidence is not an instruction source. Controls shape production/presentation,
not new audience claims or workflow lessons. For the known adopted semantic
short-emphasis rule, the provider receives a declarative style projection with
identity/version/authority, not the internal missing-alternative/preflight
procedure. The persisted preference and content-evidence refs stay unchanged.

A bounded output regression guard rejects observed internal workflow phrases
in `voice_text`/`caption_emphasis` when that preference is active and the
explicit topic/script does not ask for them. An explicit Content OS preflight
tutorial or supplied exact workflow phrase remains valid; merely mentioning
preflight in unrelated content is not banned. Metadata/materials cannot grant
this exception. Rejection uses fixed `scene_values` / `audience_control_leak`
and a safe field identifier, without quoted copy, automatic rewriting or retry.
Ordinary planning checks accounted replays before saving as well as fresh
provider outputs. Rejected replay retains the original completed ledger record
but cannot overwrite the Draft. This is a narrow regression check, not complete
semantic detection of paraphrased instructions, scene-purpose leakage or visual
adequacy. Legacy saved scenes are not edited or retrospectively approved.

The ordinary BYOK ScenePlan adapter accepts opt-in runtime reasoning effort,
output-token ceiling and socket timeout, with protocol-specific validation and
safe stage/HTTP/size/timing logs. Unset optional controls omit vendor parameters.
Chat prompts now explicitly carry the existing ScenePlan output schema; an
opt-in provider JSON-schema mode sends the same contract as `response_format`,
without implicit capability detection or fallback. Local validation remains
mandatory in either mode. Safe fixed-category validation reasons distinguish
output/field-shape/domain errors without logging creator/provider content.
Domain failures retain the existing `scene_values` category and additionally
expose allowlisted `domain_rule`/`domain_field` diagnostics: invalid fields,
missing/blank editorial reason, empty plan, scene identity/order/duplicate ID,
real-source precedence and fallback-only real sources. Only fixed identifiers
are logged, not Pydantic messages/input/context, provider values or exception
chains; unrecognized diagnostics stay unknown. Cross-field source ordering
and nonblank reasons are explicit prompt instructions and local checks,
not assumed guaranteed by provider JSON Schema. Existing API/ledger error
codes and rejection behavior remain unchanged; no repair/retry/normalization
is performed. These diagnostics cannot reconstruct a previously discarded
response or prove its exact failing rule retrospectively.
The fallback-only real-source prohibition has one narrow editorial exception:
`visual_requirement=explanatory`, a nonblank reason, typography as the first
preferred source, and no preferred AI_VIDEO/TALKING_PROFILE. Such a scene may
retain real sources only as alternatives. Unknown, creator-speaking,
action-evidence, non-typography-first and required-Talking cases retain the
prohibition. Real-source ordering when mixed into preferred_sources remains
unchanged. This allows a conceptual graphic plan, not unverified media
admission or a guarantee that a model's editorial declaration is correct.
The prompt and local validator use the same narrow exception; rejected
outputs are not reordered or reclassified to qualify.
Provider support is not inferred. Incomplete/truncated answers are rejected
before draft persistence, without automatic retry. These configuration and
offline diagnostic contracts do not establish successful Kimi generation,
billing measurement, streaming progress, material-scope selection or improved
creator quality. Setup belongs in README.

The local workspace, persistent IP/profile state, projects/drafts, sourced opportunities, read-only account/history records, publication records and feedback loop foundations exist.

Remaining evidence includes actual scoped adoption of a real failure class and changed normal new-topic decisions, plus verified production constraints. Exact approved Voice/Talking assets exist, but normal planning, orchestration and repeatability remain unproven; this module is part of that missing loop, not outside the bottleneck.

Retained whole-render rejection now has a separate ordinary backend intake and creator/IP-revision-scoped `configuration_use_limit` adoption/history/disable contract. Enabled adopted uses and their current evidence state enter fresh planning context; all versions and disable tombstones fence stale results. This is not the creator short-emphasis preference and cannot turn a whole-render finding into independent scene judgments. Exact-use preflight avoidance is offline-tested; actual scoped failure adoption, normal UI and real subsequent decision/quality evidence remain missing. Full scope/coverage belongs in TIMELINE_RENDER_LEARNING.md.

The normal project feedback panel exposes retained rejection discovery, source confirmation, evidence classification, readable interval scope selection, reasoned adoption and disable using the ordinary API. UI component tests and bundle build pass. Real user adoption and live browser-flow evidence remain pending; see TIMELINE_RENDER_LEARNING.md for exact effects and limits.
