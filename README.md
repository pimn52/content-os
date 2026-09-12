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
