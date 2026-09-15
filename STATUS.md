# Content OS — Current Status

Updated: 2026-09-15 (Asia/Shanghai)

## Current truth

- The local media/runtime foundation is in place: persistent IP/draft revisions, provider accounting/idempotency, local media import and continuous clips, ASR/vision/index boundaries, ScenePlan/Hybrid Router, browser/mobile upload, MasterNarration/timeline, Remotion rendering, Jobs/recovery and backup checks.
- The project is still an **internal Alpha**. It has not passed the R1 product gate: `new topic → creator voice → creator Talking/lip-sync → 30–60s new video`.
- Voice and Talking remain separate quality gates. Automated ASR/container QA is necessary but does not prove creator likeness, natural pacing or visible lip-sync.
- **OmniVoice** remains the local non-commercial Voice benchmark. Short samples have been usable after QA, but the 17.44s take exposed weaker perceived likeness, compressed pacing and insufficient breathing. Its official pretrained weights remain CC-BY-NC.
- **MuseTalk 1.5** and **VideoReTalking** are rejected for the mature product path.
- **LatentSync 1.5** is an optional benchmark-only local Talking adapter. A ~3s ordinary-material sample was acceptable to continue; the 17.44s result was not publishable because visible lip-sync increasingly lagged the narration. The final output reuses the original narration audio, so this visual drift must be diagnosed separately from Voice quality.
- The current 6GB RTX 3060 Laptop GPU can run the bounded LatentSync benchmark only near its hardware limit and with an evaluation-only attention compatibility fallback. This machine is valid for capability probing, not proof of the minimum product hardware requirement.
- KeySync remains deferred to a later compatible machine. No new Talking provider is being added during the current duration-boundary package.

## Active work package — Gate C2: Talking duration ceiling probe

**State: READY**  
**Primary implementation model: Terra**  
**Luna role: tests/docs/mechanical follow-up only**  
**Sol role: review only if the bounded evidence remains ambiguous after this package closes**

### Objective

Empirically locate the useful continuous Talking boundary around **9s / 8s** before considering shorter 6–7s or 3–5s scene durations. Do not pre-emptively hard-code a short-scene limit.

### Fixed controls

Use the same authorized ordinary creator source, LatentSync 1.5 runtime, checkpoint, 20 inference steps, guidance 1.5, seed, FFmpeg path and current compatibility fallback unless a run is technically impossible. Do not tune several parameters at once; duration is the variable under test.

### Step A — isolate LatentSync duration behavior

1. Reuse the existing 17.44s narration as the audio source so Voice generation is not a new variable.
2. Derive one **9.0s** contiguous test interval at a clean speech boundary and the matching authorized source-video interval.
3. Run the normal provider path and automated Talking QA.
4. Present the 9s output for explicit U-Talking review focused on:
   - whether lip motion stays aligned from start to end;
   - identity retention;
   - mouth/teeth artifacts;
   - original motion/gaze and visual-quality retention;
   - whether the visual result is publishable apart from any pre-existing Voice-quality concern.
5. If 9s passes visual U-Talking, **do not run 8s**. Record `VISUAL_PASS_9S` and move to Step B.
6. If 9s fails visible sync/quality, repeat the same procedure at **8.0s** only.
7. If 8s passes, record `VISUAL_PASS_8S`; the current observed visual boundary is between 8s and 9s.
8. If 8s also fails, record `VISUAL_FAIL_8S` and close this package. Do not automatically continue to 7s/6s; propose that as the next separate work package.

### Step B — test the actual product path at the highest visual passing duration

Only after Step A identifies 9s or 8s as visually acceptable:

1. Generate a **fresh OmniVoice take** whose natural sentence/phrase boundary targets that duration. Do not force unnaturally fast speech merely to hit an exact number.
2. Run existing automatic Voice QA.
3. Ask for explicit U-Voice judgment on timbre similarity, naturalness, pacing and breathing.
4. If U-Voice fails, record `VOICE_LIMITED_<duration>` and close the package; do not keep tuning LatentSync.
5. If U-Voice passes, run one LatentSync Talking job with that fresh take, then automated QA and U-Talking.
6. If the combined result passes, record `PRODUCT_PASS_<duration>` and close the package. The next package may use that duration as the **currently verified capability**, not as a permanent global maximum.
7. If the combined result fails despite the isolated visual test passing, record `PRODUCT_FAIL_<duration>` with the concrete reason and close the package.

## Mandatory package exit states

The package must end in exactly one of these states:

- `PRODUCT_PASS_9S`
- `PRODUCT_PASS_8S`
- `VISUAL_FAIL_8S`
- `VOICE_LIMITED_9S`
- `VOICE_LIMITED_8S`
- `PRODUCT_FAIL_9S`
- `PRODUCT_FAIL_8S`
- `BLOCKED_RUNTIME`
- `AWAITING_U_REVIEW`

`AWAITING_U_REVIEW` is not an invitation to keep working. Once the requested output/evidence is ready, update this file to that state, report the exact review artifact(s), and **stop** until the user gives the human quality decision. After that decision, update the final state and close the package.

## Acceptance / evidence

Before handoff or closure:

- preserve the existing 17.44s failed sample as evidence;
- record exact tested duration(s), provider/runtime settings and output references in local evaluation evidence or Git history, not as a long run log here;
- run the relevant automated Voice/Talking QA;
- if code changed, run the smallest relevant pytest set plus `python scripts/check_docs.py`; run the Web build only if Web code changed;
- do not add another provider, redesign Core contracts, or start the 30–60s two-topic U-Product gate in this package.

## Next work after closure

- If `PRODUCT_PASS_9S` or `PRODUCT_PASS_8S`: next package is Gate D/E integration using the verified duration as a current routing capability, while allowing shorter scenes when editorially useful.
- If `VISUAL_FAIL_8S`: next package may probe 7s/6s, but it must be opened explicitly; do not auto-descend.
- If `VOICE_LIMITED_*`: next package is Voice duration/prosody strategy, not more lip-sync tuning.
- If `PRODUCT_FAIL_*`: review the recorded failure reason before choosing shorter duration, another provider or remote compute.
- If `BLOCKED_RUNTIME`: preserve the reproduction and stop; do not hide a runtime failure as a quality result.

## Product quality gate

A Talking provider/path is admitted only when all are true:

- accepts ordinary authorized creator footage;
- speaks genuinely new verified narration;
- preserves creator identity sufficiently for publication;
- lip sync is visibly acceptable across the tested duration;
- does not unnecessarily replace original body motion/gaze/background;
- output quality is acceptable in the normal 9:16 Content OS assembly;
- runtime/cost/license constraints are explicitly known;
- human U-Talking review says the result is publishable.

## Documentation rule

`STATUS.md` records current truth, the one active package, its explicit state and next work. Per-run filenames, timestamps, cache/download history and retired-provider experiment logs belong in Git history or local evaluation evidence.