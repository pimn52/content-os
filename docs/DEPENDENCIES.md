# Dependency inventory

This is the bounded Task 002a inventory. Observed versions come from the Python environment used for the scaffold checks. Project requirements use bounded ranges instead of guessed pins; repeat this inventory before a release.

| Package | Role | Observed version | License metadata |
|---|---|---:|---|
| Python | Runtime | 3.12.13 | PSF License (runtime convention; release audit still required) |
| FastAPI | Local HTTP API | 0.141.1 | Package metadata did not declare a license; verify upstream license before release |
| Uvicorn | Local ASGI server | 0.52.4 | Package metadata did not declare a license; verify upstream license before release |
| Pydantic | API validation | 2.13.4 | Package metadata did not declare a license; verify upstream license before release |
| pytest | Test runner (optional test dependency) | 9.1.1 | Package metadata did not declare a license; verify upstream license before release |
| httpx | FastAPI test client support (optional test dependency) | 0.28.1 | BSD-3-Clause in installed metadata |

No provider SDK, database, frontend, renderer, secret, or paid API is introduced by this scaffold. Transitive packages installed by the local test environment are not project direct dependencies; include them in a full release inventory.

## External media tooling

Task 004 uses the external `ffprobe` command for local media metadata and does not add a Python dependency. A development or deployment installation must provide compatible FFmpeg/ffprobe binaries (FFmpeg licensing and build configuration must be reviewed separately before redistribution). The application invokes an explicit argv with no shell and does not bundle, download, or silently install these binaries.

## Task 014 renderer

| Package/runtime | Role | Locked/observed version | License and runtime notes |
|---|---|---:|---|
| Node.js | Renderer runtime | 24.15.0 observed; package requires >=20 | Node.js license and supported-version policy require release review |
| npm | Dependency installer/runner | 11.12.1 observed | Used only for renderer install and commands |
| Remotion / `@remotion/cli` | React timeline and local MP4 rendering | 4.0.522 exact | Remotion License; free-use eligibility depends on organization size/use case and must be reviewed before release or automation deployment |
| React / React DOM | Renderer component runtime | 18.2.0 exact | MIT |

Remotion downloads and runs a compatible Chrome Headless Shell and uses its media toolchain during rendering, so installation and first render have meaningful network, disk, and runtime cost. `package-lock.json` records the transitive Node dependency graph; `npm audit --omit=dev --audit-level=high` reported zero known vulnerabilities during Task 014, but release builds still require a fresh audit.

An FFmpeg-only compositor would be lighter, but was not selected because the frozen V0.1 baseline explicitly requires a basic Remotion timeline. The Python boundary remains replaceable and invokes the renderer with an argv and no shell.
