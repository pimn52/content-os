# Content OS

Content OS is a Local-first / BYOK content operating system for creators who want to turn reusable knowledge, identity, voice and real media into repeatable short-form content production.

Know what to create → Create it as you → Learn what works.

R1 focuses on the production loop:

new topic → IP-aware copy and visual plan → feasibility/cost preflight → necessary Voice/Talking/media production → timing-resolved edit → reviewed 30–60s export.

Start with START_HERE.md.

## Project map

- Product & R1 specification: CONTENT_OS_EXECUTION_SPEC.md
- Product module map: docs/product/README.md
- System architecture: docs/architecture/SYSTEM_ARCHITECTURE.md
- Current status / active task: STATUS.md
- Staged implementation contracts: docs/implementation/ROADMAP.md
- Durable decisions: DECISIONS.md
- Implementation contract: AGENTS.md

The project is an internal Alpha. Current product work is converting validated Voice/Talking execution evidence into a repeatable normal product flow.

## Repository map

    apps/web/                  React/Vite local workspace
    apps/renderer/             Remotion renderer
    services/api/app/          FastAPI/domain/application/providers/jobs/media/routing
    services/api/tests/        backend/integration tests
    contracts/                 exported JSON schemas/examples
    docs/product/              current product-module specifications
    docs/architecture/         stable technical architecture
    docs/                      operational/dependency supporting docs
    scripts/                   local operations and governance checks

## Windows development

From the repository root:

    py -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e ".[test]"
    .\scripts\start.ps1

The normal product website is http://127.0.0.1:8000/app/ while the API
service is running. The API serves the existing web build directly; a separate
Vite server is not required. Use `scripts/start.ps1 -NoWorker` to open the
workspace without processing queued jobs. Temporary browser fixture servers
are test-only and are not the normal product entry.

The evaluation Master panel reuses complete, reviewed narration through a
separate operator-adopted exact-use receipt. It requires a matching saved
copy/plan and retains current media authority checks. It does not dispatch
Talking or rendering. Code includes schema49; consult STATUS for live activation
and evidence before migrating an existing evaluation database. The Voice draft
panel can also select saved full-plan text; source selection does not grant a
new generation allowance or imply Master/Talking readiness.

Run tests:

    .\.venv\Scripts\python.exe -m pytest -q

Build the React workspace:

    npm --prefix apps\web install
    npm --prefix apps\web run build

Check documentation governance:

    .\.venv\Scripts\python.exe scripts\check_docs.py
    .\.venv\Scripts\python.exe -m pytest -q scripts/tests/test_check_docs.py

## Optional local providers

### BYOK scene planner controls

The normal ScenePlan API reads `CONTENT_OS_LLM_API_KEY`, `CONTENT_OS_LLM_BASE_URL`,
`CONTENT_OS_LLM_MODEL` and `CONTENT_OS_LLM_PROTOCOL` (`responses` by default).
Keep keys in the process environment, never in request bodies or committed files.
Optional controls are validated before dispatch:

- `CONTENT_OS_LLM_REASONING_EFFORT`: `low`, `high` or `max`, only with explicit `chat_completions`.
- `CONTENT_OS_LLM_MAX_OUTPUT_TOKENS`: positive integer; sent as `max_completion_tokens` for chat or `max_output_tokens` for Responses.
- `CONTENT_OS_LLM_TIMEOUT_SECONDS`: positive finite seconds, default 60; stdlib socket timeout, not a hard whole-request deadline.
- `CONTENT_OS_LLM_CHAT_OUTPUT_MODE`: `prompt` (default) or `json_schema` for explicit `chat_completions`; the latter sends strict `response_format` with the ScenePlan schema.

Chat prompts always include the complete existing ScenePlan JSON Schema, including
field names, types, source enums and milliseconds. `prompt` mode does not send a
vendor schema parameter. Opt into `json_schema` only when the selected provider/model
supports it (see [K3 structured output](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart)).
Unsupported schema mode fails without automatic downgrade/retry. Both modes retain
local ScenePlan validation; neither grants media suitability or production admission.

