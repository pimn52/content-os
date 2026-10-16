# Content OS — Recovery Status

Updated: 2026-10-16
Branch: `recovery/stoploss-20261015`
Frozen `master` source: `f32677d13fbba8a6dbdbac48030eaa5ea4f49907`

## Current truth

- Content OS is internal Alpha. No normal UI-to-publishable 30–60s new-topic + second-topic repeatability PASS has been shown.
- The Windows operator reports that V55 (26.320s TalkingRun), V60 (32.560s Master) and V64 (32.400s TalkingRun) have been matched to persisted DB/reviews and SHA-256/ffprobe evidence; 24 associated media files passed checks. No new inference/render ran. Local audit reports, not GitHub source, carry these byte-level claims.
- Backup reported at `C:\Users\ASUS\Documents\ContentOS-stoploss-backup-20261016-0055`: 20.652 GiB archive, 233,852 per-file hash matches, 5,947 SQLite consistent snapshots, 3 golden recovery probes. The backup is **on the same C: drive**; 20 access-denied historical directories were excluded. This is sufficient to start **reversible Git-tracked source cleanup**, not to delete the original local media, databases, weights, environments or denied directories.
- Source/docs/tests are ~7.11 MiB; ~38.08 GiB of local occupied space is primarily data, test databases, model weights and multiple environments. Code reduction targets maintenance/correctness, not immediate GB savings.
- Normal source-backed Voice/Talking generation previously worked, but later evaluation/production/attestation layers have grown and fresh media quality regressed. No product quality claim comes from fixture tests alone.
- Existing model-use restrictions, consent, project/source identity, private files, cost/budget, QA and original golden evidence remain binding. The old R1-W53 authority is suspended on this branch.
- Do not merge to `master` automatically. The recovery branch is not a backup of untracked user assets.

## Active work package — C1: retire unused historical experiment scripts

**State: READY. Owner approved staged cleanup after verified local backup.**

**Objective:** make one real, reversible deletion-only source cleanup without affecting current runtime, golden-media reproducibility or permissions.

**Allowed deletion candidates:** `scripts/evaluate_v21*.py` through `scripts/evaluate_v54*.py`, plus narrowly associated `scripts/diagnose_v*.py` in the same old experiment range. These are **candidates**, not an automatic filename-based delete list.

**Must first prove:** for each selected script, enumerate source/test/build/CLI/docs callers using repo-wide `rg`, and check any dynamic script-name dispatch/config/readme references. If the script is used to reproduce V55/V60/V64 evidence, remains an active CI/CLI command, or has an unresolved caller, **keep it**. Historical source is preserved in frozen Git commit; do not migrate dead scripts into another active folder.

**Change cap:** at most 15 individually proven inactive files in this batch; `git rm` tracked source only. No library/DB/media/weight deletion, no refactor/replacement implementation, no changes to provider/worker/frontend/runtime/rights rules, no newly generated files outside an ordinary short result. Do not delete tests for still-supported functionality. A zero-deletion result is permitted when the call graph does not justify deletion.

**Validation:** before/after `git diff --stat`, `git diff --check`, `git status`, script entry references and the smallest relevant unit/import tests. Run `python scripts/check_docs.py`; if unrelated existing doc governance checks fail, identify exact baseline, do not open an unrelated repair task. Explicitly compare V55/V60/V64 reference paths/scripts and ensure no golden source/evidence was touched. State exact delete count and net lines removed, not generic 'cleanup PASS'.

**Exit:** `READY` → `RUNNING` → `PASS`, `BLOCKED` or `FAIL`; stop for owner review after one local committed cleanup change (push only this recovery branch). No next package, no model call, no render.

## Short queue (not execution authority)

- C2: remove redundant native/PE source-attestation implementation **only after** the retained production adapter has one compatible maintained-process path and rights/security constraints are explicitly mapped, tested and approved.
- C3: consolidate Evaluation/Production entrypoints into one normal user workflow; then slim API and web integration based on measured call graphs.
- C4: second-topic real-media product acceptance after approved restructuring.

## Recovery rule

Do not promote code/test/schema success to video quality PASS. Preserve golden output. Protect untracked private media separately. All later cleanup slices require owner review of changed files and rollback plan.
