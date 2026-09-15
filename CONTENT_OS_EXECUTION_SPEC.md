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

- **Local-first, not desktop-only and not local-inference-only.** Creator media/library/control remain local-first; heavy model inference may use an explicitly approved replaceable remote provider.
- **BYOK / replaceable providers.** Core contracts do not depend on one model/vendor.
- **Real user media first.** Use existing creator assets before generated media when quality is adequate.
- **Continuous clips are playback assets.** Keyframes support understanding/search.
- **Upload once.** Derive audio, transcript, keyframes and metadata automatically when capabilities exist.
- **Capture is a low-cost option.** Missing material can produce a shoot list before expensive generation.
- **No hidden downgrade or cloud fallback.** Unavailable capability, remote data transfer and estimated/unknown cost must be visible.
- **Consent and rights are explicit.** Voice/face generation only uses authorized references.
- **Unknown cost is not zero.** Runtime calls use persistent budget/idempotency/usage accounting.

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
- LatentSync 1.5 remains a **benchmark-only optional local adapter**. A short ordinary-material sample was acceptable to continue, while the 17.44s result failed publishability because visible lip-sync drift accumulated and the associated Voice take was not naturally paced. The current 6GB laptop uses an evaluation-only compatibility fallback near its hardware limit. LatentSync is not a commercial-safe default and is not fully admitted for R1.
- KeySync remains deferred to a later compatible machine.

### Capability-boundary policy

Do not prematurely hard-code a Talking maximum such as 3–5s, 8s or 9s. Numeric examples are evidence points, not product requirements.

For a provider/runtime/material combination, locate the useful duration boundary with the fewest experiments:

1. maintain a known-pass lower bound and known-fail upper bound;
2. choose the next test near the midpoint of that interval, adjusted to a nearby natural speech boundary;
3. keep other variables fixed;
4. update the pass/fail bracket after explicit U-Talking review;
5. repeat only while another test is likely to change routing, UX or market-fit decisions.

Stop when the interval is sufficiently narrow for product use (roughly a couple of seconds is normally enough), results become sample-dependent/inconsistent, runtime noise prevents clean comparison, or further precision would not change the product decision.

Separate two questions:

1. **Visual Talking capacity** — reuse an existing narration and vary only duration to locate cumulative lip-sync/visual failure.
2. **Product capacity** — after a useful visual range is identified, generate a fresh Voice take at a natural phrase boundary and require both U-Voice and U-Talking to pass.

The output is an **observed capability range under stated conditions**, not a universal model limit. Scene Planner/Router may still prefer shorter segments for editorial quality even when a longer duration passes.

### Runtime routing

Do not build the product around a high-end-GPU-only route.

For compute-heavy Talking on low-spec PCs, an external/BYOK API may be the **default mature path** if it passes the same quality gate and the user explicitly accepts media transfer and cost. Local inference is optional when provider-specific readiness, privacy, quality and total runtime cost justify it.

Do not hard-code one universal VRAM threshold. Runtime readiness is provider-specific and must distinguish implemented / configured / locally available / verified.

## 8. Hybrid Asset Router

Per scene prefer the lowest-cost adequate route.

### TALKING

1. original suitable Talking content when the original words actually match;
2. authorized lip-synced creator clip — local or remote Provider according to readiness/cost/privacy;
3. authorized digital twin/avatar only as an explicit fallback;
4. explicit gap/capture option.

### B-roll

1. user/historical production-authorized clip;
2. optional low-cost capture;
3. local static/screenshot/typography;
4. stock if implemented;
5. AI image/video only if implemented, approved and budgeted.

Reuse penalty is a ranking input, not a reason to discard strong real material when alternatives are poor.

## 9. Account vs Market Intelligence

R1 may use/import the creator's own historical account/content data. Broad competitor/trend crawling and market prediction remain deferred. Future market signals must be evidence-backed and provider-neutral.

## 10. Mobile / remote operation

R1 architecture supports browser/mobile upload, shoot-task capture, review/status from a phone and private LAN/Tailscale-style access while the local node is online. Remote Windows desktop is not the primary UX.

## 11. Cost and capability discipline

Separate development-agent cost from product runtime/provider cost.

Runtime provider flow:

> estimate when known → reserve budget/idempotency ownership → execute → persist result/usage → reconcile actual/unknown cost

Retries count. Unknown price remains unknown. Remote media transfer must be explicit.

Capability experiments should optimize **information gained per run**, not number of runs. Do not repeat similar tests merely because a prior prompt mentioned a specific duration.

## 12. Implementation model policy

Quality first, then lowest capable cost:

`Luna → Terra → Sol`

- **Luna**: isolated UI/CRUD/tests/docs/simple adapters/mechanical fixes.
- **Terra**: cross-module core logic, media/timeline, Provider integrations, planner/router, jobs/recovery, migrations and capability-boundary debugging.
- **Sol**: architecture/security/critical quality gate, or unresolved Terra failure with a concrete reproduction.

Task importance alone never justifies Sol; lower token price never justifies architecture-changing work by Luna.

## 13. Current gates

### Gate A — Foundation integrity
Provider accounting/idempotency, revision invalidation and real media/timeline boundaries remain green.

### Gate B — Voice
Maintain provider-neutral Voice jobs/QA. OmniVoice is the benchmark; select a commercial-safe production path later without changing Core contracts.

### Gate C — Talking
The current package is an adaptive LatentSync capability-boundary search between the known short passing sample and the 17.44s failed sample. Admission still requires ordinary-material automated evidence plus human publishability review.

### Gate D — Integrated creator flow
`new topic → IP-aware copy → voice → Talking → Hybrid Router → MasterNarration/timeline → Remotion render → cost/status/retry`.

### Gate E — U-Product
Two real new topics, 30–60 second exports, normal UI, recoverable failures, no manual per-scene audio cutting and no false capability claims.

## 14. Stop conditions

Proceed autonomously only inside the one active bounded work package in `STATUS.md`. Stop and update package state when:

- a requested U-Voice/U-Talking artifact is ready (`AWAITING_U_REVIEW`);
- the capability boundary is sufficiently resolved for a product decision;
- the bounded experiment reaches its declared fail/blocked/inconclusive state;
- a concrete runtime/external blocker prevents completion;
- the next step would change Provider, paid service, architecture, product scope, data boundary, or leave the declared search rule.

A failed or inconclusive bounded experiment is a valid completion. Do not keep expanding work merely to obtain a passing result.

## 15. Documentation system

Only six root documents are active controls: `START_HERE.md`, `CONTENT_OS_EXECUTION_SPEC.md`, `STATUS.md`, `DECISIONS.md`, `AGENTS.md`, `README.md`. Historical experiments belong to Git history or local evaluation evidence. Run `python scripts/check_docs.py` before handoff.