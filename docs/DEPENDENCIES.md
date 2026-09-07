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
