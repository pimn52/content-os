# Content OS — System Architecture

This document is the stable **technical system map**. It explains how product modules become code/runtime boundaries. It is not a work log and does not prescribe implementation-agent model tiers.

## 1. Architectural shape

```text
                         Content OS
                              │
                ┌─────────────┴─────────────┐
                │                           │
          Product / Local Core        Execution Layer
                │                           │
      ┌─────────┼─────────┐         ┌───────┼────────┐
      │         │         │         │       │        │
 Creator/IP   Media    Project     Local   Remote   Resource
 Context      Assets   Timeline   Compute  Compute  Control
      │         │         │         │       │        │
      └─────────┴─────────┴─────────┴───────┴────────┘
                              │
                         Evidence / QA
                              │
                            Render
                              │
                      Review / Publication
```

The architecture is **Local-first Data + Hybrid Compute**:

- creator assets, project state, evidence and control stay local-first;
- inference may be local or explicitly approved remote/BYOK;
- product semantics do not depend on one provider.

## 2. Stable product layers

### Creator / Intelligence

Owns IP Profile, creator context, sourced opportunities, historical account data and feedback evidence.

It answers:

> Who is this creator, what context is trusted, and what should the content express?

### Narrative / Project

Owns Project, Draft, script revisions, editable narration performance intent,
its deterministic delivery-plan compilation, and ScenePlan.

It answers:

> What is the editorial intent?

It must not hard-code current model limits as narrative rules. Feasibility feedback can revise the plan explicitly before dispatch; it cannot silently change the creator's intent.

### Media / Asset Intelligence

Owns Asset, Clip, AudioAsset, analysis evidence, candidate selection and Shoot Tasks.

It answers:

> What real or generated production material can satisfy the editorial need?

### Hybrid Asset Router

Chooses the **visual/content route**: real creator media, capture, typography/static, generated Talking, future stock/image/video.

It does not choose the execution provider.

### Execution Planner / Compute Router

Owns provider/runtime/machine capability evidence and effective parameter resolution.

It answers:

> How should this requested capability execute under current quality, hardware, privacy, license and cost constraints?

Current implementation includes capability profiles, parameter precedence, Advanced Settings and local GPU lease—not whole-production cost optimization or automatic Local↔Remote selection. Feasibility/cost preflight belongs before generation; per-call accounting does not substitute for that decision.

### Voice

Produces new creator speech, independent QA evidence and the asset-specific
U-Voice record. A technically verified generated asset remains ineligible for
Talking/assembly until that record is approved.

Long-form generation may be implemented as bounded provider calls + verified composition. The final product object is the verified Master Narration, not the provider call.

### Talking

Produces creator-visible video for new speech.

Execution may use short provider-bounded slices. Slices are not product objects.

The product-level output is a **TalkingRun**: a reviewed continuous visual run mapped to one Master Narration interval and one authorized continuous source-performance run.

### Visual Direction / EditPlan

Owns the presentation decision between routed material and deterministic rendering.

It answers:

> How should the selected material be framed, styled, captioned and transitioned so the scene is safe and editorially useful?

EditPlan remains provider/render-neutral. Preliminary visual intent and source feasibility precede production; measured timing and admitted outputs then resolve that plan for assembly. Current presentation resolution around selected assets is only part of this lifecycle. Unknown crop safety blocks fixed cropping; containment is not an editorial-quality guarantee.
An authorized horizontal source may instead use a source-preserving portrait panel: a deterministic canvas presentation that leaves source pixels intact and keeps any new semantic text outside known burned-in subtitle regions. Subtitle removal or inpainting is a distinct future media-derivation capability, not a renderer side effect.

Media derivation owns source-to-new-Asset transforms, separately from suitability assessment. The fixed-crop boundary checks supplied interval assertions, subtitle geometry, source bytes/hash and crop/method-bound claim fields; it does not independently verify full-interval face/motion safety or authenticate the caller's judgment. A real byte-level transform is not a suitability pass; see MEDIA_ASSET_SYSTEM.md. Neither a reference string nor a mocked transform proves usable media.

### Timeline / Assembly

Owns MasterNarration and VideoSpec.

It compiles an accepted EditPlan plus verified audio/visual Assets/Clips onto one renderable timeline. Provider execution details must disappear before this boundary.

### Render / Review / Learning

