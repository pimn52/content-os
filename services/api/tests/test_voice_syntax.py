import pytest

from app.voice_syntax import (
    CONTRASTIVE_CORRECTION_RULE_ID,
    PREDICATE_COMPLETION_RULE_ID,
    RULE_SET_VERSION,
    VoiceSyntaxError,
    find_syntax_boundaries,
    rewrite_syntax_boundaries,
)


@pytest.mark.parametrize(
    "copy",
    (
        "这不是跟风，是城市夜间环境真的变了。",
        "问题不是成本，是时间。",
        "他并不是反对你，而是希望方案更稳。",
        "这不是要不要做的问题，是何时做的问题。",
    ),
)
def test_contrastive_correction_closes_negated_member_before_affirmation(copy: str) -> None:
    boundaries = find_syntax_boundaries(copy, predicate_completion=False)

    assert len(boundaries) == 1
    boundary = boundaries[0]
    assert boundary.rule_id == CONTRASTIVE_CORRECTION_RULE_ID
    assert copy[boundary.char_index] in "，,"
    assert copy[boundary.char_index + 1:].startswith(("是", "而是"))


@pytest.mark.parametrize(
    "copy",
    (
        "这不是跟风，也不是盲目扩张。",
        "不是所有变化都要慢，先看证据。",
        "这是不是跟风，是城市环境给出的答案。",
        "问题不是成本、时间，也没有缺少证据。",
    ),
)
def test_contrastive_rule_does_not_guess_without_affirmed_是_alternative(copy: str) -> None:
    assert find_syntax_boundaries(copy, predicate_completion=False) == ()


@pytest.mark.parametrize(
    "copy",
    (
        "一个具体场景：骑行道和步行道分开，护栏亮着暖光。",
        "方案已经做好，团队开始执行。",
        "素材检查完，剪辑师开始整理。",
        "文件写清楚，审阅者才知道边界。",
    ),
)
def test_resultative_predicate_completion_becomes_a_terminal_boundary(copy: str) -> None:
    boundaries = find_syntax_boundaries(copy, contrastive_correction=False)

    assert len(boundaries) == 1
    boundary = boundaries[0]
    assert boundary.rule_id == PREDICATE_COMPLETION_RULE_ID
    assert copy[boundary.char_index] in "，,"


@pytest.mark.parametrize(
    "copy",
    (
        "方案已经做好，再开始拍摄。",
        "他站起来，先看了看四周。",
        "天气特别好，大家决定出门。",
        "照明差、人车混行的路段，夜里骑反而更危险。",
        "差别很明显，先别急着下结论。",
    ),
)
def test_resultative_rule_keeps_continuations_and_uncovered_syntax(copy: str) -> None:
    assert find_syntax_boundaries(copy, contrastive_correction=False) == ()


def test_syntax_rewrite_changes_only_boundary_punctuation_and_preserves_words() -> None:
    copy = (
        "一个具体场景：骑行道和步行道分开，护栏亮着暖光。"
        "这不是跟风，是城市夜间环境真的变了。"
    )
    boundaries = find_syntax_boundaries(copy)
    application = rewrite_syntax_boundaries(copy, boundaries)

    assert application.provider_text == (
        "一个具体场景：骑行道和步行道分开。\n\n护栏亮着暖光。"
        "这不是跟风。\n\n是城市夜间环境真的变了。"
    )
    assert application.provider_text.replace("\n\n", "") == copy.replace("，", "。")
    assert all(item.rule_id in {
        CONTRASTIVE_CORRECTION_RULE_ID, PREDICATE_COMPLETION_RULE_ID,
    } for item in application.applied)


def test_rule_set_version_and_limits_are_explicit() -> None:
    assert RULE_SET_VERSION == "zh-clause-binding-v1"
    with pytest.raises(VoiceSyntaxError, match="limit_invalid"):
        find_syntax_boundaries("这不是跟风，是城市。", max_negated_chars=0)
    with pytest.raises(VoiceSyntaxError, match="limit_invalid"):
        find_syntax_boundaries("方案做好，团队开始。", max_clause_chars=121)
