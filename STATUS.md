# Content OS — Recovery: product-first structural reduction

Updated: 2026-10-16
Working branch: `recovery/stoploss-20261015`
Protected master baseline: `f32677d13fbba8a6dbdbac48030eaa5ea4f49907`

## Decision

**The former C1 "delete up to 15 old scripts" task is CANCELLED as a misplaced priority.** Disk cleanup and historical script pruning are ancillary; they do not by themselves repair degraded Voice/Talking, duplicated workflows or absence of a finished product.

**Goal:** recover one repeatable, ordinary-user short-video production path with output quality no worse than the approved media, while **removing** duplicated and nonessential implementation layers, not building new defensive/governance architecture.

## Evidence baseline

- Content OS is internal Alpha. No normal UI new-topic → publishable 30–60s video + second-topic repeatability PASS yet.
- Local operator report (2026-10-16): V55 26.320s TalkingRun, V60 32.560s Master and V64 32.400s TalkingRun were verified with SHA-256/ffprobe and review/DB linkage; 24 related media files verified. Current user reported significant quality regression in later re-generated ~23.2s Voice/Talking.
- Preserved local backup reportedly verified: `C:\Users\ASUS\Documents\ContentOS-stoploss-backup-20261016-0055` (20.652 GiB, 233,852 hashed files, 5,947 SQLite snapshots). It is on the **same C:** drive; 20 inaccessible historic directories were excluded. No destructive local-data cleanup is authorized.
- The existing `worker_cli.py` can construct the old local OmniVoice subprocess adapter; later native-attestation preparation can separately reject calls. UI mounts both production and evaluation Voice/Talking/composition flows. These overlaps require trace-and-collapse, not assumption that any large file is safe to delete.
- Basic privacy, consent, rights/model-license, no-unapproved upload/payment, project isolation, budget and end-product quality checks remain. Multiple layers of speculative native/host attestation, versioned certificates and duplicated review/Run types are **not** presumed necessary; each must justify its existence against actual execution risk and verified use.
- `master` is untouched. Git branch snapshots protect tracked files only.

## Active work package — P0: production-path rescue and architectural deletion plan

**State: READY FOR OWNER-GATED READ-ONLY INVESTIGATION. No implementation or inference authorized by this status change.**

**Question:** What is the *shortest lawful, repeatable implementation path* from ordinary UI to a decent finished video using **existing approved media**, and what code blocks that path?

Required evidence, not a new architecture document:

1. **Trace the user-visible call chain** from `ProductionPanel`/API through `ProductionRun`, `EvaluationMaster/Talking/Composition`, Worker, `VideoSpec`, Remotion/FFmpeg. Identify hard stops, redundant hops, branch splits and concrete file/function callers.
2. **Golden reuse:** determine exactly why V55/V60/V64 cannot simply be selected for the applicable same-copy project and consumed without new generation; identify the smallest supported integration change. Do not force a mismatched transcript/project merely to show a success.
3. **Delete-oriented architecture decision:** for each redundant evaluation-production/runtime-attestation layer, output KEEP / COLLAPSE / REPLACE / DELETE, dependent callers, behavior to preserve, estimated net lines/dependencies removed and regression tests. Prioritize removal of parallel production paths and heavy native-loader proof machinery over old scripts. Do not add a new adapter/state machine to protect obsolete abstractions.
4. **Minimum production acceptance:** one normal-UI, fully playable 30–60s sample with approved source Voice/Talking reused where exact identity matches, safe visual framing, no duplicate burned-in captions, U-Product quality verdict and observable manual steps; next repeat on a second topic. No fixture-only PASS.
5. **Hard stop:** return the concrete cut list and one first implementation slice for owner approval. No generation, render, model calls, file deletions, dependency installs, DB mutations or new architecture in this assessment.

## Implementation priorities (not automatic tasks)

1. **Collapse duplicate normal/evaluation product workflows** and bind already approved Voice/Talking/asset consumption to one ordinary production path.
2. **Remove speculative native/PE/source proof complexity** once a simpler maintained runtime path preserves actually necessary consent, license, privacy, source checks and budget controls.
3. **Remove orphaned code/tests/scripts/docs** exposed by those cuts; refactor oversized API/UI handlers only insofar as needed to reduce dependency crossings.
4. **Prove quality:** golden regression first; then one genuinely new-topic video; then a second-topic repeat. Measure user operations, human quality, wall time and inferencing cost.

## Stop-loss rules

- **Negative scope budget:** every structural change should eliminate more branches, modules, abstractions or maintenance work than it creates. Report net deletion and user-visible effect, not test counts.
- Don't spend a batch on documentation, scripts, disk caches or policy variants unless a concrete production-path blocker is removed.
- No renewed C4 native-loader study, schema/certificate escalation, new generic state machines or agent task recursion without the owner's explicit product-grounded approval.
- Avoid real-media re-generation when exact compatible approved assets exist; prevent quality regressions through side-by-side human comparison before replacing a golden output.
- Preserve authorizations and legitimate restrictions in their **smallest auditable form**; no blanket permission bypass for evaluation or commerce.
- Only merge to `master` after owner-approved cleanups and real-path verification. Each implementation slice ends for owner review; never automatically start the next slice.
