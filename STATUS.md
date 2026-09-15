# Content OS — Current Status

Updated: 2026-09-15 (Asia/Shanghai)

## Current truth

- Content OS remains an **internal Alpha**. R1 has not yet passed: `new topic → creator voice → creator Talking/lip-sync → hybrid 30–60s export`.
- Core media/runtime foundations are in place: local media/library, continuous Clips, IP/draft state, ScenePlan/Hybrid Asset Router, MasterNarration/timeline, Remotion/FFmpeg render, durable Jobs/recovery, provider accounting/budget and Voice/Talking QA boundaries.
- **Local-first Data + Hybrid Compute** is now the product architecture direction. Local-first governs creator data/control; heavy inference may route to local or explicitly approved remote compute according to evidence, privacy, cost and runtime capability.
- Voice/Talking remain provider-neutral. **Chatterbox, MuseTalk 1.5 and VideoReTalking are rejected** for the current R1 path.
- **OmniVoice** remains the local non-commercial Voice benchmark. Official pretrained weights are CC-BY-NC and are not a commercial-safe default.
- **LatentSync 1.5** remains an optional benchmark-only local Talking adapter. On the current RTX 3060 Laptop 6GB configuration, fresh-Voice product evidence includes:
  - **2.58s PASS** — U-Voice-approved new OmniVoice speech + LatentSync passed U-Talking;
  - **5.12s FAIL** — complete dynamic output, but lip-sync progressively drifted late in the segment;
  - an earlier 4.78s existing-take prefix was only “basically acceptable” and does not override the fresh-Voice product bracket.
- The LatentSync adapter now rejects a truncated raw model response before duration normalization; it can no longer turn a short raw output into a frozen padded false-positive.
- OmniVoice CPU execution on this machine is not a viable default interactive route (a short test ran about 29 minutes without output); the same benchmark on CUDA completed in roughly 10 seconds. GPU scheduling/resource contention therefore matters for future local execution.
- **Multi-short-segment continuity is unproven.** Individually passing 2–3s Talking segments must not be presented as evidence that a long continuous presenter can be created by simple concatenation.
- KeySync remains deferred to a compatible machine. No new provider is admitted merely because it can produce a demo.

## Active work package — Gate D4: capability profile + routing decision foundation

**State: READY**  
**Primary implementation model: Terra**  
**Luna role: focused tests/docs/UI follow-up only**  
**Sol role: not required unless a concrete architecture conflict remains after Terra implementation**

### Objective

Implement the smallest maintainable foundation that can turn real provider/machine evidence into a deterministic execution decision **without yet adding a remote Talking provider or a full Advanced Settings UI**.

This package establishes the boundary between:

1. Narrative / Scene intent;
2. Hybrid Asset Router (what visual/content route is needed);
3. Execution Planner / Compute Router (which provider/runtime/configuration should execute it).

### Required behavior

1. Add a provider-neutral **capability profile / execution evidence** representation scoped to a concrete provider/model/runtime/machine configuration.
2. Represent at least:
   - capability type;
   - local/remote mode;
   - readiness state (`implemented/configured/available/verified` or equivalent);
   - verified/observed operating bounds where known;
   - continuity/quality evidence status;
   - latency/resource/cost fields when known;
   - license/commercial status;
   - provenance / last verification information.
3. Add a deterministic routing/config resolution function with this precedence:
   - per-job explicit override;
   - saved user/provider+machine override;
   - locally verified profile value;
   - provider-known conservative default;
   - unknown.
4. `unknown` must remain explicit; do not fabricate an optimized value.
5. Existing LatentSync/OmniVoice evidence may be represented as seed/runtime evidence for the current development machine, but must not become global defaults.
6. Expose a machine-readable **routing reason / parameter provenance** so future UI can explain why a route/value was selected.
7. Preserve existing provider-neutral Voice/Talking contracts and durable Job/provider-call accounting.

### Safety / override rules

