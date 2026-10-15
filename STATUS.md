# Content OS — Recovery Status (STOP-LOSS)

Updated: 2026-10-15
Working branch: `recovery/stoploss-20261015`
Frozen source commit: `f32677d13fbba8a6dbdbac48030eaa5ea4f49907`

## Recovery decision — effective immediately on this branch

**STOP all feature implementation and unattended package chaining.** The former R1-W53 and other queued packages are suspended. Their historic authority does not authorize new Voice/Talking calls, render attempts, provider/runtime research, code changes, dependency installation or new architecture on this branch.

This is **not** a product PASS or a finding that the old code is safe to delete. The next work is evidence-based simplification, approved in steps.

## Current verified/reported truth

- Content OS is internal Alpha. Normal UI-to-publishable-video and second-topic repeatability have not been demonstrated.
- Prior user-approved media must be preserved: V55 (2026-09-25, reported 26.32s / 9 Talking pieces), V60 (32.560s MasterNarration) and V64 (32.400s TalkingRun). These descriptions come from retained project/review records; **the local media bytes, hashes, database bindings, consent and review artifacts must be rechecked before claiming a runnable golden regression**.
- Newer approximately 23.2s Voice and regressed Talking output are **not** permitted to overwrite those baselines.
- The 2026-10-15 master introduced broad production/evaluation/runtime changes while no new publishable full-video U-Product PASS was established. Contract, fixture and code checks cannot substitute for user-assessed real-media quality.
- The current implementation contains large potentially redundant native-loader/attestation, evaluation-script and multi-entry orchestration regions. These are **audit candidates only**, not yet proven unreachable/deletable.
- Existing privacy, authorization, model-license, consent, provider costs, asset provenance and real quality gates stay in effect. Recovery is not a bypass.

## One active work item — A0: preserve baseline and produce deletion-impact map

**State: AWAITING_U_REVIEW** — only the recovery-branch document freeze is authorized in this step. Do not auto-execute A0 before the owner approves the next read-only audit.

### A0 scope, after owner confirmation

1. Have the owner protect the local `content-os-data` media/DB/evidence externally, without placing personal media, secrets or model weights in Git.
2. Verify exact retained golden paths, hashes, IDs, human review and execution configuration; record missing artifacts honestly.
3. Trace from normal UI/API/worker/render entrypoints to modules. Classify candidate code as **KEEP / MERGE / ISOLATE / DELETE-CANDIDATE**, with real callers, test evidence, expected net simplification and rollback conditions.
4. Identify the minimum viable production flow and compare it with the last approved golden path; distinguish a code-level pass from an actual media/U-Product pass.
5. Return a **bounded review table** to the owner. No deletions or implementation changes in A0.

### Prohibited until a separately approved cleanup slice

- no model inference, regeneration, provider shopping or render;
- no Git history rewrite or destructive repository/file/DB actions;
- no additional native loader, attestation, defense framework, state machine or governance framework;
- no automatic next package when an audit ends;
- no relaxing rights, consent, license, privacy, billing or quality checks to get a green status.

## Gate to subsequent steps

After A0, owner explicitly accepts a per-module change list (files to keep/change/delete, impact, precise tests and rollback). Then implement **one bounded deletion/merge slice at a time**, compare golden outputs, report net code/dependency reduction and stop for approval again.

## Execution handoff

Read `AGENTS.md` recovery override first, then this STATUS and only the relevant code. The old `docs/implementation/ROADMAP.md` and R1-W53 instructions are retained as historical context **but are not execution authority during recovery**. If local media/DB or authorizations are unavailable, mark that fact `BLOCKED`; do not invent a new task to work around it.
