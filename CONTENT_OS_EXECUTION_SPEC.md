# Content OS R1 — Execution Specification

Updated: 2026-09-15 (Asia/Shanghai)

This is the **single authoritative product/execution contract** for R1. Read `START_HERE.md` for navigation and `STATUS.md` for current progress.

## 1. Product direction

Long-term loop:

> **Know what to create → Create it as you → Learn what works**

Content OS is Local-first / BYOK infrastructure for an individual creator or small creator workflow. R1 focuses on `Create it as you`; it is not a generic AI video generator and broad Market Intelligence is not an R1 requirement.

## 2. R1 product gate

R1 is not complete until an explicitly authorized creator can:

1. initialize one persistent IP profile from confirmed information and real materials;
2. enter a new topic and obtain editable copy using current IP context/evidence;
3. generate **new speech in the creator's authorized cloned voice**;
4. generate at least one **new Talking/lip-sync segment of that creator speaking new words**;
5. combine it with reusable real clips, optional capture, typography/static material and subtitles;
6. export a 30–60 second vertical video through the normal UI;
7. repeat without re-recording complete narration or manually cutting all media again.

Source-led recuts, imported finished narration, old mouth motion, generic TTS or generic avatars may remain explicit fallback paths but **do not satisfy this gate**.

Human judgment is required for creator voice likeness/naturalness and Talking likeness/naturalness/publishability.

## 3. Product invariants

- **Local-first Data + Hybrid Compute.** Creator media/library/profile/project/control remain local-first. Heavy inference may run locally or through an explicitly approved replaceable remote provider according to verified capability, privacy, latency and cost.
- **BYOK / replaceable providers.** Core contracts do not depend on one model/vendor.
- **Real user media first.** Use existing creator assets before generated media when quality is adequate.
- **Continuous clips are playback assets.** Keyframes support understanding/search.
- **Upload once.** Derive audio, transcript, keyframes and metadata automatically when capabilities exist.
- **Capture is a low-cost option.** Missing material can produce a shoot list before expensive generation.
- **No hidden downgrade or cloud fallback.** Unavailable capability, remote data transfer and estimated/unknown cost must be visible.
- **Consent and rights are explicit.** Voice/face generation only uses authorized references.
- **Unknown cost is not zero. Unknown capability is not verified capability.** Runtime calls use persistent budget/idempotency/usage accounting, and routing may not promote documentation claims or another machine's results to local verified evidence.

## 4. Architecture to preserve

Keep:

- FastAPI local API + SQLite;
- provider-neutral domain contracts;
- idempotent Job/Worker model with retry/recovery;
- local media import, FFmpeg/ffprobe, continuous segmentation and ASR/vision/index boundaries;
- IP profile and revisioned project/draft state;
- ScenePlan + real-media-first Hybrid Asset Router;
- `MasterNarration` / timeline assembly boundary;
- Remotion + FFmpeg render path;
- browser/mobile upload and private-network access boundary;
- cost/budget/provider-call ledger;
- backup/restore and dependency inventory.

R1 does **not** require Redis, Celery, n8n, Kubernetes or a microservice rewrite.

## 5. Evidence modes

| Mode | Purpose | May prove |
|---|---|---|
| fixture | deterministic regression | contracts, state, encoding, orchestration |
| assisted-test | real samples analyzed with an authorized development tool/model | quality for those samples; not independent runtime |
| runtime | application executes configured local/BYOK provider itself | repeatable product capability |

Do not call fixture success “AI understanding”, and do not call assisted-test success an independently runnable product.

## 6. Voice strategy

`VoiceProvider` remains replaceable.

### OmniVoice

OmniVoice is approved as a **local non-commercial evaluation provider / quality benchmark**. Its technical fit includes zero-shot cloning, short references, multilingual coverage and reusable clone prompts.

License boundary:

- repository/source: Apache-2.0;
- official pretrained weights: currently CC-BY-NC.

Therefore official OmniVoice weights must not be bundled or advertised as a commercial-safe default. OmniVoice-specific state must not leak into Core contracts.

### Current provider decision

- Chatterbox has been tested and **rejected for the current R1 path** because creator timbre similarity/naturalness were clearly below the OmniVoice benchmark.
- A commercial-safe local Voice provider is currently **unselected**.
- A BYOK/cloud Voice path remains valid for machines without suitable local inference or where quality/license constraints require it.

### Voice QA

Generated narration cannot reach final render without recorded QA covering at least copy coverage, missing/duplicate content, duration/silence sanity, playability and provider/reference provenance. Naturalness, pacing, breathing and likeness remain an explicit U-Voice human gate.

## 7. Talking / lip-sync strategy

`TalkingHeadProvider` remains replaceable. R1 must prove at least one real new creator Talking segment but does not lock to one model.

### Admission rule

A mature Talking candidate must work from **ordinary, consented creator footage**, including material where the creator is naturally speaking or moving. It cannot require special silent/closed-mouth/expressionless AI-only recordings as a product prerequisite.

For the primary R1 path, prefer models/services that perform:

