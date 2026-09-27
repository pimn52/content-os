# Content OS — Current Status

Updated: 2026-09-27

## Current truth

- Content OS remains an internal Alpha. The first normal 30–60s new-topic vertical export and second-topic repeatability gate have not passed.
- V60's exact 32.560s new-topic Master AudioAsset `c1346d02-d967-4475-b5f4-4bdf7933b2d3` passed independent full-copy QA and immutable six-dimension U-Voice; the script was assisted editor input, not product scene-planner output. V64 admitted its reviewed 32.400s TalkingRun; final VideoSpec/render remains under V65.
- The V50 26.600s Master and V54/V55 nine-child Talking preview passed their exact human gates; V56 admitted the preview-byte-identical 26.320s TalkingRun `b644df48-c20f-4e7e-8bf5-ca6057e5c3b7`. These are not the first 30–60s R1 export.
- R1 remains OmniVoice-first, with the existing authorized LatentSync Talking benchmark. Official OmniVoice weights and LatentSync benchmark model remain non-commercial evaluation material; commercial clearance is separate.
- Performance Plan semantics, generalized in-take pause control and automatic emphasis/rhythm mapping remain unproven. Exact asset-specific human passes do not establish those generalized capabilities.
- Historical V17–V54 per-run evidence and failed Jobs remain in Git history or local `content-os-data/evaluation-evidence/`; retrieve only a named record when a concrete question requires it.

## Recent completed/blocked evidence — V55–V59

- V55: nine exact V54 Talking child videos and the 26.320s continuity preview passed separate human gates; immutable continuity review `1c672913-66b4-4ea1-862f-6d86df7105bd` binds the approved series.
- V56: normal TalkingRun `b644df48-c20f-4e7e-8bf5-ca6057e5c3b7` and whole Clip were admitted. Its imported media is byte-identical to the human-approved 26.320s preview and accepted by VideoSpec. This is not a 30–60s R1 result.
- V57–V59: an assisted new-topic draft entered normal APIs; five initial Voice Spans passed, one homophone-adjacent QA conflict was preserved and independently attributed, then one bounded replacement passed. The six-Span Master passed copy QA but was 25.330s, below R1 length. Failed Jobs and diagnostic provenance remain in `content-os-data/evaluation-evidence/v57-assisted-master/`, `v58-copy-conflict/`, `v59-assisted-master/`.

## Completed package — V60: One bounded new-topic length completion

**State: PASS**

**Primary implementation model: Terra**

### Objective

Extend the editable V57 new-topic draft by one substantive final sentence, reuse the six independently verified V59 child takes, generate one new authorized OmniVoice Span with unchanged reference/settings and fresh child QA, then compose and independently QA one new Master. Require a measured 30–60s result before any U-Voice request. This is still an assisted-input rehearsal; the scene-planner credential gap remains explicit.

### Allowed files / stable interfaces

- Allowed: one draft revision, one new Voice Job and child QA, one normal Master composition and master QA, source-bound evidence and STATUS. No re-generation of six verified children, no new Provider/settings/reference, Talking or render.
- Stable: failed V57 Job remains failed; V59 child QA/provenance and authorization; exact editorial copy and consent; normal Master/U-Voice gates.

### Acceptance / exit

- `AWAITING_U_REVIEW`: final 30–60s Master passes fresh exact-copy QA and is listed for U-Voice.
- `BLOCKED`: new Span or whole Master fails QA, provenance or duration; preserve evidence and stop without blind retry.
- `PASS`/`FAIL`: only after explicit U-Voice on the exact 30–60s asset.

Tests/build: real child/Master probe and independent QA, `python scripts/check_docs.py`, `git diff --check`.

V60 technical exit: **AWAITING_U_REVIEW**. The editable assisted draft was revised with one substantive 42-character ending. Its one new OmniVoice Span passed fresh independent Voice QA; the six V59 verified children were reused without regeneration. Normal composed Master AudioAsset `c1346d02-d967-4475-b5f4-4bdf7933b2d3`, WAV `content-os-data/assets/audio-originals/756492b4d61555654884276cde33f27f1cb57f1517181d712eb5b1977f21c480.wav`, SHA-256 `756492b4d61555654884276cde33f27f1cb57f1517181d712eb5b1977f21c480`, duration **32.560s**. Fresh whole-Master QA Job completed: playable, full copy coverage 1.0, zero missing/duplicate/substitution tokens, source bytes/hash valid, seven ordered source takes. Manifest: `content-os-data/evaluation-evidence/v60-length-completion/manifest.json`. No human Voice verdict, Talking or render yet. Ask U-Voice to judge the exact audio's creator likeness, naturalness, emphasis, pace, pauses, rhetorical rhythm and publishability, with the earlier pause caveat in mind. This assisted-copy result does not prove the missing product scene-planner gate.

