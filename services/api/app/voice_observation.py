"""Read-only, source-bound observations of a QA-verified narration.

ASR segment times can locate a copy boundary only when that boundary falls
*between* exactly matched segments. PCM quiet is a separate acoustic fact: it
neither proves the words spoken nor identifies semantic focus or quality.
"""
from __future__ import annotations

from array import array
import hashlib
import io
import math
from pathlib import Path
import re
import subprocess
import sys
import wave

from pydantic import JsonValue

from app.domain.models import AudioAsset, NarrationPerformancePlan, TranscriptSegment
from app.narration_performance import (
    derive_narration_boundary_map,
    narration_copy_fingerprint,
    narration_performance_plan_fingerprint,
    validate_narration_performance_plan,
)
from app.runtime import resolve_local_executable
from app.voice_boundary_alignment import align_voice_boundaries
from app.voice_qa import comparison_tokens


class VoiceObservationError(ValueError):
    """The source/eligibility contract or acoustic decoder cannot be trusted."""


_SENTENCE_CLOSE = re.compile(r"[。！？!?；;]+")
_BOUNDARY_PUNCTUATION = frozenset("。！？!?；;，、,:：")
_QUIET_THRESHOLD_DBFS = -45
_MIN_QUIET_MS = 30
_FRAME_MS = 10
_FFMPEG_TIMEOUT_SECONDS = 120


def observe_voice_performance(audio: AudioAsset, exact_copy: str, *, data_root: Path) -> dict[str, JsonValue]:
    """Measure a verified asset without changing its bytes or asserting U-Voice.

    Rates count ``comparison_tokens`` (CJK characters / ASCII words), not
    linguistic words. They are valid only for comparisons within this copy.
    """

    if not isinstance(audio, AudioAsset):
        raise VoiceObservationError("observation requires an AudioAsset")
    if not isinstance(exact_copy, str) or not exact_copy.strip():
        raise VoiceObservationError("observation requires non-empty exact copy")
    generation = audio.metadata.get("voice_generation")
    if not isinstance(generation, dict) or generation.get("qa_state") != "verified":
        raise VoiceObservationError("observation requires verified Voice QA")
    qa = generation.get("qa")
    if (
        not isinstance(qa, dict)
        or qa.get("qa_state") != "verified"
        or qa.get("copy_coverage") != 1.0
        or qa.get("missing_token_count") != 0
        or qa.get("duplicate_token_count") != 0
    ):
        raise VoiceObservationError("observation requires a complete independent Voice QA report")
    if generation.get("target_text") != exact_copy:
        raise VoiceObservationError("exact copy differs from the verified generation target")

    source = _resolve_source_path(audio.source_file, data_root)
    if not source.is_file():
        raise VoiceObservationError("source audio is missing")
    source_hash = _sha256(source)
    if source_hash != audio.content_hash:
        raise VoiceObservationError("source audio hash differs from AudioAsset content_hash")
    sample_rate, quiet_intervals, duration_ms, pcm_source = _measure_quiet(source)
    # Lossy/container duration may include codec delay that the decoder trims.
    # This is an integrity sanity check, not a speech-duration quality gate.
    duration_tolerance_ms = max(100 if pcm_source.startswith("ffmpeg_") else 30, round(audio.duration_ms * 0.01))
    if abs(duration_ms - audio.duration_ms) > duration_tolerance_ms:
        raise VoiceObservationError("decoded duration differs from AudioAsset duration")

    target_tokens = comparison_tokens(exact_copy)
    alignment, token_edges = _align_segments(audio.transcript_segments, audio.transcript_source, target_tokens, duration_ms)
    boundary_map = derive_narration_boundary_map(exact_copy)
    boundaries = [
        _boundary_observation(item.char_index, item.kind.value, item.rationale, exact_copy,
                              target_tokens, token_edges, quiet_intervals)
        for item in boundary_map.boundaries
    ]
    sentences = _interval_observations(_sentence_ranges(exact_copy), exact_copy, target_tokens,
                                       token_edges, quiet_intervals)
    clause_ends = [item.char_index for item in boundary_map.boundaries
                   if item.char_index > 0 and exact_copy[item.char_index - 1] in _BOUNDARY_PUNCTUATION]
    clauses = _interval_observations(_ranges_from_ends(exact_copy, clause_ends), exact_copy,
                                     target_tokens, token_edges, quiet_intervals)
    pace_pairs = _adjacent_pace(sentences)
    diagnostics = _diagnostics(boundaries)
    limitations = [
        "PCM quiet is not proof of copy, breathing, emphasis, intonation, naturalness or publishability.",
        "Rates count normalized comparison tokens; they are not words per minute or a universal pace target.",
        "10 ms, -45 dBFS quiet detection may include low-energy speech; ASR boundaries are not word timing.",
    ]
    if alignment["state"] != "aligned":
        limitations.append("Copy-to-time alignment is unavailable; sentence/clause timing and punctuation-pause claims are withheld.")
    elif any(item["timing_state"] != "aligned" for item in boundaries):
        limitations.append("Some syntax boundaries fall inside ASR segments and cannot be timed individually.")

    plan_reference = _plan_reference(generation, exact_copy, boundaries, sentences)
    word_alignment = align_voice_boundaries(
        exact_copy, audio.metadata.get("voice_word_timing"), quiet_intervals,
        source_sha256=source_hash, duration_ms=duration_ms,
    )
    return {
        "observation_version": "1.1",
        "audio_asset_id": str(audio.id),
        "source_file": audio.source_file,
        "source_sha256": source_hash,
        "copy_fingerprint": narration_copy_fingerprint(exact_copy),
        "qa_state": "verified",
        "transcript_source": audio.transcript_source,
        "plan_reference": plan_reference,
        "plan_comparison_state": "intent_context_only" if plan_reference["state"] == "matching" else "unavailable",
        "measurement": {
            "source": pcm_source,
            "sample_rate_hz": sample_rate,
            "duration_ms": duration_ms,
            "frame_ms": _FRAME_MS,
            "quiet_threshold_dbfs": _QUIET_THRESHOLD_DBFS,
            "minimum_quiet_ms": _MIN_QUIET_MS,
            "physical_quiet_intervals": quiet_intervals,
        },
        "alignment_state": alignment["state"],
        "alignment": alignment,
        "word_boundary_alignment": word_alignment,
        "syntax_boundaries": boundaries,
        "sentences": sentences,
        "clauses": clauses,
        "adjacent_sentence_pace": pace_pairs,
        "diagnostics": diagnostics,
        "limitations": limitations,
    }