Owns Remotion/FFmpeg output, human quality gates, explicit publication records, usage evidence and feedback.

A narrow Talking repair service atomically preserves predecessor evidence, creates one replacement Job and switches only the affected scene's active binding. Fresh QA/preview links a successor while old collections/reviews stay immutable. The current application guard and repair call/charge limits run inside the existing provider-reservation SQLite transaction before inference; repair does not confer human approval.

`SourceUseConstraintService` owns retained rejection intake and separately authorized exact-use policy versions/history/idempotency. Thin API routes expose configured-root discovery/import/adoption. Planning context and production preflight consume this service without changing Asset/Clip admission. Ordinary production Render Jobs retain a policy-context fingerprint for Worker rechecks; legacy unsnapshotted Jobs remain outside that hook. SQLite additive storage stays inside the modular monolith; neither a generic rules engine nor a new human-review authority is introduced.

## 3. First-class product objects

```text
Workspace / IP Profile
        │
Project / Draft
        ├─────────────── NarrationPerformanceSuggestions (ephemeral, reviewable assistant input)
        │                    └─ explicit user edit/save → NarrationPerformancePlan (exact-copy-bound editorial intent)
        │                                                   └─ NarrationDeliveryPlan (derived, pre-execution structure)
        │
ScenePlan
        ├─────────────── preliminary EditPlan / visual intent
        ├─────────────── suitability + execution/cost preflight
        │
        ├─────────────── Asset / Clip
        │
        ├─────────────── MasterNarration
        │
        └─────────────── TalkingRun
                              │
                              └─ execution-only Talking slices / child jobs
        │
Resolved EditPlan (admitted outputs + measured timing)
        │
VideoSpec
        │
Render
        │
Publication / Feedback
```

`NarrationPerformanceSuggestions` is deliberately not a persisted product
object. It exposes transparent rhetorical patterns for the current copy and
becomes execution-relevant only after the creator explicitly saves an edited
`NarrationPerformancePlan`. It therefore remains outside provider capability,
adapter receipt and audio-quality evidence.

### Execution-only objects

The following support reliable execution but should not leak into product semantics unless needed for diagnosis:

- provider child calls;
- VoiceGenerationSpans and their bounded generation/QA attempts;
- Talking slice plans;
- source reference windows;
- local GPU leases;
- adapter-specific temporal context;
- retry/lease metadata.

Standalone `ExecutionSpecificationV3` represents exact host3/component-policy2 evidence; the execution-record discriminator explicitly supports schema1/2/3 in policy2 reports, attestation2 bindings and snapshot2 observations without changing old serialized meanings or upgrading receipts. Adoption/current-evidence checks bind canonical inventory and exact declared OS/component dependency entries to implementation-owned resource bytes and host component digest; unknown recipes reject. Existing scope/frozen identity/ledger services preserve every ancestor and current purpose/operation limit, including mixed nested versions and older policy1 evidence. Application-only `PreparedOmniVoiceIdentity` owns a separately staged model tree and retains the same caller-owned prepared runtime/component host; snapshots independently recheck model membership/bytes, runtime, host and registry/installation CPU identity. It accepts no expected report/hash, performs no model import and has no invocation/execute method. File-preparation tests do not establish full native/model/device/dtype operation: the normal evaluation stop and full producer/QA/review/render requirements remain.

## 4. TalkingRun boundary

A reviewed TalkingRun should contain or reference:

- project identity;
- Master Narration asset and master-relative interval;
- authorized continuous reference Clip/run;
- provider/model/runtime/machine provenance;
- ordered child generation evidence;
- assembled **visual** result;
- automated QA result;
- human continuity/publishability decision;
- generated first-class Asset/Clip identity.

For a multi-slice run, the Master Narration remains the authoritative final audio. Child video outputs contribute the generated visual stream.

Intermediate slices are continuation slices. Only the final slice may request terminal face closeout when the selected adapter supports it.

