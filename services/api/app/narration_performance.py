"""Provider-neutral validation for editable narration delivery intent.

The product must be able to say how a speech should be delivered without
pretending that a particular voice adapter can realize it.  This module binds
semantic cues to exact copy and deliberately contains no SSML, audio editing,
or provider controls.
"""
from __future__ import annotations

import hashlib
import json
import re

from app.domain.models import (
    NarrationDeliveryPlan,
    NarrationDeliverySegment,
    NarrationEmphasis,
    NarrationPace,
    NarrationPause,
    NarrationPerformanceCue,
    NarrationPerformanceCueKind,
    NarrationPerformancePlan,
    NarrationPerformancePlanSource,
    NarrationPerformanceSuggestion,
    NarrationPerformanceSuggestionPattern,
    NarrationPerformanceSuggestions,
    NarrationRhythm,
)


class NarrationPerformancePlanError(ValueError):
    """The copy, plan, or their immutable binding is invalid."""


_SENTENCE_BOUNDARY = re.compile(r"[。！？!?；;]+")
_PARALLEL_NEGATIVE_CLAUSE = re.compile(r"(?:没有|缺少|缺乏|不是)(?P<concept>[^，。；、！？!?]{1,24})")
_ENUMERATION_MARKER = re.compile(r"哪些|什么|第一|第二|第三|首先|其次|最后|一是|二是|三是|一方面|另一方面")
_TURN_MARKERS = ("但是", "然而", "不过", "反而", "却", "而不是")
_ASSERTION_MARKERS = ("关键", "核心", "本质", "结论", "应该", "必须", "意味着", "没有", "缺少", "缺乏", "需要")


def suggest_narration_performance(copy: str) -> NarrationPerformanceSuggestions:
    """Suggest reviewable rhetorical direction without acoustic or provider claims.

    This intentionally small local assistant recognizes only transparent copy
    structure: sentence placement, contrast/parallel claims, enumerations and
    assertion boundaries.  It is not a semantic model of speech quality and
    never saves, executes or learns from a suggestion automatically.
    """

    fingerprint = narration_copy_fingerprint(copy)
    sentences = _sentence_ranges(copy)
    if not sentences:
        raise NarrationPerformancePlanError("Narration performance suggestions require non-empty editable copy")
    entries: list[NarrationPerformanceSuggestion] = []
    pause_boundaries: set[int] = set()
    for index, (start_char, end_char) in enumerate(sentences):
        sentence = copy[start_char:end_char]
        concepts = _parallel_concept_ranges(sentence, start_char)
        role, role_pattern, role_rationale = _sentence_rhythm(index, len(sentences), sentence)
        entries.append(_suggestion(
            NarrationPerformanceCue(
                kind=NarrationPerformanceCueKind.RHYTHM,
                start_char=start_char,
                end_char=end_char,
                rhythm=role,
                note=role_rationale,
            ),
            role_pattern,
            role_rationale,
        ))
        if len(concepts) >= 2:
            for concept_start, concept_end in concepts:
                entries.append(_suggestion(
                    NarrationPerformanceCue(
                        kind=NarrationPerformanceCueKind.EMPHASIS,
                    start_char=concept_start,
                    end_char=concept_end,
                    emphasis=NarrationEmphasis.STRONG,
                    note="平行论断：让这个概念单独落下，不把整句读平。",
                ),
                NarrationPerformanceSuggestionPattern.PARALLEL_CLAIM,
                "平行论断承载不同概念；分别突出概念落点，避免整句被读平。",
            ))
        if _is_enumeration(sentence):
            list_start = _enumeration_start(sentence, start_char)
            entries.append(_suggestion(
                NarrationPerformanceCue(
                    kind=NarrationPerformanceCueKind.PACE,
                    start_char=list_start,
                    end_char=end_char,
                    pace=NarrationPace.DRIVEN,
                    note="列举：让这一小段局部推进，但不要带快前后的主张。",
                ),
                NarrationPerformanceSuggestionPattern.ENUMERATION,
                "重复列举可在局部推进，与主张前后的从容落点形成节奏对比。",
            ))
        pause, pause_pattern, pause_note, pause_rationale = _pause_suggestion(sentence, len(concepts))
        if pause is not None:
            assert pause_pattern is not None and pause_note is not None and pause_rationale is not None
            if end_char not in pause_boundaries:
                pause_boundaries.add(end_char)
                entries.append(_suggestion(
                    NarrationPerformanceCue(
                        kind=NarrationPerformanceCueKind.PAUSE,
                        start_char=end_char,
                        end_char=end_char,
                        pause=pause,
                        note=pause_note,
                    ),
                    pause_pattern,
                    pause_rationale,
                ))

    ordered = sorted(entries, key=lambda item: (
        item.cue.start_char,
        item.cue.end_char,
        _cue_kind_order(item.cue.kind),
    ))
    cues = [item.cue for item in ordered]
    plan = build_narration_performance_plan(
        copy,
        delivery_goal="让关键概念落点清晰，重要观点后留出思考空间，列举处保持局部推进。",
        overall_pace=NarrationPace.CONVERSATIONAL,
        cues=cues,
        source=NarrationPerformancePlanSource.ASSISTED,
        evidence_refs=[],
    )
    return NarrationPerformanceSuggestions(
        copy_fingerprint=fingerprint,
        suggested_plan=plan,
        suggestions=ordered,
    )


