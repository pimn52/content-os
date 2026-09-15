# Content OS

Content OS is a **Local-first / BYOK personal content engine** for creators who want to reuse their real knowledge, voice and media instead of repeatedly filming and editing every short video from scratch.

Long-term loop:

> **Know what to create → Create it as you → Learn what works**

R1 currently focuses on `Create it as you`: new topic → editable IP-aware copy → authorized creator voice → new Talking/lip-sync segment → reusable real media / typography / subtitles → 30–60s export.

> Start with [`START_HERE.md`](START_HERE.md). Do not use historical plans/reviews as current execution instructions.

## Current state

The repository already contains substantial local infrastructure:

- FastAPI + SQLite local application;
- persistent creator/IP, projects, revisions and jobs;
- local media import/hash dedupe/ffprobe/continuous Clip segmentation;
- ASR, vision, embedding/search provider boundaries;
- ScenePlan and real-media-first Hybrid Asset Router;
- optional capture/Shoot Tasks and browser/mobile uploads;
- static/typography/image/audio asset paths;
- `MasterNarration` timeline contracts;
- Remotion + FFmpeg vertical rendering;
- provider-call idempotency, budgets, cost ledger and retry/recovery;
- local backup/restore and private-network access guidance.

The project is still an **internal Alpha**. Voice cloning and Talking/lip-sync are the current core product gap and must pass real creator quality gates before the project can claim it replaces repeated filming.

See [`STATUS.md`](STATUS.md) for the current ready task.

## Voice provider strategy

Voice is provider-neutral.

OmniVoice is being incorporated as a **non-commercial local evaluation / benchmark provider** because of its strong multilingual zero-shot cloning fit. Its source code is Apache-2.0, while the official pretrained weights are currently CC-BY-NC, so those weights are not a commercial-safe default and must not be bundled as such.

Content OS keeps commercial-safe local and BYOK cloud provider paths interchangeable behind the same contracts. Model-weight licenses are reviewed separately from source-code licenses.

## Quick start — Windows development

From the repository root:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\scripts\start.ps1
```

The normal local service binds to `127.0.0.1` by default.

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Check the documentation/control hierarchy:

```powershell
.\.venv\Scripts\python.exe scripts\check_docs.py
```

Build the formal React workspace:

```powershell
npm --prefix apps\web install
npm --prefix apps\web run build
```

The API will serve the built UI when `apps/web/dist` exists.

## Optional local ASR

Local CPU ASR is opt-in:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[local-asr]"
$env:CONTENT_OS_ASR_PROVIDER = "local"
$env:CONTENT_OS_ASR_LOCAL_MODEL = "small"
$env:CONTENT_OS_ASR_DEVICE = "cpu"
$env:CONTENT_OS_ASR_COMPUTE_TYPE = "int8"
.\scripts\start.ps1
```

Model weights are not bundled in the base installation.

## Optional local Talking / LatentSync 1.5

LatentSync is an optional local benchmark adapter for ordinary authorized
creator footage. It does not download a model automatically and is not a
commercial-safe default because the official benchmark weights are
non-commercial. Configure it only for an explicit Talking worker run:

```powershell
$env:CONTENT_OS_TALKING_PROVIDER = "latentsync"
$env:CONTENT_OS_LATENTSYNC_PYTHON = "C:\path\to\latentsync-runtime\python.exe"
$env:CONTENT_OS_LATENTSYNC_REPO = "C:\path\to\LatentSync"
$env:CONTENT_OS_LATENTSYNC_CHECKPOINT = "C:\path\to\latentsync_unet.pt"
# Recommended when the bundled FFmpeg lacks the tpad filter used for output normalization.
$env:CONTENT_OS_LATENTSYNC_FFMPEG = "C:\path\to\latentsync-runtime\Library\bin\ffmpeg.exe"
.\scripts\start-worker.ps1 --job-type generate_talking
```