Admitted TalkingRun is eligible media to Router/VideoSpec under the shared project/copy/admission gate; generated children remain ineligible. D025's single-planned-child bridge uses a versioned planned-origin snapshot on a one-element series and a durable local Job preparing a technically probed Master-audio preview. Completed generation/QA payloads remain unchanged. A narrow application service binds independent continuity review to exact candidate/origin/report hashes, then atomically promotes the same Asset bytes to existing TalkingRun/Clip and ProductionRun binding. API entry points share that service; replay and downstream consumers revalidate authority and provenance, and file consumption rehashes media. Portable paths resolve under configured data root. Already consumed origins avoid re-entering inference routing. Review-only previews remain unroutable. Narrow planned resume pins every admitted native-portrait Clip and Master, resolves measured timing through existing assembly, and atomically enters the existing render stage. Original generation fingerprints remain immutable; the resolved EditPlan/VideoSpec is the render payload snapshot. The shared origin gate remains valid during render while rechecking current source authority; resume replay compares the rebuilt specification and file consumers rehash media. Other scenes are deterministic typography. The same ProductionRun may retain planned Talking requirements before Voice production, use existing Voice/QA Jobs and exact U-Voice, then atomically transition to Talking waiting with the new bound Master. Voice/QA references and original generation provenance remain intact; current Voice authority is reopened before downstream consumption. This is a narrow application-service transition, not general dependency orchestration. Admission/resume is fixture-integrated and does not prove real planned rendering or human approval.

## 5. Provider and capability boundary

Universal Core contracts may describe capabilities and intent, such as:

- Voice;
- Talking;
- terminal face closeout;
- requested continuity;
- execution cost/privacy requirements.

Provider adapters own model-specific controls such as LatentSync silent look-ahead.

Capability evidence is scoped to:

> provider + model/version + runtime + machine/profile

Configuration precedence is:

```text
job override
> saved provider+machine override
> locally verified evidence
> provider conservative default
> unknown
```

Unknown remains explicit.

