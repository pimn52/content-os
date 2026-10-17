"""Owned reference-window extraction using the existing FFmpeg recipe.

This prepares input, not a provider call or permission/quality grant.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import wave

from app.providers.omnivoice_entry_v2 import read_reference
from app.providers.prepared import ExecutionPreparationError
from app.providers.runtime_tree import _exact_path


def reference_extraction_command(ffmpeg, source, target, start_ms, end_ms, *, canonical=False):
    # Default preserves the existing Worker CLI command exactly.
    command = [str(ffmpeg), "-y", "-ss", f"{start_ms / 1000:.3f}", "-i", str(source),
        "-t", f"{(end_ms - start_ms) / 1000:.3f}", "-vn", "-ac", "1", "-ar", "24000",
        "-c:a", "pcm_s16le", str(target)]
    if canonical:
        # Decode on the source timeline before selecting samples. MP4/AAC
        # seeking can discard decoder priming even with output-side -ss.
        # Do not pad the tail: a truly short source must still fail validation.
        command = [str(ffmpeg), "-nostdin", "-v", "error", "-n", "-protocol_whitelist", "file",
            "-i", str(source), "-vn", "-af",
            f"aresample=24000:async=1:min_hard_comp=0.001:first_pts=0,"
            f"atrim=start_sample={start_ms * 24}:end_sample={end_ms * 24},asetpts=PTS-STARTPTS",
            "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(target)]
        command[-1:-1] = ["-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact"]
    return command


def _hash(path):
    digest = hashlib.sha256()
    _exact_path(path, directory=False)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_OWNED = object()


class OwnedVoiceReference:
    def __init__(self, token, temporary, root, source, ffmpeg, window, tool_sha, inputs, provenance):
        if token is not _OWNED: raise ExecutionPreparationError("voice_reference_owned_required")
        self._temporary, self._root, self._source, self._ffmpeg = temporary, root, source, ffmpeg
        self._window = window
        self._tool_sha, self._inputs = tool_sha, inputs.model_dump_json()
        self._provenance = json.dumps(provenance, sort_keys=True, separators=(",", ":"))
        self._closed = False

    def inputs_snapshot(self):
        from app.providers.omnivoice_entry_v2 import LocalCloneInputV2
        return LocalCloneInputV2.model_validate_json(self._inputs)

    def provenance_snapshot(self): return json.loads(self._provenance)

    def verify_for(self, window, selection, inputs):
        if self._closed or window != self._window or Path(window.source_file).resolve() != self._source:
            raise ExecutionPreparationError("voice_reference_binding_changed")
        expected = self.inputs_snapshot()
        keys = ("reference_audio", "reference_text", "reference_sha256", "reference_start_frame",
            "reference_end_frame", "reference_max_bytes", "reference_max_frames")
        if (selection.source_content_hash != self.provenance_snapshot()["source_sha256"]
            or any(getattr(inputs, key) != getattr(expected, key) for key in keys)
            or _hash(self._source) != selection.source_content_hash or _hash(self._ffmpeg) != self._tool_sha):
            raise ExecutionPreparationError("voice_reference_binding_changed")
        read_reference(expected, 24000)
        return self.provenance_snapshot()

    def close(self):
        self._closed = True
        self._temporary.cleanup()

    def __enter__(self): return self
    def __exit__(self, *_args): self.close()


def prepare_voice_reference(*, data_root, staging_parent, ffmpeg, window, selection, inputs, timeout_seconds=120):
    """Extract exactly the authorized interval; no arbitrary command/callback."""
    temporary = None
    try:
        root, staging, tool = Path(data_root).resolve(), Path(staging_parent), Path(ffmpeg)
        source = Path(window.source_file)
        _exact_path(root, directory=True); _exact_path(staging, directory=True)
        _exact_path(tool, directory=False); _exact_path(source, directory=False)
        if (not source.is_relative_to(root) or not staging.is_relative_to(root)
            or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600
            or window.clip_id != selection.clip_id or window.asset_id != selection.asset_id
            or window.start_ms != selection.start_ms or window.end_ms != selection.end_ms
            or inputs.reference_text != window.transcript.strip()
            or _hash(source) != selection.source_content_hash):
            raise ExecutionPreparationError("voice_reference_binding_invalid")
        frames = (window.end_ms - window.start_ms) * 24
        if not 0 < frames <= inputs.reference_max_frames or 44 + frames * 2 > inputs.reference_max_bytes:
            raise ExecutionPreparationError("voice_reference_limit_exceeded")
        tool_sha = _hash(tool)
        temporary = TemporaryDirectory(prefix="voice-reference-", dir=staging)
        target = Path(temporary.name) / "reference.wav"
        command = reference_extraction_command(tool, source, target, window.start_ms, window.end_ms, canonical=True)
        environment = {"PATH": str(tool.parent)}
        if os.name == "nt":
            from app.providers.maintained_runtime import _system_root
            system = _system_root()
            environment.update(SystemRoot=str(system), PATH=str(tool.parent) + os.pathsep + str(system / "System32"))
        result = subprocess.run(command, cwd=temporary.name, env=environment, shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout_seconds)
        if result.returncode != 0 or not target.is_file() or not 44 < target.stat().st_size <= inputs.reference_max_bytes:
            raise ExecutionPreparationError("voice_reference_extraction_failed")
        with wave.open(str(target), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()) != (1, 2, 24000, frames):
                raise ExecutionPreparationError("voice_reference_output_invalid")
        derived = inputs.model_copy(update=dict(reference_audio=str(target), reference_sha256=_hash(target),
            reference_start_frame=0, reference_end_frame=frames))
        provenance = dict(recipe="ffmpeg-selected-mono24k-pcm16-v1", tool_sha256=tool_sha,
            timing="source_pts_then_sample_trim",
            source_sha256=selection.source_content_hash, clip_id=str(window.clip_id), asset_id=str(window.asset_id),
            start_ms=window.start_ms, end_ms=window.end_ms, derived_sha256=derived.reference_sha256,
            sampling_rate=24000, frames=frames)
        owned = OwnedVoiceReference(_OWNED, temporary, root, source, tool, window, tool_sha, derived, provenance)
        owned.verify_for(window, selection, derived)
        return owned
    except BaseException as error:
        if temporary is not None: temporary.cleanup()
        if isinstance(error, (ExecutionPreparationError, KeyboardInterrupt)): raise
        if isinstance(error, subprocess.TimeoutExpired):
            raise ExecutionPreparationError("voice_reference_extraction_timeout") from None
        raise ExecutionPreparationError("voice_reference_extraction_failed") from None