V60 human exit: **PASS for this exact 32.560s Master.** Asked whether its likeness, naturalness, emphasis, pace, pauses, rhetorical rhythm and overall publishability pass, the user answered “可以通过”. Normal project-scoped immutable U-Voice review now records all six `pass` outcomes and approval on AudioAsset `c1346d02-d967-4475-b5f4-4bdf7933b2d3`, evidence reference `user:2026-09-25-v60-six-dimension-publishability-pass`. This does not validate product-generated copy, generalized performance control or commercial release.

## Blocked package — V61: New-topic source-forward Talking series

**State: BLOCKED**

**Primary implementation model: Terra**

### Objective

Use the normal project-scoped Talking slice-series API and existing consented LatentSync profile/source to generate a source-forward visual series for the exact U-Voice-approved V60 Master. Generate each planned short child once; independently probe and technically QA real output files. Stop at `AWAITING_U_REVIEW` with exact ordered child videos for U-Talking. Do not infer human visual approval from Voice approval or the earlier V54 series.

### Allowed files / stable interfaces

- Allowed: one immutable Talking series, its declared child Jobs and independent real-file QA, local review copies/evidence, STATUS. No Provider switch, source change, automatic retry, user-review fabrication, continuity approval, TalkingRun admission or render.
- Stable: V60 Master SHA/QA/U-Voice, authorized TalkingProfile `c47d4a97-2ff0-457e-bc41-8fcd744365e4`, source Clip `e3547414-ae03-5f1d-a2be-1b959fc75a58`, machine's 4.460s verified child bound and normal ProviderCall accounting.

### Acceptance / exit

- `AWAITING_U_REVIEW`: all ordered children complete and each passes independent real-media/timing Talking QA; list exact videos and stop for visual U-Talking.
- `BLOCKED`: admission, generation, QA, consent or runtime gate fails; retain evidence and stop without blind retries.
- `PASS`/`FAIL`: only after explicit review of the exact child series.

Tests/build: real child files/probes, focused Talking tests if code changes, `python scripts/check_docs.py`, `git diff --check`.

V61 exit: **BLOCKED at child 9/10**, not a U-Talking verdict. Immutable normal series `d4c236ae-2b89-42d3-89f0-9db8068689ec` used V60's approved Master and the existing consented source-forward reference. Children 0–8 each completed once, passed independent playable-video/audio and duration QA (0–20ms drift), and have hash-matched review copies in `content-os-data/evaluation-evidence/v61-talking-series/review/01.mp4`–`09.mp4`. Child 9 Job `16d88b58-e26a-4f1c-a5cb-07b84eba4104` failed on its first attempt after its accounted local ProviderCall `15f62028-9ef4-4d43-ae8a-33c3cd7776e9` with broad code `talking_invalid_request`; no output Asset exists. No child U-Talking review, continuity decision, TalkingRun or render was created. Original Job/Call and manifest remain preserved; do not silently retry or pretend nine children are the complete series.

## Completed package — V62: Attribute and recover one failed Talking child

**State: PASS**

**Primary implementation model: Terra**

### Objective

Identify whether V61's last-child failure comes from deterministic source/audio staging, the pinned LatentSync inference, raw-output coverage or normalization, without replaying inference first. If no deterministic input violation exists, add the smallest explicit series recovery seam that preserves the failed Job/ProviderCall and reuses all nine verified children, then permit at most one same-source/settings replacement child with independent QA. Stop at the exact ten-child U-Talking gate if completed.

### Allowed files / stable interfaces

- Allowed: source-bound read-only media diagnostics, narrow Talking application/repository recovery code and focused tests if required, one new normal accounted child Job only after attribution, evidence and STATUS.
- Stable: immutable V61 failed Job/Call, nine verified child Asset hashes, Master/U-Voice approval, source-forward timing, same provider/runtime/settings, independent technical QA and separate human/continuity gates.
- Non-goals: provider change, parameter sweep, regenerating nine passed children, manual DB rewrite, bypassing series provenance, automatic human approval, final render.