D029 keeps immutable schema1 and compact specification/receipt plus inventory. Private-tree/startup primitives independently copy/scan selected roots/bootstrap, verify full membership/bytes and freeze one command; CPython3.12 uses `_pth`, no site, `-I -S -B`, replacement env. Base-only actual startup passed; complete base/package composition remains synthetic evidence. Host observation retains OS/driver locations, changes and reported-origin gates; static PE preflight can reject unknown dependencies before loading. Explicit v2 owns a canonical finite OS policy at an obligatory inventoried slot, requires exact recipe/version pairing and independently compares its actual/private bytes. The finite owned physical profile contains22 reviewed components, with exact name-bound UCRT/OS-servicing URL exceptions and an empty API-contract list; reference syntax is not authority or an online runtime lookup. Exact system CRT members do not exempt compiler/vendor redistributables; changing it changes the execution digest and cannot reuse/adopt an old receipt automatically. The composed v2 preparation binds one private command/tree/profile/host, rechecks changes and rejects reported same-name OS shadows; this seam has synthetic evidence only and its execute method stops before subprocess launch. Windows owns internal forwarding/API-set resolution within the declared substrate; OS-API locations/build/revision and actual origins remain mandatory, without blanket System32/DriverStore/PATH, recursive OS attestation or a custom loader. v1 semantics stay fixed; changed policy bytes require a new execution digest/receipt. An application-only controlled loader adds bounded current-process native/Python origin readers, ordered pre/post gates, explicit System32/default and absolute target flags, consumed failures/handle cleanup and external redirection/collision rejection. Active contexts/resource-bearing native targets and private siblings remain unsupported; the same-child factory and target ABI/lifecycle remain synthetic-tested; a separate fresh base-only diagnostic now has actual enumeration, no-active-context/search restriction and post-setup origin checks. CUDA observation, vendor dynamic closure, bootstrap integration/device lifecycle and M4 carrying the same prepared object through reservation/launch remain pending. Full-tree work stays outside write locks; native/whole-purpose lane stays unclosed and evaluation dispatch disabled. Shared stdlib-only error/path/CPU/native readers also feed a separate owned base-only diagnostic child, avoiding provider-package fan-out. The parent freezes actual tree/CPU host/argv plus bounded nonce transport; child independently scans complete membership/bytes before helper import and checks observed sources/host. Shared topology checks cover direct root/Lib/DLLs/Lib/site-packages and explicit target-parent members; inactive nested stdlib helpers stay fully inventoried, while actually loaded native same-name different-source modules reject. The real base-only child matched tree/CPU host and enumerated origins; the retained five-entry-profile first-error report stopped on bcrypt.dll. The expanded finite profile has offline tests and a separate actual base-only baseline; neither verifies target loading or native closure. An explicit protocol2 diagnostic extension now aggregates all unsupported origins in the captured native/Python snapshots into bounded deterministic safe-name/kind/location-category/opaque-path-ID rows. It rechecks tree/CPU before blocked evidence, never truncates, and rejects stale/incoherent/downgraded transport; historical protocol1 remains separately interpretable. Actual base-only capture yielded17 blocked native system-directory origins, under the earlier profile, failing before activation/search. Reports remain evidence only, never policy/admission inputs; finite eligibility follows D029's component/API or reviewed OS-servicing evidence rule, retaining compiler/vendor and exact-origin boundaries. Baseline/search/no-active-context/recheck now have base-only actual evidence; target activation/loading, device/vendor dynamic closure and full native execution remain unverified. This diagnostic is not production admission. Current activation observation uses GetCurrentActCtx for the calling thread only; its null result does not attest absent process-default or module-associated manifests. D029 defines a future bounded resource/manifest and exact Common Controls binding lane, but that binding lane is not integrated: current all-resource rejection and native dispatch stop remain. Full default/module binding evidence and any versioned component-store exception remain pending. Application-only pe_resources/native_manifest/native_dependencies helpers now provide bounded resource classification and a diagnostic reachable dependency plan. PE imports reuse the unchanged parser; private export forwarding is recorded as a dependency, while mapped zero-initialized exports need no raw-byte read. Full prepared inventory/search checks remain, and an owned-reader comparison verifies exact required root-private identities. The planner is disconnected from dispatch/ControlledLoader admission; pending Common Controls classification cannot load a target. These are tested seams plus static-byte evidence, with actual SxS/default/module-context binding still pending. New windows_activation/common_controls_binding diagnostic helpers bound API buffers/strings/counts, recognize only exact serviced Common Controls component paths and compare component file hashes; created contexts are released without activation/target load. Effective/module snapshot changes or missing API evidence stop. A separate inert typed host v3 carrier preserves old v1/v2 canonical forms and is deliberately not accepted by current execution specifications or prepared/child witnesses. These seams have fake ABI/temp-file evidence plus a bounded actual metadata failure at an unsupported assembly shape. API return success is distinct from decoder support and binding eligibility; raw flags are explicitly retained at context/assembly/file levels as untrusted diagnostic data, while the binding policy rejects nonzero/missing/invalid flags and extra rosters before component reads. No implicit zero defaults or new host/load permission is introduced. Actual complete binding shape and full default/effective/module comparisons must close before any new eligibility. Real metadata decoding now observes a Common Controls Resources assembly beyond the frozen named-assembly contract. Decoder success is distinct from its code/data-file and context closure; zero file counts do not establish absence. No component-store eligibility or active prepared/child v3 witness is added by that observation. D029 separately freezes future component-policy2: one optional exact Resources/MUI data-only companion; neutral identity derived from the actual definition manifest; independently read fixed code/data bytes despite API files0; required opaque root flags, with independent binding gates unchanged. Bounded PE32 resource data is not x86 code authority. Original v1/v2 and inert v3/policy1 semantics remain; policy2 needs its own typed branch and owned digest, no auto-adoption. Future owned fresh-child closure compares EXE resource1 prediction with initial effective binding and queries relevant private resource2 associations at actual handles, not every trusted OS module; missing query evidence is never absence. The policy2 static comparator, narrow component_manifest/pe_data helpers, owned windows_component_policy resource and separate HostRuntimeObservationV3Policy2 carrier are implemented. Old schemas/comparator retain their meanings; new carrier JSON and instances remain rejected by old host parsers. Bounded component rechecks have temporary-file evidence and a retained-metadata/current-file comparison; a separate protocol3 diagnostic now composes fixed owned stdlib helpers, independent prepared CPU host/tree/created binding, effective/associated comparisons, nonce transport and same-role/post-launch rechecks. Its new bounded owned bundle allows at most24 files without widening the legacy eight-file recipe; actual runtime evidence remains pending. The added relevant-private context reader selects the actual executable/root, checks private source hashes/resources and actual handles, skips resource-free private and trusted-system context queries, and verifies snapshot/metadata/file stability. This diagnostic seam has fake-API evidence only and adds no host/loader eligibility; the separate owned protocol3 caller now composes full inventory/OS-origin/startup checks and policy2 comparison under fake-API tests. It does not certify all threads or dormant activation stacks, permit component-store loaded code, integrate target lifecycle or change production admission. The separate CPU diagnostic also has actual prepared-tree/current created-context binding evidence. Canonical absolute directory Path comparison handles the OS trailing separator without accepting alternative roots or aliases. Legacy protocol3 still stops on unsupported fileless origins; the separate protocol4 CPU source/context closure is described below. Target lifecycle and production dispatch remain unverified. Policy2 internal comparison errors expose only a fixed first-checkpoint, retaining the existing base exception/error identity and all checks. Bounded raw metadata capture belongs to the separate local evaluator, before comparison, not host/receipt/production payloads. A checkpoint locates a failed check, not a verified cause or permission to bypass it. Python-origin rejections now preserve the same generic stop and add bounded fixed-checkpoint/safe-module diagnostics only in separate protocol3 reports; historical reports without the field still parse, and protocols1/2 are unchanged. A bounded actual child identified pyexpat.errors at missing_file before context comparison. Default source observation rejects both fileless pyexpat data objects and same-name impostors; only the separately versioned owner recipe below can establish the finite owner relation. D029 now permits a finite owner relation in separate protocol4/cpython312-expat-owner-v1: exact private pyexpat extension/native origin/inventory and built-in owner identity, two bounded data-module objects and optional exact stdlib aliases, with strong-reference/data/file rechecks. A standalone owned factory/report implements that relation; protocol1-3/default source checks and existing host/receipt contracts keep their semantics. This source observation assumes verified fresh owned startup before application/plugin code and does not attest hostile process memory. Offline tests cover replacement, file/native mismatch, extra code/namespace, aliases and transport downgrade. The standalone protocol4 CPU diagnostic has now passed actual parent/child created-binding equality, effective resource1 and six relevant private resource2 contexts, restricted-search source/owner rechecks and full-tree/OS stability. This proves only that fresh owned diagnostic process, not target loading, model/device/dtype closure, all threads/dormant activation stacks or production dispatch. Policy2 predicts with current-user UI language (no explicit LANGID); raw mixed-case language is retained against the independent definition, with a lowercase directory key only. Associated context directories bind to each independently inventoried source parent; effective context binds to the EXE parent. WindowsPath lookup preserves actual-source checks and returns the inventory name. Owned policy bytes identify these rules; old receipts cannot acquire them implicitly. Exact per-role metadata and all cross-role resource/language/version/file/hash comparisons remain enforced. Failure reports retain bounded raw role/source/checkpoint evidence, never a bypass. Separate protocol5/cpython312-bz2-mapping-v1 now composes the shared bounded dependency graph, exact baseline/native/owner checks, pre-load independent resource prediction, post-load planned-origin/context validation and one-use reference release. The fixed stdlib-only 22-file bundle and typed report remain isolated from protocols1–4 and normal dispatch; parent independently recomputes graph/target identity. This diagnostic maps only DLLs/_bz2.pyd, never calls exports/PyInit or initializes a model. Offline ABI/transport tests cover preflight rejection, post-load failure cleanup and consumed replay; they are not actual load evidence. The bounded actual fixed-target diagnostic stopped at execution_native_plan_dependency_unknown before any LoadLibraryExW, because the owned API-contract list is empty and the reachable graph contains unclassified API-set imports. Actual target/postload/release verification therefore remains absent. Unknown names remain denied; an observed import is not reviewed OS authority. Device/dtype/vendor dynamic closure, M4 normal execution, receipt adoption and whole-purpose eligibility remain pending. The current owned OS profile now has13 exact virtual API contracts: core-path-l1-1-0 and12 required CRT-l1-1-0 subsets. Reviewed primary naming/OS-substrate evidence is retained with each entry; exact URL/name exceptions do not authorize all Support/UWP pages or all API prefixes. Graph classification can consume these names, while actual loaded same-name files remain rejected as virtual-name shadows. This changes policy/inventory/execution identity, never implicitly migrates an old receipt. The former empty-list preflight gap is addressed in code; actual new-target closure still requires its own bounded runtime evidence. The separate protocol5 fixed-target mapping diagnostic now has actual fresh-child evidence for the four-node/39-edge _bz2.pyd graph, independent pre-resource prediction, actual planned target origin/resource2 association, unchanged baseline/effective bindings, one reference release and post-release source/tree/OS checks. It performs no exports/PyInit/model/device initialization and grants no production authority. Application-only frozen_worker retains one prepared object plus a detached typed identity and current Job/owning-ancestor/receipt/profile/settings facts: expensive file/runtime/report/inventory/terms observation is outside write locks; transaction guards compare DB/frozen values only; late rechecks remain outside locks. ProviderExecutionService has backward-compatible optional reservation/pre-action/replay hooks and a narrow same-DB/local-Voice prepared method; exact execution hash/scope/Job/input participates in durable request identity. Only the reservation owner receives the retained object; replays never invoke it, current license still rechecks, and late failures/crashes follow existing failure/unknown/reconciliation without automatic retry/refund. These application seams are temporary-DB/fake-action tested only and are not wired to enable normal evaluation API/Worker dispatch. Full audited model/dynamic/device/dtype preparation, producer/output-purpose integration and normal workflow closure remain required.