For a separately authorized bounded K3 experiment, explicitly selecting `low`,
an output ceiling (for example 4096 tokens) and a timeout avoids relying on vendor
defaults. This example is not an adequacy guarantee or an authorization to call.
The ceiling may include reasoning tokens; truncated output is rejected without
automatic continuation/retry. See [Kimi's parameter reference](https://platform.kimi.ai/docs/api/chat).
Unset optional controls omit vendor parameters; other providers may reject unsupported
fields, with no silent fallback or model switch.

The `app.providers.scene_planner` logger emits numeric/allowlisted outcome, stage,
HTTP status, request-byte count and elapsed milliseconds. Enable INFO logging to
include successful calls; failures use WARNING. `awaiting_headers` includes
connection/TLS and waiting for HTTP headers, not proof of model execution;
`reading_body` means headers arrived; injected transports without stage evidence
report `transport`. HTTP rejection and local validation are separate stages.
`validation_code` distinguishes response JSON/envelope, incomplete answer, missing
output text, output JSON, plan shape, scene shape and scene values. It reports only
fixed categories, not untrusted field names, values or validation tracebacks.
No keys, prompts, URLs, response/error text or reasoning text are logged.
This is non-streaming diagnostic support, not persisted usage/billing evidence.

Local provider runtimes are opt-in and do not change Core product semantics. Content OS does not install/download a runtime on startup. However, the legacy upstream inference loaders may resolve missing auxiliary models through caches/Hub/URLs: a local primary checkpoint does not prove offline or complete execution. Legacy and whole-production evaluation dispatch remains disabled. The separately configured maintained Voice draft module has its own exact evidence, accounting and downstream review boundary described below. Do not treat these benchmark commands as a hermetic execution or license guarantee.

### Local ASR

    $env:CONTENT_OS_ASR_PROVIDER = "local"
    $env:CONTENT_OS_ASR_LOCAL_MODEL = "small"
    $env:CONTENT_OS_ASR_DEVICE = "cpu"
    $env:CONTENT_OS_ASR_COMPUTE_TYPE = "int8"

### Maintained evaluation Talking

The normal evaluation Talking panel consumes an eligible saved Master and an
operator-adopted exact input/output-use combination. The generation receipt,
current verified capability/source/consent and budget remain independent gates.
Technical QA, exact scoped preview, six-dimensional review and TalkingRun
admission are explicit stages; this does not authorize render or publication.

Select the dedicated Worker policy explicitly:

```powershell
$env:CONTENT_OS_TALKING_EXECUTION_POLICY = "evaluation-talking-run-v1"
$env:CONTENT_OS_MAINTAINED_TALKING_CONFIG = "C:\path\to\talking-config.json"
$env:CONTENT_OS_MAINTAINED_TALKING_OPERATOR_KEY = "<configured operator key>"
python -m app.worker_cli --job-type generate_talking --job-type verify_talking --job-type prepare_talking_run_preview
```

Config version1 retains the schema9/origin4 single-venv recipe. Config version2
explicitly selects schema10/origin5 with `runtime_options.engine` pointing to
the separate Conda Python3.10 root and `invocation_policy` set to
`maintained-latentsync-dual-runtime-v1`. Its application environment/base remain
Python3.12; no Conda activation or automatic version fallback occurs. Parameters
also require `attention_policy: latentsync-retained-explicit-math-v1` and
`allocator: expandable_segments:True`. Both complete runtime inventories, the
role-specific origin, current host/device, resource/cache/tool identities and
the exact configuration-quality evidence must be adopted independently.
The selected vendor/resources/tools roots must fit the declared disjoint layout;
an existing legacy command or installation path alone does not satisfy it.
The owned application stages media and verified resources in private scratch;
the engine receives safe relative media operands and fixed no-site startup.
Configuration does not install/download packages or grant execution permission.
Invalid configuration does not fall back to legacy generation. Completed calls
recover saved output; failed/unknown calls require reconciliation. Current
implementation evidence is synthetic integration and isolated browser playback;
real Talking activation requires its own eligible runtime, inputs and grant.

### LatentSync benchmark Talking

    $env:CONTENT_OS_TALKING_PROVIDER = "latentsync"
    $env:CONTENT_OS_LATENTSYNC_PYTHON = "C:\path\to\runtime\python.exe"
    $env:CONTENT_OS_LATENTSYNC_REPO = "C:\path\to\LatentSync"
    $env:CONTENT_OS_LATENTSYNC_CHECKPOINT = "C:\path\to\latentsync_unet.pt"
    $env:CONTENT_OS_LATENTSYNC_FFMPEG = "C:\path\to\ffmpeg.exe"

### OmniVoice benchmark Voice

The values below are path-shaped examples, not a verified machine profile.
Choose device and provider parameters only through an explicit job override,
saved provider+machine profile, or locally verified capability evidence. In
particular, do not infer that `cuda` is usable merely because it appears below.

    $env:CONTENT_OS_VOICE_PROVIDER = "omnivoice"
    $env:CONTENT_OS_OMNIVOICE_PYTHON = "C:\path\to\python.exe"
    $env:CONTENT_OS_OMNIVOICE_MODEL = "C:\path\to\model-snapshot"
    $env:CONTENT_OS_OMNIVOICE_DEVICE = "cuda"
    $env:CONTENT_OS_VOICE_QA_ASR_MODEL = "C:\path\to\faster-whisper-snapshot"

### Maintained evaluation Voice drafts

The normal Voice draft panel supports bounded evaluation paths. `voice-draft-v1` requires an exact verified schema6 capability; explicitly selected `voice-calibration-v1` may use a configured candidate with unknown quality only with a separately adopted report-policy3 receipt bound to the exact request and one atomic Job claim. Both require an existing consented OmniVoice profile/reference, current license/runtime/source admission and an explicit finite call budget allowing unknown local cost. Missing prerequisites block submission; diagnostic import/PCM evidence is not generation permission. First-validation execution does not promote quality, and failed/unknown calls require reconciliation rather than automatic retry. QA, Talking and complete ProductionRun admission remain separate.

An operator can configure the owned Worker factory with `CONTENT_OS_VOICE_PROVIDER=omnivoice-maintained`, `CONTENT_OS_MAINTAINED_VOICE_CONFIG` pointing to a local JSON file, and `CONTENT_OS_MAINTAINED_VOICE_OPERATOR_KEY` supplied separately in the environment. The JSON has exactly `config_version: 1` and `runtime_options`. Required options are absolute `environment`, `base`, `model_root` paths, typed `origin_reference` (`reference` and `sha256`), exact `parameters`, `installation_id`, `runtime_label` and `machine_id`; optional options are `gpu_uuid`, `max_bytes` and `timeout_seconds`. Their values must come from current verified evidence; this is not a template license/configuration grant. JSON cannot supply the operator key, a command, request input or replay authority. Default benchmark selection is unchanged.

Independent evaluation QA is an explicit separate lane. Set `CONTENT_OS_VOICE_QA_POLICY=voice-evaluation-qa-v1`, `CONTENT_OS_MAINTAINED_ASR_CONFIG` and separately `CONTENT_OS_MAINTAINED_ASR_OPERATOR_KEY`, then select the `verify_voice` Worker type. ASR JSON uses `config_version: 1` and `runtime_options` with absolute `environment`, `base`, `model_root`, typed `origin_reference`, `installation_id`, `runtime_label`, `machine_id`, optional `max_bytes`/`timeout_seconds`; it cannot supply commands, audio/text, prompts or an operator key. Schema7/v1 fixes CPU/int8, local-only model/tokenizer, all decode defaults and QA thresholds. Explicit `config_version: 2` requires `invocation_policy: "isolated-zip-asr-v2"` in runtime_options and selects schema8: owned CPython3.12 `-I -S -B` startup, fixed inventoried environment package root after ZIP/stdlib, and exact startup/invocation binding. It rejects `._pth`; ordinary hooks remain inventoried but are not automatically initialized. Config/version mismatch is denied; existing v1 startup checks and records retain their meaning. This is maintained operator/vendor/OS trust, not system-level network isolation. A current transcription-specific origin and report4 must bind the exact existing audio/producer/target and one call; old Voice/import acceptance is insufficient. The normal saved-draft panel selects an eligible QA receipt and explicitly submits once. Failure/unknown accounting never permits another recognition; a committed result can reconcile the same Job without inference. The default commercial QA handler remains unchanged. Code and fake-child tests do not authorize a real ASR call or establish U-Voice/production quality.

Use the ordinary API/UI to enqueue and inspect drafts. The owned handler rechecks authorization before dispatch and commits generated AudioAssets as QA-pending. Interrupted or failed provider calls require explicit accounting reconciliation; the UI does not automatically retry them. This configuration guide does not authorize running a model, installing dependencies or publishing. Current implementation tests use synthetic subprocess/audio fixtures, not real generation.

These benchmark providers have separate source/model-weight license considerations. See docs/DEPENDENCIES.md and docs/product/VOICE_TALKING.md.

### Talking review and legacy evaluation boundary

Talking source-review admission is disabled unless `CONTENT_OS_TALKING_REVIEW_KEY_HASHES` is configured as a JSON object mapping distinct reviewer/operator IDs to SHA-256 hex digests of separate high-entropy keys (at least 32 characters). A reviewer presents the matching secret only in the `X-Content-OS-Talking-Review-Key` header when adopting or revoking an exact assessment. The general optional `CONTENT_OS_ACCESS_TOKEN` is separate and does not establish reviewer identity. Do not commit, log or reuse raw review keys. Admission also requires the assessment's evidence reference to be an existing, hash-matched file beneath `CONTENT_OS_DATA_ROOT`; it never creates a Talking Job.

Old direct Talking Job/series creation and failed-child recovery are disabled by default, including execution of previously queued direct Talking Jobs. `CONTENT_OS_ALLOW_LEGACY_TALKING_EVALUATION=1` explicitly re-enables that path for local benchmark evaluation only. Do not enable it for normal production: it does not apply the planned source-review, commercial-license or Worker revalidation gates.

The separate ProductionRun Talking-dispatch action can link one scene-bounded Job only after a current trusted receipt, unchanged plan and media, an exact verified commercial-safe provider/model/runtime/machine profile, a verified `fresh_voice_talking_duration_ms` bound, and budget admission. The Talking Worker must identify its actual scope through `CONTENT_OS_TALKING_RUNTIME_ID` and `CONTENT_OS_TALKING_MACHINE_ID`; these must match the admitted capability profile. A missing or unknown scope blocks planned execution. Add `--job-type verify_talking` to that Worker when local `ffprobe` is available to schedule technical QA after generation; a separate QA Worker can recover a missed handoff once at startup, or the product API can retry `talking-qa-advance`. QA completion does not assemble/admit a TalkingRun, satisfy U-Talking or resume rendering. Do not treat the local fixture capability profile as a commercially admitted provider.

## Security / privacy baseline

- local-first data and project control;
- explicit consent for Voice/face generation;
- no committed/logged provider secrets;
- explicit remote/BYOK media transfer and cost;
- durable provider-call budget/idempotency accounting;
- private-network access only when explicitly configured.

## Documentation rule

The active documentation hierarchy is intentional. Do not create parallel root PRDs, review reports, freezes or handoff documents for normal work.

Completed product behavior belongs in the relevant current module spec. Implementation history belongs in Git/evaluation evidence.

The fixed evaluation ASR recipe reuses a local FasterWhisper directory with `model.bin`, `config.json`, `tokenizer.json` and `vocabulary.txt`. Optional `preprocessor_config.json` must preserve the pinned feature defaults; absence uses installed-package defaults bound by the recipe and inventory. No model-name/download fallback is allowed.

Saved evaluation drafts expose separate six-dimensional U-Voice records after verified terminal QA. The normal panel uses the explicit evaluation-human-review GET/POST action and current QA fingerprint; exact replay preserves the immutable record, while conflicting decisions and legacy evaluation metadata mutations are rejected. This does not grant Master/Talking/production eligibility or another provider call.
