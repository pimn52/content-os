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

## Active work package — P1: ordinary retained-media entry / bounded stop

**State: BLOCKED** — the owner's 2026-10-16 instruction authorized this one integration slice and explicitly required stopping if multiple existing state machines need migration. That stop condition was reproduced before production edits. No next package is running.

- Objective: let the ordinary entry select current, authorized retained Voice/Talking using existing EvaluationComposition, eliminating only orchestration actually replaced; no media generation. Allowed seams were the existing composition/production service and API, ProductionPanel and directly affected tests/module documentation. After the stop, the only tracked change is this status; no production source or tests were added/deleted. Stable interfaces: provider-neutral Core, existing schemas/state machines, consent, source/privacy/license, review versions, scoped use, idempotency and budget. No new proof, Provider or authorization issuance.
- Acceptance: actual local DB/API must identify compatible exact media, retain current restrictions and create no inference/render work, followed by relevant tests and Web build. Full selection/consumption acceptance is **not met**; a passing preflight/test does not mean a working reuse workflow. Non-goals: publication, commercial promotion, new inference/render, fresh QA/subjective approvals, broad legacy migration, native-runtime research and cleanup of unrelated files. Exit: stop with minimal blocker, commit/push this branch for owner review; never merge master.
- Work class I, existing host-selected model/reasoning retained; exact host telemetry and subscription cost unavailable, not inferred from a document assignment. No agents/model switch. Escalation trigger: cross-contract migration rather than a bounded delegation. Evidence: actual SQLite online snapshot with query_only API reads and unchanged full-table fingerprint; separate synthetic tests. External calls limited to this branch's Git fetch/push. Budget: no inference/render; no further candidate-list/runtime investigation after its 40s read timeout. Expected manual reduction: none demonstrated because integration stopped.

### Actual reuse result and minimum blocker

| Retained media | Actual project/copy/review | Current consumer result |
|---|---|---|
| V55 + its 26.600s Master | Same project and exact current 141-character copy; approved nine-child 26.320s Run, policy1, source bytes unchanged | Ordinary preflight already reuses the old Master with Voice required=false/max_attempts=0, but selects no old TalkingRun for any of the five current scenes and proposes new Talking. It is blocked, not authorized regeneration. |
| V60 32.560s Master + V64 32.400s Run | Same historical project, approved ten-child policy1 Run; old copy 193 characters versus current draft 328 characters | Current-copy reuse is incompatible. Do not replace the draft or call the new copy equivalent. The original Master and Run also have no modern evaluation binding. |

- Both golden Masters lack the current evaluation QA claim/human-review record, Master use admission/binding and execution scope. Master API returns `voice_human_review_audio_unbound`. Both golden TalkingRuns have policy1, no planned origin and no EvaluationTalking operation/output scope; existing `require_admission_projection` requires the policy2 operation/result/review linkage. Existing CompositionSelection takes a Master **Run**, not an AudioAsset ID; substituting an AudioAsset ID correctly returns `composition_current_policy2_master_required` and is not a valid compatibility adapter.
- The 14 retained provider-use receipts concern other exact Voice/ASR/calibration executions, all in V55's project; none supplies a current exact golden Master/Composition consumer binding. Historic internal consent/quality approval is preserved, but current composition permission was **not verified** and commercial permission was not granted. Ordinary preflight also reports current license/use-admission blockers; V60's current draft additionally hits budget/capability blockers.
- Normal `ProductionPanel` already mounts evaluation Master/Talking/Composition panels; its general `ProductionRunService.start` still refuses internal execution, whereas EvaluationComposition consumes its own exact parent/operation records. A route/UI redirect alone cannot make either golden pair eligible. Retrofitting them into the present consumer would cross Voice QA/human review/Master-use and Talking operation/review contracts. No synthetic modern lineage, policy1-to-2 relabeling, receipt issuance or extra generation was attempted. No orchestration was actually replaced, so nothing was deleted under a false simplification claim.
- Minimum unresolved decision: a separately bounded way to consume **historical approved bytes with current internal-use permission**, preserving historical quality facts without reconstructing modern execution history. That decision is not made or implemented here. The owner-requested stop takes precedence over starting such a migration.

### Validation and handoff

- Six actual API preflight calls over a consistent real DB snapshot completed in about3s, query_only enforced. Full-table fingerprint unchanged; 233 Jobs,211 provider calls,2 ProductionRuns,10 evaluation Talking operations unchanged. V55/V64 assembled hashes and both golden Master WAV hashes/current retained QA/human approval rechecked. Zero model/render calls; the production DB/media were not mutated. Private evidence: `.tmp/stoploss-mainline-20261016/real-api-audit.json`, `current-master-consent-check.json` and the earlier verified preservation packet. No private database, text, media, weights or credentials are staged for Git.
- An initial sandbox TestClient stalled before entering business code; bounded stack evidence identified Windows asyncio socketpair setup. Normal-host execution resolved that transport issue. A separate full candidate-list GET exceeded40s while parsing existing runtime inventory; it was stopped, and is **not** a completed candidate/current-runtime test. The successful six-call audit excludes that slow list and does not claim current runtime readiness.
- Validation PASS: existing `test_evaluation_master.py`, `test_evaluation_composition.py`, `test_production_preflight.py`:43 passed,2 existing dependency deprecation warnings,516.16s; Web suite112 passed; `npm.cmd run build` passed; `python scripts/check_docs.py` and `git diff --check` passed. All backend cases are synthetic/provider-mocked evidence, separate from the six actual DB/API reads; no real model or video render. XML retained locally under `.tmp/stoploss-mainline-20261016/tests.xml`. Dependencies/licenses: unchanged. Production code net change:0. No new user operation has been delivered; this is the requested minimal blocker exit, not a product PASS or an implementation-completion claim.

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