### Acceptance / exit

- `AWAITING_U_REVIEW`: a complete recovered ten-child series has independent real-file QA on every exact child, with original failure provenance preserved; list ordered videos and stop.
- `BLOCKED`: deterministic invalid input, unresolved runtime failure or replacement QA failure; preserve evidence and stop.
- `PASS`/`FAIL`: only after explicit human review of the exact ten videos.

Tests/build: local staging/probe checks, focused recovery/QA tests, `python scripts/check_docs.py`, `git diff --check`.

V62 technical result: the one replacement Job `e0fe4695-a5bb-4702-a765-61ec09f82af4` completed with generated Asset `6bbfcfb8-7fc8-4cb6-a25c-12c17ba9918a`, independently passed real-file/timing QA (0ms drift), and produced hash-matched review video `content-os-data/evaluation-evidence/v61-talking-series/review/10.mp4`. All ten ordered review copies `01.mp4`–`10.mp4` in that review directory match their generated Asset hashes; children 0–8 were not rerun. Original failed Job `16d88b58-e26a-4f1c-a5cb-07b84eba4104` and ProviderCall remain recorded, with recovery history on the same series. Exact asset mapping is in the V61/V62 manifests.

V62 human exit: **PASS for the exact ordered 10 review videos.** After receiving all ten links and being asked to judge likeness, lip-sync/naturalness, continuity and publishability, the user replied “通过”. This approves the presented child-video set only; a whole assembled preview has not yet been reviewed, and no continuity record, TalkingRun or final render exists.

## Completed package — V63: Persist U-Talking and prepare whole-run continuity review

**State: PASS**

**Primary implementation model: Terra**

### Objective

Bind the user's V62 approval to the ten exact hash-matched generated child Assets through normal immutable project-scoped U-Talking reviews. Then make one non-admitted review-only whole-run preview using the verified V60 Master as audio, independently probe its real media/timing, and stop for a separate whole-preview continuity/publishability judgment.

### Allowed files / stable interfaces

- Allowed: exact V61/V62 child hash checks, normal child human-review API records, existing TalkingRun assembly path solely for review preview, real-file probe, evidence and STATUS.
- Stable: ten child Jobs/Assets, V60 Master, original V61 failed Job/Call, no new Talking inference, no continuity approval before whole-preview review.
- Non-goals: provider/parameter change, generated child retry, TalkingRun admission, VideoSpec, final render, second topic or commercial clearance.

### Acceptance / exit

- `AWAITING_U_REVIEW`: ten immutable approved U-Talking records, one exact playable 30–60s review-only preview with independent timing probe and SHA, then stop for whole-preview judgment.
- `BLOCKED`: asset identity, human-review persistence, media assembly or probe fails; preserve evidence and stop.
- `PASS`/`FAIL`: only after explicit human judgment of the whole preview.

Tests/build: exact file hashes, normal API/readback, real preview probe, `python scripts/check_docs.py`, `git diff --check`.

V63 technical exit: all ten exact child Assets now carry immutable approved project-scoped U-Talking records with evidence `user:2026-09-27-v62-ordered-ten-talking-videos-pass`; the normal series readiness is `ready_for_human_continuity_review`. One **review-only**, non-admitted assembled preview is `content-os-data/evaluation-evidence/v63-talking-continuity/review-only.mp4`, SHA-256 `4e5f49859a4cc441b09332791a4be658cbabeb5c6a770f3dbcd1e226a06bf3b8`, duration 32.400s, independently probed with video and Master-audio streams. Evidence: `content-os-data/evaluation-evidence/v63-talking-continuity/manifest.json`. Stop for human judgment on the **whole assembled preview's** joins, visual continuity and publishability. No continuity decision, TalkingRun or final render has been created.

V63 human exit: **PASS for the exact 32.400s preview.** Asked whether its joins, visual continuity and overall publishability all pass, the user replied “均通过”. This is an asset-specific whole-preview judgment, not proof of a new final VideoSpec/render or of general Talking reliability.

## Completed package — V64: Admit the reviewed 32.4s TalkingRun

**State: PASS**

**Primary implementation model: Terra**

### Objective