def _resolve_source_path(source_file: str, data_root: Path) -> Path:
    root = Path(data_root).resolve()
    value = Path(source_file)
    if value.is_absolute():
        # Imported assets may predate portable paths. Their bytes still must
        # match the retained AudioAsset hash before any observation is made.
        return value.resolve()
    candidate = root.parent / value if value.parts and value.parts[0].casefold() == root.name.casefold() else root / value
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise VoiceObservationError("portable source path escapes data_root") from exc
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise VoiceObservationError("source audio could not be read") from exc
    return digest.hexdigest()


def _measure_quiet(path: Path) -> tuple[int, list[dict[str, int]], int, str]:
    if path.suffix.casefold() == ".wav":
        try:
            with wave.open(str(path), "rb") as source:
                if source.getcomptype() == "NONE" and source.getsampwidth() == 2 and source.getframerate() > 0:
                    rate, channels = source.getframerate(), source.getnchannels()
                    raw = source.readframes(source.getnframes())
                    intervals, duration = _pcm_quiet(raw, rate, channels)
                    return rate, intervals, duration, "pcm16_wav_10ms_rms"
        except (OSError, EOFError, wave.Error):
            pass  # A valid non-PCM WAV may still be decoded by local FFmpeg.
    command = [resolve_local_executable("ffmpeg"), "-v", "error", "-i", str(path),
               "-f", "wav", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", "-"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=_FFMPEG_TIMEOUT_SECONDS, check=False)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise VoiceObservationError("unsupported audio: local FFmpeg decode unavailable") from exc
    if result.returncode != 0 or not result.stdout:
        raise VoiceObservationError("unsupported audio: local FFmpeg could not decode source")
    try:
        with wave.open(io.BytesIO(result.stdout), "rb") as decoded:
            if decoded.getcomptype() != "NONE" or decoded.getsampwidth() != 2 or decoded.getnchannels() != 1 or decoded.getframerate() != 16_000:
                raise VoiceObservationError("unsupported audio: FFmpeg output is not expected PCM16 mono WAV")
            raw = decoded.readframes(decoded.getnframes())
    except (EOFError, wave.Error) as exc:
        raise VoiceObservationError("unsupported audio: FFmpeg produced invalid PCM WAV") from exc
    intervals, duration = _pcm_quiet(raw, 16_000, 1)
    return 16_000, intervals, duration, "ffmpeg_decoded_pcm16_mono_10ms_rms"


def _pcm_quiet(raw: bytes, rate: int, channels: int) -> tuple[list[dict[str, int]], int]:
    if channels < 1 or len(raw) % (channels * 2):
        raise VoiceObservationError("invalid PCM16 frame layout")
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    sample_frames = len(samples) // channels
    if sample_frames == 0:
        raise VoiceObservationError("decoded PCM audio is empty")
    window_frames = max(1, round(rate * _FRAME_MS / 1000))
    threshold = 32768 * 10 ** (_QUIET_THRESHOLD_DBFS / 20)
    intervals: list[dict[str, int]] = []
    quiet_start: int | None = None
    for start_frame in range(0, sample_frames, window_frames):
        end_frame = min(sample_frames, start_frame + window_frames)
        values = samples[start_frame * channels:end_frame * channels]
        rms = math.sqrt(sum(int(value) * int(value) for value in values) / len(values))
        if rms < threshold:
            if quiet_start is None:
                quiet_start = start_frame
        elif quiet_start is not None:
            _append_quiet(intervals, quiet_start, start_frame, rate)
            quiet_start = None
    if quiet_start is not None:
        _append_quiet(intervals, quiet_start, sample_frames, rate)
    return intervals, round(sample_frames * 1000 / rate)


def _append_quiet(intervals: list[dict[str, int]], start_frame: int, end_frame: int, rate: int) -> None:
    start_ms, end_ms = round(start_frame * 1000 / rate), round(end_frame * 1000 / rate)
    if end_ms - start_ms >= _MIN_QUIET_MS:
        intervals.append({"start_ms": start_ms, "end_ms": end_ms, "duration_ms": end_ms - start_ms})


def _align_segments(
    segments: list[TranscriptSegment], transcript_source: str | None,
    target: tuple[str, ...], duration_ms: int,
) -> tuple[dict[str, JsonValue], dict[int, tuple[int, int]]]:
    unavailable = lambda reason: ({"state": "unavailable", "reason": reason, "method": "exact_ordered_comparison_tokens_at_asr_segment_edges", "segment_count": len(segments)}, {})
    if not transcript_source or not segments:
        return unavailable("timed_asr_evidence_missing")
    if not target:
        return unavailable("copy_has_no_comparison_tokens")
    ordered = sorted(segments, key=lambda segment: (segment.start_ms, segment.end_ms))
    if ordered != segments or any(
        item.start_ms < 0 or item.end_ms > duration_ms or item.end_ms <= item.start_ms
        or (index and item.start_ms < ordered[index - 1].end_ms)
        for index, item in enumerate(ordered)
    ):
        return unavailable("asr_segments_invalid_or_overlapping")
    stream = tuple(token for segment in segments for token in comparison_tokens(segment.text))
    if stream != target or any(not comparison_tokens(segment.text) for segment in segments):
        return unavailable("asr_segment_tokens_do_not_exactly_match_copy")
    edges: dict[int, tuple[int, int]] = {0: (0, segments[0].start_ms)}
    cursor = 0
    gaps: list[dict[str, int]] = []
    for index, segment in enumerate(segments):
        cursor += len(comparison_tokens(segment.text))
        next_start = segments[index + 1].start_ms if index + 1 < len(segments) else duration_ms
        edges[cursor] = (segment.end_ms, next_start)
        if index + 1 < len(segments):
            gaps.append({"after_segment_index": index, "start_ms": segment.end_ms,
                         "end_ms": next_start, "duration_ms": next_start - segment.end_ms})
    return ({"state": "aligned", "reason": None,
             "method": "exact_ordered_comparison_tokens_at_asr_segment_edges",
             "segment_count": len(segments), "asr_gaps": gaps}, edges)


def _token_position(copy: str, char_index: int, target: tuple[str, ...]) -> int | None:
    prefix = comparison_tokens(copy[:char_index])
    return len(prefix) if target[:len(prefix)] == prefix else None


def _boundary_observation(
    char_index: int, kind: str, rationale: str, copy: str,
    tokens: tuple[str, ...], edges: dict[int, tuple[int, int]],
    quiet: list[dict[str, int]],
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {
        "char_index": char_index, "kind": kind, "rationale": rationale,
        "timing_state": "unavailable", "asr_gap_ms": None, "physical_quiet_ms": None,
        "physical_quiet_candidate_ms": None, "physical_quiet_candidate_provenance": None,
        "physical_quiet_candidate_confidence": None,
    }
    position = _token_position(copy, char_index, tokens)
    if position is None or position not in edges or position == 0 or position == len(tokens):
        return result
    start, end = edges[position]
    result["timing_state"] = "aligned"
    result["asr_gap_ms"] = end - start
    # An ASR gap is not quiet. Only PCM windows contained inside the gap can
    # be conservatively attributed to this syntax boundary.
    if end - start >= _MIN_QUIET_MS:
        overlapping = [item for item in quiet if item["start_ms"] < end and item["end_ms"] > start]
        if all(item["start_ms"] >= start and item["end_ms"] <= end for item in overlapping):
            result["physical_quiet_ms"] = sum(item["duration_ms"] for item in overlapping)
    if result["physical_quiet_ms"] is None:
        crossing = [item for item in quiet if item["start_ms"] < start < item["end_ms"]
                    or item["start_ms"] < end < item["end_ms"]]
        if len(crossing) == 1:
            # An ASR edge is a coarse locator, not an acoustic boundary. This
            # candidate stays low-confidence and never enters the hierarchy
            # diagnostic or definite boundary-pause measurements.
            result["physical_quiet_candidate_ms"] = crossing[0]["duration_ms"]
            result["physical_quiet_candidate_provenance"] = "candidate_crosses_asr_edge"
            result["physical_quiet_candidate_confidence"] = "low"
    return result


def _sentence_ranges(copy: str) -> list[tuple[int, int]]:
    return _ranges_from_ends(copy, [match.end() for match in _SENTENCE_CLOSE.finditer(copy)])


def _ranges_from_ends(copy: str, ends: list[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    for end in sorted(set([*ends, len(copy)])):
        if end > start and copy[start:end].strip():
            ranges.append((start, end))
        start = end
    return ranges


def _interval_observations(
    ranges: list[tuple[int, int]], copy: str, tokens: tuple[str, ...],
    edges: dict[int, tuple[int, int]], quiet: list[dict[str, int]],
) -> list[dict[str, JsonValue]]:
    observations: list[dict[str, JsonValue]] = []
    for index, (start_char, end_char) in enumerate(ranges):
        start_position = _token_position(copy, start_char, tokens)
        end_position = _token_position(copy, end_char, tokens)
        count = 0 if start_position is None or end_position is None else end_position - start_position
        item: dict[str, JsonValue] = {
            "index": index, "start_char": start_char, "end_char": end_char,
            "text": copy[start_char:end_char], "comparison_token_count": count,
            "timing_state": "unavailable", "start_ms": None, "end_ms": None,
            "duration_ms": None, "physical_quiet_ms": None,
            "apparent_tokens_per_second": None, "quiet_excluded_tokens_per_second": None,
        }
        if start_position is not None and end_position is not None and count > 0 and start_position in edges and end_position in edges:
            start_ms, end_ms = edges[start_position][1], edges[end_position][0]
            if end_ms > start_ms:
                quiet_ms = sum(max(0, min(end_ms, interval["end_ms"]) - max(start_ms, interval["start_ms"])) for interval in quiet)
                active_ms = end_ms - start_ms - quiet_ms
                item.update({
                    "timing_state": "aligned", "start_ms": start_ms, "end_ms": end_ms,
                    "duration_ms": end_ms - start_ms, "physical_quiet_ms": quiet_ms,
                    "apparent_tokens_per_second": round(count * 1000 / (end_ms - start_ms), 3),
                    "quiet_excluded_tokens_per_second": round(count * 1000 / active_ms, 3) if active_ms > 0 else None,
                })
        observations.append(item)
    return observations


def _adjacent_pace(sentences: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    comparisons: list[dict[str, JsonValue]] = []
    for earlier, later in zip(sentences, sentences[1:]):
        first, second = earlier["apparent_tokens_per_second"], later["apparent_tokens_per_second"]
        comparisons.append({
            "earlier_sentence_index": earlier["index"], "later_sentence_index": later["index"],
            "state": "measured" if isinstance(first, float) and isinstance(second, float) and first > 0 else "unavailable",
            "later_to_earlier_apparent_rate_ratio": round(second / first, 3) if isinstance(first, float) and isinstance(second, float) and first > 0 else None,
        })
    return comparisons


def _diagnostics(boundaries: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    terminal = [item for item in boundaries if item["kind"] == "terminal" and isinstance(item["physical_quiet_ms"], int)]
    continuing = [item for item in boundaries if item["kind"] == "continuing" and isinstance(item["physical_quiet_ms"], int)]
    if not terminal or not continuing:
        return []
    shortest_terminal = min(terminal, key=lambda item: item["physical_quiet_ms"])
    longest_continuing = max(continuing, key=lambda item: item["physical_quiet_ms"])
    if longest_continuing["physical_quiet_ms"] <= shortest_terminal["physical_quiet_ms"]:
        return []
    return [{
        "kind": "review_punctuation_pause_hierarchy",
        "interpretation": "A measured continuing-boundary quiet interval exceeds a measured terminal-boundary interval; review in context, not an automatic failure.",
        "terminal_char_index": shortest_terminal["char_index"],
        "terminal_physical_quiet_ms": shortest_terminal["physical_quiet_ms"],
        "continuing_char_index": longest_continuing["char_index"],
        "continuing_physical_quiet_ms": longest_continuing["physical_quiet_ms"],
    }]


def _plan_reference(
    generation: dict[str, JsonValue], exact_copy: str,
    boundaries: list[dict[str, JsonValue]], sentences: list[dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    performance = generation.get("narration_performance")
    nested = performance.get("plan") if isinstance(performance, dict) else None
    direct = generation.get("performance_plan")
    if nested is None and direct is None:
        return {"state": "absent"}
    payloads = [item for item in (nested, direct) if item is not None]
    plans: list[NarrationPerformancePlan] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            return {"state": "invalid", "reason": "plan_payload_invalid"}
        try:
            plans.append(validate_narration_performance_plan(NarrationPerformancePlan.model_validate(payload), exact_copy))
        except ValueError:
            return {"state": "invalid", "reason": "plan_payload_invalid_or_copy_mismatch"}
    fingerprints = {narration_performance_plan_fingerprint(item) for item in plans}
    if len(fingerprints) != 1:
        return {"state": "invalid", "reason": "conflicting_plan_snapshots"}
    plan = plans[0]
    intents: list[dict[str, JsonValue]] = []
    for cue in plan.cues:
        value = cue.pause or cue.pace or cue.emphasis or cue.rhythm
        intent: dict[str, JsonValue] = {
            "kind": cue.kind.value, "start_char": cue.start_char, "end_char": cue.end_char,
            "semantic_value": value.value if value is not None else None,
            "acoustic_realization_state": "not_assessed",
        }
        if cue.kind.value == "pause":
            matched = next((item for item in boundaries if item["char_index"] == cue.start_char), None)
            intent["syntax_boundary_kind"] = matched["kind"] if matched else None
            intent["physical_quiet_ms_at_boundary"] = matched["physical_quiet_ms"] if matched else None
        elif cue.kind.value == "pace":
            intent["overlapping_sentence_indexes"] = [
                item["index"] for item in sentences
                if cue.start_char < item["end_char"] and item["start_char"] < cue.end_char
            ]
        intents.append(intent)
    return {
        "state": "matching", "plan_fingerprint": narration_performance_plan_fingerprint(plan),
        "copy_fingerprint": plan.copy_fingerprint, "source": plan.source.value,
        "cue_count": len(plan.cues), "cue_intents": intents,
        "snapshot_locations": [name for name, payload in (("narration_performance.plan", nested), ("performance_plan", direct)) if payload is not None],
        "application_state_recorded": performance.get("application_state") if isinstance(performance, dict) else None,
        "interpretation": "Editorial intent is linked to observed timing where available; no cue is verified as acoustically realized.",
    }


__all__ = ["VoiceObservationError", "observe_voice_performance"]