def _sentence_ranges(copy: str) -> list[tuple[int, int]]:
    """Keep sentence punctuation inside an exact copy interval."""

    ranges: list[tuple[int, int]] = []
    start_char = 0
    for match in _SENTENCE_BOUNDARY.finditer(copy):
        end_char = match.end()
        if copy[start_char:end_char].strip():
            ranges.append((start_char, end_char))
        start_char = end_char
    if copy[start_char:].strip():
        ranges.append((start_char, len(copy)))
    return ranges


def _parallel_concept_ranges(sentence: str, offset: int) -> list[tuple[int, int]]:
    """Return compact concepts from transparent repeated negative clauses."""

    values: list[tuple[int, int]] = []
    for match in _PARALLEL_NEGATIVE_CLAUSE.finditer(sentence):
        raw = match.group("concept")
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        if right <= left:
            continue
        start_char = offset + match.start("concept") + left
        end_char = offset + match.start("concept") + right
        if end_char > start_char:
            values.append((start_char, end_char))
    # Repeated phrasing can produce the same range through a future expanded
    # pattern; keep suggestion anchors unique before constructing the plan.
    return list(dict.fromkeys(values))


def _is_enumeration(sentence: str) -> bool:
    return len(tuple(_ENUMERATION_MARKER.finditer(sentence))) >= 2


def _enumeration_start(sentence: str, offset: int) -> int:
    first = next(iter(_ENUMERATION_MARKER.finditer(sentence)), None)
    return offset if first is None else offset + first.start()


def _is_assertion(sentence: str) -> bool:
    return any(marker in sentence for marker in _ASSERTION_MARKERS)


def _is_turn(sentence: str) -> bool:
    return any(marker in sentence for marker in _TURN_MARKERS)


def _pause_suggestion(
    sentence: str,
    concept_count: int,
) -> tuple[
    NarrationPause | None,
    NarrationPerformanceSuggestionPattern | None,
    str | None,
    str | None,
]:
    """Return one conservative semantic boundary candidate per sentence.

    The cue describes a review opportunity, not a duration.  A claim receives
    a stronger thinking beat; a pure rhetorical turn receives only a brief
    boundary so a list of transitions cannot turn into mechanical silence.
    """

    if concept_count >= 2 or _is_assertion(sentence):
        return (
            NarrationPause.BEAT,
            NarrationPerformanceSuggestionPattern.CLAIM_BOUNDARY,
            "重要观点：说完后留出换气和观众思考的停顿。",
            "这是观点的语义边界；建议在其后留一个可审阅的换气/思考停顿。",
        )
    if _is_turn(sentence):
        return (
            NarrationPause.BRIEF,
            NarrationPerformanceSuggestionPattern.TURN,
            "转折：在进入下一个观点前留一个短边界。",
            "这是修辞转向；建议短暂停顿，让听者感知观点已经改变。",
        )
    return None, None, None, None