Persist the exact V63 human continuity approval through the normal project API; admit one TalkingRun from that reviewed series; verify the admitted whole media is byte-identical to the approved preview, has a usable full-duration Clip, and is eligible for normal VideoSpec routing. Then hand off to the queued 30–60s VideoSpec/render package.

### Allowed files / stable interfaces

- Allowed: exact preview hash/readiness checks, immutable continuity review API, normal TalkingRun admission, independent real-file/provenance/VideoSpec eligibility verification, evidence and STATUS.
- Stable: ten approved child Assets, V60 verified/U-Voice-approved Master, V63 reviewed preview SHA, existing authorized source, no new generation or editorial copy change.
- Non-goals: infer final-video U-Product approval, new Voice/Talking generation, provider change, second-topic repeatability or commercial license clearance.

### Acceptance / exit

- `PASS`: immutable continuity review, TalkingRun, whole Asset/Clip and source-bound VideoSpec eligibility all verified; start the queued final-render package.
- `BLOCKED`: admission, byte identity, media or routing gate fails; preserve evidence and stop.
- `FAIL`: only on explicit human rejection of this exact result.

Tests/build: normal API/readback, real file SHA/probe, VideoSpec stored-clip test, `python scripts/check_docs.py`, `git diff --check`.

V64 exit: immutable approved continuity review `5995e529-ecc5-4a7c-8147-afe20ed5ec92` now binds the exact ten-child series. Normal TalkingRun `d6d03f20-ccd0-4d4b-8f35-9a47f51878e6` produced whole Asset `2c98b774-f109-4377-a491-95a948d9b0ee` and Clip `f60981ff-5c88-4f8a-96e5-990b61d3de51`. Its 32.400s playable media SHA-256 `4e5f49859a4cc441b09332791a4be658cbabeb5c6a770f3dbcd1e226a06bf3b8` is byte-identical to the approved preview, and the normal VideoSpec stored-clip gate accepts it. Manifest: `content-os-data/evaluation-evidence/v64-talking-run/manifest.json`. No final render or U-Product verdict yet.

## Blocked package — V65: First assisted 32s new-topic vertical render

**State: BLOCKED**

**Primary implementation model: Terra**

### Objective

Use the exact V60 Master and V64 admitted TalkingRun to assemble one normal 30–60s vertical VideoSpec with a small assisted editorial scene map and readable typography where appropriate, then render once, independently probe the MP4 and stop for U-Product. Existing authorized real-library video samples were inspected but are semantically unrelated to this long-interview editing topic; do not use them as misleading B-roll.

### Allowed files / stable interfaces

- Allowed: deterministic source-bound assisted scene selections using persisted Master timestamps, interval Clips on the admitted TalkingRun, normal VideoSpec/render APIs, local render output, technical QA, evidence and STATUS.
- Stable: exact approved Master/TalkingRun, no new Voice/Talking calls, no revision of script, no false claim of product scene-planner completion or media relevance.
- Non-goals: new recording, unrelated real footage, provider change, publication, second-topic repeatability or commercial release.

### Acceptance / exit

- `AWAITING_U_REVIEW`: one 30–60s playable vertical MP4 with correct Master audio/timing, admitted TalkingRun plus typography, readable captions and exact manifest; stop for final-video U-Product.
- `BLOCKED`: assembly/render/media QA fails; preserve evidence and stop.
- `PASS`/`FAIL`: only after explicit review of the exact final video.

Tests/build: normal VideoSpec/render API, real MP4 probe, duration/orientation/audio QA, `python scripts/check_docs.py`, `git diff --check`.

V65 exit: **BLOCKED at visual QA, before U-Product.** One normal render produced playable vertical MP4 `content-os-data/renders/d45f42fb-06eb-4ec5-bded-66116ea6a872/a7018507-730a-4dc9-923e-454269b2c68e.mp4`, 32.619s, 1080×1920 with audio, SHA-256 `3855a8dbd55c44edd29e81f2f5b1622549b30f89ed85292054206db5bd1abb17`. The assembled Talking source retains unrelated burned-in source subtitles (e.g. “我去年暑假”) while new Master captions render separately; typography sections also show full paragraph and timed captions together. This conflicts with current copy and publishability, despite container/timing QA. Frame evidence and exact VideoSpec are in `content-os-data/evaluation-evidence/v65-assisted-render/`; no second render or U-Product review occurred. V60 Master, V64 TalkingRun and V63 human approvals remain valid. The next bounded repair must preflight subtitle treatment and avoid conflicting text before spending another render, without claiming this V65 output passed.

