# Content OS

Content OS is a Local-first / BYOK content operating system for creators who want to turn reusable knowledge, identity, voice and real media into repeatable short-form content production.

Know what to create → Create it as you → Learn what works.

R1 focuses on the production loop:

new topic → editable IP-aware copy → authorized creator Voice → creator Talking → real media / typography / subtitles → 30–60s export.

Start with START_HERE.md.

## Project map

- Product & R1 specification: CONTENT_OS_EXECUTION_SPEC.md
- Product module map: docs/product/README.md
- System architecture: docs/architecture/SYSTEM_ARCHITECTURE.md
- Current status / active task: STATUS.md
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

## Optional local providers

Local provider runtimes are opt-in. They do not auto-download weights and they do not change Core product semantics.

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