def _sentence_rhythm(
    index: int,
    count: int,
    sentence: str,
) -> tuple[NarrationRhythm, NarrationPerformanceSuggestionPattern, str]:
    if index == 0 and count > 1:
        return (
            NarrationRhythm.SETUP,
            NarrationPerformanceSuggestionPattern.OPENING,
            "开场句：先交代铺垫，再进入中心观点。",
        )
    if index == count - 1:
        return (
            NarrationRhythm.LAND,
            NarrationPerformanceSuggestionPattern.LANDING,
            "结尾句：让结论落下，别加速冲过去。",
        )
    if _is_turn(sentence):
        return (
            NarrationRhythm.TURN,
            NarrationPerformanceSuggestionPattern.TURN,
            "转折词：让修辞转向能被听见。",
        )
    return (
        NarrationRhythm.BUILD,
        NarrationPerformanceSuggestionPattern.BUILD,
        "论证句：向下一个观点推进，不把它当作最终落点。",
    )


def _suggestion(
    cue: NarrationPerformanceCue,
    pattern: NarrationPerformanceSuggestionPattern,
    rationale: str,
) -> NarrationPerformanceSuggestion:
    return NarrationPerformanceSuggestion(cue=cue, pattern=pattern, rationale=rationale)


def _cue_kind_order(kind: NarrationPerformanceCueKind) -> int:
    return {
        NarrationPerformanceCueKind.RHYTHM: 0,
        NarrationPerformanceCueKind.EMPHASIS: 1,
        NarrationPerformanceCueKind.PACE: 2,
        NarrationPerformanceCueKind.PAUSE: 3,
    }[kind]


def narration_copy_fingerprint(copy: str) -> str:
    """Return the stable identity of the exact visible narration copy."""

    if not isinstance(copy, str) or not copy.strip():
        raise NarrationPerformancePlanError("Narration performance intent requires non-empty editable copy")
    return hashlib.sha256(copy.encode("utf-8")).hexdigest()


def build_narration_performance_plan(
    copy: str,
    *,
    delivery_goal: str,
    overall_pace: NarrationPace,
    cues: list[NarrationPerformanceCue],
    source: NarrationPerformancePlanSource,
    evidence_refs: list[str],
) -> NarrationPerformancePlan:
    """Bind creator- or assistant-supplied semantic cues to one exact script."""

    plan = NarrationPerformancePlan(
        copy_fingerprint=narration_copy_fingerprint(copy),
        delivery_goal=delivery_goal,
        overall_pace=overall_pace,
        cues=cues,
        source=source,
        evidence_refs=evidence_refs,
    )
    return validate_narration_performance_plan(plan, copy)


def validate_narration_performance_plan(plan: NarrationPerformancePlan, copy: str) -> NarrationPerformancePlan:
    """Validate every cue against its source copy without retargeting guesses."""

    if not isinstance(plan, NarrationPerformancePlan):
        raise NarrationPerformancePlanError("Narration performance plan has an invalid contract")
    fingerprint = narration_copy_fingerprint(copy)
    if plan.copy_fingerprint != fingerprint:
        raise NarrationPerformancePlanError("Narration performance plan is bound to different copy")
    copy_length = len(copy)
    for cue in plan.cues:
        _validate_cue_anchor(cue, copy, copy_length)
    return plan