User overrides may tune quality/runtime parameters, but the resolver must not allow them to bypass:

- consent/identity authorization;
- budget/paid-call gates;
- license/commercial restrictions;
- provenance requirements;
- hard runtime-safety/integrity checks.

### Allowed modules

Prefer small new modules under existing `services/api/app/routing/` or an equally narrow capability/config package, plus focused domain/config types and tests.

Modify `runtime.py` only when needed to expose capability evidence/readiness; do not grow `main.py` with routing logic.

### Acceptance criteria

- deterministic tests cover all five precedence levels;
- unknown remains unknown when no evidence/default/override exists;
- one local profile can differ from another machine/profile without changing global provider defaults;
- a user override is reversible and narrowly scoped;
- hard safety/license/budget constraints can reject an override;
- LatentSync current-machine evidence can be represented without hard-coding LatentSync fields into universal Core contracts;
- routing result exposes reason/provenance;
- existing focused Voice/Talking/provider-accounting tests remain green;
- `python scripts/check_docs.py` passes.

### Non-goals

Do **not** in this package:

- implement or register a new remote/cloud Talking provider;
- build the full Advanced Settings UI;
- change Scene Planner semantics to fixed 2.58s/3s/4s scenes;
- claim multi-segment Talking continuity;
- run paid APIs;
- continue fine-grained duration probing;
- implement a general distributed scheduler/Celery/Redis system.

### Exit states

This package must close as exactly one of:

- `PASS` — capability profile + deterministic resolver + tests meet the criteria;
- `FAIL` — the bounded design cannot satisfy the criteria without redesigning preserved Core contracts;
- `BLOCKED` — a concrete dependency prevents implementation and the smallest clearing action is documented.

No human U-Voice/U-Talking review is required for this architecture package because it must not generate new subjective-quality evidence.

## Queued packages after D4

These are **not active simultaneously**. Open one only after D4 closes.

### D5 — Advanced Settings product surface

Build a common Advanced Settings entry that can render provider-specific parameter schemas, show value provenance (`verified / provider_default / user_override / unknown`), save at the narrowest useful scope, and reset to automatic/verified defaults. It must reuse the D4 resolver rather than introduce another configuration system.

### D6 — short-Talking continuity experiment

Use one natural MasterNarration and consecutive ordinary creator footage to compare:

- **bare continuity**: multiple independently generated short Talking segments directly adjacent;
- **product continuity**: the same content realized through Talking + B-roll/typography/reframe transitions.

This package answers whether current short local capability supports a publishable hybrid creator-video experience. It does not assume that multiple short passes equal a continuous presenter.

### Optional D3b — refine fresh-Voice duration bracket

Only if a tighter local routing boundary would materially change D5/D6 behavior, run **one** high-information natural fresh-Voice midpoint between 2.58s PASS and 5.12s FAIL. Do not chase sub-second precision.

### Future — GPU resource lease / scheduler

After routing behavior is stable, add a lightweight local GPU resource lease/queue if concurrent Voice/Talking jobs can contend for VRAM. Reuse the existing JobRunner; do not introduce infrastructure meant for distributed systems.

## Product routing principles

- Content intent is not rewritten to match a provider limitation. Execution adapts the intent.
- Existing real media remains cheapest/preferred when adequate.
- A constrained local provider may satisfy only short Talking hooks; a future capable local/remote provider may satisfy longer continuous requirements without changing ScenePlan semantics.
- Automatic routing uses verified evidence when available and conservative defaults otherwise.
- When evidence is insufficient, users must have an Advanced Settings path instead of receiving a falsely precise automatic choice.
- Successful user tuning may become local evidence after QA/human acceptance; it does not automatically become a global default.

## Documentation rule

`STATUS.md` contains current truth, exactly one active package and a short queued sequence. Detailed completed experiment logs remain in Git history or local evaluation evidence. Every package must close as `PASS`, `FAIL`, `BLOCKED`, or an explicitly required human-review state before another package becomes active.