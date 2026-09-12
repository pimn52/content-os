# Content OS R1 — Execution Specification

Updated: 2026-09-12 (Asia/Shanghai)

This is the **single authoritative product/execution contract** for R1. Read `START_HERE.md` for navigation and `STATUS.md` for current progress.

## 1. Product direction

Long-term loop:

> **Know what to create → Create it as you → Learn what works**

Content OS is Local-first / BYOK infrastructure for an individual creator or small creator workflow. It is not a generic AI video generator and R1 is not a broad market-intelligence SaaS.

R1 focuses on `Create it as you` while preserving extension points for account intelligence, market signals and performance learning.

## 2. R1 product gate

R1 is not complete until an explicitly authorized creator can:

1. initialize one persistent IP profile from confirmed information and real materials;
2. enter a new topic and obtain editable copy that uses the current IP context/evidence;
3. generate **new speech in the creator's authorized cloned voice**;
4. generate at least one **new Talking/lip-sync segment of that creator speaking new words**;
5. combine that with reusable real clips, optional capture, typography/static material and subtitles;
6. export a 30–60 second vertical video through the normal UI;
7. repeat the flow without re-recording complete narration or manually cutting all media again.

Source-led recuts, imported finished narration, old mouth motion, generic TTS or generic avatars may remain explicit fallback paths but **do not satisfy this gate**.

Human judgment is required for creator voice likeness/naturalness and Talking likeness/naturalness.

## 3. Product invariants

- **Local-first, not desktop-only.** The home computer can be the processing/storage node; browser/mobile can capture, review and control while it is online.
- **BYOK / replaceable providers.** Core contracts do not depend on one model/vendor.
- **Real user media first.** Use existing creator assets before generated media when quality is adequate.
- **Continuous clips are playback assets.** Keyframes exist for understanding/search, not as the default playback unit.
- **Upload once.** Derive audio, transcript, keyframes and metadata automatically when capabilities exist.
- **Capture is a low-cost option.** Missing material should produce an optional shoot list before expensive generation where appropriate.
- **No hidden downgrade.** Unavailable Voice/Talking/market capabilities are shown as unavailable, not silently replaced and declared complete.
- **Consent and rights are explicit.** Voice/face generation only uses authorized references.
- **Unknown cost is not zero.** Paid/runtime calls use persistent budget/idempotency/usage accounting.

## 4. Current architecture to preserve

Keep the existing:

- FastAPI local API;
- SQLite persistence;
- provider-neutral domain contracts;
- idempotent Job/Worker model with retry/recovery;
- local media import, ffprobe/FFmpeg, segmentation, extraction and ASR/vision/index boundaries;
- IP profile and revisioned project/draft state;
- ScenePlan and Hybrid Asset Router;
- `MasterNarration` / timeline assembly boundary;
- Remotion + FFmpeg render path;
- browser/mobile upload and private-network access boundary;
- cost/budget/provider-call ledger;
- backup/restore and dependency inventory.

R1 does **not** require Redis, Celery, n8n, Kubernetes or a microservice rewrite.

## 5. Development evidence modes

Keep these separate:

| Mode | Purpose | May prove |
|---|---|---|
| fixture | deterministic regression | contracts, state, encoding, orchestration |
| assisted-test | real samples analyzed with an authorized development tool/model | quality for those samples; not independent runtime |
| runtime | application executes configured local/BYOK provider itself | repeatable product capability |

Do not call fixture success “AI understanding”, and do not call assisted-test success an independently runnable product.

## 6. Voice strategy

`VoiceProvider` remains a replaceable capability layer.

### 6.1 OmniVoice

OmniVoice is approved for a **local non-commercial evaluation provider / benchmark** because it is a strong technical fit:

- zero-shot voice cloning;
- short reference samples;
- 600+ languages;
- reusable clone prompt;
- local Python API;
- pronunciation/expressive controls.

License boundary:

- repository/source code: Apache-2.0;
- official pretrained weights: currently CC-BY-NC due to upstream training-data constraints.

Therefore R1 may evaluate OmniVoice locally, but Content OS must **not**:

- bundle official OmniVoice weights in a commercial release;
- advertise those weights as a commercial-safe default;
- store OmniVoice-specific state in core contracts;
- require OmniVoice for the minimum installation.

Install it only as an optional provider/worker dependency.

When Content OS already has an authorized clean reference clip and transcript, reuse them rather than redundantly invoking another ASR pass.

### 6.2 Commercial-safe paths

Maintain a schema-compatible path for:

- a commercially usable local voice provider (e.g. Chatterbox candidate, subject to release-time source/model license verification);
- a BYOK cloud provider for machines without suitable local inference or when local quality is insufficient.

No provider becomes “commercial-safe” solely because its source repository uses a permissive license; model-weight and dependency licenses must also pass the dependency inventory/release review.

### 6.3 Voice QA gate

Generated narration cannot flow directly to final render without QA. At minimum record/check:

- copy coverage via ASR/alignment or equivalent;
- missing/duplicated sentence detection;
- duration and long-silence sanity;
- playable/non-clipped output;
- provider/model/version and reference provenance;
- retry/fallback outcome.

Naturalness and likeness remain a U-Voice human gate.