def narration_performance_plan_fingerprint(plan: NarrationPerformancePlan) -> str:
    """Identify semantic direction as well as its source copy.

    Two plans can be attached to identical copy but have different rhetorical
    direction. A delivery plan must therefore retain a plan fingerprint in
    addition to the copy fingerprint.
    """

    if not isinstance(plan, NarrationPerformancePlan):
        raise NarrationPerformancePlanError("Narration performance plan has an invalid contract")
    encoded = json.dumps(
        plan.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compile_narration_delivery_plan(plan: NarrationPerformancePlan, copy: str) -> NarrationDeliveryPlan:
    """Derive copy-preserving delivery segments without an acoustic claim.

    The compiler simply exposes the editorial structure already chosen in the
    plan. It creates neither durations nor provider parameters, and it never
    changes punctuation or whitespace in the source copy.
    """

    validate_narration_performance_plan(plan, copy)
    copy_length = len(copy)
    boundaries = {0, copy_length}
    pause_at: dict[int, NarrationPerformanceCue] = {}
    for cue in plan.cues:
        if cue.kind is NarrationPerformanceCueKind.PAUSE:
            pause_at[cue.start_char] = cue
            boundaries.add(cue.start_char)
        else:
            boundaries.add(cue.start_char)
            boundaries.add(cue.end_char)

    ordered_boundaries = sorted(boundaries)
    segments: list[NarrationDeliverySegment] = []
    for index, (start_char, end_char) in enumerate(zip(ordered_boundaries, ordered_boundaries[1:])):
        active = _active_range_cues(plan, start_char, end_char)
        pace = _cue_value(active, NarrationPerformanceCueKind.PACE, plan.overall_pace)
        emphasis = _cue_value(active, NarrationPerformanceCueKind.EMPHASIS, None)
        rhythm = _cue_value(active, NarrationPerformanceCueKind.RHYTHM, None)
        pause_cue = pause_at.get(end_char) if end_char < copy_length else None
        cue_notes = [cue.note for cue in active if cue.note]
        if pause_cue is not None and pause_cue.note:
            cue_notes.append(pause_cue.note)
        text = copy[start_char:end_char]
        segments.append(NarrationDeliverySegment(
            index=index,
            start_char=start_char,
            end_char=end_char,
            text=text,
            is_spoken=bool(text.strip()),
            pace=pace,
            emphasis=emphasis,
            rhythm=rhythm,
            pause_after=None if pause_cue is None else pause_cue.pause,
            cue_notes=list(dict.fromkeys(cue_notes)),
        ))

    if "".join(segment.text for segment in segments) != copy:
        raise NarrationPerformancePlanError("Narration delivery plan must preserve exact source copy")
    opening = pause_at.get(0)
    closing = pause_at.get(copy_length)
    return NarrationDeliveryPlan(
        copy_fingerprint=plan.copy_fingerprint,
        performance_plan_fingerprint=narration_performance_plan_fingerprint(plan),
        opening_pause=None if opening is None else opening.pause,
        segments=segments,
        closing_pause=None if closing is None else closing.pause,
    )


def _active_range_cues(
    plan: NarrationPerformancePlan,
    start_char: int,
    end_char: int,
) -> tuple[NarrationPerformanceCue, ...]:
    active: list[NarrationPerformanceCue] = []
    for cue in plan.cues:
        if cue.kind is NarrationPerformanceCueKind.PAUSE:
            continue
        overlaps = cue.start_char < end_char and start_char < cue.end_char
        if not overlaps:
            continue
        if not (cue.start_char <= start_char and end_char <= cue.end_char):
            raise NarrationPerformancePlanError("Narration delivery segment would split a semantic cue")
        active.append(cue)
    return tuple(active)


def _cue_value(
    cues: tuple[NarrationPerformanceCue, ...],
    kind: NarrationPerformanceCueKind,
    default: NarrationPace | NarrationEmphasis | NarrationRhythm | None,
) -> NarrationPace | NarrationEmphasis | NarrationRhythm | None:
    matching = [cue for cue in cues if cue.kind is kind]
    if not matching:
        return default
    if len(matching) != 1:
        raise NarrationPerformancePlanError(f"Narration delivery segment has ambiguous {kind.value} cues")
    cue = matching[0]
    values = {
        NarrationPerformanceCueKind.PACE: cue.pace,
        NarrationPerformanceCueKind.EMPHASIS: cue.emphasis,
        NarrationPerformanceCueKind.RHYTHM: cue.rhythm,
    }
    return values[kind]


def _validate_cue_anchor(cue: NarrationPerformanceCue, copy: str, copy_length: int) -> None:
    if cue.start_char > copy_length or cue.end_char > copy_length:
        raise NarrationPerformancePlanError("Narration performance cue exceeds current copy")
    if cue.kind is NarrationPerformanceCueKind.PAUSE:
        # A pause sits between characters, including a deliberate opening or
        # closing beat. No implicit punctuation or milliseconds are inserted.
        return
    anchored_text = copy[cue.start_char:cue.end_char]
    if not anchored_text.strip():
        raise NarrationPerformancePlanError("Narration performance cue must anchor spoken copy, not whitespace")


__all__ = [
    "NarrationPerformancePlanError",
    "build_narration_performance_plan",
    "compile_narration_delivery_plan",
    "narration_copy_fingerprint",
    "narration_performance_plan_fingerprint",
    "suggest_narration_performance",
    "validate_narration_performance_plan",
]
