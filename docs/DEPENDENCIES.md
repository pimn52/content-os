# Dependency inventory

This is the bounded Task 002a inventory. Observed versions come from the Python environment used for the scaffold checks. Project requirements use bounded ranges instead of guessed pins; repeat this inventory before a release.

| Package | Role | Observed version | License metadata |
|---|---|---:|---|
| Python | Runtime | 3.12.13 | PSF License (runtime convention; release audit still required) |
| FastAPI | Local HTTP API | 0.141.1 | MIT (installed `License-Expression`; release notice still required) |
| python-multipart | Browser upload parsing | 0.0.32 | Apache-2.0 (installed `License-Expression`) |
| Uvicorn | Local ASGI server | 0.52.4 | BSD-3-Clause (installed `License-Expression`) |
| Pydantic | API validation | 2.13.4 | MIT (installed `License-Expression`) |
| pytest | Test runner (optional test dependency) | 9.1.1 | MIT (installed `License-Expression`) |
| httpx | FastAPI test client support (optional test dependency) | 0.28.1 | BSD-3-Clause in installed metadata |

The Python API boundary introduces no provider SDK, database server, secret, or paid API dependency. The formal Web and Remotion dependencies are listed below. Transitive packages installed by the local test environment are not project direct dependencies; include them in a full release inventory.

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

## R1 formal Web workspace

The production Web entry is the checked-in `apps/web/package-lock.json` graph. The
direct packages below are the versions observed in that lock file; transitive
packages remain subject to the same release inventory and audit.

| Package | Role | Locked version | Declared license |
|---|---|---:|---|
| React | UI runtime | 18.2.0 | MIT |
| React DOM | Browser renderer | 18.2.0 | MIT |
| Vite | Development/build server | 5.4.8 | MIT |
| `@vitejs/plugin-react` | Vite React transform | 4.3.1 | MIT |
| TypeScript | Type checking/build input | 5.5.4 | Apache-2.0 |
| `@types/react` / `@types/react-dom` | Type declarations (dev) | 18.2.79 / 18.2.25 | MIT |

`apps/web` currently has no runtime network dependency beyond the local API
proxy. The production dependency audit was clean at the time of the R1 check;
the development graph includes build tooling and must be audited again for a
release. Do not redistribute FFmpeg, Remotion's browser binary, or model
weights until the exact build and applicable license terms have been reviewed.

## Optional local CPU ASR

The R1 ASR contract also has an opt-in local provider. Install it with
`pip install -e ".[local-asr]"`, then set `CONTENT_OS_ASR_PROVIDER=local` before
running an explicit transcription worker. The default installation and the
default worker remain unchanged; model weights are not bundled and may be
downloaded by `faster-whisper` on the first transcription job.

| Package | Role | Observed version | License/runtime notes |
|---|---|---:|---|
| `faster-whisper` | CTranslate2-backed local Whisper transcription | 1.2.1 | MIT; CPU `int8` is the default local configuration |
| `ctranslate2` | Local inference runtime | 4.8.2 | MIT; CPU path avoids a GPU requirement |
| `av` | Local audio/video decoding used by the provider | 18.1.0 | BSD-3-Clause (installed `License-Expression`) |
| `onnxruntime` | Runtime dependency of the optional stack | 1.29.0 | MIT License |
| `huggingface-hub` / `tokenizers` | Model acquisition/tokenization dependencies | not installed / 0.23.2 | Tokenizers reports Apache-2.0 classifier; model acquisition and model-card licenses must be reviewed before redistribution |

This path adds roughly tens of MB of Windows wheels and CPU latency; model
weights are a separate download and can be hundreds of MB depending on the
selected model. The implementation is lazy and never downloads during
readiness inspection. The existing OpenAI-compatible BYOK adapter remains
available as a replaceable alternative.