## Active work package — V66: Subtitle-safe assisted render

**State: FAIL**

**Primary implementation model: Terra**

### Objective

Reuse the exact V65 Master, admitted TalkingRun, scene timing and visual route. On the two Talking scenes, apply only the existing evidence-bearing vertical crop so unrelated burned-in source subtitles are not visible; on the two typography scenes, replace the redundant full-paragraph display with short editorial titles while keeping actual timed narration captions. Preflight representative source frames and VideoSpec semantics before one fresh normal render, then visually and technically QA the exact MP4.

### Allowed files / stable interfaces

- Allowed: existing VideoSpec `vertical_reframe_mode` / `source_bottom_crop_ratio` / evidence reference and scene display caption fields, exact V65 input manifest, diagnostic frame images, one normal render, evidence and STATUS.
- Stable: original V65 render and failure evidence, V60 Master, V64 TalkingRun, four scene intervals, actual transcript captions, no regenerated media or changed voice copy.
- Non-goals: new renderer architecture, new Provider, media re-recording, unrelated B-roll, publication, second-topic repeatability.

### Acceptance / exit

- `AWAITING_U_REVIEW`: one 30–60s vertical MP4 passes real-media and visual subtitle QA; list its exact hash/path and stop for U-Product.
- `BLOCKED`: preflight, render or visual QA fails; preserve all evidence and stop without another render.
- `PASS`/`FAIL`: only after explicit U-Product on that exact output.

Tests/build: VideoSpec validation, real MP4 probe, visual frame checks, focused VideoSpec/render tests, `python scripts/check_docs.py`, `git diff --check`.

V66 technical exit: **AWAITING_U_REVIEW**. One normal render produced `content-os-data/renders/d45f42fb-06eb-4ec5-bded-66116ea6a872/7b59bcf8-35a7-4567-9c02-3507c40992fb.mp4`, SHA-256 `da8f33ec775d26127144cc96e44a7a4d6dda377cd129b04e423af246863c2a8d`, duration 32.619s, 1080×1920 with audio. Relative to V65, only the two Talking scenes' explicit evidence-backed 0.15 bottom crop/center reframe and the two typography scene display titles changed; Master audio, exact copy, four intervals, authorized TalkingRun and timed captions are unchanged. Source and final-frame checks at 3, 6, 12, 16, 21, 24 and 31s show the original subtitle conflict removed, face visible and no full-paragraph/line-caption duplication. Manifest and frame evidence: `content-os-data/evaluation-evidence/v66-subtitle-safe-render/`. This sampled visual QA does not establish whole-video publishability; stop for U-Product on the exact MP4. The earlier V65 failed render remains preserved.

V66 human exit: **U-Product FAIL for that exact MP4.** The user identified frequent face/head departures from the fixed vertical crop as the main problem, plain text-only non-talking cards as a secondary problem, and the overall edit as too rough. Sampled frames were insufficient to establish face-safe framing over moving footage. The failure is in final visual treatment/editorial assembly, not a revocation of V60 Voice or V63 TalkingRun approval. The exact review and SHA are retained in the V66 manifest; no new render was started after this verdict.

## Next-package boundary

Next ready package: assess the full TalkingRun for face-safe framing across time and compare bounded subtitle-safe treatments that preserve the face (for example an explicit source subtitle mask/full-frame composition or evidence-backed motion-aware crop). Replace plain text cards with a meaningful visual treatment or relevant authorized media; do not insert unrelated footage. Preflight the complete visual plan before one new render. After U-Product, address missing product-generated ScenePlan and relevant real-media route, then second-topic repeatability. An assisted sample cannot substitute for those R1 gates.

## Current blockers

- R1 full 30–60s new-topic export and second-topic repeatability are unverified.
- OmniVoice official pretrained weights remain non-commercial evaluation material; commercial release clearance is separate.
- Generalized in-take pause/semantic emphasis control remains unproven. Exact V50 human approval is asset-specific.

## Documentation rule

STATUS holds current truth, one active package, blockers and short queue. Historical evidence is in Git and local evaluation records; current product behavior belongs in module specs.
