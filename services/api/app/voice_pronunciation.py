"""Context-aware pronunciation decisions for Chinese polyphonic characters."""
from __future__ import annotations

from dataclasses import dataclass
import re

from pypinyin import Style, pinyin


RULE_SET_VERSION = "zh-common-polyphones-v1"
CHA_RULE_ID = "quality_condition_predicate_cha4"
CHA_EVIDENCE_REFERENCE = "user-voice-feedback-2026-11-07-polyphone-context"

_CLAUSE_END = re.compile(r"(?:[，、,:：。；;！？!?]|的|$)")
_QUALITY_SUBJECTS = (
    "照明", "灯光", "光线", "采光", "信号", "网络", "质量", "品质",
    "条件", "环境", "路况", "效果", "性能", "体验", "音质", "画质",
    "视力", "听力", "成绩", "表现", "状态", "服务", "天气",
)
_DEGREE_MARKERS = (
    "比较", "非常", "特别", "有点", "有些", "不够",
    "很", "较", "太", "更", "最", "不", "也",
)
_NON_POOR_COMPOUNDS = (
    "差异", "差别", "差距", "差额", "差价", "误差", "偏差", "时差",
    "温差", "落差", "等差", "级差", "视差", "色差", "顺差", "逆差",
    "出差", "差旅", "差遣", "差事", "参差", "差不多",
)


@dataclass(frozen=True)
class ContextualPronunciationDecision:
    char_index: int
    character: str
    provider_spelling: str
    reading: str
    copy_context: str
    rule_id: str
    reason: str
    confidence: str
    evidence_reference: str


def resolve_contextual_pronunciation(
    text: str, *, evidence_reference: str = CHA_EVIDENCE_REFERENCE,
) -> tuple[ContextualPronunciationDecision, ...]:
    """Resolve only high-confidence polyphone readings from local syntax context."""
    if not isinstance(text, str) or not text:
        return ()
    decisions = []
    for match in re.finditer("差", text):
        index = match.start()
        if _inside_compound(text, index, _NON_POOR_COMPOUNDS):
            continue
        if not _CLAUSE_END.match(text[index + 1:]):
            continue
        prefix = text[max(0, index - 16):index]
        reduced = prefix
        for marker in _DEGREE_MARKERS:
            if reduced.endswith(marker):
                reduced = reduced[:-len(marker)]
                break
        if not any(reduced.endswith(subject) for subject in _QUALITY_SUBJECTS):
            continue
        readings = pinyin("差", style=Style.TONE3, heteronym=True)
        if not readings or "cha4" not in readings[0]:
            continue
        decisions.append(ContextualPronunciationDecision(
            char_index=index,
            character="差",
            provider_spelling="CHA4",
            reading="chà",
            copy_context=_clause_context(text, index),
            rule_id=CHA_RULE_ID,
            reason="In this clause 差 is a predicate describing poor quality or conditions, not a difference or assignment.",
            confidence="high",
            evidence_reference=evidence_reference,
        ))
    return tuple(decisions)


def apply_contextual_pronunciation(
    text: str, *, evidence_reference: str,
) -> tuple[str, tuple[ContextualPronunciationDecision, ...]]:
    decisions = resolve_contextual_pronunciation(text, evidence_reference=evidence_reference)
    result = text
    for decision in reversed(decisions):
        result = (
            result[:decision.char_index] + decision.provider_spelling
            + result[decision.char_index + 1:]
        )
    return result, decisions


def _inside_compound(text: str, index: int, compounds: tuple[str, ...]) -> bool:
    for compound in compounds:
        offset = 0
        while (found := text.find(compound, offset)) != -1:
            if found <= index < found + len(compound):
                return True
            offset = found + 1
    return False


def _clause_context(text: str, index: int) -> str:
    start = max(0, index - 16)
    boundary = max(
        (match.end() for match in re.finditer(r"[，。！？!?；;：:\n]", text[start:index])),
        default=0,
    )
    return text[start + boundary:index + 1]


__all__ = [
    "CHA_EVIDENCE_REFERENCE", "CHA_RULE_ID", "ContextualPronunciationDecision",
    "RULE_SET_VERSION", "apply_contextual_pronunciation",
    "resolve_contextual_pronunciation",
]
