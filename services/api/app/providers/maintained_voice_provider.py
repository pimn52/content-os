"""Existing local clone runtime behind the ordinary Voice provider contract."""
from __future__ import annotations

import json
import hashlib
import wave
from pathlib import Path

from app.media.voice_reference import prepare_voice_reference
from .maintained_voice import MaintainedVoiceParameters
from .maintained_voice_execution import prepare_maintained_voice_child
from .omnivoice_entry_v2 import LocalCloneInputV2
from .prepared import ExecutionPreparationError
from .voice import VoiceConfigurationError, VoiceProviderResponseError, VoiceTimeout


def read_generation_segments(target, text, data_root):
    from .maintained_voice import narration_scene_spans
    spans=narration_scene_spans(text)
    if len(spans)==1:return ()
    try:
        manifest=Path(target).with_suffix('.segments.json')
        if manifest.stat().st_size>1_000_000:raise ValueError()
        saved=json.loads(manifest.read_text(encoding='utf8'))
        if (saved['state']!='completed' or saved['text_sha256']!=hashlib.sha256(text.encode('utf8')).hexdigest()
            or saved['audio_sha256']!=hashlib.sha256(Path(target).read_bytes()).hexdigest()
            or len(saved['segments'])!=len(spans)):raise ValueError()
        cursor=0
        for segment,(start,end,_) in zip(saved['segments'],spans):
            part=(Path(data_root)/segment['source_file']).resolve()
            if (part.parent!=Path(target).parent/(Path(target).stem+'.parts')
                or segment['start_char']!=start or segment['end_char']!=end or segment['start_sample']!=cursor
                or type(segment['end_sample']) is not int or segment['end_sample']<=cursor
                or hashlib.sha256(part.read_bytes()).hexdigest()!=segment['sha256']):raise ValueError()
            with wave.open(str(part),'rb') as audio:
                if audio.getnframes()!=segment['end_sample']-cursor or audio.getframerate()!=saved['sample_rate']:raise ValueError()
            cursor=segment['end_sample']
        with wave.open(str(target),'rb') as audio:
            if audio.getnframes()!=cursor or audio.getframerate()!=saved['sample_rate']:raise ValueError()
        return tuple(saved['segments'])
    except (OSError,ValueError,KeyError,TypeError,wave.Error):
        raise VoiceProviderResponseError('maintained_voice_segment_provenance_invalid') from None


def read_maintained_voice_options(reference: str, data_root: Path) -> dict:
    """Read installation settings once; no evaluation admission or credentials."""
    try:
        source = Path(reference)
        if not reference or not source.is_file() or source.stat().st_size > 128_000:
            raise ValueError()
        document = json.loads(source.read_text(encoding="utf-8"))
        if set(document) != {"config_version", "runtime_options"} or type(document["config_version"]) is not int or document["config_version"] != 1:
            raise ValueError()
        options = document["runtime_options"]
        required = {"environment", "base", "model_root", "parameters", "runtime_label", "machine_id"}
        # Older installation files retain inert evidence references. They do not
        # grant authority or require their former operator secret to synthesize.
        legacy = {"origin_reference", "installation_id", "max_bytes"}
        if not isinstance(options, dict) or not required <= options.keys() or options.keys() - required - legacy - {"gpu_uuid", "timeout_seconds"}:
            raise ValueError()
        result = {key: options[key] for key in required}
        for name in ("environment", "base", "model_root"):
            path = Path(result[name])
            if not path.is_absolute() or not path.is_dir():
                raise ValueError()
            result[name] = path.resolve()
        if not (result["environment"] / "Scripts/python.exe").is_file() or not result["model_root"].is_relative_to(data_root.resolve()):
            raise ValueError()
        if any(not isinstance(result[name], str) or not result[name].strip() for name in ("runtime_label", "machine_id")):
            raise ValueError()
        result["parameters"] = MaintainedVoiceParameters.model_validate(result["parameters"]).model_dump()
        timeout = options.get("timeout_seconds", 600)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError()
        result["timeout_seconds"] = timeout
        gpu = options.get("gpu_uuid")
        if gpu is not None and (not isinstance(gpu, str) or not gpu.strip()):
            raise ValueError()
        result["gpu_uuid"] = gpu
        return result
    except (ValueError, TypeError, OSError):
        raise VoiceConfigurationError("maintained Voice runtime configuration is invalid") from None


def synthesize_maintained_voice(*, options: dict, data_root: Path, ffmpeg: Path,
        window, selection, text: str, target: Path, language: str) -> Path:
    """Execute only synthesis; the ordinary Job handler owns usage and QA."""
    root = data_root.resolve()
    staging = root / "execution-staging"
    staging.mkdir(parents=True, exist_ok=True)
    inputs = LocalCloneInputV2(text=text, reference_audio=str(Path(window.source_file).resolve()),
        reference_text=window.transcript.strip(), language=language, output_path=str(target.resolve()),
        reference_sha256=selection.source_content_hash, reference_start_frame=0,
        reference_end_frame=(window.end_ms - window.start_ms) * 24,
        reference_max_frames=32 * 1024**2, reference_max_bytes=64 * 1024**2)
    try:
        with prepare_voice_reference(data_root=root, staging_parent=staging, ffmpeg=ffmpeg,
                window=window, selection=selection, inputs=inputs) as reference:
            arguments = {name: options[name] for name in (
                "environment", "base", "model_root", "parameters", "timeout_seconds", "gpu_uuid")}
            with prepare_maintained_voice_child(**arguments, data_root=root, staging_parent=staging,
                    inputs=reference.inputs_snapshot()) as child:
                return child.execute()
    except ExecutionPreparationError as exc:
        if "timeout" in str(exc):
            raise VoiceTimeout("maintained Voice generation timed out") from None
        raise VoiceProviderResponseError("maintained Voice reference or synthesis failed") from None