## 6. Data and provenance

Persistent state lives in SQLite and local media roots unless an explicitly approved remote provider is invoked.

Important generated assets retain enough provenance to answer:

- which source/reference was authorized;
- which master narration interval was used;
- which provider/configuration executed;
- which QA evidence admitted the result;
- whether human review approved the result;
- what project/output used it.

Portable stored media paths must resolve through the configured local data-root boundary; worker process current directory is not a data contract.

## 7. Job and resource model

Heavy work is executed through durable Jobs with:

- idempotency;
- provider-call ledger;
- budget/cost accounting;
- retry/recovery boundaries;
- explicit QA states.

Local Voice/Talking GPU work may hold a SQLite-local named resource lease to prevent same-machine VRAM contention. This is intentionally not a distributed scheduler.

## 8. Quality gates

Automated evidence and human judgment are separate.

### Voice

Automated:
- copy/timing/silence/playability/provenance.

Human:
- likeness, naturalness, emphasis, pace, pauses and rhetorical rhythm;
- one durable review record for the exact generated asset, after automated QA.

### Talking

Automated:
- playable streams, duration/timing, narration-copy inheritance, raw-output integrity.

Human:
- visible sync, identity, mouth/teeth artifacts, source motion/gaze retention, continuity and publishability.

Policy v1 series admission requires child approvals and whole-run continuity; direct generated-child production consumption fails closed through the common admission gate. D026's v2 API lane pins policy at new planned ProductionRun creation and preserves v1 canonical origin serialization. A distinct v2 origin keeps technical/media/execution provenance without optional child approval. The shared policy service requires explicit six-dimension review of the exact Master-audio preview and retains append-only subject-bound concerns/answers. Pending or negative concern evidence blocks admission and consumption; exact positive resolution can reuse an unchanged aggregate judgment. Legacy/evaluation and multi-child paths remain v1; normal UI migration is separate. Synthetic/local and fixture evidence do not establish real v2 human approval.

