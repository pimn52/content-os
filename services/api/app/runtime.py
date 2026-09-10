"""Provider-neutral runtime capability inspection.

The readiness check is deliberately side-effect free: it never sends a
provider request and never returns credential values.  It exists so the UI
can distinguish a missing implementation from a missing runtime configuration
before a user starts a workflow.
"""
from __future__ import annotations

import os
import shutil
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


CapabilityStatus = str


@dataclass(frozen=True)
class RuntimeCapability:
    key: str
    status: CapabilityStatus
    provider: str | None
    model: str | None
    detail: str


def resolve_local_executable(
    name: str,
    env: Mapping[str, str] | None = None,
    *,
    executable_lookup: Callable[[str], str | None] | None = None,
) -> str:
    """Resolve a local media/runtime command without coupling to a provider.

    Explicit environment overrides win, followed by PATH, then the Remotion
    compositor package bundled with this checkout. Returning the unresolved
    command name preserves the normal subprocess error when nothing exists.
    """

    values = env if env is not None else os.environ
    override = values.get(f"CONTENT_OS_{name.upper()}", "").strip()
    if override:
        return override

    locate = executable_lookup or shutil.which
    found = locate(name)
    if found:
        return found

    repository_root = Path(__file__).resolve().parents[3]
    package_root = repository_root / "apps" / "renderer" / "node_modules" / "@remotion" / "compositor-win32-x64-msvc"
    for candidate in (package_root / f"{name}.exe", package_root / name):
        if candidate.is_file():
            return str(candidate)
    return name


def inspect_runtime_capabilities(
    env: Mapping[str, str] | None = None,
    *,
    executable_lookup: Callable[[str], str | None] | None = None,
) -> tuple[RuntimeCapability, ...]:
    """Return a stable, non-secret snapshot of local runtime readiness.

    ``provider_not_configured`` means the capability has an implementation but
    no runtime credential was supplied. ``not_developed`` is reserved for
    capabilities intentionally outside the current local runtime surface.
    ``unavailable`` means local execution prerequisites are missing.
    """

    values = env if env is not None else os.environ
    locate = executable_lookup or shutil.which

    def configured(*names: str) -> bool:
        return any(bool(values.get(name, "").strip()) for name in names)

    def selected_model(*names: str) -> str | None:
        for name in names:
            value = values.get(name, "").strip()
            if value:
                return value
        return None

    def provider_capability(
        key: str,
        *,
        names: tuple[str, ...],
        provider: str,
        model_names: tuple[str, ...],
        default_model: str,
    ) -> RuntimeCapability:
        model = selected_model(*model_names) or default_model
        if configured(*names):
            return RuntimeCapability(key, "ready", provider, model, "runtime credential is configured; no probe request was sent")
        return RuntimeCapability(key, "provider_not_configured", provider, model, "runtime credential is not configured")

    ffmpeg = resolve_local_executable("ffmpeg", values, executable_lookup=executable_lookup)
    ffprobe = resolve_local_executable("ffprobe", values, executable_lookup=executable_lookup)
    npm = resolve_local_executable("npm", values, executable_lookup=executable_lookup)

    def available(command: str) -> bool:
        return locate(command) is not None or Path(command).is_file()

    local_media_missing = [name for name, command in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not available(command)]
    local_media = RuntimeCapability(
        "local_media",
        "ready" if not local_media_missing else "unavailable",
        "local",
        None,
        "ffmpeg and ffprobe are available" if not local_media_missing else f"missing local executable(s): {', '.join(local_media_missing)}",
    )
    renderer = RuntimeCapability(
        "render",
        "ready" if available(npm) else "unavailable",
        "remotion",
        "4.x",
        "npm is available for the local Remotion renderer" if available(npm) else "npm is not available for the local Remotion renderer",
    )

    embedding = provider_capability(
        "embedding",
        names=("CONTENT_OS_EMBEDDING_API_KEY", "OPENAI_API_KEY"),
        provider="openai-compatible",
        model_names=("CONTENT_OS_EMBEDDING_MODEL",),
        default_model="text-embedding-3-small",
    )
    retrieval_mode = values.get("CONTENT_OS_RETRIEVAL_MODE", "embedding").strip().lower()
    if retrieval_mode == "lexical":
        retrieval = RuntimeCapability(
            "retrieval", "ready", "local", None,
            "explicit local lexical retrieval is available; it does not create or write vectors",
        )
    elif retrieval_mode == "embedding":
        retrieval = RuntimeCapability(
            "retrieval", embedding.status, embedding.provider, embedding.model,
            f"embedding retrieval: {embedding.detail}",
        )
    else:
        retrieval = RuntimeCapability(
            "retrieval", "unavailable", "local", None,
            "CONTENT_OS_RETRIEVAL_MODE must be embedding or lexical",
        )

    asr_provider = values.get("CONTENT_OS_ASR_PROVIDER", "openai-compatible").strip().lower()
    if asr_provider == "local":
        asr_model = selected_model("CONTENT_OS_ASR_LOCAL_MODEL") or "small"
        if importlib.util.find_spec("faster_whisper") is None:
            asr_capability = RuntimeCapability(
                "asr",
                "unavailable",
                "faster-whisper",
                asr_model,
                "local ASR selected but optional faster-whisper is not installed",
            )
        else:
            asr_capability = RuntimeCapability(
                "asr",
                "ready",
                "faster-whisper",
                asr_model,
                "local CPU ASR is installed; model weights may download on the first explicit transcription job",
            )
    elif asr_provider in {"", "openai-compatible"}:
        asr_capability = provider_capability(
            "asr",
            names=("CONTENT_OS_ASR_API_KEY", "OPENAI_API_KEY"),
            provider="openai-compatible",
            model_names=("CONTENT_OS_ASR_MODEL",),
            default_model="whisper-1",
        )
    else:
        asr_capability = RuntimeCapability(
            "asr",
            "unavailable",
            asr_provider,
            selected_model("CONTENT_OS_ASR_MODEL", "CONTENT_OS_ASR_LOCAL_MODEL"),
            "CONTENT_OS_ASR_PROVIDER must be local or openai-compatible",
        )

    return (
        local_media,
        renderer,
        provider_capability(
            "scene_planning",
            names=("CONTENT_OS_LLM_API_KEY", "OPENAI_API_KEY"),
            provider="openai-compatible",
            model_names=("CONTENT_OS_LLM_MODEL",),
            default_model="gpt-4o-mini",
        ),
        asr_capability,
        provider_capability(
            "vision",
            names=("CONTENT_OS_VISION_API_KEY", "OPENAI_API_KEY"),
            provider="openai-compatible",
            model_names=("CONTENT_OS_VISION_MODEL",),
            default_model="gpt-4o-mini",
        ),
        embedding,
        retrieval,
        RuntimeCapability("tts", "not_developed", None, None, "TTS is intentionally deferred; import an authorized local recording instead"),
        RuntimeCapability("talking", "not_developed", None, None, "Talking generation is intentionally deferred and requires explicit consent"),
        RuntimeCapability("publishing", "not_developed", None, None, "automatic publishing is not enabled; use the manual publication record flow"),
    )
