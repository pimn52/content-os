# Execution Planner, Capability Profiles and Hybrid Compute

## Product responsibility

Translate product intent into an executable provider/runtime/configuration choice without leaking one model's limitations into the narrative or media model.

## Layering

```text
Narrative / Scene intent
        ↓
Hybrid Asset Router
what visual/content route is needed?
        ↓
Execution Planner / Compute Router
which provider/runtime/config executes it?
```

The two routers are deliberately separate.

## Local-first Data + Hybrid Compute

Local-first governs creator data ownership and control. It does **not** require every heavy model to execute locally.

```text
Content OS
├─ Local Core
│  ├─ Assets
│  ├─ IP / Project state
│  ├─ Evidence / QA
│  └─ Render / control
└─ Compute
   ├─ Local providers when suitable
   └─ Explicit approved remote/BYOK providers when needed
```

There is no hidden cloud fallback. Remote media transfer, cost and privacy implications must be explicit.

## Capability profile

Capability evidence belongs to a concrete:

> provider + model/version + runtime + machine/profile

A profile may record:

- capability type and local/remote mode;
- readiness;
- verified operating bounds;
- optional feature support;
- quality/continuity evidence;
- latency and resource observations;
- cost;
- license/commercial status;
- provenance and verification time.

Evidence from one machine does not silently become another machine's verified result.

## Configuration precedence

```text
per-job explicit override
> saved provider+machine/user override
> locally verified profile value
> provider conservative default
> unknown
```

Unknown remains unknown.

## Advanced Settings

Advanced Settings is the expert override surface over the same resolution system.

It should:

- expose provider-owned parameters through a common product pattern;
- show value provenance such as verified / provider default / user override / unknown;
- allow narrow-scope overrides and reset-to-auto;
- preserve hard consent, budget, license, provenance and runtime-integrity gates;
- never auto-promote a successful user tweak into a global default.

Narration performance intent is **not** an Advanced Setting. It is an
exact-copy-bound editorial plan for emphasis, pace, pauses and rhythm. A
provider schema must separately expose whether it can apply that plan; an
unknown feature stays unknown even when the provider has a numeric `speed`
parameter. Only an explicit adapter application receipt, followed by Voice QA
and U-Voice review, can establish a performance result.

Its derived Delivery Plan is likewise pre-execution structure, not a request
to create a duration, silence or provider-specific control. Execution may only
use it through an admitted adapter with explicit feature support.

## Provider selection maturity

Current product code has a real capability-profile/configuration resolver and Advanced Settings surface.

It is **not yet a full automatic Local↔Remote provider selector**. Remote Talking/Voice providers and cross-provider quality/cost/privacy selection remain future work.

## Local resource scheduling

Local GPU-heavy Voice/Talking work uses a bounded local lease so same-machine jobs do not silently compete for the same VRAM. This is a local scheduler guard, not a distributed cluster scheduler.

## Product boundaries

- No universal VRAM threshold.
- No provider-specific knobs in universal domain contracts.
- No license/consent/budget bypass through overrides.
- No high-end-GPU-only R1 architecture.
- No Redis/Celery/Kubernetes requirement for current R1.
