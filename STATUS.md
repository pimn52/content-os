# Content OS — Current Status

Updated: 2026-09-15 (Asia/Shanghai)

## Current truth

- The local media/runtime foundation is in place: persistent IP/draft revisions, provider accounting/idempotency, local media import and continuous clips, ASR/vision/index boundaries, ScenePlan/Hybrid Router, browser/mobile upload, MasterNarration/timeline, Remotion rendering, Jobs/recovery and backup checks.
- The project is still an **internal Alpha**. It has not passed the R1 product gate: `new topic → creator voice → creator Talking/lip-sync → 30–60s new video`.
- Voice and Talking remain separate quality gates. Automated ASR/container QA is necessary but does not prove creator likeness, natural pacing or visible lip-sync.
- **OmniVoice** remains the local non-commercial Voice benchmark. Short samples have been usable after QA, but the 17.44s take exposed weaker perceived likeness, compressed pacing and insufficient breathing. Its official pretrained weights remain CC-BY-NC.
- **MuseTalk 1.5** and **VideoReTalking** are rejected for the mature product path.
- **LatentSync 1.5** is an optional benchmark-only local Talking adapter. A short ordinary-material sample around 3s was acceptable to continue; the 17.44s result was not publishable because visible lip-sync increasingly lagged the narration. The final output reuses the original narration audio, so visual sync drift must be diagnosed separately from Voice quality.
- The current 6GB RTX 3060 Laptop GPU can run the bounded LatentSync benchmark only near its hardware limit and with an evaluation-only attention compatibility fallback. This machine is valid for capability probing, not proof of the minimum product hardware requirement.
- KeySync remains deferred to a later compatible machine. No new Talking provider is being added during the current capability-boundary package.

## Active work package — Gate C2: Talking capability-boundary search

**State: READY**  
**Primary implementation model: Terra**  
**Luna role: tests/docs/mechanical follow-up only**  
**Sol role: review only if the bounded evidence remains ambiguous after this package closes**

### Objective

Find the **highest practically usable continuous Talking duration** for the current LatentSync/runtime/hardware/material combination with the fewest repeated experiments. The goal is an evidence-backed product capability boundary, not a predetermined 8s/9s answer and not a mathematically exact threshold.

### Known bracket

- Lower bound: one short ordinary-material sample around **3s** was visually acceptable enough to continue.
- Upper bound: the **17.44s** sample failed publishability because visible lip-sync drift accumulated.

Treat this as an initial pass/fail bracket. Exact historical sample lengths remain evidence, not universal product constants.

### Search rule

Use an **adaptive interval search**:

1. Keep provider/runtime/source material/inference settings fixed unless a technical blocker makes that impossible.
2. Reuse the existing narration when testing visual Talking duration so Voice generation is not a new variable.
3. Choose the next duration near the midpoint of the current known pass/fail bracket, adjusted only to a nearby clean speech boundary.
4. Run the normal provider path and automated Talking QA, then request U-Talking review.
5. If the sample passes visual U-Talking, move the lower bound upward to that duration.
6. If it fails visual U-Talking, move the upper bound downward to that duration.
7. Repeat only while another sample is likely to change a product decision.

### Stop precision

Stop the visual boundary search when **any** of these is true:

- the pass/fail bracket is within about **2 seconds**;
- the next midpoint would not materially change Scene Planner/Router behavior or market fit;
- two nearby durations produce inconsistent human results, indicating material/sample variance is larger than the duration difference;
- runtime/hardware variability prevents a clean duration comparison;
- the user explicitly judges the current evidence sufficient for product routing.

Do not chase a 0.1s or 1-frame theoretical maximum. This is a product capability estimate.

### Human-review focus

For every candidate duration, U-Talking should judge:

- lip motion alignment from start to end;
- identity retention;
- mouth/teeth artifacts;
- original motion/gaze/background retention;
- visual-quality retention;
- whether the result is publishable apart from any already-known Voice-quality concern.

### Product-path confirmation

After the visual search establishes a useful approximate upper bound:

1. Generate a **fresh OmniVoice take** at a natural sentence/phrase boundary near that capability range; do not force speech speed merely to hit a numeric target.
2. Run existing automatic Voice QA.
3. Require U-Voice review for timbre similarity, naturalness, pacing and breathing.
4. If U-Voice fails, close as `VOICE_LIMITED` and move the next package to Voice duration/prosody; do not keep tuning LatentSync.
5. If U-Voice passes, run one combined Talking job near the verified visual range, then automated QA and U-Talking.
6. If the combined result passes, record the observed range as the **currently verified product capability**. It is not a permanent global maximum and does not require every Talking scene to use that duration.
7. If it fails, close as `PRODUCT_LIMITED` with the concrete cause.

## Mandatory package exit states

The package must end in exactly one of these states:

- `CAPABILITY_BOUNDARY_ESTABLISHED`
- `VOICE_LIMITED`
- `PRODUCT_LIMITED`
- `BLOCKED_RUNTIME`
- `INCONCLUSIVE_VARIANCE`
- `AWAITING_U_REVIEW`

`AWAITING_U_REVIEW` is not an invitation to keep working. Once a requested output/evidence artifact is ready, update this file to that state, list the exact artifact(s), and **stop** until the user gives the human-quality decision. After that decision, update the bracket or final state and continue only if the current bounded search rule still allows another informative experiment.

## Acceptance / evidence

Before handoff or closure:

- preserve the existing short passing evidence and 17.44s failed evidence;
- record tested duration(s), current pass/fail bracket, provider/runtime settings and output references in local evaluation evidence or Git history, not as a long run log here;
- run the relevant automated Voice/Talking QA;
- if code changes are genuinely needed, run the smallest relevant pytest set plus `python scripts/check_docs.py`; run the Web build only if Web code changed;
- do not add another provider, redesign Core contracts, or start the 30–60s two-topic U-Product gate in this package.

## Next work after closure

- `CAPABILITY_BOUNDARY_ESTABLISHED`: Gate D/E integration should use the observed range as routing evidence, while allowing shorter scenes whenever editorially better.
- `VOICE_LIMITED`: open a Voice duration/prosody package.
- `PRODUCT_LIMITED`: review whether shorter Talking, hybrid editing, another provider or remote compute best matches market needs.
- `INCONCLUSIVE_VARIANCE`: improve test material/benchmark design before more duration probing.
- `BLOCKED_RUNTIME`: preserve the reproduction and stop; do not convert a runtime failure into a quality conclusion.

## Product quality gate

A Talking provider/path is admitted only when all are true:

- accepts ordinary authorized creator footage;
- speaks genuinely new verified narration;
- preserves creator identity sufficiently for publication;
- lip sync is visibly acceptable across its claimed capability range;
- does not unnecessarily replace original body motion/gaze/background;
- output quality is acceptable in the normal 9:16 Content OS assembly;
- runtime/cost/license constraints are explicitly known;
- human U-Talking review says the result is publishable.

## Documentation rule

`STATUS.md` records current truth, the one active package, its explicit state and next work. Per-run filenames, timestamps, cache/download history and retired-provider experiment logs belong in Git history or local evaluation evidence.