### Product

The final U-Product gate judges the rendered 30–60s output rather than internal job success.

## 9. Current implementation pressure

The codebase should remain a modular monolith for R1: FastAPI, SQLite, local Job/Worker, provider adapters and Remotion/FFmpeg.

Do not introduce microservices, Redis/Celery/Kubernetes simply because execution complexity grows.

As orchestration grows, move application workflows out of `main.py` into narrow application services rather than changing deployment topology.

Extract only the seam needed by the active package: plan preflight, Master/Run orchestration, or bounded recovery. Keep the same deployment and durable accounting. Future package contracts live in docs/implementation/ROADMAP.md, not this architecture map.

## 10. Productization rule

A capability is not fully productized merely because a controlled experiment succeeded.

It becomes a product capability when:

1. the behavior exists behind a normal product contract;
2. required evidence/QA is persisted;
3. the product can create or select it without experiment scripts;
4. it produces a first-class object consumable by downstream normal flows;
5. the normal UI can expose the outcome at the appropriate abstraction level;
6. failure/recovery is explicit.

Report code/contracts, real runtime, normal UI/workflow, repeatability and autonomous decisions separately. Exact approved Master/Run evidence does not prove all five. Human review records only become reusable policy through explicit scope/version, adoption, regression and consumption by later planning.
