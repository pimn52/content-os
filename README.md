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

Local provider runtimes are opt-in and do not change Core product semantics. Content OS does not install/download a runtime on startup. However, the legacy upstream inference loaders may resolve missing auxiliary models through caches/Hub/URLs: a local primary checkpoint does not prove offline or complete execution. The purpose-scoped evaluation lane remains disabled until an audited local-only loader, complete artifact/runtime selection and downstream scope enforcement are available. Do not treat these benchmark commands as a hermetic execution or license guarantee.

### Local ASR

    $env:CONTENT_OS_ASR_PROVIDER = "local"
    $env:CONTENT_OS_ASR_LOCAL_MODEL = "small"
    $env:CONTENT_OS_ASR_DEVICE = "cpu"
    $env:CONTENT_OS_ASR_COMPUTE_TYPE = "int8"

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
