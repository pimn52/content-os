"""General semantic focus cues and a bounded local Voice emphasis executor.

This module connects editorial focus to provider output without changing the
visible copy. It deliberately does not infer stress acoustically or claim that
louder audio is natural speech. The executor only applies an explicit,
reviewable gain to a Provider-generated segment that exactly matches an
emphasis cue; language quality still requires the normal Voice review.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
import sys
from typing import Mapping, Sequence
import wave

from app.domain.models import (
    NarrationEmphasis,
    NarrationPace,
    NarrationPerformanceCue,
    NarrationPerformanceCueKind,
    NarrationPerformancePlan,
    NarrationPerformancePlanSource,
)
from app.narration_performance import (
    NarrationPerformancePlanError,
    build_narration_performance_plan,
    validate_narration_performance_plan,
)


RULE_SET_VERSION = "zh-focus-emphasis-v1"
EXECUTION_METHOD = "pcm16_mono_24khz_exact_segment_gain"
EDGE_RAMP_MS = 10
MAX_FOCUS_CUES = 8
GAIN_BY_STRENGTH = {
    NarrationEmphasis.LIGHT: 1.06,
    NarrationEmphasis.CLEAR: 1.12,
    NarrationEmphasis.STRONG: 1.20,
}

_SENTENCE_END = frozenset("。！？!?；;\n")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_ENUMERATION_TERM = re.compile(
    r"(?P<term>最后|最终)(?P<punct>[，,：:])"
)
_ENUMERATION_EARLIER = re.compile(
    r"(?:先|首先|第一|一是|再|然后|其次|接下来|另一方面)"
)
_QUANTITY_FRAME = re.compile(
    r"(?P<lead>记住|牢记|掌握|做到|分成|归纳为|只需要|重点是|关键是)?"
    r"(?P<count>[0-9一二两三四五六七八九十百]+)"
    r"(?P<unit>个方面|个阶段|件事|步|点|类|条)"
    r"(?P<punct>[：:]?)"
)
_SEQUENCE_AFTER = re.compile(
    r"(?:先|首先|第一|一是|再|然后|其次|最后|另一方面)"
)
_ASSERTIVE_CLAUSE = re.compile(
    r"(?P<clause>(?:真正的问题|关键|核心|重点)(?:是|在于)"
    r"[^，。！？!?；;\n]{1,18}[。！？!?；;]?)"
)
_CONTRASTIVE_AFFIRMATION = re.compile(
    r"不是[^。！？!?\n]{1,40}[，,]"
    r"(?P<clause>(?:而是|是)[^，。！？!?；;\n]{1,18}[。！？!?；;]?)"
)
_CHINESE_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


class VoiceEmphasisError(ValueError):
    pass


@dataclass(frozen=True)
class VoiceFocusRuleMatch:
    start_char: int
    end_char: int
    rule_id: str
    emphasis: NarrationEmphasis
    rationale: str


@dataclass(frozen=True)
class VoiceFocusTextApplication:
    provider_text: str
    target_break_positions: tuple[int, ...]
    applied: tuple[dict[str, object], ...]
    skipped: tuple[dict[str, object], ...]

    @property
    def evidence(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "rule_set_version": RULE_SET_VERSION,
            "state": "applied" if self.applied else "not_applicable",
            "method": "independent_provider_generation_boundaries",
            "target_break_positions": list(self.target_break_positions),
            "applied": list(self.applied),
            "skipped": list(self.skipped),
        }


@dataclass(frozen=True)
class VoiceFocusAudioApplication:
    output_path: Path
    generation_segments: tuple[dict[str, object], ...]
    focus_segment_indexes: tuple[int, ...]
    evidence: dict[str, object]


def find_voice_focus_rules(copy: str) -> tuple[VoiceFocusRuleMatch, ...]:
    """Find transparent semantic focus structures in exact editorial copy."""
    if not isinstance(copy, str) or not copy.strip():
        raise VoiceEmphasisError("voice emphasis requires non-empty editorial copy")
    values: list[VoiceFocusRuleMatch] = []
    values.extend(_enumeration_terminal_rules(copy))
    values.extend(_quantity_frame_rules(copy))
    values.extend(_pattern_rules(
        copy,
        _ASSERTIVE_CLAUSE,
        rule_id="assertive_clause",
        emphasis=NarrationEmphasis.CLEAR,
        rationale="A transparent assertion lead and its short predicate form one focus unit.",
    ))
    values.extend(_pattern_rules(
        copy,
        _CONTRASTIVE_AFFIRMATION,
        rule_id="contrastive_affirmation",
        emphasis=NarrationEmphasis.CLEAR,
        rationale="The affirmed side of a contrast carries the corrected focus.",
    ))
    ordered = sorted(
        dict.fromkeys(values),
        key=lambda item: (item.start_char, -(item.end_char - item.start_char), item.rule_id),
    )
    selected: list[VoiceFocusRuleMatch] = []
    for item in ordered:
        if any(item.start_char < prior.end_char and prior.start_char < item.end_char
               for prior in selected):
            continue
        selected.append(item)
        if len(selected) >= MAX_FOCUS_CUES:
            break
    return tuple(sorted(selected, key=lambda item: item.start_char))


def automatic_emphasis_plan(copy: str) -> NarrationPerformancePlan | None:
    """Build a reviewable emphasis-only plan for ordinary Voice generation."""
    matches = find_voice_focus_rules(copy)
    if not matches:
        return None
    cues = [
        NarrationPerformanceCue(
            kind=NarrationPerformanceCueKind.EMPHASIS,
            start_char=item.start_char,
            end_char=item.end_char,
            emphasis=item.emphasis,
            note=f"focus_rule={item.rule_id}; {item.rationale}",
        )
        for item in matches
    ]
    return build_narration_performance_plan(
        copy,
        delivery_goal=(
            "把列举收束、数量框架、短断言和对照修正的语义焦点说清楚；"
            "保持完整原文和同一声线，不把其他句子机械加重。"
        ),
        overall_pace=NarrationPace.CONVERSATIONAL,
        cues=cues,
        source=NarrationPerformancePlanSource.ASSISTED,
        evidence_refs=[f"voice-focus-rules:{RULE_SET_VERSION}"],
    )


def emphasis_only(plan: NarrationPerformancePlan | None) -> bool:
    return (
        isinstance(plan, NarrationPerformancePlan)
        and bool(plan.cues)
        and all(cue.kind is NarrationPerformanceCueKind.EMPHASIS for cue in plan.cues)
    )


def apply_focus_text_boundaries(
    copy: str,
    provider_text: str,
    plan: NarrationPerformancePlan,
    *,
    target_break_positions: Sequence[int] = (),
) -> VoiceFocusTextApplication:
    """Split only exact cue phrases into Provider generation units."""
    try:
        validate_narration_performance_plan(plan, copy)
    except NarrationPerformancePlanError as exc:
        raise VoiceEmphasisError(str(exc)) from exc
    if not emphasis_only(plan):
        raise VoiceEmphasisError("voice focus text execution requires an emphasis-only plan")
    if not isinstance(provider_text, str) or not provider_text.strip():
        raise VoiceEmphasisError("voice focus provider text is empty")
    paragraph_spans = _paragraph_spans(copy)
    existing_breaks = tuple(sorted(set(target_break_positions)))
    provider_cursor = 0
    matches: list[tuple[int, int, NarrationPerformanceCue]] = []
    skipped: list[dict[str, object]] = []
    for cue in sorted(plan.cues, key=lambda item: (item.start_char, item.end_char)):
        phrase = copy[cue.start_char:cue.end_char]
        provider_start = provider_text.find(phrase, provider_cursor)
        if provider_start < 0:
            skipped.append({
                "start_char": cue.start_char,
                "end_char": cue.end_char,
                "reason": "provider_text_phrase_changed_or_ambiguous",
            })
            continue
        provider_end = provider_start + len(phrase)
        matches.append((provider_start, provider_end, cue))
        provider_cursor = provider_end
    injected: list[tuple[int, int, NarrationPerformanceCue]] = []
    target_breaks = set(existing_breaks)
    for provider_start, provider_end, cue in matches:
        if _inside_paragraph(paragraph_spans, cue.start_char):
            target_breaks.add(cue.start_char)
        if _inside_paragraph(paragraph_spans, cue.end_char):
            target_breaks.add(cue.end_char)
        injected.append((provider_start, provider_end, cue))
    result = provider_text
    for provider_start, provider_end, _ in sorted(injected, reverse=True):
        if provider_end < len(result) and not _is_paragraph_boundary(result, provider_end):
            result = result[:provider_end] + "\n\n" + result[provider_end:]
        if provider_start > 0 and not _is_paragraph_boundary(result, provider_start):
            result = result[:provider_start] + "\n\n" + result[provider_start:]
    applied = tuple({
        "start_char": cue.start_char,
        "end_char": cue.end_char,
        "copy_text": copy[cue.start_char:cue.end_char],
        "emphasis": cue.emphasis.value if cue.emphasis is not None else None,
        "rule_id": _cue_rule_id(cue),
    } for _, _, cue in injected)
    return VoiceFocusTextApplication(
        provider_text=result,
        target_break_positions=tuple(sorted(target_breaks)),
        applied=applied,
        skipped=tuple(skipped),
    )


def apply_focus_audio_emphasis(
    source_path: str | Path,
    output_path: str | Path,
    *,
    copy: str,
    plan: NarrationPerformancePlan,
    generation_segments: Sequence[Mapping[str, object]],
    sample_rate: int,
    data_root: str | Path | None = None,
) -> VoiceFocusAudioApplication:
    """Boost exact Provider segments while preserving duration and spoken text."""
    try:
        validate_narration_performance_plan(plan, copy)
    except NarrationPerformancePlanError as exc:
        raise VoiceEmphasisError(str(exc)) from exc
    if not emphasis_only(plan):
        raise VoiceEmphasisError("voice focus audio execution requires an emphasis-only plan")
    if sample_rate != 24_000:
        raise VoiceEmphasisError("voice focus audio execution requires 24 kHz PCM")
    source, output = Path(source_path), Path(output_path)
    root = None if data_root is None else Path(data_root).resolve()
    if source.resolve() == output.resolve():
        raise VoiceEmphasisError("voice focus output must not overwrite provider audio")
    if output.exists():
        raise VoiceEmphasisError("voice focus output already exists")
    try:
        with wave.open(str(source), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(),
                    wav.getcomptype()) != (1, 2, 24_000, "NONE"):
                raise VoiceEmphasisError("voice focus requires 24kHz mono PCM16 WAV")
            pcm = wav.readframes(wav.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        raise VoiceEmphasisError("voice focus could not read provider PCM") from exc
    segments = [dict(segment) for segment in generation_segments]
    focused: list[int] = []
    applications: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    mutable = array("h")
    mutable.frombytes(pcm)
    if sys.byteorder != "little":
        mutable.byteswap()
    for index, segment in enumerate(segments):
        matching = _focus_cues_matching_segment(segment, plan)
        if not matching:
            continue
        start_sample = segment.get("start_sample")
        end_sample = segment.get("end_sample")
        if (type(start_sample) is not int or type(end_sample) is not int
                or start_sample < 0 or end_sample <= start_sample
                or end_sample > len(mutable)):
            raise VoiceEmphasisError("voice focus segment samples are invalid")
        strengths = {cue.emphasis for cue in matching if cue.emphasis is not None}
        if len(strengths) != 1:
            raise VoiceEmphasisError("voice focus segment has ambiguous emphasis strength")
        desired = GAIN_BY_STRENGTH[next(iter(strengths))]
        slice_values = mutable[start_sample:end_sample]
        peak = max((abs(value) for value in slice_values), default=0)
        gain = desired if peak == 0 else min(desired, 32767 / peak)
        if gain <= 1.0:
            skipped.append({
                "segment_index": index,
                "reason": "headroom_insufficient",
                "peak": peak,
            })
            continue
        before_rms = _rms(slice_values)
        _apply_constant_gain_with_edge_ramp(
            mutable, start_sample, end_sample, gain, sample_rate,
        )
        after_rms = _rms(mutable[start_sample:end_sample])
        updated_segment = dict(segment)
        if root is not None:
            source_reference = updated_segment.get("source_file")
            if not isinstance(source_reference, str) or not source_reference.strip():
                raise VoiceEmphasisError("voice focus segment source path is unavailable")
            source_part = (root / source_reference).resolve()
            if not source_part.is_relative_to(root) or not source_part.is_file():
                raise VoiceEmphasisError("voice focus segment source path is invalid")
            focus_part = source_part.with_name(source_part.stem + ".focus-emphasis.wav").resolve()
            if focus_part.exists():
                raise VoiceEmphasisError("voice focus segment output already exists")
            frames = array("h", mutable[start_sample:end_sample])
            if sys.byteorder != "little":
                frames.byteswap()
            with focus_part.open("xb") as stream:
                with wave.open(stream, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(24_000)
                    wav.writeframes(frames.tobytes())
            updated_segment["focus_source_file"] = source_reference
            updated_segment["focus_source_sha256"] = updated_segment["sha256"]
            updated_segment["source_file"] = focus_part.relative_to(root).as_posix()
            updated_segment["sha256"] = hashlib.sha256(focus_part.read_bytes()).hexdigest()
            segments[index] = updated_segment
        focused.append(index)
        applications.append({
            "segment_index": index,
            "start_char": segment.get("start_char"),
            "end_char": segment.get("end_char"),
            "start_sample": start_sample,
            "end_sample": end_sample,
            "requested_gain": round(desired, 4),
            "applied_gain": round(gain, 4),
            "applied_gain_db": round(20 * math.log10(gain), 3),
            "rms_before": round(before_rms, 3),
            "rms_after": round(after_rms, 3),
            "rule_ids": sorted({_cue_rule_id(cue) for cue in matching}),
        })
    if not applications:
        for cue in plan.cues:
            if not any(_segment_matches_cue(segment, cue) for segment in segments):
                skipped.append({
                    "start_char": cue.start_char,
                    "end_char": cue.end_char,
                    "reason": "no_exact_generation_segment",
                })
    if sys.byteorder != "little":
        mutable.byteswap()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        with wave.open(stream, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24_000)
            wav.writeframes(mutable.tobytes())
    evidence = {
        "schema_version": "1.0",
        "rule_set_version": RULE_SET_VERSION,
        "method": EXECUTION_METHOD,
        "state": "applied" if applications else "no_focus_applied",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "source_copy_sha256": hashlib.sha256(copy.encode("utf-8")).hexdigest(),
        "duration_preserved": True,
        "visible_copy_changed": False,
        "global_speed_changed": False,
        "edge_ramp_ms": EDGE_RAMP_MS,
        "gain_by_strength": {key.value: value for key, value in GAIN_BY_STRENGTH.items()},
        "focus_segment_indexes": focused,
        "applications": applications,
        "skipped": skipped,
    }
    return VoiceFocusAudioApplication(
        output_path=output,
        generation_segments=tuple(segments),
        focus_segment_indexes=tuple(focused),
        evidence=evidence,
    )


def _enumeration_terminal_rules(copy: str) -> list[VoiceFocusRuleMatch]:
    values: list[VoiceFocusRuleMatch] = []
    for match in _ENUMERATION_TERM.finditer(copy):
        before = copy[:match.start()].rstrip()
        after = copy[match.end():].lstrip()
        sentence_count = len(re.findall(r"[。！？!?；;]", before))
        earlier_markers = len(_ENUMERATION_EARLIER.findall(before))
        if sentence_count < 2 and earlier_markers < 2:
            continue
        if after.startswith(("终于", "总算", "还是", "只能")):
            continue
        values.append(VoiceFocusRuleMatch(
            start_char=match.start(),
            end_char=match.end(),
            rule_id="enumeration_terminal",
            emphasis=NarrationEmphasis.STRONG,
            rationale="A sequence-closing 最后/最终 plus its punctuation is a clear focus unit.",
        ))
    return values


def _quantity_frame_rules(copy: str) -> list[VoiceFocusRuleMatch]:
    values: list[VoiceFocusRuleMatch] = []
    for match in _QUANTITY_FRAME.finditer(copy):
        count = _small_count(match.group("count"))
        if count is None or count < 2:
            continue
        tail_end = min(len(copy), match.end() + 100)
        tail = copy[match.end():tail_end]
        if not match.group("punct") and len(_SEQUENCE_AFTER.findall(tail)) < 2:
            continue
        values.append(VoiceFocusRuleMatch(
            start_char=match.start(),
            end_char=match.end(),
            rule_id="quantity_frame",
            emphasis=NarrationEmphasis.STRONG,
            rationale="A numbered step/category frame introduces the structure the listener must retain.",
        ))
    return values


def _pattern_rules(
    copy: str,
    pattern: re.Pattern[str],
    *,
    rule_id: str,
    emphasis: NarrationEmphasis,
    rationale: str,
) -> list[VoiceFocusRuleMatch]:
    return [
        VoiceFocusRuleMatch(
            start_char=match.start("clause"),
            end_char=match.end("clause"),
            rule_id=rule_id,
            emphasis=emphasis,
            rationale=rationale,
        )
        for match in pattern.finditer(copy)
    ]


def _small_count(value: str) -> int | None:
    if value.isdigit():
        number = int(value)
        return number if number <= 999 else None
    if "百" in value and value != "百":
        return None
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = 1 if not left else _CHINESE_DIGITS.get(left)
        ones = 0 if not right else _CHINESE_DIGITS.get(right)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    if len(value) == 1:
        return _CHINESE_DIGITS.get(value)
    return None


def _paragraph_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for part in re.split(r"\n\s*\n", text):
        start = text.index(part, cursor)
        cursor = start + len(part)
        left = start + len(part) - len(part.lstrip())
        right = cursor - (len(part) - len(part.rstrip()))
        if left < right:
            spans.append((left, right))
    if not spans:
        raise VoiceEmphasisError("voice focus requires non-empty paragraphs")
    return tuple(spans)


def _inside_paragraph(spans: Sequence[tuple[int, int]], position: int) -> bool:
    return any(start < position < end for start, end in spans)


def _is_paragraph_boundary(text: str, position: int) -> bool:
    left = text[:position].rstrip()
    right = text[position:].lstrip()
    return not left or not right or text[:position].endswith("\n\n") or text[position:].startswith("\n\n")


def _cue_rule_id(cue: NarrationPerformanceCue) -> str:
    note = cue.note or ""
    prefix = "focus_rule="
    if note.startswith(prefix):
        return note[len(prefix):].split(";", 1)[0].strip() or "explicit_emphasis"
    return "explicit_emphasis"


def _segment_matches_cue(segment: Mapping[str, object], cue: NarrationPerformanceCue) -> bool:
    return (
        segment.get("start_char") == cue.start_char
        and segment.get("end_char") == cue.end_char
    )


def _focus_cues_matching_segment(
    segment: Mapping[str, object], plan: NarrationPerformancePlan,
) -> tuple[NarrationPerformanceCue, ...]:
    return tuple(cue for cue in plan.cues if _segment_matches_cue(segment, cue))


def _apply_constant_gain_with_edge_ramp(
    samples: array, start: int, end: int, gain: float, sample_rate: int,
) -> None:
    ramp = min((end - start) // 2, sample_rate * EDGE_RAMP_MS // 1000)
    for index in range(start, end):
        local = index - start
        edge = min(local, end - index - 1)
        factor = gain if ramp == 0 or edge >= ramp else 1 + (gain - 1) * (edge + 1) / ramp
        samples[index] = max(-32768, min(32767, int(round(samples[index] * factor))))


def _rms(samples: Sequence[int]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(value * value for value in samples) / len(samples))


__all__ = [
    "EXECUTION_METHOD",
    "GAIN_BY_STRENGTH",
    "RULE_SET_VERSION",
    "VoiceEmphasisError",
    "VoiceFocusAudioApplication",
    "VoiceFocusRuleMatch",
    "VoiceFocusTextApplication",
    "apply_focus_audio_emphasis",
    "apply_focus_text_boundaries",
    "automatic_emphasis_plan",
    "emphasis_only",
    "find_voice_focus_rules",
]