The optional `CONTENT_OS_LATENTSYNC_RUNNER` and
`CONTENT_OS_LATENTSYNC_UNET_CONFIG` variables override the official runner
and U-Net config when a compatible local wrapper is required.
`CONTENT_OS_LATENTSYNC_FFMPEG` selects the provider runtime's FFmpeg for
staging, audio preparation and output duration normalization; this is needed
when the general-purpose bundled FFmpeg does not include `tpad`. Optional
`CONTENT_OS_LATENTSYNC_STEPS`, `CONTENT_OS_LATENTSYNC_GUIDANCE_SCALE`,
`CONTENT_OS_LATENTSYNC_SEED` and `CONTENT_OS_LATENTSYNC_TIMEOUT_SECONDS`
control bounded inference settings. Readiness checks local paths only; local
inference is recorded as USD 0 external cost, while the provider-call ledger
still records the operation.

## Optional local Voice / OmniVoice benchmark

The formal UI can enqueue a local OmniVoice Voice Job when the benchmark
runtime and model snapshot are explicitly configured. The worker resolves the
first consented reference Clip and extracts its real audio/transcript; it does
not accept a path or text from an untrusted profile field and does not download
weights automatically:

```powershell
$env:CONTENT_OS_VOICE_PROVIDER = "omnivoice"
$env:CONTENT_OS_OMNIVOICE_PYTHON = "C:\path\to\omnivoice-runtime\Scripts\python.exe"
$env:CONTENT_OS_OMNIVOICE_MODEL = "C:\path\to\models--k2-fsa--OmniVoice\snapshots\<revision>"
$env:CONTENT_OS_OMNIVOICE_DEVICE = "cuda" # use "cpu" only when CUDA is unavailable
$env:CONTENT_OS_VOICE_QA_ASR_MODEL = "C:\path\to\faster-whisper-small\snapshot"
.\scripts\start.ps1
```

OmniVoice remains a non-commercial benchmark because its official pretrained
weights are CC-BY-NC. Generated audio stays QA-pending until independent real
ASR verifies copy coverage, silence, timing and playability; only then can the
Talking form use it. Setting `CONTENT_OS_VOICE_QA_ASR_MODEL` enables the local
`verify_voice` worker and the UI's separate Voice QA Job; it must point to an
already-downloaded local model directory and never downloads weights implicitly.

## Repository map

```text
apps/web/                  React/Vite user workspace
apps/renderer/             Remotion renderer
services/api/app/          FastAPI/domain/providers/jobs/media/routing
services/api/tests/        backend/integration tests
contracts/                 exported JSON schemas/examples
docs/                      dependency, remote-access and supporting docs
scripts/                   local operations and validation
```

## Active project-control docs

Only these root files define the current project:

- [`START_HERE.md`](START_HERE.md) — navigation;
- [`CONTENT_OS_EXECUTION_SPEC.md`](CONTENT_OS_EXECUTION_SPEC.md) — scope, architecture constraints and acceptance gates;
- [`STATUS.md`](STATUS.md) — current facts and next ready task;
- [`DECISIONS.md`](DECISIONS.md) — durable decisions;
- [`AGENTS.md`](AGENTS.md) — implementation/model-cost policy;
- `README.md` — contributor/user overview.

Historical PRDs, freezes, handoffs and reviews remain available through Git history; they do not outrank these active controls.

## Development model policy

Quality first, then use the lowest capable implementation model:

`Luna → Terra → Sol`

- Luna: isolated UI/CRUD/tests/docs/simple adapters.
- Terra: media/timeline/provider/planner/router/jobs/migrations and other cross-module core work.
- Sol: architecture/security/critical quality gate only, or unresolved Terra failure with a concrete reproduction.

See [`AGENTS.md`](AGENTS.md) for the exact escalation rule.

## Security / privacy baseline

- Local-first by default.
- Provider credentials are never committed or printed in logs.
- Private-network access requires explicit configuration; the project does not automatically open router/firewall access.
- Voice/face generation requires explicit rights/consent records.
- Runtime provider calls are budgeted/idempotent and unknown price is not treated as zero.

## License note

Content OS dependency/model licensing is tracked in [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md). Source-code and model-weight licenses must both be checked before redistribution or commercial release.