Scene-based generation/retry is preferred over regenerating a complete long narration when it improves reliability, but the final timeline may use one normalized `MasterNarration` track with aligned ranges.

## 7. Talking strategy

`TalkingHeadProvider` remains replaceable.

R1 must prove at least one real new creator Talking segment, but does not lock to one implementation.

Preferred evaluation order:

1. reuse a suitable authorized Talking clip as reference;
2. local lip-sync provider when the machine/reference quality supports it;
3. BYOK cloud Digital Twin / avatar as optional fallback if explicitly approved and budgeted.

Do not build the product around a high-end-GPU-only route. Runtime readiness must distinguish:

- implemented;
- configured;
- locally available;
- verified.

## 8. Hybrid Asset Router

Per scene, prefer the lowest-cost adequate route rather than maximum AI generation.

### TALKING

1. original suitable Talking content when the original words actually match;
2. authorized lip-synced creator clip;
3. authorized digital twin / avatar provider;
4. explicit fallback or gap.

### B-roll

1. user/historical production-authorized clip;
2. optional low-cost capture;
3. local static/screenshot/typography;
4. stock if implemented;
5. AI image/video only if implemented, approved and budgeted.

### Explainer

Prefer screenshot / typography / chart / real media before generated video when adequate.

Reuse penalty is a ranking input, not a reason to discard good real material when alternatives are poor.

## 9. Account vs Market intelligence

Keep these separate.

### Account Intelligence

R1 may use/import the creator's own historical account/content data to understand what that creator has published and how it performed. Account access remains read-only unless explicitly changed.

### Market Intelligence

Broad competitor/trend crawling and market prediction are not R1 requirements. Future market signals must be evidence-backed and provider-neutral; absent external evidence, label suggestions as creator/content recommendations rather than invented market trends.

## 10. Mobile / remote operation

R1 architecture should support:

- browser/mobile upload;
- shoot-task capture;
- review/approval/status from a phone;
- private LAN/Tailscale-style access while the local node is online.

Do not make remote Windows desktop the primary UX. Do not automatically open firewall/router access.

## 11. Cost and capability discipline

Separate **development-agent cost** from **product runtime/provider cost**.

Runtime provider flow:

> estimate (when known) → reserve budget/idempotency ownership → execute → persist result/usage → reconcile actual/unknown cost.

Retries count. Unknown price remains unknown.

Capability UI/status must distinguish `not implemented`, `not configured`, `locally unavailable`, `verification failed`, and `verified`.

## 12. Implementation model policy

Quality first, then lowest capable cost:

`Luna → Terra → Sol`

- **Luna**: isolated UI/CRUD/tests/docs/simple adapters/mechanical fixes.
- **Terra**: cross-module core logic, media/timeline, Provider integrations, planner/router, jobs/recovery, migrations.
- **Sol**: architecture/security/critical quality gate, or unresolved Terra failure with a concrete reproduction.

Task importance alone never justifies Sol. Lower token price never justifies assigning architecture-changing work to Luna.

Detailed constraints are in `AGENTS.md`.

## 13. Current dependency order

Do not restart old numbered plans. Continue from `STATUS.md` using these gates:

### Gate A — Foundation integrity

Provider accounting/idempotency, revision invalidation, real media/timeline boundaries remain green.

### Gate B — Voice evaluation

- establish explicitly authorized reference audio;
- implement provider-neutral Voice generation job;
- evaluate OmniVoice as non-commercial benchmark and at least one commercial-safe/fallback path where feasible;
- add Voice QA and human U-Voice comparison.

### Gate C — Talking evaluation

- establish authorized Talking reference;
- implement one real Talking provider path;
- verify new words, basic sync/playability and human likeness/naturalness.

### Gate D — Integrated creator flow

new topic → IP-aware copy → voice → Talking → Hybrid Router → MasterNarration/timeline → Remotion render → cost/status/retry.

### Gate E — U-Product

Two real new topics, 30–60 second exports, normal UI, recoverable failures, no manual per-scene audio cutting, and no false capability claims.

After Gate E, account/market/performance expansion may resume according to product evidence.

## 14. User input / stop conditions

Implementation should proceed autonomously for reversible engineering work inside this contract.

Pause only when required for:

- identity/voice/face consent or subjective likeness judgment;
- first paid provider use or budget increase;
- external account authorization/publishing action;
- irreversible user-data change;
- material product-scope change;
- a blocker that cannot be resolved from current repository/evidence.

Do not create a new strategy/freeze/review document for a normal blocker. Record current facts in `STATUS.md`, durable choices in `DECISIONS.md`, and continue the next ready dependency.

## 15. Documentation system

Only six root documents are active controls:

- `START_HERE.md` — navigation;
- `CONTENT_OS_EXECUTION_SPEC.md` — this contract;
- `STATUS.md` — current truth and next task;
- `DECISIONS.md` — durable decisions;
- `AGENTS.md` — implementation/model policy;
- `README.md` — user/contributor overview.

Old PRDs, development plans, freezes, handoffs and review reports are historical evidence available through Git history and must not compete with this hierarchy.

Run `python scripts/check_docs.py` before handoff. CI should enforce the same document rules.
