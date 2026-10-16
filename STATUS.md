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

## Active work package — P2: retained media consumption / authorization stop

**State: BLOCKED** — the owner's latest 2026-10-16 slice explicitly permits decoupling current media consumption from historical generation, but requires an immediate stop when current use permission is missing. This exit is an unverified-use-authorization blocker, **not** a requirement to manufacture policy2 history or migrate old reviews. No next task is running.

- Objective/allowed: unify retained-media consumption in existing `talking/admission.py`, `production_preflight.py`, `production_runs.py` and directly necessary consumers/tests; remove only superseded checks. Existing media/project/quality/rights/privacy/hash/copy/timing/budget stay authoritative. No new service, Provider, state machine or compatibility proof. No inference, Voice/Talking generation, render or live production DB mutation. Acceptance: exact real V55 preflight consumes the five compatible intervals without proposing generation, tests pass and complexity falls; otherwise stop at the smallest technical/authorization blocker, commit/push only this recovery branch and wait. PASS is not met.
- Work class I; existing host-selected model/reasoning retained, exact host telemetry/cost unavailable; no agents or model switch. Evidence is actual read-only DB/media plus focused assertions, separate from earlier synthetic tests. External calls: branch pull/push only. Budget: one bounded current-use/timing check, no candidate/runtime investigation or new experiment. Escalation/exit: missing current permission or cross-state-machine redesign. Manual work reduction: none delivered.

### Real V55 check

- Project and full current copy match. Existing `_unique_scene_speech_span` finds exactly one ordered mapping; `_matches_interval_copy` independently accepts each scene's contained transcript. V55 Master and Talking bytes match their retained SHA-256, Voice QA/human approval and whole-Run continuity approval remain positive. The old policy1 review is retained as valid historical quality evidence; no policy2 generation requirement is inferred from its age.

| Current scene | Exact Master speech interval / offset in reviewed full Run (ms) |
|---|---:|
| scene-1 | 0–4900 |
| scene-2 | 4900–9340 |
| scene-3 | 9340–14380 |
| scene-4 | 14380–21680 |
| scene-5 | 21680–26300 |

- These five speech intervals are contiguous inside the reviewed0–26300ms Run. The26.600s Master has a separate final300ms beyond that speech coverage; this is **not** proven Talking coverage or permission to truncate/extend the video. No assembly/render was attempted. Millisecond transcript mapping is not a rendered/frame-accurate audiovisual acceptance. V60/V64 still fail current-copy matching and all five current scene mappings; their real files/old quality approvals remain intact.

### Minimum authorization blocker and affected functions

- Current Voice/Talking profile consent still confirms the named internal subject authorization, and the old assets retain their internal/evaluation references. Those positive facts are **not** discarded because modern execution sidecars are absent. However, the retained current-use evidence found does not verify internal **selection/composition reuse of the two exact V55 media**: all three adopted Master-use reviews name the different23.2s Master; the single batch binds that Master's Run; there is no adopted composition-use record. Fourteen provider-use records cover other named Voice/ASR/calibration actions. The retained Voice licence assessments explicitly limit their recorded activity to one local clone/private audition; they do not establish this old pair's present composition use. This is an evidence/applicability gap, not a finding that consent was revoked or the publisher forbids all internal reuse.
- No particular new receipt schema or historical-generation proof is demanded. A verifiable applicable current permission is required on its actual terms; permissions bound to another media/input cannot be borrowed. The present instruction authorizes implementation conditional on current media-use permission, not an automatic replacement of that permission. Per the owner's strict stop, no production edits follow this finding.
- Affected seam: `talking.admission.talking_visual_blocker` currently checks retained identity/quality/copy but its nonempty authorization reference alone does not verify the intended current use; `production_preflight.build_production_preflight` discovers the old Master yet proposes five new Talking actions; `ProductionRunService.start`/`ExecutionScopeService.prepare` currently gate internal execution through generation scope. These are the future bounded decoupling locations, not authority to rewrite them before the missing use evidence is supplied. No extra architecture task was opened.

### Actual regression / handoff

- Real DB opened with SQLite `mode=ro` and `PRAGMA query_only=ON`; full-table fingerprint before/after identical.233 Jobs,211 provider-call records,2 ProductionRuns unchanged. Thirteen focused real-record assertions pass:5 exact V55 scene/copy intervals,5 incompatible V60/V64 scene refusals,2 media hash groups and1 no-mutation/no-model/no-render check. Private evidence: `.tmp/stoploss-media-use-20261016/read-only-evidence.json`; no DB, text, media, weights or credentials are staged. No claim of completed reuse is made: the untouched preflight still proposes Talking generation, so the requested PASS acceptance fails.
- Only `STATUS.md` changes; production code/test changes and deletions:0. Dependencies/licenses unchanged. No unrelated test/build batch is started after the authorization stop; the earlier43 backend/112 Web/build results remain historical and are not represented as new implementation evidence. `python scripts/check_docs.py` and `git diff --check` pass. `master` remains at protected baseline `f32677d13fbba8a6dbdbac48030eaa5ea4f49907`. Stop and wait for owner review.

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
