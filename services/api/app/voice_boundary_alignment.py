"""Conservative punctuation-to-time alignment from independently retained ASR words.

An exact normalized ASR word stream locates text positions; it does not
replace independent Voice QA or prove the semantic/acoustic quality of speech.
"""
from __future__ import annotations

from pydantic import JsonValue
from uuid import UUID

from app.db import JobRepository
from app.domain.models import AudioAsset
from app.narration_performance import derive_narration_boundary_map
from app.voice_qa import comparison_tokens


def align_voice_boundaries(
    copy: str,
    word_evidence: object,
    quiet: list[dict[str, int]],
    *,
    source_sha256: str,
    duration_ms: int,
) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {
        "version": "1.0", "source_sha256": source_sha256,
        "method": "exact_ordered_asr_word_tokens_plus_pcm_quiet",
        "state": "unavailable", "reason": None, "boundaries": [],
        "limitations": [
            "Word timestamps are ASR estimates, not proof of copy or exact acoustic onset/offset.",
            "PCM quiet is not proof of words, breath, emphasis or publishability.",
            "Only PCM quiet wholly inside an exact-copy word gap is definite boundary quiet.",
        ],
    }

    def unavailable(reason: str) -> dict[str, JsonValue]:
        base["reason"] = reason
        return base

    if not isinstance(word_evidence, dict) or word_evidence.get("source_sha256") != source_sha256:
        return unavailable("missing_or_stale_source_bound_word_evidence")
    raw_words = word_evidence.get("words")
    if not isinstance(raw_words, list) or not raw_words:
        return unavailable("timed_asr_words_missing")
    words: list[tuple[int, int, tuple[str, ...]]] = []
    stream: list[str] = []
    for raw in raw_words:
        if not isinstance(raw, dict):
            return unavailable("asr_word_invalid")
        start, end, value = raw.get("start_ms"), raw.get("end_ms"), raw.get("text")
        if (isinstance(start, bool) or not isinstance(start, int) or
            isinstance(end, bool) or not isinstance(end, int) or
            start < 0 or end <= start or end > duration_ms or
            words and start < words[-1][1] or not isinstance(value, str)):
            return unavailable("asr_word_invalid_or_overlapping")
        tokens = comparison_tokens(value)
        if not tokens:
            return unavailable("asr_word_has_no_comparison_tokens")
        words.append((start, end, tokens))
        stream.extend(tokens)
    target = comparison_tokens(copy)
    if not target or tuple(stream) != target:
        return unavailable("asr_word_tokens_do_not_exactly_match_copy")

    edges: dict[int, tuple[int, int]] = {}
    cursor = 0
    for index, (_start, end, tokens) in enumerate(words[:-1]):
        cursor += len(tokens)
        edges[cursor] = (end, words[index + 1][0])

    boundaries: list[dict[str, JsonValue]] = []
    for item in derive_narration_boundary_map(copy).boundaries:
        position = len(comparison_tokens(copy[:item.char_index]))
        entry: dict[str, JsonValue] = {
            "char_index": item.char_index, "kind": item.kind.value,
            "timing_state": "unavailable", "reason": "boundary_inside_asr_word_or_at_copy_edge",
            "word_gap_start_ms": None, "word_gap_end_ms": None,
            "physical_quiet_ms": None, "physical_quiet_candidate_ms": None,
        }
        if 0 < position < len(target) and position in edges:
            start, end = edges[position]
            entry.update({"timing_state": "aligned", "reason": None,
                          "word_gap_start_ms": start, "word_gap_end_ms": end})
            if end - start >= 30:
                intersecting = [q for q in quiet if q["start_ms"] < end and q["end_ms"] > start]
                if all(q["start_ms"] >= start and q["end_ms"] <= end for q in intersecting):
                    entry["physical_quiet_ms"] = sum(q["duration_ms"] for q in intersecting)
                elif len(intersecting) == 1:
                    entry["physical_quiet_candidate_ms"] = intersecting[0]["duration_ms"]
            elif start == end:
                crossing = [q for q in quiet if q["start_ms"] < start < q["end_ms"]]
                if len(crossing) == 1:
                    entry["physical_quiet_candidate_ms"] = crossing[0]["duration_ms"]
        boundaries.append(entry)
    base["state"] = "aligned"
    base["reason"] = None
    base["word_count"] = len(words)
    base["boundaries"] = boundaries
    return base


def voice_asset_project_id(audio: AudioAsset, jobs: JobRepository) -> UUID | None:
    """Resolve the owning project from direct provenance or its source Job."""
    generation = audio.metadata.get("voice_generation")
    if not isinstance(generation, dict):
        return None
    direct = generation.get("project_id")
    if isinstance(direct, str):
        try:
            return UUID(direct)
        except ValueError:
            return None
    source_id = generation.get("job_id")
    if isinstance(source_id, str):
        try:
            source = jobs.get(UUID(source_id))
        except ValueError:
            return None
        return source.project_id if source is not None else None
    return None
