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

## Active work package — P2: retained media consumption

**State: BLOCKED — technical execution boundary.** The owner's current authorization covers their own voice, likeness and original media for this project's local, noncommercial tests. The earlier missing-personal-use-permission conclusion is superseded. Absence of modern generation receipts is not evidence that the owner withheld permission, and is not a demand to manufacture policy2 history. No next package is running.

- Objective: ordinary product selection/preflight/VideoSpec consumption of the original approved V55 Master/TalkingRun, without new generation; delete replaced orchestration. Allowed: the existing admission/preflight/production consumers, directly required UI/tests and this status. Stable: existing asset, QA, project, hash, consent, license, timing and budget boundaries; no new state machine, service or proof protocol. Acceptance: real database-copy/API reuse with no Voice/Talking generation demand, valid VideoSpec, focused tests/Web build and an actual reduction in duplicate code. One local preview is conditional on those checks and applicable rights. Exit: PASS or the owner's explicit license/large-redesign stop. PASS is not achieved.
- Work class I; current host-selected model/reasoning retained, exact telemetry/cost unknown. No agents, model/provider calls, source-media changes or live DB writes. Existing real-media evidence is reused rather than audited again. External access: recovery branch Git synchronization only. Scope budget: one bounded seam inspection; stop before changing multiple execution/authority lifecycles. Expected user benefit: select the approved pair once instead of regenerating/rebinding; actual reduction delivered: zero.

### Retained facts and current permission

- The already verified V55 mapping remains 0–4900,4900–9340,9340–14380,14380–21680,21680–26300ms. Original26.600s Master and original TalkingRun stay intact; the final300ms is not Talking coverage. V60/V64 remain incompatible with their current copy. No new media/DB audit was run.
- Existing retained publisher evidence identifies OmniVoice weights as CC-BY-NC, LatentSync1.5 as Open Rail++-M, and separately identifies the tokenizer/encoder/face-model dependencies. The current owner's personal local test authorization does not extend those licenses or grant publication, distribution, commercial use or training. The prior one-call execution receipts are not borrowed as V55 consumption authority. No new third-party prohibition or missing publisher-license finding is asserted as this exit's cause.

### Exact technical stop

- `ProductionRunService.start` (`production_runs.py`) unconditionally routes internal purpose through `ExecutionScopeService.prepare`, then rejects with `evaluation_execution_not_integrated`. `prepare` and `ExecutionUseScope.valid_bindings` require provider-generation admissions even for a retained-only render. Changing only `talking_visual_blocker`/preflight would not make this operation executable.
- `require_evaluation_lane_closed` (`execution_scope.py`), called by both Worker dispatch and the ordinary render handler, again checks generation authority and rejects internal jobs. Ordinary renderer/media access structurally rejects restricted content; the private rendering exception is owned by `EvaluationCompositionService.require_current_job`/`require_media_identity`, not by a normal ProductionRun or caller-supplied internal-purpose label.
- Reusing that exception as implemented requires `EvaluationCompositionService._parent` to find an eligible policy2 EvaluationMaster, and `_context` to find one exact EvaluationTalkingOperation bound to each scene and exact Run interval. V55 has one historical whole Run spanning five scenes and no such operation chain. Its existing quality approval is not invalidated by this fact. Using a different Master's binding, inventing operations or disabling the Worker/private-media fences would not implement legitimate reuse.
- Therefore the cut reaches three existing lifecycles: production Run/Job admission, EvaluationMaster/Talking/Composition ownership, and restricted render/output access. There is no existing retained-only internal consumption entry that these callers can simply share. Closing all of them now would require broadening the agreed cut into an execution/authority redesign. Per the owner's explicit stop condition, no partial gate removal or new compatibility/proof layer is committed. This is an implementation-scope stop, not a claim that third-party terms forbid personal local reuse.

### Verification and handoff

- No production code, test, dependency or license changes; no redundant executable code deleted; executable net change0 lines. Only this existing STATUS entry is corrected and shortened. No new user operation is enabled.
- Prior database-copy API evidence and read-only mapping/hash evidence remain historical: `.tmp/stoploss-mainline-20261016/real-api-audit.json` and `.tmp/stoploss-media-use-20261016/read-only-evidence.json`. They show the unchanged failure, not a new passing regression. No new database/API regression, Web build or render was run after the technical stop; no valid ordinary-entry V55 VideoSpec or preview is claimed. Documentation/diff checks are the only new handoff checks.
- Original DB/media and `master` remain unchanged. No private material, weights or sensitive configuration are staged. Commit/push this recovery branch and stop for owner review; no next task is authorized by this status.

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
