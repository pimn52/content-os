"""Explicit PCM-only compaction of measured quiet at known Voice take seams."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Mapping, Sequence
import wave

from app.voice_recipe import SEAM_METHOD, VoiceGenerationRecipe
from app.voice_observation import VoiceObservationError, _pcm_quiet
from app.narration_performance import NarrationBoundaryKind, derive_narration_boundary_map


def take_boundary_kinds(texts: Sequence[str]) -> tuple[NarrationBoundaryKind, ...]:
    """Derive join roles from exact editorial copy, never acoustic guesses."""
    parts = [text.strip() for text in texts]
    if not parts or any(not part for part in parts):
        raise VoiceSeamCompactionError("Voice seam copy must be nonempty")
    mapping = derive_narration_boundary_map(" ".join(parts))
    offset = 0
    kinds = []
    for part in parts[:-1]:
        offset += len(part)
        kinds.append(mapping.kind_at(offset))
        offset += 1
    return tuple(kinds)


class VoiceSeamCompactionError(ValueError):
    pass


@dataclass(frozen=True)
class MeasuredSeamCompaction:
    output_path: Path
    raw_sha256: str
    final_sha256: str
    raw_duration_ms: int
    final_duration_ms: int
    cuts: tuple[tuple[int, int, int], ...]  # seam index, raw start ms, raw end ms
    boundary_kinds: tuple[NarrationBoundaryKind, ...] = ()
    skipped: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class VoicePausePolicyApplication:
    output_path: Path
    generation_segments: tuple[dict[str, object], ...]
    evidence: dict[str, object]


def compact_measured_seam_quiet(
    source_path: str | Path, output_path: str | Path, boundaries_ms: Sequence[int], *,
    margin_ms: int = 60, max_cut_ms: int = 300,
    boundary_kinds: Sequence[NarrationBoundaryKind] | None = None,
    compactable_kinds: Sequence[NarrationBoundaryKind] | None = None,
    preserve_seam_indexes: Sequence[int] = (),
    strict: bool = True,
) -> MeasuredSeamCompaction:
    """Keep spoken frames byte-exact; delete only quiet inside a known seam.

    This measures an acoustic interval around an already-known concat point.
    It does not infer word, phrase or punctuation boundaries from a waveform.
    """
    if margin_ms < 40 or max_cut_ms <= 0:
        raise VoiceSeamCompactionError("Voice seam margins or maximum cut are invalid")
    source, output = Path(source_path), Path(output_path)
    if source.resolve() == output.resolve():
        raise VoiceSeamCompactionError("Voice seam compaction must not overwrite the raw master")
    if output.exists():
        raise VoiceSeamCompactionError("Voice seam output already exists")
    try:
        with wave.open(str(source), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 24_000, "NONE"):
                raise VoiceSeamCompactionError("Voice seam compaction requires 24kHz mono PCM16 WAV")
            pcm = wav.readframes(wav.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        raise VoiceSeamCompactionError("Voice seam compaction could not read PCM WAV") from exc
    try:
        quiet, duration_ms = _pcm_quiet(pcm, 24_000, 1)
    except VoiceObservationError as exc:
        raise VoiceSeamCompactionError("Voice seam PCM measurement failed") from exc
    if not boundaries_ms or len(set(boundaries_ms)) != len(boundaries_ms):
        raise VoiceSeamCompactionError("Voice seam compaction needs unique ordered take boundaries")
    if boundary_kinds is not None and (
        len(boundary_kinds) != len(boundaries_ms)
        or any(not isinstance(kind, NarrationBoundaryKind) for kind in boundary_kinds)
    ):
        raise VoiceSeamCompactionError("Voice seam roles must match the ordered take boundaries")
    if compactable_kinds is not None and (
        not compactable_kinds
        or any(not isinstance(kind, NarrationBoundaryKind) for kind in compactable_kinds)
    ):
        raise VoiceSeamCompactionError("Voice seam compactable roles are invalid")
    preserved = tuple(sorted(set(preserve_seam_indexes)))
    if any(type(index) is not int or index < 0 or index >= len(boundaries_ms)
           for index in preserved):
        raise VoiceSeamCompactionError("Voice preserved seams are invalid")
    allowed_kinds = (tuple(compactable_kinds) if compactable_kinds is not None
                     else (NarrationBoundaryKind.CONTINUING,))
    cuts: list[tuple[int, int, int]] = []
    skipped: list[tuple[int, str]] = []
    previous = 0
    for index, boundary in enumerate(boundaries_ms):
        if boundary <= previous or boundary >= duration_ms:
            raise VoiceSeamCompactionError("Voice seam boundary is not an interior ordered point")
        if index in preserved:
            skipped.append((index, "focus_emphasis_boundary"))
            previous = boundary
            continue
        if boundary_kinds is not None and boundary_kinds[index] not in allowed_kinds:
            skipped.append((index, f"boundary_{boundary_kinds[index].value}_kept"))
            previous = boundary
            continue
        matching = [item for item in quiet if item["start_ms"] <= boundary <= item["end_ms"]]
        if not matching:
            if strict:
                raise VoiceSeamCompactionError(f"Voice seam {index} lacks one measured quiet interval")
            skipped.append((index, "no_measured_quiet"))
            previous = boundary
            continue
        if len(matching) != 1:
            if strict:
                raise VoiceSeamCompactionError(f"Voice seam {index} lacks one measured quiet interval")
            skipped.append((index, "ambiguous_measured_quiet"))
            previous = boundary
            continue
        interval = matching[0]
        start, end = interval["start_ms"] + margin_ms, interval["end_ms"] - margin_ms
        if end > start:
            if end - start > max_cut_ms:
                if strict:
                    raise VoiceSeamCompactionError(f"Voice seam {index} exceeds the bounded quiet cut")
                skipped.append((index, "exceeds_max_cut"))
                previous = boundary
                continue
            if start < previous:
                if strict:
                    raise VoiceSeamCompactionError(f"Voice seam {index} exceeds the bounded quiet cut")
                skipped.append((index, "overlaps_previous_seam"))
                previous = boundary
                continue
            cuts.append((index, start, end))
        else:
            skipped.append((index, "measured_quiet_within_margin"))
        previous = boundary
    parts: list[bytes] = []
    cursor_frames = 0
    for _, start_ms, end_ms in cuts:
        start_frames = start_ms * 24
        end_frames = end_ms * 24
        if start_frames < cursor_frames or end_frames > len(pcm) // 2:
            raise VoiceSeamCompactionError("Voice seam cuts overlap or escape the raw audio")
        parts.append(pcm[cursor_frames * 2:start_frames * 2])
        cursor_frames = end_frames
    parts.append(pcm[cursor_frames * 2:])
    compacted = b"".join(parts)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        with wave.open(stream, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24_000)
            wav.writeframes(compacted)
    return MeasuredSeamCompaction(
        output_path=output, raw_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        final_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        raw_duration_ms=duration_ms, final_duration_ms=round(len(compacted) / 2 / 24_000 * 1000),
        cuts=tuple(cuts), boundary_kinds=tuple(boundary_kinds or ()), skipped=tuple(skipped),
    )


def apply_voice_pause_policy(
    source_path: str | Path, output_path: str | Path, *,
    recipe: VoiceGenerationRecipe, text: str,
    generation_segments: Sequence[Mapping[str, object]], sample_rate: int,
    preserve_seam_indexes: Sequence[int] = (),
) -> VoicePausePolicyApplication:
    """Apply the reviewed paragraph-seam policy without changing spoken frames."""
    policy = recipe.seam_policy
    if (sample_rate != 24_000 or policy.method != SEAM_METHOD
            or policy.scope != recipe.composition_policy
            or policy.preserve_spoken_frames is not True
            or policy.insert_silence is not False):
        raise VoiceSeamCompactionError("Voice pause policy cannot be applied to this output")
    if len(generation_segments) < 2 or len(generation_segments) > 100:
        raise VoiceSeamCompactionError("Voice pause policy requires ordered paragraph segments")
    preserved = tuple(sorted(set(preserve_seam_indexes)))
    if any(type(index) is not int or index < 0 or index >= len(generation_segments) - 1
           for index in preserved):
        raise VoiceSeamCompactionError("Voice pause preserved seams are invalid")
    segments = [dict(segment) for segment in generation_segments]
    texts: list[str] = []
    text_cursor = 0
    sample_cursor = 0
    boundaries: list[int] = []
    for segment in segments:
        start, end = segment.get("start_char"), segment.get("end_char")
        first, last = segment.get("start_sample"), segment.get("end_sample")
        if (type(start) is not int or type(end) is not int or type(first) is not int
                or type(last) is not int or start < text_cursor or end <= start
                or last <= first or first != sample_cursor):
            raise VoiceSeamCompactionError("Voice pause segment provenance is invalid")
        part = text[start:end].strip()
        if not part:
            raise VoiceSeamCompactionError("Voice pause segment copy is empty")
        texts.append(part)
        boundaries.append(last * 1000 // sample_rate)
        text_cursor = end
        sample_cursor = last
    try:
        with wave.open(str(source_path), "rb") as source:
            if source.getnframes() != sample_cursor:
                raise VoiceSeamCompactionError("Voice pause segment samples do not match the raw master")
    except (OSError, EOFError, wave.Error) as exc:
        raise VoiceSeamCompactionError("Voice pause policy could not measure the raw master") from exc
    kinds = take_boundary_kinds(texts)
    allowed = tuple(NarrationBoundaryKind(value) for value in policy.eligible_boundary_kinds)
    compaction = compact_measured_seam_quiet(
        source_path, output_path, boundaries[:-1],
        margin_ms=policy.margin_ms_each_side, max_cut_ms=policy.max_cut_ms,
        boundary_kinds=kinds, compactable_kinds=allowed, strict=False,
        preserve_seam_indexes=preserved,
    )
    cuts = tuple((start * sample_rate // 1000, end * sample_rate // 1000)
                 for _, start, end in compaction.cuts)

    def shifted(value: int) -> int:
        return value - sum(max(0, min(value, end) - start) for start, end in cuts)

    for segment in segments:
        segment["final_start_sample"] = shifted(segment["start_sample"])
        segment["final_end_sample"] = shifted(segment["end_sample"])
    cut_evidence = []
    for seam_index, start_ms, end_ms in compaction.cuts:
        segment = segments[seam_index]
        cut_evidence.append({
            "seam_index": seam_index,
            "boundary_char_index": segment["end_char"],
            "boundary_kind": kinds[seam_index].value,
            "raw_boundary_sample": segment["end_sample"],
            "start_ms": start_ms, "end_ms": end_ms,
            "start_sample": start_ms * sample_rate // 1000,
            "end_sample": end_ms * sample_rate // 1000,
            "removed_samples": (end_ms - start_ms) * sample_rate // 1000,
        })
    evidence = {
        "schema_version": "1.0",
        "recipe_id": recipe.recipe_id,
        "state": "applied" if compaction.cuts else "no_cut",
        "scope": policy.scope,
        "method": policy.method,
        "eligible_boundary_kinds": list(policy.eligible_boundary_kinds),
        "margin_ms_each_side": policy.margin_ms_each_side,
        "max_cut_ms": policy.max_cut_ms,
        "preserve_spoken_frames": True,
        "inserted_silence_ms": 0,
        "global_speed_changed": False,
        "raw_sha256": compaction.raw_sha256,
        "final_sha256": compaction.final_sha256,
        "raw_duration_ms": compaction.raw_duration_ms,
        "final_duration_ms": compaction.final_duration_ms,
        "boundary_kinds": [kind.value for kind in kinds],
        "preserved_seam_indexes": list(preserved),
        "cuts": cut_evidence,
        "skipped": [{"seam_index": index, "reason": reason}
                    for index, reason in compaction.skipped],
    }
    return VoicePausePolicyApplication(
        output_path=compaction.output_path,
        generation_segments=tuple(segments),
        evidence=evidence,
    )


__all__ = [
    "MeasuredSeamCompaction", "VoicePausePolicyApplication", "VoiceSeamCompactionError",
    "apply_voice_pause_policy", "compact_measured_seam_quiet", "take_boundary_kinds",
]