> **existing creator video + new audio → minimal necessary lip/face retargeting while preserving identity, original motion/gaze/background and visual quality**

over systems that regenerate the whole person/video when that regeneration is not required.

### Current provider decision

- MuseTalk 1.5 has been **rejected** by U-Talking on ordinary material and is not an admitted product provider.
- VideoReTalking has been **rejected** by U-Talking on ordinary material: the real run showed severe mouth deformation, blur and local scale/warp artifacts. It is not an admitted product provider and has no Core adapter.
- LatentSync 1.5 remains a **benchmark-only optional local adapter**. Current fresh-Voice evidence on this 6GB development machine includes a 2.58s U-Talking pass and a 5.12s U-Talking fail with late cumulative misalignment. These are routing evidence for this configuration, not universal model limits.
- KeySync remains deferred to a later compatible machine.

### Capability-boundary policy

Do not prematurely hard-code a Talking maximum such as 3–5s, 8s or 9s. Numeric examples are evidence points, not product requirements.

For a provider/runtime/material combination, locate the useful duration boundary with the fewest experiments:

1. maintain a known-pass lower bound and known-fail upper bound;
2. choose the next test near the midpoint of that interval, adjusted to a nearby natural speech boundary;
3. keep other variables fixed;
4. update the pass/fail bracket after explicit U-Talking review;
5. repeat only while another test is likely to change routing, UX or market-fit decisions.

Stop when the interval is sufficiently narrow for product use, results become sample-dependent/inconsistent, runtime noise prevents clean comparison, or further precision would not change the product decision.

Separate two questions:

1. **Visual Talking capacity** — reuse an existing narration and vary only duration to locate cumulative lip-sync/visual failure.
2. **Product capacity** — generate fresh Voice at natural phrase boundaries and require both U-Voice and U-Talking to pass.

The output is an **observed capability range under stated conditions**, not a universal model limit.

### Continuity policy

A set of individually passing short Talking clips does **not** prove continuous-presenter capability. Multi-segment continuity requires its own evidence.

Keep narrative intent independent from provider limits:

> Narrative/Scene Planner states what the content needs; Execution Planner decides how to realize it with current capabilities.

For example, a 9s creator explanation may be realized as one 9s Talking scene on a capable provider, or as several short Talking appearances separated by B-roll/typography on a constrained local provider. Do not rewrite the narrative merely because the current machine has a short generation ceiling.

### Terminal face-closeout policy

When a narrative itself ends on a Talking segment, the product may request a **face-visible terminal closeout**: the delivered audio/video ends at the final persisted speech timestamp while the final visible frame remains the creator face. A provider may use extra non-delivered context to predict that ending, but B-roll, black, Typography, frozen clones and model-only silent audio must not hide or extend it.

This is a provider-neutral capability, not a global parameter. For the current LatentSync 1.5 adapter, the user-facing setting is **结束静音前瞻（ms）** (`trailing_silence_lookahead_ms`): the amount of silent audio appended only to the model input after the final spoken sound. It gives the model time to move from the final phoneme toward a resting mouth; the silent context is then cropped out and the delivered asset retains only the original narration audio and duration.

The setting belongs to that provider/model adapter, not to a generic Talking contract or a machine-speed control. `600ms` is the current conservative adapter baseline, informed by D6g; it can run on another compatible machine as a `provider_default`, while the D6g human-quality pass remains evidence only for its exact provider/model/runtime/machine/reference conditions. **Content OS owns the closeout-protection request**: an adapter may implement it with a native Provider control or with a Content-OS context-and-crop technique, even if the upstream Provider has never named the defect. Provider adapters expose their own controllable temporal-context settings; their effective value follows `job override > saved provider+machine override > locally verified profile > provider default > unknown`. An unadapted Provider is explicitly `unsupported` or `unknown` for this product protection—not assumed fixed, not passed the LatentSync parameter, and never covered with a visual workaround.

## 8. Capability-aware Compute Router

R1 should evolve toward **Local-first Data + Hybrid Compute** without making remote compute mandatory.

The routing decision is separate from the Hybrid Asset Router:

- **Hybrid Asset Router** answers *what visual source/type should satisfy the scene?*
- **Compute Router / Execution Planner** answers *which provider/runtime/configuration should execute the requested capability?*

### Routing inputs

A compute decision may consider:

- capability type (`voice`, `talking`, future image/video generation);
- requested duration and continuity requirement;
- quality target;
- privacy/data-transfer policy;
- budget and known/unknown cost;
- latency target;
- current machine/runtime readiness;
- verified provider/configuration evidence;
- license/commercial-use constraints.

### Provider capability profile

Capabilities are scoped to a concrete configuration, not just a model name. A profile should be able to represent:

- provider + model/version;
- local vs remote execution;
- machine/runtime fingerprint relevant to inference;
- implemented/configured/available/verified state;
- observed pass/fail duration or other operating range;
- quality/continuity evidence level;
- expected/observed latency and resource use;
- cost information;
- license/commercial status;
- last verification time and provenance.

A result on one machine or provider configuration does not silently become a global default.

### Parameter precedence

Production configuration must have one explicit precedence order:

