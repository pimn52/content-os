"""Versioned Voice quality anchors reused by ordinary production."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Sequence
from uuid import UUID

from pypinyin import Style, pinyin

from app.db import AudioAssetRepository
from app.domain.models import VoiceProfile, VoiceReferenceWindowSelection
from app.voice_pronunciation import RULE_SET_VERSION, apply_contextual_pronunciation
from app.voice_qa import voice_human_review_status
from app.voice_syntax import (
    CONTRASTIVE_CORRECTION_RULE_ID,
    PREDICATE_COMPLETION_RULE_ID,
    RULE_SET_VERSION as SYNTAX_RULE_SET_VERSION,
    VoiceSyntaxError,
    find_syntax_boundaries,
    rewrite_syntax_boundaries,
)


RECIPE_VERSION = 6
REVIEW_DIMENSIONS = ("likeness", "naturalness", "emphasis", "pace", "pauses", "rhythm")
PAUSE_LEVELS = {
    "phrase_internal": "continuous_no_cut",
    "comma": "short_continuing_boundary",
    "sentence": "natural_close_longer_than_comma",
    "paragraph": "allow_one_beat",
    "key_point": "allow_one_thinking_beat",
    "ending": "natural_landing",
}
PAUSE_INVARIANTS = {
    "sentence_outranks_comma": True,
    "global_speed_is_not_pause_control": True,
    "insert_unmeasured_silence": False,
}
SEAM_METHOD = "pcm16_mono_24khz_10ms_rms_below_minus45dbfs"
SEAM_BOUNDARY_KINDS = frozenset({"terminal", "continuing"})
DELIVERY_REVIEW_STATES = frozenset({"not_required", "pending_user_review", "approved"})
_PINYIN_TONE = re.compile(r"[A-Z]+[1-5]")
_CJK = re.compile(r"[\u3400-\u9fff]")


class VoiceRecipeError(ValueError):
    pass


@dataclass(frozen=True)
class VoiceGenerationRecipe:
    recipe_id: str
    quality_anchor_audio_id: UUID
    quality_anchor_sha256: str
    quality_anchor_evidence_reference: str
    human_review: Mapping[str, object]
    reference_window: VoiceReferenceWindowSelection
    runtime_label: str
    runtime_parameters_sha256: str
    composition_policy: str
    pause_hierarchy: "VoicePauseHierarchy"
    seam_policy: "VoiceSeamPolicy"
    delivery_adjustments: "VoiceDeliveryAdjustments"


@dataclass(frozen=True)
class VoicePauseHierarchy:
    levels: Mapping[str, str]
    invariants: Mapping[str, bool]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class VoiceSeamPolicy:
    scope: str
    method: str
    eligible_boundary_kinds: tuple[str, ...]
    margin_ms_each_side: int
    max_cut_ms: int
    preserve_spoken_frames: bool
    insert_silence: bool
    evidence_reference: str


@dataclass(frozen=True)
class VoiceDeliveryAdjustment:
    copy_text: str
    provider_text: str
    reason: str
    evidence_reference: str


@dataclass(frozen=True)
class VoiceDeliveryAdjustments:
    review_state: str
    punctuation_overrides: tuple[VoiceDeliveryAdjustment, ...]
    pronunciation_overrides: tuple[VoiceDeliveryAdjustment, ...]
    generation_breaks: tuple["VoiceGenerationBreak", ...]
    copula_complement_break: "CopulaComplementBreakPolicy"
    contextual_pronunciation: "ContextualPronunciationPolicy"
    contrastive_correction_break: "ContrastiveCorrectionBreakPolicy"
    predicate_completion_break: "PredicateCompletionBreakPolicy"
    syntax_rule_set_version: str

    @property
    def configured(self) -> bool:
        return bool(
            self.punctuation_overrides or self.pronunciation_overrides
            or self.generation_breaks or self.copula_complement_break.enabled
            or self.contextual_pronunciation.enabled
            or self.contrastive_correction_break.enabled
            or self.predicate_completion_break.enabled
        )


@dataclass(frozen=True)
class VoiceGenerationBreak:
    copy_break_after: str
    provider_break_after: str
    reason: str
    evidence_reference: str


@dataclass(frozen=True)
class CopulaComplementBreakPolicy:
    enabled: bool
    max_complement_chars: int
    reason: str
    evidence_reference: str


@dataclass(frozen=True)
class ContextualPronunciationPolicy:
    enabled: bool
    rule_set_version: str
    evidence_reference: str


@dataclass(frozen=True)
class ContrastiveCorrectionBreakPolicy:
    enabled: bool
    max_negated_chars: int
    reason: str
    evidence_reference: str


@dataclass(frozen=True)
class PredicateCompletionBreakPolicy:
    enabled: bool
    max_clause_chars: int
    reason: str
    evidence_reference: str


@dataclass(frozen=True)
class VoiceDeliveryApplication:
    provider_text: str
    target_break_positions: tuple[int, ...]
    evidence: Mapping[str, object]


def runtime_parameters_sha256(parameters: Mapping[str, object]) -> str:
    encoded = json.dumps(dict(parameters), ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_generation_recipe(profile: VoiceProfile) -> VoiceGenerationRecipe | None:
    value = profile.generation_recipe
    if value is None:
        return None
    required = {
        "schema_version", "recipe_id", "version", "quality_anchor_audio_id",
        "quality_anchor_sha256", "quality_anchor_evidence_reference", "human_review",
        "reference_window", "runtime_label", "runtime_parameters_sha256",
        "composition_policy", "pause_hierarchy", "seam_policy", "delivery_adjustments",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        raise VoiceRecipeError("voice_generation_recipe_invalid")
    review = value.get("human_review")
    if (value.get("schema_version") != "0.5" or type(value.get("version")) is not int
        or value["version"] != RECIPE_VERSION or not isinstance(value.get("recipe_id"), str)
        or not value["recipe_id"].strip() or not isinstance(review, dict)
        or review.get("approved") is not True
        or any(review.get(key) != "pass" for key in REVIEW_DIMENSIONS)
        or not isinstance(value.get("quality_anchor_sha256"), str)
        or len(value["quality_anchor_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in value["quality_anchor_sha256"])
        or not isinstance(value.get("quality_anchor_evidence_reference"), str)
        or not value["quality_anchor_evidence_reference"].strip()
        or not isinstance(value.get("runtime_label"), str) or not value["runtime_label"].strip()
        or not isinstance(value.get("runtime_parameters_sha256"), str)
        or len(value["runtime_parameters_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in value["runtime_parameters_sha256"])
        or value.get("composition_policy") != "paragraph_spans"):
        raise VoiceRecipeError("voice_generation_recipe_invalid")
    try:
        anchor_id = UUID(str(value["quality_anchor_audio_id"]))
        window = VoiceReferenceWindowSelection.model_validate(value["reference_window"])
        pause_hierarchy = _parse_pause_hierarchy(value["pause_hierarchy"])
        seam_policy = _parse_seam_policy(value["seam_policy"])
        delivery_adjustments = _parse_delivery_adjustments(value["delivery_adjustments"])
    except (ValueError, TypeError):
        raise VoiceRecipeError("voice_generation_recipe_invalid") from None
    if seam_policy.scope != value["composition_policy"]:
        raise VoiceRecipeError("voice_generation_recipe_invalid")
    return VoiceGenerationRecipe(
        recipe_id=value["recipe_id"].strip(),
        quality_anchor_audio_id=anchor_id,
        quality_anchor_sha256=value["quality_anchor_sha256"],
        quality_anchor_evidence_reference=value["quality_anchor_evidence_reference"].strip(),
        human_review=dict(review),
        reference_window=window,
        runtime_label=value["runtime_label"].strip(),
        runtime_parameters_sha256=value["runtime_parameters_sha256"],
        composition_policy=value["composition_policy"],
        pause_hierarchy=pause_hierarchy,
        seam_policy=seam_policy,
        delivery_adjustments=delivery_adjustments,
    )


def _parse_pause_hierarchy(value: object) -> VoicePauseHierarchy:
    if not isinstance(value, dict) or set(value) != {"schema_version", "levels", "invariants", "evidence_refs"}:
        raise ValueError()
    levels = value["levels"]
    invariants = value["invariants"]
    evidence_refs = _string_list(value["evidence_refs"])
    if (value["schema_version"] != "1.0" or not isinstance(levels, dict)
            or dict(levels) != PAUSE_LEVELS or not isinstance(invariants, dict)
            or dict(invariants) != PAUSE_INVARIANTS or not evidence_refs):
        raise ValueError()
    return VoicePauseHierarchy(dict(levels), dict(invariants), evidence_refs)


def _parse_seam_policy(value: object) -> VoiceSeamPolicy:
    required = {
        "scope", "method", "eligible_boundary_kinds", "margin_ms_each_side",
        "max_cut_ms", "preserve_spoken_frames", "insert_silence", "evidence_reference",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError()
    kinds = _string_list(value["eligible_boundary_kinds"])
    margin = value["margin_ms_each_side"]
    maximum = value["max_cut_ms"]
    evidence = value["evidence_reference"]
    if (value["scope"] != "paragraph_spans" or value["method"] != SEAM_METHOD
            or not kinds or set(kinds) - SEAM_BOUNDARY_KINDS
            or type(margin) is not int or not 40 <= margin <= 200
            or type(maximum) is not int or not 40 <= maximum <= 500
            or value["preserve_spoken_frames"] is not True
            or value["insert_silence"] is not False
            or not isinstance(evidence, str) or not evidence.strip()):
        raise ValueError()
    return VoiceSeamPolicy(
        scope=value["scope"], method=value["method"], eligible_boundary_kinds=kinds,
        margin_ms_each_side=margin, max_cut_ms=maximum,
        preserve_spoken_frames=True, insert_silence=False,
        evidence_reference=evidence.strip(),
    )


def _parse_delivery_adjustments(value: object) -> VoiceDeliveryAdjustments:
    required = {
        "schema_version", "review_state", "punctuation_overrides",
        "pronunciation_overrides", "generation_breaks", "copula_complement_break",
        "contextual_pronunciation", "contrastive_correction_break",
        "predicate_completion_break", "syntax_rule_set_version",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError()
    if (
        value["schema_version"] != "1.4"
        or value["review_state"] not in DELIVERY_REVIEW_STATES
        or value["syntax_rule_set_version"] != SYNTAX_RULE_SET_VERSION
    ):
        raise ValueError()
    punctuation = _parse_delivery_adjustment_list(value["punctuation_overrides"], pronunciation=False)
    pronunciation = _parse_delivery_adjustment_list(value["pronunciation_overrides"], pronunciation=True)
    generation_breaks = _parse_generation_break_list(value["generation_breaks"])
    copula_policy = _parse_copula_complement_break(value["copula_complement_break"])
    contextual_pronunciation = _parse_contextual_pronunciation(value["contextual_pronunciation"])
    contrastive_correction = _parse_contrastive_correction_break(
        value["contrastive_correction_break"],
    )
    predicate_completion = _parse_predicate_completion_break(
        value["predicate_completion_break"],
    )
    return VoiceDeliveryAdjustments(
        review_state=value["review_state"],
        punctuation_overrides=punctuation,
        pronunciation_overrides=pronunciation,
        generation_breaks=generation_breaks,
        copula_complement_break=copula_policy,
        contextual_pronunciation=contextual_pronunciation,
        contrastive_correction_break=contrastive_correction,
        predicate_completion_break=predicate_completion,
        syntax_rule_set_version=value["syntax_rule_set_version"],
    )


def _parse_delivery_adjustment_list(value: object, *, pronunciation: bool) -> tuple[VoiceDeliveryAdjustment, ...]:
    required = {"copy_text", "provider_text", "reason", "evidence_reference"}
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError()
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError()
        fields = tuple(item[key] for key in ("copy_text", "provider_text", "reason", "evidence_reference"))
        if any(not isinstance(field, str) or not field.strip() for field in fields):
            raise ValueError()
        adjustment = VoiceDeliveryAdjustment(
            copy_text=item["copy_text"],
            provider_text=item["provider_text"],
            reason=item["reason"].strip(),
            evidence_reference=item["evidence_reference"].strip(),
        )
        if adjustment.copy_text == adjustment.provider_text:
            raise ValueError()
        if pronunciation:
            if not _valid_pronunciation_override(adjustment):
                raise ValueError()
        elif (_plain_text(adjustment.copy_text) != _plain_text(adjustment.provider_text)
              or not re.search(r"[\W_]", adjustment.copy_text + adjustment.provider_text, flags=re.UNICODE)):
            raise ValueError()
        result.append(adjustment)
    return tuple(result)


def _parse_generation_break_list(value: object) -> tuple[VoiceGenerationBreak, ...]:
    required = {"copy_break_after", "provider_break_after", "reason", "evidence_reference"}
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError()
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError()
        fields = tuple(item[key] for key in required)
        if any(not isinstance(field, str) or not field.strip() for field in fields):
            raise ValueError()
        punctuation = ("，", ",", "：", ":", "。", "！", "？", "!", "?", "；", ";")
        if (not item["copy_break_after"].endswith(punctuation)
                or not item["provider_break_after"].endswith(punctuation)):
            raise ValueError()
        result.append(VoiceGenerationBreak(
            copy_break_after=item["copy_break_after"],
            provider_break_after=item["provider_break_after"],
            reason=item["reason"].strip(),
            evidence_reference=item["evidence_reference"].strip(),
        ))
    return tuple(result)


def _parse_copula_complement_break(value: object) -> CopulaComplementBreakPolicy:
    required = {"enabled", "max_complement_chars", "reason", "evidence_reference"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError()
    enabled = value["enabled"]
    maximum = value["max_complement_chars"]
    reason = value["reason"]
    evidence = value["evidence_reference"]
    if (
        type(enabled) is not bool or type(maximum) is not int or not 1 <= maximum <= 24
        or not isinstance(reason, str) or not reason.strip()
        or not isinstance(evidence, str) or not evidence.strip()
    ):
        raise ValueError()
    return CopulaComplementBreakPolicy(
        enabled=enabled, max_complement_chars=maximum,
        reason=reason.strip(), evidence_reference=evidence.strip(),
    )


def _parse_contextual_pronunciation(value: object) -> ContextualPronunciationPolicy:
    required = {"enabled", "rule_set_version", "evidence_reference"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError()
    enabled = value["enabled"]
    version = value["rule_set_version"]
    evidence = value["evidence_reference"]
    if (
        type(enabled) is not bool or not isinstance(version, str) or not version.strip()
        or not isinstance(evidence, str) or not evidence.strip()
        or version.strip() != RULE_SET_VERSION
    ):
        raise ValueError()
    return ContextualPronunciationPolicy(
        enabled=enabled, rule_set_version=version.strip(),
        evidence_reference=evidence.strip(),
    )


def _parse_contrastive_correction_break(value: object) -> ContrastiveCorrectionBreakPolicy:
    required = {"enabled", "max_negated_chars", "reason", "evidence_reference"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError()
    enabled = value["enabled"]
    maximum = value["max_negated_chars"]
    reason = value["reason"]
    evidence = value["evidence_reference"]
    if (
        type(enabled) is not bool or type(maximum) is not int or not 1 <= maximum <= 120
        or not isinstance(reason, str) or not reason.strip()
        or not isinstance(evidence, str) or not evidence.strip()
    ):
        raise ValueError()
    return ContrastiveCorrectionBreakPolicy(
        enabled=enabled, max_negated_chars=maximum,
        reason=reason.strip(), evidence_reference=evidence.strip(),
    )


def _parse_predicate_completion_break(value: object) -> PredicateCompletionBreakPolicy:
    required = {"enabled", "max_clause_chars", "reason", "evidence_reference"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError()
    enabled = value["enabled"]
    maximum = value["max_clause_chars"]
    reason = value["reason"]
    evidence = value["evidence_reference"]
    if (
        type(enabled) is not bool or type(maximum) is not int or not 1 <= maximum <= 120
        or not isinstance(reason, str) or not reason.strip()
        or not isinstance(evidence, str) or not evidence.strip()
    ):
        raise ValueError()
    return PredicateCompletionBreakPolicy(
        enabled=enabled, max_clause_chars=maximum,
        reason=reason.strip(), evidence_reference=evidence.strip(),
    )


def _valid_pronunciation_override(adjustment: VoiceDeliveryAdjustment) -> bool:
    if not _PINYIN_TONE.search(adjustment.provider_text):
        # A reviewed homophone alias is provider-only text, not a rewrite of
        # the visible copy. It must have the same default pronunciation
        # character by character; this is useful when numeric pinyin controls
        # are not honored consistently by a local provider.
        if (
            len(adjustment.copy_text) != len(adjustment.provider_text)
            or not all(
                _CJK.fullmatch(copy_character) and _CJK.fullmatch(provider_character)
                for copy_character, provider_character in zip(
                    adjustment.copy_text, adjustment.provider_text,
                )
            )
        ):
            return False
        copy_readings = pinyin(
            adjustment.copy_text, style=Style.TONE3, heteronym=False,
            neutral_tone_with_five=True, strict=True,
        )
        provider_readings = pinyin(
            adjustment.provider_text, style=Style.TONE3, heteronym=False,
            neutral_tone_with_five=True, strict=True,
        )
        return (
            len(copy_readings) == len(provider_readings)
            and all(
                isinstance(copy_reading, list)
                and isinstance(provider_reading, list)
                and len(copy_reading) == len(provider_reading) == 1
                and isinstance(copy_reading[0], str)
                and copy_reading[0] == provider_reading[0]
                for copy_reading, provider_reading in zip(copy_readings, provider_readings)
            )
        )
    pattern = "".join(
        rf"(?:{re.escape(char)}|[A-Z]+[1-5])" if _CJK.fullmatch(char) else re.escape(char)
        for char in adjustment.copy_text
    )
    return re.fullmatch(pattern, adjustment.provider_text) is not None


def _plain_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def apply_recipe_delivery_adjustments(copy: str, recipe: VoiceGenerationRecipe) -> VoiceDeliveryApplication:
    """Render a provider-only text without changing the visible editorial copy."""
    if not isinstance(copy, str) or not copy.strip():
        raise VoiceRecipeError("voice_delivery_adjustment_copy_invalid")
    matches = []
    for kind, values in (
        ("punctuation", recipe.delivery_adjustments.punctuation_overrides),
        ("pronunciation", recipe.delivery_adjustments.pronunciation_overrides),
    ):
        for adjustment in values:
            indexes = [match.start() for match in re.finditer(re.escape(adjustment.copy_text), copy)]
            if len(indexes) > 1 and kind != "pronunciation":
                raise VoiceRecipeError("voice_delivery_adjustment_ambiguous")
            for start in indexes:
                matches.append((start, start + len(adjustment.copy_text), kind, adjustment))
    matches.sort(key=lambda item: (item[0], item[1]))
    if any(start < previous_end for (_, previous_end, _, _), (start, _, _, _) in zip(matches, matches[1:])):
        raise VoiceRecipeError("voice_delivery_adjustment_overlap")
    parts, cursor, applied = [], 0, []
    for start, end, kind, adjustment in matches:
        parts.extend((copy[cursor:start], adjustment.provider_text))
        cursor = end
        applied.append({
            "kind": kind,
            "copy_text": adjustment.copy_text,
            "provider_text": adjustment.provider_text,
            "reason": adjustment.reason,
            "evidence_reference": adjustment.evidence_reference,
        })
    parts.append(copy[cursor:])
    provider_text = "".join(parts)
    break_matches = []
    for adjustment in recipe.delivery_adjustments.generation_breaks:
        copy_indexes = [
            match.start() for match in re.finditer(re.escape(adjustment.copy_break_after), copy)
        ]
        if len(copy_indexes) > 1:
            raise VoiceRecipeError("voice_delivery_break_ambiguous")
        if not copy_indexes:
            continue
        provider_indexes = [
            match.start()
            for match in re.finditer(re.escape(adjustment.provider_break_after), provider_text)
        ]
        if len(provider_indexes) != 1:
            raise VoiceRecipeError("voice_delivery_break_provider_unbound")
        copy_end = copy_indexes[0] + len(adjustment.copy_break_after)
        provider_end = provider_indexes[0] + len(adjustment.provider_break_after)
        break_matches.append((provider_end, copy_end, adjustment))
    break_matches.sort()
    if any(previous[0] == current[0] or previous[1] == current[1]
           for previous, current in zip(break_matches, break_matches[1:])):
        raise VoiceRecipeError("voice_delivery_break_overlap")
    for provider_end, _, _ in reversed(break_matches):
        provider_text = provider_text[:provider_end] + "\n\n" + provider_text[provider_end:]
    if break_matches:
        applied.extend({
            "kind": "generation_break",
            "copy_text": adjustment.copy_break_after,
            "provider_text": adjustment.provider_break_after,
            "reason": adjustment.reason,
            "evidence_reference": adjustment.evidence_reference,
        } for _, _, adjustment in break_matches)
    syntax_policy = recipe.delivery_adjustments.contrastive_correction_break
    predicate_policy = recipe.delivery_adjustments.predicate_completion_break
    syntax_boundaries = find_syntax_boundaries(
        copy,
        contrastive_correction=syntax_policy.enabled,
        max_negated_chars=syntax_policy.max_negated_chars,
        predicate_completion=predicate_policy.enabled,
        max_clause_chars=predicate_policy.max_clause_chars,
    )
    contrastive_guard_boundaries = find_syntax_boundaries(
        copy, predicate_completion=False,
    )
    copula_replacements = []
    copula_policy = recipe.delivery_adjustments.copula_complement_break
    if copula_policy.enabled:
        pattern = re.compile(
            rf"(?P<subject>[^，。！？!?；;\n]{{1,60}})，"
            rf"是(?P<complement>[^，。！？!?；;\n]{{1,{copula_policy.max_complement_chars}}})。"
        )
        grouped: dict[str, dict[str, object]] = {}
        explicitly_adjusted_spans = [(start, end) for start, end, _, _ in matches]
        for match in pattern.finditer(copy):
            if any(match.start() < end and start < match.end()
                   for start, end in explicitly_adjusted_spans):
                continue
            if any(match.start() <= item.char_index < match.end()
                   for item in contrastive_guard_boundaries):
                continue
            source_sentence = match.group(0)
            provider_sentence = f"{match.group('subject')}是{match.group('complement')}。"
            group = grouped.setdefault(source_sentence, {
                "target": [], "provider_sentence": provider_sentence,
            })
            group["target"].append(match)
        for source_sentence, group in grouped.items():
            target_matches = group["target"]
            provider_sentence = group["provider_sentence"]
            provider_positions = [
                match.start()
                for match in re.finditer(re.escape(source_sentence), provider_text)
            ]
            if len(provider_positions) != len(target_matches):
                raise VoiceRecipeError("voice_copula_complement_break_provider_unbound")
            for target_match, provider_start in zip(target_matches, provider_positions):
                copula_replacements.append((
                    provider_start,
                    provider_start + len(source_sentence),
                    target_match.end(),
                    source_sentence,
                    provider_sentence,
                ))
    copula_replacements = list(dict.fromkeys(copula_replacements))
    for provider_start, provider_end, _, _, provider_sentence in sorted(copula_replacements, reverse=True):
        separator = "" if provider_text[provider_end:provider_end + 2] == "\n\n" else "\n\n"
        provider_text = (
            provider_text[:provider_start] + provider_sentence + separator
            + provider_text[provider_end:]
        )
    if copula_replacements:
        applied.extend({
            "kind": "copula_complement",
            "copy_text": copy_text,
            "provider_text": provider_sentence,
            "reason": copula_policy.reason,
            "evidence_reference": copula_policy.evidence_reference,
        } for _, _, _, copy_text, provider_sentence in copula_replacements)
    explicitly_adjusted_spans = [(start, end) for start, end, _, _ in matches]
    explicit_break_ends = {copy_end for _, copy_end, _ in break_matches}
    syntax_boundaries = tuple(
        decision for decision in syntax_boundaries
        if not any(start <= decision.char_index < end for start, end in explicitly_adjusted_spans)
        and decision.copy_end not in explicit_break_ends
    )
    try:
        syntax_application = rewrite_syntax_boundaries(provider_text, syntax_boundaries)
    except VoiceSyntaxError as exc:
        raise VoiceRecipeError(str(exc)) from exc
    provider_text = syntax_application.provider_text
    for decision in syntax_application.applied:
        policy = (
            syntax_policy
            if decision.rule_id == CONTRASTIVE_CORRECTION_RULE_ID
            else predicate_policy
        )
        applied.append({
            "kind": "syntax_boundary",
            "copy_text": decision.copy_context,
            "provider_text": decision.copy_context[:-1] + "。",
            "reason": policy.reason,
            "rule_id": decision.rule_id,
            "evidence_reference": policy.evidence_reference,
        })
    contextual_decisions = ()
    pronunciation_policy = recipe.delivery_adjustments.contextual_pronunciation
    if pronunciation_policy.enabled:
        provider_text, contextual_decisions = apply_contextual_pronunciation(
            provider_text, evidence_reference=pronunciation_policy.evidence_reference,
        )
        applied.extend({
            "kind": "contextual_pronunciation",
            "copy_text": decision.copy_context,
            "provider_text": decision.provider_spelling,
            "reason": decision.reason,
            "rule_id": decision.rule_id,
            "reading": decision.reading,
            "confidence": decision.confidence,
            "evidence_reference": decision.evidence_reference,
        } for decision in contextual_decisions)
    target_break_positions = sorted({
        copy_end for _, copy_end, _ in break_matches
    } | {
        copy_end for _, _, copy_end, _, _ in copula_replacements
    } | {
        decision.copy_end for decision in syntax_application.applied
    })
    return VoiceDeliveryApplication(
        provider_text=provider_text,
        target_break_positions=tuple(target_break_positions),
        evidence={
            "schema_version": "1.0",
            "state": "applied" if applied else "not_applicable",
            "review_state": recipe.delivery_adjustments.review_state,
            "target_copy_sha256": hashlib.sha256(copy.encode("utf-8")).hexdigest(),
            "provider_text_sha256": hashlib.sha256(provider_text.encode("utf-8")).hexdigest(),
            "target_break_positions": target_break_positions,
            "adjustments": applied,
        },
    )


def map_generation_segments_to_copy(
    *, target_text: str, generated_text: str, generation_segments: Sequence[Mapping[str, object]],
    target_break_positions: Sequence[int] = (),
) -> tuple[dict[str, object], ...]:
    """Map provider-only text spans back to the unchanged editorial paragraphs."""
    target_spans = copy_spans_with_breaks(target_text, target_break_positions)
    generated_spans = _paragraph_spans(generated_text)
    if len(target_spans) != len(generated_spans) or len(generation_segments) != len(generated_spans):
        raise VoiceRecipeError("voice_generation_paragraph_mapping_invalid")
    result = []
    for segment, generated_span, target_span in zip(generation_segments, generated_spans, target_spans):
        value = dict(segment)
        if (value.get("start_char"), value.get("end_char")) != generated_span:
            raise VoiceRecipeError("voice_generation_paragraph_mapping_invalid")
        value["start_char"], value["end_char"] = target_span
        result.append(value)
    return tuple(result)


def copy_spans_with_breaks(text: str, break_positions: Sequence[int]) -> tuple[tuple[int, int], ...]:
    positions = tuple(sorted(break_positions))
    if len(set(positions)) != len(positions) or any(type(value) is not int for value in positions):
        raise VoiceRecipeError("voice_generation_paragraph_mapping_invalid")
    spans = list(_paragraph_spans(text))
    for position in positions:
        if any(position in (start, end) for start, end in spans):
            # Delivery rules may reinforce an editorial paragraph boundary.
            # It is already a full generation break, not a second split.
            continue
        matching = [
            index for index, (start, end) in enumerate(spans)
            if start < position < end
        ]
        if len(matching) != 1:
            raise VoiceRecipeError("voice_generation_paragraph_mapping_invalid")
        index = matching[0]
        start, end = spans[index]
        spans[index:index + 1] = [(start, position), (position, end)]
    return tuple(spans)


def _paragraph_spans(text: str) -> tuple[tuple[int, int], ...]:
    if not isinstance(text, str) or not text.strip():
        raise VoiceRecipeError("voice_generation_paragraph_mapping_invalid")
    spans = []
    cursor = 0
    for part in re.split(r"\n\s*\n", text):
        start = text.index(part, cursor)
        cursor = start + len(part)
        left = start + len(part) - len(part.lstrip())
        right = cursor - (len(part) - len(part.rstrip()))
        if left < right:
            spans.append((left, right))
    if not spans or len(spans) > 100:
        raise VoiceRecipeError("voice_generation_paragraph_mapping_invalid")
    return tuple(spans)


def _string_list(value: object) -> tuple[str, ...]:
    if (not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(set(value)) != len(value)):
        raise ValueError()
    return tuple(item.strip() for item in value)


def require_recipe_quality_anchor(db, recipe: VoiceGenerationRecipe, *, voice_profile_id: UUID | None = None):
    audio = AudioAssetRepository(db).get(recipe.quality_anchor_audio_id)
    if (audio is None or audio.content_hash != recipe.quality_anchor_sha256
        or voice_human_review_status(audio) != "approved"):
        raise VoiceRecipeError("voice_generation_recipe_quality_anchor_changed")
    generation = audio.metadata.get("voice_generation")
    review = generation.get("human_review") if isinstance(generation, dict) else None
    if isinstance(review, dict) and any(review.get(key) != "pass" for key in REVIEW_DIMENSIONS):
        raise VoiceRecipeError("voice_generation_recipe_quality_anchor_changed")
    if voice_profile_id is not None and isinstance(generation, dict):
        direct = generation.get("voice_profile_id")
        composition = generation.get("composition")
        source_ids = composition.get("source_take_audio_ids") if isinstance(composition, dict) else None
        source_profile_ids = set()
        if isinstance(source_ids, list):
            repository = AudioAssetRepository(db)
            for source_id in source_ids:
                try:
                    source = repository.get(UUID(str(source_id)))
                except (ValueError, TypeError):
                    source = None
                source_generation = None if source is None else source.metadata.get("voice_generation")
                if isinstance(source_generation, dict) and source_generation.get("voice_profile_id"):
                    source_profile_ids.add(str(source_generation["voice_profile_id"]))
        observed = {str(value) for value in (direct, *source_profile_ids) if value}
        if observed != {str(voice_profile_id)}:
            raise VoiceRecipeError("voice_generation_recipe_profile_mismatch")
    return audio


def require_recipe_execution(profile: VoiceProfile, *, runtime_label: str,
                             parameters: Mapping[str, object]) -> VoiceGenerationRecipe | None:
    recipe = parse_generation_recipe(profile)
    if recipe is None:
        return None
    if (runtime_label != recipe.runtime_label
            or runtime_parameters_sha256(parameters) != recipe.runtime_parameters_sha256):
        raise VoiceRecipeError("voice_generation_recipe_runtime_changed")
    return recipe


def public_recipe_metadata(profile: VoiceProfile) -> dict[str, object] | None:
    recipe = parse_generation_recipe(profile)
    if recipe is None:
        return None
    return {
        "schema_version": "0.5",
        "recipe_id": recipe.recipe_id,
        "version": RECIPE_VERSION,
        "quality_anchor_audio_id": str(recipe.quality_anchor_audio_id),
        "quality_anchor_sha256": recipe.quality_anchor_sha256,
        "quality_anchor_evidence_reference": recipe.quality_anchor_evidence_reference,
        "human_review": dict(recipe.human_review),
        "reference_window": recipe.reference_window.model_dump(mode="json"),
        "runtime_label": recipe.runtime_label,
        "runtime_parameters_sha256": recipe.runtime_parameters_sha256,
        "composition_policy": recipe.composition_policy,
        "pause_hierarchy": {
            "schema_version": "1.0",
            "levels": dict(recipe.pause_hierarchy.levels),
            "invariants": dict(recipe.pause_hierarchy.invariants),
            "evidence_refs": list(recipe.pause_hierarchy.evidence_refs),
        },
        "seam_policy": {
            "scope": recipe.seam_policy.scope,
            "method": recipe.seam_policy.method,
            "eligible_boundary_kinds": list(recipe.seam_policy.eligible_boundary_kinds),
            "margin_ms_each_side": recipe.seam_policy.margin_ms_each_side,
            "max_cut_ms": recipe.seam_policy.max_cut_ms,
            "preserve_spoken_frames": recipe.seam_policy.preserve_spoken_frames,
            "insert_silence": recipe.seam_policy.insert_silence,
            "evidence_reference": recipe.seam_policy.evidence_reference,
        },
        "delivery_adjustments": {
            "schema_version": "1.4",
            "review_state": recipe.delivery_adjustments.review_state,
            "syntax_rule_set_version": recipe.delivery_adjustments.syntax_rule_set_version,
            "punctuation_overrides": [
                {
                    "copy_text": item.copy_text,
                    "provider_text": item.provider_text,
                    "reason": item.reason,
                    "evidence_reference": item.evidence_reference,
                }
                for item in recipe.delivery_adjustments.punctuation_overrides
            ],
            "pronunciation_overrides": [
                {
                    "copy_text": item.copy_text,
                    "provider_text": item.provider_text,
                    "reason": item.reason,
                    "evidence_reference": item.evidence_reference,
                }
                for item in recipe.delivery_adjustments.pronunciation_overrides
            ],
            "generation_breaks": [
                {
                    "copy_break_after": item.copy_break_after,
                    "provider_break_after": item.provider_break_after,
                    "reason": item.reason,
                    "evidence_reference": item.evidence_reference,
                }
                for item in recipe.delivery_adjustments.generation_breaks
            ],
            "copula_complement_break": {
                "enabled": recipe.delivery_adjustments.copula_complement_break.enabled,
                "max_complement_chars": recipe.delivery_adjustments.copula_complement_break.max_complement_chars,
                "reason": recipe.delivery_adjustments.copula_complement_break.reason,
                "evidence_reference": recipe.delivery_adjustments.copula_complement_break.evidence_reference,
            },
            "contextual_pronunciation": {
                "enabled": recipe.delivery_adjustments.contextual_pronunciation.enabled,
                "rule_set_version": recipe.delivery_adjustments.contextual_pronunciation.rule_set_version,
                "evidence_reference": recipe.delivery_adjustments.contextual_pronunciation.evidence_reference,
            },
            "contrastive_correction_break": {
                "enabled": recipe.delivery_adjustments.contrastive_correction_break.enabled,
                "max_negated_chars": recipe.delivery_adjustments.contrastive_correction_break.max_negated_chars,
                "reason": recipe.delivery_adjustments.contrastive_correction_break.reason,
                "evidence_reference": recipe.delivery_adjustments.contrastive_correction_break.evidence_reference,
            },
            "predicate_completion_break": {
                "enabled": recipe.delivery_adjustments.predicate_completion_break.enabled,
                "max_clause_chars": recipe.delivery_adjustments.predicate_completion_break.max_clause_chars,
                "reason": recipe.delivery_adjustments.predicate_completion_break.reason,
                "evidence_reference": recipe.delivery_adjustments.predicate_completion_break.evidence_reference,
            },
        },
    }


__all__ = [
    "PAUSE_INVARIANTS", "PAUSE_LEVELS", "RECIPE_VERSION", "REVIEW_DIMENSIONS",
    "ContextualPronunciationPolicy", "ContrastiveCorrectionBreakPolicy",
    "CopulaComplementBreakPolicy", "DELIVERY_REVIEW_STATES", "PredicateCompletionBreakPolicy",
    "SEAM_BOUNDARY_KINDS", "SEAM_METHOD",
    "VoiceDeliveryAdjustment", "VoiceDeliveryAdjustments", "VoiceDeliveryApplication",
    "VoiceGenerationRecipe", "VoicePauseHierarchy", "VoiceRecipeError", "VoiceSeamPolicy",
    "apply_recipe_delivery_adjustments", "copy_spans_with_breaks", "map_generation_segments_to_copy",
    "parse_generation_recipe",
    "public_recipe_metadata", "require_recipe_execution", "require_recipe_quality_anchor",
    "runtime_parameters_sha256",
]
