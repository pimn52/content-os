"""Provider-neutral runtime capability inspection."""
from __future__ import annotations

import importlib.util
import os
import shutil
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping

from app.domain.models import CostCategory, UsageCost

from app.providers.latentsync import (
    LATENTSYNC_MODEL,
    LATENTSYNC_PROCESSING_RESOLUTION_PX,
    LATENTSYNC_PROVIDER,
    LATENTSYNC_STATED_MINIMUM_VRAM_GB,
)

CapabilityStatus = str

@dataclass(frozen=True)
class RuntimeCapability:
    key: str
    status: CapabilityStatus
    provider: str | None
    model: str | None
    detail: str
    estimated_cost: UsageCost | None = None
    constraints: tuple[str, ...] = ()

def resolve_local_executable(
    name: str,
    env: Mapping[str, str] | None = None,
    *,
    executable_lookup: Callable[[str], str | None] | None = None,
) -> str:
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

    missing = [name for name, command in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not available(command)]
    local_media = RuntimeCapability(
        "local_media",
        "ready" if not missing else "unavailable",
        "local",
        None,
        "ffmpeg and ffprobe are available" if not missing else f"missing local executable(s): {', '.join(missing)}",
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
        retrieval = RuntimeCapability("retrieval", "ready", "local", None, "explicit local lexical retrieval is available; it does not create or write vectors")
    elif retrieval_mode == "embedding":
        retrieval = RuntimeCapability("retrieval", embedding.status, embedding.provider, embedding.model, f"embedding retrieval: {embedding.detail}")
    else:
        retrieval = RuntimeCapability("retrieval", "unavailable", "local", None, "CONTENT_OS_RETRIEVAL_MODE must be embedding or lexical")

    asr_provider = values.get("CONTENT_OS_ASR_PROVIDER", "openai-compatible").strip().lower()
    if asr_provider == "local":
        asr_model = selected_model("CONTENT_OS_ASR_LOCAL_MODEL") or "small"
        if importlib.util.find_spec("faster_whisper") is None:
            asr = RuntimeCapability("asr", "unavailable", "faster-whisper", asr_model, "local ASR selected but optional faster-whisper is not installed")
        else:
            asr = RuntimeCapability("asr", "ready", "faster-whisper", asr_model, "local CPU ASR is installed; model weights may download on the first explicit transcription job")
    elif asr_provider in {"", "openai-compatible"}:
        asr = provider_capability(
            "asr",
            names=("CONTENT_OS_ASR_API_KEY", "OPENAI_API_KEY"),
            provider="openai-compatible",
            model_names=("CONTENT_OS_ASR_MODEL",),
            default_model="whisper-1",
        )
    else:
        asr = RuntimeCapability("asr", "unavailable", asr_provider, selected_model("CONTENT_OS_ASR_MODEL", "CONTENT_OS_ASR_LOCAL_MODEL"), "CONTENT_OS_ASR_PROVIDER must be local or openai-compatible")

    selected_talking = values.get("CONTENT_OS_TALKING_PROVIDER", "").strip().lower()
    if selected_talking == LATENTSYNC_PROVIDER:
        talking_model = selected_model("CONTENT_OS_TALKING_MODEL") or LATENTSYNC_MODEL
        estimated_cost = UsageCost(
            category=CostCategory.TALKING,
            amount=Decimal("0"),
            currency="USD",
            provider=LATENTSYNC_PROVIDER,
            note="local inference; no external provider charge",
        )
        repo_value = values.get("CONTENT_OS_LATENTSYNC_REPO", "").strip()
        checkpoint_value = values.get("CONTENT_OS_LATENTSYNC_CHECKPOINT", "").strip()
        python_value = values.get("CONTENT_OS_LATENTSYNC_PYTHON", "").strip()
        repo_path = Path(repo_value) if repo_value else None
        runner_path = Path(values.get("CONTENT_OS_LATENTSYNC_RUNNER", "").strip() or (repo_path / "scripts" / "inference.py" if repo_path else ""))
        config_path = Path(values.get("CONTENT_OS_LATENTSYNC_UNET_CONFIG", "").strip() or (repo_path / "configs" / "unet" / "stage2.yaml" if repo_path else ""))
        missing_talking = []
        if not python_value or not (Path(python_value).is_file() or locate(python_value) is not None):
            missing_talking.append("Python runtime")
        if repo_path is None or not repo_path.is_dir():
            missing_talking.append("repository")
        if not checkpoint_value or not Path(checkpoint_value).is_file():
            missing_talking.append("checkpoint")
        if not runner_path.is_file():
            missing_talking.append("runner")
        if not config_path.is_file():
            missing_talking.append("U-Net config")
        talking = RuntimeCapability(
            "talking",
            "ready" if not missing_talking else "unavailable",
            LATENTSYNC_PROVIDER,
            talking_model,
            (
                "local LatentSync 1.5 is configured; readiness checks paths only and sends no inference request"
                if not missing_talking
                else f"LatentSync 1.5 is selected but missing local runtime item(s): {', '.join(missing_talking)}"
            ),
            estimated_cost,
            (
                f"processing_resolution={LATENTSYNC_PROCESSING_RESOLUTION_PX}px",
                f"stated_minimum_vram={LATENTSYNC_STATED_MINIMUM_VRAM_GB:g}GB",
                "official benchmark weights are non-commercial",
                "no inference probe request sent",
            ),
        )
    elif selected_talking:
        talking = RuntimeCapability(
            "talking",
            "unavailable",
            selected_talking,
            selected_model("CONTENT_OS_TALKING_MODEL"),
            "selected Talking provider is not implemented in this revision",
        )
    else:
        talking = RuntimeCapability(
            "talking",
            "provider_not_configured",
            None,
            None,
            "no Talking provider is currently admitted/configured in Core; candidates are evaluated in isolation before adapter admission",
        )

    selected_voice = values.get("CONTENT_OS_VOICE_PROVIDER", "").strip().lower()
    if selected_voice == "omnivoice":
        voice_model = selected_model("CONTENT_OS_OMNIVOICE_MODEL") or "official-pretrained"
        voice_python = values.get("CONTENT_OS_OMNIVOICE_PYTHON", "").strip()
        voice_checkpoint = values.get("CONTENT_OS_OMNIVOICE_MODEL", "").strip()
        missing_voice = []
        if not voice_python or not (Path(voice_python).is_file() or locate(voice_python) is not None):
            missing_voice.append("Python runtime")
        if not voice_checkpoint or not Path(voice_checkpoint).exists():
            missing_voice.append("model snapshot")
        voice = RuntimeCapability(
            "tts",
            "ready" if not missing_voice else "unavailable",
            "omnivoice",
            voice_model,
            (
                "local OmniVoice benchmark is configured; readiness checks paths only and sends no inference request"
                if not missing_voice
                else f"OmniVoice is selected but missing local runtime item(s): {', '.join(missing_voice)}"
            ),
            UsageCost(
                category=CostCategory.VOICE,
                amount=Decimal("0"),
                currency="USD",
                provider="omnivoice",
                note="local benchmark inference; no external provider charge",
            ),
            (
                "official pretrained weights are non-commercial",
                "reference Clip must have a real transcript",
                "no inference probe request sent",
            ),
        )
    elif selected_voice:
        voice = RuntimeCapability(
            "tts",
            "unavailable",
            selected_voice,
            selected_model("CONTENT_OS_VOICE_MODEL"),
            "selected Voice provider is not implemented in this revision",
        )
    else:
        voice = RuntimeCapability("tts", "not_developed", None, None, "no optional Voice benchmark provider is configured")

    qa_model = values.get("CONTENT_OS_VOICE_QA_ASR_MODEL", "").strip()
    if not qa_model:
        voice_qa = RuntimeCapability(
            "voice_qa", "provider_not_configured", "faster-whisper", None,
            "local Voice QA ASR is not configured; generated narration remains QA-pending",
        )
    elif importlib.util.find_spec("faster_whisper") is None:
        voice_qa = RuntimeCapability(
            "voice_qa", "unavailable", "faster-whisper", qa_model,
            "Voice QA selected but optional faster-whisper is not installed",
        )
    elif not Path(qa_model).exists():
        voice_qa = RuntimeCapability(
            "voice_qa", "unavailable", "faster-whisper", qa_model,
            "Voice QA is selected but the local ASR model directory is missing",
        )
    else:
        voice_qa = RuntimeCapability(
            "voice_qa", "ready", "faster-whisper", qa_model,
            "local real-ASR Voice QA is configured; readiness checks paths only and sends no inference request",
            UsageCost(
                category=CostCategory.ASR,
                amount=Decimal("0"),
                currency="USD",
                provider="faster-whisper",
                note="local QA inference; no external provider charge",
            ),
            ("independent ASR evidence is required before Talking", "no inference probe request sent"),
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
        asr,
        provider_capability(
            "vision",
            names=("CONTENT_OS_VISION_API_KEY", "OPENAI_API_KEY"),
            provider="openai-compatible",
            model_names=("CONTENT_OS_VISION_MODEL",),
            default_model="gpt-4o-mini",
        ),
        embedding,
        retrieval,
        voice,
        voice_qa,
        talking,
        RuntimeCapability("publishing", "not_developed", None, None, "automatic publishing is not enabled; use the manual publication record flow"),
    )
