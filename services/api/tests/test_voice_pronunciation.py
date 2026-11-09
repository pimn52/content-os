from app.voice_pronunciation import (
    CHA_RULE_ID,
    apply_contextual_pronunciation,
    resolve_contextual_pronunciation,
)


def test_quality_or_condition_predicate_resolves_cha_to_fourth_tone() -> None:
    examples = (
        "照明差、人车混行的路段",
        "信号很差。",
        "画质比较差。",
        "服务差，体验也会受影响。",
        "照明条件差。",
    )
    for text in examples:
        decisions = resolve_contextual_pronunciation(text, evidence_reference="fixture")
        assert len(decisions) == 1
        assert decisions[0].rule_id == CHA_RULE_ID
        assert decisions[0].reading == "chà"
        assert decisions[0].provider_spelling == "CHA4"
        assert decisions[0].copy_context.endswith("差")
        rendered, applied = apply_contextual_pronunciation(
            text, evidence_reference="fixture",
        )
        assert "差" not in rendered
        assert "CHA4" in rendered
        assert applied == decisions


def test_difference_assignment_and_uneven_compounds_are_not_overridden() -> None:
    examples = (
        "时差。",
        "温差。",
        "差异。",
        "差别。",
        "出差。",
        "参差。",
        "差不多。",
    )
    for text in examples:
        assert resolve_contextual_pronunciation(text) == ()
        rendered, decisions = apply_contextual_pronunciation(
            text, evidence_reference="fixture",
        )
        assert rendered == text and decisions == ()


def test_multiple_quality_predicates_can_be_resolved_without_rewriting_context() -> None:
    text = "照明差、信号也差，环境本身不能替代道路条件。"
    rendered, decisions = apply_contextual_pronunciation(
        text, evidence_reference="fixture",
    )
    assert len(decisions) == 2
    assert rendered == "照明CHA4、信号也CHA4，环境本身不能替代道路条件。"