1. **per-job explicit override**;
2. **saved user override for this provider + machine/profile**;
3. **locally verified capability/profile values**;
4. **provider-known conservative defaults**;
5. **unknown**.

Do not convert `unknown` into a fabricated optimized value.

### Advanced Settings

When a provider/machine combination lacks enough empirical evidence, expose an **Advanced Settings** entry rather than pretending the automatic router knows the optimum.

Advanced Settings should:

- be provider-specific but surfaced through a common UI pattern;
- distinguish safe/common settings from expert/experimental settings;
- show current source of each value (`verified`, `provider default`, `user override`, `unknown`);
- warn when a value exceeds a verified local range or requires remote media transfer/cost;
- allow reset to automatic/verified defaults;
- save overrides at the narrowest appropriate scope (job, project, or provider+machine profile);
- never bypass consent, budget, license, provenance or hard runtime-safety checks.

Successful user runs may later become **local evidence** after QA/human acceptance; they do not automatically become global product defaults.

### Runtime routing

Do not build the product around a high-end-GPU-only route.

For compute-heavy Talking on low-spec PCs, an external/BYOK API may be the mature route if it passes the same quality gate and the user explicitly accepts media transfer and cost. Local inference remains available when provider-specific readiness, privacy, quality and runtime economics justify it.

Do not hard-code one universal VRAM threshold.

## 9. Hybrid Asset Router

Per scene prefer the lowest-cost adequate route.

### TALKING

1. original suitable Talking content when the original words actually match;
2. authorized lip-synced creator clip — local or remote Provider according to Compute Router readiness/cost/privacy;
3. authorized digital twin/avatar only as an explicit fallback;
4. explicit gap/capture option.

### B-roll

1. user/historical production-authorized clip;
2. optional low-cost capture;
3. local static/screenshot/typography;
4. stock if implemented;
5. AI image/video only if implemented, approved and budgeted.

Reuse penalty is a ranking input, not a reason to discard strong real material when alternatives are poor.

## 10. Account vs Market Intelligence

R1 may use/import the creator's own historical account/content data. Broad competitor/trend crawling and market prediction remain deferred. Future market signals must be evidence-backed and provider-neutral.

## 11. Mobile / remote operation

R1 architecture supports browser/mobile upload, shoot-task capture, review/status from a phone and private LAN/Tailscale-style access while the local node is online. Remote Windows desktop is not the primary UX.

## 12. Cost and capability discipline

Separate development-agent cost from product runtime/provider cost.

Runtime provider flow:

> estimate when known → reserve budget/idempotency ownership → execute → persist result/usage → reconcile actual/unknown cost

Retries count. Unknown price remains unknown. Remote media transfer must be explicit.

Capability experiments should optimize **information gained per run**, not number of runs.

## 13. Implementation model policy

Quality first, then lowest capable cost:

`Luna → Terra → Sol`

- **Luna**: isolated UI/CRUD/tests/docs/simple adapters/mechanical fixes.
- **Terra**: cross-module core logic, media/timeline, Provider integrations, capability profiles, Compute Router/Execution Planner, jobs/recovery, migrations and capability-boundary debugging.
- **Sol**: architecture/security/critical quality gate, or unresolved Terra failure with a concrete reproduction.

Task importance alone never justifies Sol; lower token price never justifies architecture-changing work by Luna.

## 14. Current gates

### Gate A — Foundation integrity
Provider accounting/idempotency, revision invalidation and real media/timeline boundaries remain green.

### Gate B — Voice
Maintain provider-neutral Voice jobs/QA. OmniVoice is the benchmark; select a commercial-safe production path later without changing Core contracts.

### Gate C — Talking
Maintain evidence-backed provider/configuration capability profiles. Current local LatentSync evidence is short-segment only; continuity remains unproven.

### Gate D — Integrated creator flow
`new topic → IP-aware copy → voice → Execution Planner/Compute Router → Talking/Hybrid Router → MasterNarration/timeline → Remotion render → cost/status/retry`.

### Gate E — U-Product
Two real new topics, 30–60 second exports, normal UI, recoverable failures, no manual per-scene audio cutting and no false capability claims.

## 15. Stop conditions

Proceed autonomously only inside the one active bounded work package in `STATUS.md`. Stop and update package state when:

- a requested U-Voice/U-Talking artifact is ready (`AWAITING_U_REVIEW`);
- the capability boundary is sufficiently resolved for a product decision;
- the bounded experiment reaches its declared fail/blocked/inconclusive state;
- a concrete runtime/external blocker prevents completion;
- the next step would change Provider, paid service, architecture, product scope, data boundary, or leave the declared search rule.

A failed or inconclusive bounded experiment is a valid completion. Do not keep expanding work merely to obtain a passing result.

## 16. Documentation system

Only six root documents are active controls: `START_HERE.md`, `CONTENT_OS_EXECUTION_SPEC.md`, `STATUS.md`, `DECISIONS.md`, `AGENTS.md`, `README.md`. Historical experiments belong to Git history or local evaluation evidence. Run `python scripts/check_docs.py` before handoff.
