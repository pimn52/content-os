"""Provider-neutral Chinese clause-boundary rules for Voice delivery."""
from __future__ import annotations

from dataclasses import dataclass
import re


RULE_SET_VERSION = "zh-clause-binding-v1"
CONTRASTIVE_CORRECTION_RULE_ID = "contrastive_correction"
PREDICATE_COMPLETION_RULE_ID = "resultative_predicate_completion"

_SENTENCE_DELIMITERS = "。！？!?；;\n"
_CONTINUATION_LEADS = (
    "而", "但", "然后", "再", "又", "才", "就", "还", "也", "却",
    "并且", "而且", "所以", "因此", "因为", "如果", "要是", "只要",
    "为了", "先", "接着", "随后", "同时", "继续", "开始", "准备",
    "可以", "能够", "让", "把", "将", "从", "向", "对", "和", "与",
)
_ACTION_STEMS = tuple(sorted((
    "分", "做", "看", "说", "讲", "写", "改", "修", "调", "整",
    "接", "关", "放", "拿", "穿", "洗", "切", "找", "变", "拆",
    "合", "连", "排", "理", "检查", "整理", "安排", "确认", "处理",
    "验证", "完成", "准备", "关闭", "打开", "连接", "合并",
), key=len, reverse=True))
_RESULT_COMPLETIONS = tuple(sorted((
    "清楚", "明白", "干净", "开", "完", "成", "好", "到", "掉",
), key=len, reverse=True))
_RESULTATIVE_AT_END = re.compile(
    rf"(?P<predicate>(?:{'|'.join(map(re.escape, _ACTION_STEMS))})"
    rf"(?:{'|'.join(map(re.escape, _RESULT_COMPLETIONS))}))$"
)


class VoiceSyntaxError(ValueError):
    pass


@dataclass(frozen=True)
class VoiceSyntaxBoundary:
    char_index: int
    copy_start: int
    copy_end: int
    copy_context: str
    rule_id: str
    reason: str


@dataclass(frozen=True)
class VoiceSyntaxApplication:
    provider_text: str
    applied: tuple[VoiceSyntaxBoundary, ...]


def find_syntax_boundaries(
    copy: str,
    *,
    contrastive_correction: bool = True,
    max_negated_chars: int = 48,
    predicate_completion: bool = True,
    max_clause_chars: int = 60,
) -> tuple[VoiceSyntaxBoundary, ...]:
    """Find conservative syntax-supported sentence boundaries in exact copy."""
    if not isinstance(copy, str) or not copy.strip():
        return ()
    decisions: list[VoiceSyntaxBoundary] = []
    if contrastive_correction:
        decisions.extend(_contrastive_boundaries(copy, max_negated_chars))
    if predicate_completion:
        decisions.extend(_predicate_completion_boundaries(copy, max_clause_chars))
    by_index: dict[int, VoiceSyntaxBoundary] = {}
    for decision in sorted(decisions, key=lambda item: (
        item.char_index,
        0 if item.rule_id == CONTRASTIVE_CORRECTION_RULE_ID else 1,
    )):
        by_index.setdefault(decision.char_index, decision)
    return tuple(by_index[index] for index in sorted(by_index))


def rewrite_syntax_boundaries(
    provider_text: str,
    decisions: tuple[VoiceSyntaxBoundary, ...],
) -> VoiceSyntaxApplication:
    """Turn selected copy commas into sentence boundaries without word edits."""
    if not decisions:
        return VoiceSyntaxApplication(provider_text, ())
    grouped: dict[str, list[VoiceSyntaxBoundary]] = {}
    for decision in decisions:
        grouped.setdefault(decision.copy_context, []).append(decision)
    result = provider_text
    for context, matches in sorted(
        grouped.items(), key=lambda item: item[1][0].copy_start, reverse=True,
    ):
        observed = result.count(context)
        if observed != len(matches):
            raise VoiceSyntaxError("voice_syntax_provider_unbound")
        terminal = "." if context.endswith(",") else "。"
        result = result.replace(context, context[:-1] + terminal + "\n\n")
    return VoiceSyntaxApplication(result, decisions)


def _contrastive_boundaries(
    copy: str, max_negated_chars: int,
) -> tuple[VoiceSyntaxBoundary, ...]:
    if type(max_negated_chars) is not int or not 1 <= max_negated_chars <= 120:
        raise VoiceSyntaxError("voice_syntax_rule_limit_invalid")
    pattern = re.compile(
        rf"(?<!是)(?:并)?不是[^，,。！？!?；;\n]{{1,{max_negated_chars}}}"
        r"[，,](?=(?:而)?是[^，,。！？!?；;\n])"
    )
    result = []
    for match in pattern.finditer(copy):
        char_index = match.end() - 1
        result.append(VoiceSyntaxBoundary(
            char_index=char_index,
            copy_start=match.start(),
            copy_end=match.end(),
            copy_context=match.group(0),
            rule_id=CONTRASTIVE_CORRECTION_RULE_ID,
            reason=(
                "The negated alternative is a complete contrast member; close it "
                "before the affirmed alternative introduced by 是."
            ),
        ))
    return tuple(result)


def _predicate_completion_boundaries(
    copy: str, max_clause_chars: int,
) -> tuple[VoiceSyntaxBoundary, ...]:
    if type(max_clause_chars) is not int or not 1 <= max_clause_chars <= 120:
        raise VoiceSyntaxError("voice_syntax_rule_limit_invalid")
    result = []
    for comma in re.finditer(r"[，,]", copy):
        clause_start = _clause_start(copy, comma.start())
        while clause_start < comma.start() and copy[clause_start].isspace():
            clause_start += 1
        clause = copy[clause_start:comma.start()]
        if not clause or len(clause) > max_clause_chars:
            continue
        predicate = _RESULTATIVE_AT_END.search(clause)
        if predicate is None or predicate.start() <= 0:
            continue
        following = copy[comma.end():].lstrip()
        if not following or following[0] in _SENTENCE_DELIMITERS or following.startswith(_CONTINUATION_LEADS):
            continue
        result.append(VoiceSyntaxBoundary(
            char_index=comma.start(),
            copy_start=clause_start,
            copy_end=comma.end(),
            copy_context=copy[clause_start:comma.end()],
            rule_id=PREDICATE_COMPLETION_RULE_ID,
            reason=(
                "The clause ends in a verb-result completion, so the next clause "
                "must start after the completed predicate rather than inside it."
            ),
        ))
    return tuple(result)


def _clause_start(copy: str, index: int) -> int:
    starts = [
        match.end() for match in re.finditer(
            rf"[：:。！？!?；;\n]", copy[:index],
        )
    ]
    return max(starts, default=0)


__all__ = [
    "CONTRASTIVE_CORRECTION_RULE_ID", "PREDICATE_COMPLETION_RULE_ID",
    "RULE_SET_VERSION", "VoiceSyntaxApplication", "VoiceSyntaxBoundary",
    "VoiceSyntaxError", "find_syntax_boundaries", "rewrite_syntax_boundaries",
]
