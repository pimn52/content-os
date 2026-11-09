from datetime import datetime, timezone
from copy import deepcopy
import hashlib
from pathlib import Path
import wave
from uuid import uuid4

import pytest

from app.domain.models import Asset, Clip, ConsentRecord, NarrationEmphasis, NarrationPace, NarrationPause, NarrationPerformanceCue, NarrationPerformanceCueKind, NarrationPerformancePlanSource, RationalFps, VoiceProfile
from app.narration_performance import NarrationBoundaryKind, build_narration_performance_plan, compile_narration_delivery_plan, derive_narration_boundary_map
from app.voice_recipe import (VoiceRecipeError, apply_recipe_delivery_adjustments,
    copy_spans_with_breaks, map_generation_segments_to_copy, parse_generation_recipe,
    require_recipe_execution, runtime_parameters_sha256)
from app.voice_performance import authorized_voice_reference_choices, plan_voice_generation_spans, plan_voice_performance_units, select_voice_reference_windows


COPY = "先把判断说清楚。但是，结构决定观众能否听懂。最后，让结论落下。"


def test_performance_units_preserve_copy_and_pause_provenance() -> None:
    plan = build_narration_performance_plan(COPY, delivery_goal="Land the contrast.", overall_pace=NarrationPace.CONVERSATIONAL, source=NarrationPerformancePlanSource.USER, evidence_refs=[], cues=[
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.EMPHASIS, start_char=COPY.index("判断"), end_char=COPY.index("判断") + 2, emphasis=NarrationEmphasis.STRONG),
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PAUSE, start_char=COPY.index("。") + 1, end_char=COPY.index("。") + 1, pause=NarrationPause.BRIEF),
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PAUSE, start_char=COPY.index("。", COPY.index("结构")) + 1, end_char=COPY.index("。", COPY.index("结构")) + 1, pause=NarrationPause.BEAT),
    ])
    rendered = plan_voice_performance_units(COPY, plan)
    assert "".join(unit.text for unit in rendered.units) == COPY
    assert [unit.pause_after for unit in rendered.units] == [NarrationPause.BRIEF, NarrationPause.BEAT, None]


def test_generation_spans_merge_adjacent_units_but_keep_semantic_provenance() -> None:
    plan = build_narration_performance_plan(COPY, delivery_goal="Land the contrast.", overall_pace=NarrationPace.CONVERSATIONAL, source=NarrationPerformancePlanSource.USER, evidence_refs=[], cues=[
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.EMPHASIS, start_char=COPY.index("判断"), end_char=COPY.index("判断") + 2, emphasis=NarrationEmphasis.STRONG),
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PAUSE, start_char=COPY.index("。") + 1, end_char=COPY.index("。") + 1, pause=NarrationPause.BRIEF),
        NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PAUSE, start_char=COPY.index("。", COPY.index("结构")) + 1, end_char=COPY.index("。", COPY.index("结构")) + 1, pause=NarrationPause.BEAT),
    ])
    rendered = plan_voice_performance_units(COPY, plan)
    spans = plan_voice_generation_spans(rendered, maximum_adjacent_units=2)
    assert len(spans) == 2
    assert "".join(span.text for span in spans) == COPY
    assert spans[0].units == rendered.units[:2]
    assert spans[0].units[0].pause_after == NarrationPause.BRIEF
    assert spans[0].pause_after == NarrationPause.BEAT


def test_unpunctuated_complete_phrase_remains_one_generation_unit() -> None:
    copy = "先明确问题再解释原因"
    plan = build_narration_performance_plan(
        copy, delivery_goal="Explain the answer.", overall_pace=NarrationPace.CONVERSATIONAL,
        source=NarrationPerformancePlanSource.USER, evidence_refs=[], cues=[
            NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PACE, start_char=0,
                end_char=len(copy), pace=NarrationPace.CONVERSATIONAL),
        ],
    )
    rendered = plan_voice_performance_units(copy, plan)
    spans = plan_voice_generation_spans(rendered, maximum_adjacent_units=1)

    assert len(rendered.units) == len(spans) == 1
    assert rendered.units[0].text == spans[0].text == copy


def test_boundary_map_preserves_punctuation_hierarchy_and_forward_binding() -> None:
    boundaries = derive_narration_boundary_map(COPY)

    assert boundaries.kind_at(COPY.index("。") + 1) is NarrationBoundaryKind.TERMINAL
    but_end = COPY.index("但是") + len("但是")
    last_end = COPY.index("最后") + len("最后")
    assert boundaries.kind_at(but_end) is NarrationBoundaryKind.FORWARD_BINDING
    assert boundaries.kind_at(but_end + 1) is NarrationBoundaryKind.FORWARD_BINDING
    assert boundaries.kind_at(last_end) is NarrationBoundaryKind.FORWARD_BINDING
    assert boundaries.kind_at(COPY.index("决定")) is NarrationBoundaryKind.NO_BREAK


def test_punctuation_classification_does_not_create_numeric_or_automatic_pause_controls() -> None:
    copy = "不是清晰度本身。源里已经丢失的细节，单纯提高码率无法恢复；判断画质，还要结合源质量。"
    boundaries = derive_narration_boundary_map(copy)
    assert boundaries.kind_at(copy.index("。") + 1) is NarrationBoundaryKind.TERMINAL
    assert boundaries.kind_at(copy.index("；") + 1) is NarrationBoundaryKind.TERMINAL
    assert boundaries.kind_at(copy.index("，") + 1) is NarrationBoundaryKind.CONTINUING
    plan = build_narration_performance_plan(
        copy, delivery_goal="Keep the reasoning intelligible.", overall_pace=NarrationPace.CONVERSATIONAL,
        source=NarrationPerformancePlanSource.USER, evidence_refs=[], cues=[
            NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PACE, start_char=0,
                end_char=len(copy), pace=NarrationPace.CONVERSATIONAL),
        ],
    )
    delivery = compile_narration_delivery_plan(plan, copy)
    assert "".join(segment.text for segment in delivery.segments) == copy
    assert all(segment.pause_after is None for segment in delivery.segments)
    assert delivery.opening_pause is None and delivery.closing_pause is None
    assert {"duration_ms", "silence_ms", "provider"}.isdisjoint(delivery.model_dump())


def test_boundary_map_uses_generic_contrast_and_resultative_rules_without_guessing_milliseconds() -> None:
    copy = (
        "这不是跟风，是城市夜间环境真的变了。"
        "骑行道和步行道分开，护栏亮着暖光。"
        "方案已经做好，再开始拍摄。"
    )
    boundaries = derive_narration_boundary_map(copy)

    contrast_comma = copy.index("，")
    resultative_comma = copy.index("，", contrast_comma + 1)
    continuation_comma = copy.index("，", resultative_comma + 1)
    assert boundaries.kind_at(contrast_comma + 1) is NarrationBoundaryKind.TERMINAL
    assert boundaries.kind_at(resultative_comma + 1) is NarrationBoundaryKind.TERMINAL
    assert boundaries.kind_at(continuation_comma + 1) is NarrationBoundaryKind.CONTINUING


def test_local_pause_revision_moves_boundary_without_rewriting_copy_or_other_cues() -> None:
    copy = "不是清晰度本身。源里已经丢失的细节，单纯提高码率无法恢复；判断画质，还要结合源质量。"
    sentence_boundary = copy.index("源里")
    before_judgment = copy.index("判断画质")
    after_judgment = before_judgment + len("判断画质，")

    def delivery_at(judgment_boundary: int):
        plan = build_narration_performance_plan(
            copy, delivery_goal="Review two local boundaries.", overall_pace=NarrationPace.CONVERSATIONAL,
            source=NarrationPerformancePlanSource.USER, evidence_refs=["offline-test-only"], cues=[
                NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PAUSE, start_char=sentence_boundary,
                    end_char=sentence_boundary, pause=NarrationPause.BEAT),
                NarrationPerformanceCue(kind=NarrationPerformanceCueKind.PAUSE, start_char=judgment_boundary,
                    end_char=judgment_boundary, pause=NarrationPause.BEAT),
            ],
        )
        return compile_narration_delivery_plan(plan, copy), plan_voice_performance_units(copy, plan)

    earlier, _ = delivery_at(after_judgment)
    revised, units = delivery_at(before_judgment)
    assert earlier.copy_fingerprint == revised.copy_fingerprint
    assert earlier.performance_plan_fingerprint != revised.performance_plan_fingerprint
    assert "".join(segment.text for segment in revised.segments) == copy
    assert {s.end_char for s in earlier.segments if s.pause_after is not None} == {sentence_boundary, after_judgment}
    assert {s.end_char for s in revised.segments if s.pause_after is not None} == {sentence_boundary, before_judgment}
    assert units.units[-1].text.startswith("判断画质，")
    spans = plan_voice_generation_spans(units, maximum_adjacent_units=2)
    assert spans[0].units[0].pause_after is NarrationPause.BEAT
    # An interior semantic cue remains intent, not a separately materialized gap.
    assert "".join(span.text for span in spans) == copy


def test_reference_window_ranking_only_proposes_candidates_without_quality_or_boundary_approval(tmp_path: Path) -> None:
    source = tmp_path / "creator.media"
    source.write_bytes(b"media")
    asset = Asset(source_file=str(source), content_hash="a" * 64, duration_ms=20_000, width=320, height=180, fps=RationalFps(numerator=25, denominator=1), authorization_reference="creator-rights", imported_at=datetime.now(timezone.utc))
    analysis_audio = tmp_path / "assets" / "audio" / f"{asset.content_hash}-16000-1.wav"
    analysis_audio.parent.mkdir(parents=True)
    with wave.open(str(analysis_audio), "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16_000)
        output.writeframes((10_000).to_bytes(2, "little", signed=True) * 16_000 * 20)
    clip = Clip(asset_id=asset.id, start_ms=15_000, end_ms=20_000, asset_duration_ms=20_000, transcript_source="faster-whisper:small", transcript_segments=[
        {"start_ms": 0, "end_ms": 2_000, "text": "截断尾音"}, {"start_ms": 2_100, "end_ms": 4_500, "text": "片段内容"}, {"start_ms": 4_600, "end_ms": 7_000, "text": "截断句尾"},
    ], speech_quality=None)
    windows = select_voice_reference_windows([clip], {asset.id: asset}, data_root=tmp_path)
    assert windows and windows[0].clip_id == clip.id
    assert 5_000 <= windows[0].end_ms - windows[0].start_ms <= 10_000
    assert any(window.start_ms < clip.start_ms for window in windows)
    assert windows[0].rank_score is not None
    assert all(window.source_timebase == "analysis_wav" for window in windows)
    assert all(window.source_file == str(analysis_audio) and window.source_content_hash == hashlib.sha256(analysis_audio.read_bytes()).hexdigest() for window in windows)
    profile = VoiceProfile(name="Creator", provider="omnivoice", provider_profile_id="creator",
        reference_clip_ids=[clip.id], consent=ConsentRecord(subject_name="Creator", basis="self",
        confirmed=True, confirmed_at=datetime.now(timezone.utc)), created_at=datetime.now(timezone.utc))
    assert authorized_voice_reference_choices(profile, [clip], {asset.id: asset}, data_root=tmp_path) == ()
    automatic = authorized_voice_reference_choices(
        profile, [clip], {asset.id: asset}, data_root=tmp_path, allow_auto_windows=True,
    )
    assert automatic and automatic[0][0].clip_id == clip.id
    assert automatic[0][0].start_ms == min(window.start_ms for _, window in automatic)
    assert 5_000 <= automatic[0][0].end_ms - automatic[0][0].start_ms <= 10_000


def test_profile_generation_recipe_pins_human_reviewed_reference_and_runtime(tmp_path: Path) -> None:
    source = tmp_path / "quality-anchor.media"
    source.write_bytes(b"human-reviewed-source")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    asset = Asset(source_file=str(source), content_hash=source_hash, duration_ms=20_000,
        width=1080, height=1920, fps=RationalFps(numerator=25, denominator=1), has_audio=True,
        authorization_reference="creator-rights", imported_at=datetime.now(timezone.utc))
    transcript = "完整开头 完整结尾"
    clip = Clip(asset_id=asset.id, start_ms=0, end_ms=10_000, asset_duration_ms=20_000,
        transcript_source="owner-confirmed-reference:human-review",
        transcript_segments=[
            {"start_ms": 2_000, "end_ms": 5_000, "text": "完整开头"},
            {"start_ms": 5_000, "end_ms": 8_000, "text": "完整结尾"},
        ])
    parameters = {"num_step": 32, "speed": 1.0, "guidance_scale": 2.0}
    recipe = {
        "schema_version": "0.5", "recipe_id": "v60-human-approved", "version": 6,
        "quality_anchor_audio_id": str(uuid4()), "quality_anchor_sha256": "a" * 64,
        "quality_anchor_evidence_reference": "audio:human-approved-v60",
        "human_review": {"approved": True, "likeness": "pass", "naturalness": "pass",
            "emphasis": "pass", "pace": "pass", "pauses": "pass", "rhythm": "pass"},
        "reference_window": {
            "clip_id": str(clip.id), "asset_id": str(asset.id), "source_content_hash": source_hash,
            "start_ms": 2_000, "end_ms": 8_000,
            "transcript_sha256": hashlib.sha256(transcript.encode()).hexdigest(),
        },
        "runtime_label": "v13-product-recipe",
        "runtime_parameters_sha256": runtime_parameters_sha256(parameters),
        "composition_policy": "paragraph_spans",
        "pause_hierarchy": {
            "schema_version": "1.0",
            "levels": {
                "phrase_internal": "continuous_no_cut",
                "comma": "short_continuing_boundary",
                "sentence": "natural_close_longer_than_comma",
                "paragraph": "allow_one_beat",
                "key_point": "allow_one_thinking_beat",
                "ending": "natural_landing",
            },
            "invariants": {
                "sentence_outranks_comma": True,
                "global_speed_is_not_pause_control": True,
                "insert_unmeasured_silence": False,
            },
            "evidence_refs": [
                "v19b-u-voice-punctuation-hierarchy-review",
                "content-os-data/evaluation-evidence/v19c-c-punctuation-hierarchy/v19c-c-punctuation-hierarchy.json",
            ],
        },
        "seam_policy": {
            "scope": "paragraph_spans",
            "method": "pcm16_mono_24khz_10ms_rms_below_minus45dbfs",
            "eligible_boundary_kinds": ["terminal", "continuing"],
            "margin_ms_each_side": 60,
            "max_cut_ms": 300,
            "preserve_spoken_frames": True,
            "insert_silence": False,
            "evidence_reference": "content-os-data/evaluation-evidence/v60-length-completion/manifest.json",
        },
        "delivery_adjustments": {
            "schema_version": "1.4", "review_state": "pending_user_review",
            "syntax_rule_set_version": "zh-clause-binding-v1",
            "punctuation_overrides": [], "pronunciation_overrides": [], "generation_breaks": [],
            "copula_complement_break": {
                "enabled": False, "max_complement_chars": 12,
                "reason": "disabled in fixture", "evidence_reference": "fixture",
            },
            "contextual_pronunciation": {
                "enabled": False, "rule_set_version": "zh-common-polyphones-v1",
                "evidence_reference": "fixture",
            },
            "contrastive_correction_break": {
                "enabled": False, "max_negated_chars": 48,
                "reason": "disabled in fixture", "evidence_reference": "fixture",
            },
            "predicate_completion_break": {
                "enabled": False, "max_clause_chars": 60,
                "reason": "disabled in fixture", "evidence_reference": "fixture",
            },
        },
    }
    profile = VoiceProfile(name="Creator", provider="omnivoice", provider_profile_id="creator",
        reference_clip_ids=[clip.id], consent=ConsentRecord(subject_name="Creator", basis="self",
        confirmed=True, confirmed_at=datetime.now(timezone.utc)),
        generation_recipe=recipe, created_at=datetime.now(timezone.utc))

    parsed = parse_generation_recipe(profile)
    assert parsed is not None and parsed.recipe_id == "v60-human-approved"
    assert parsed.pause_hierarchy.levels["sentence"] == "natural_close_longer_than_comma"
    assert parsed.pause_hierarchy.invariants["global_speed_is_not_pause_control"] is True
    assert parsed.seam_policy.margin_ms_each_side == 60
    assert parsed.seam_policy.max_cut_ms == 300
    assert require_recipe_execution(profile, runtime_label="v13-product-recipe",
        parameters=parameters) == parsed
    with pytest.raises(VoiceRecipeError, match="runtime_changed"):
        require_recipe_execution(profile, runtime_label="w51-rejected", parameters=parameters)
    speed_recipe = deepcopy(recipe)
    speed_recipe["pause_hierarchy"]["invariants"]["global_speed_is_not_pause_control"] = False
    with pytest.raises(VoiceRecipeError, match="recipe_invalid"):
        parse_generation_recipe(profile.model_copy(update={"generation_recipe": speed_recipe}))

    choices = authorized_voice_reference_choices(
        profile, [clip], {asset.id: asset}, data_root=tmp_path,
    )
    assert choices[0][0].start_ms == 2_000 and choices[0][0].end_ms == 8_000
    assert choices[0][1].transcript == transcript


def test_recipe_delivery_generic_syntax_and_pinyin_preserve_exact_copy() -> None:
    target = (
        "这不是跟风，是城市夜间环境真的变了。\n\n"
        "一个具体场景：骑行道和步行道分开，护栏亮着暖光。\n\n"
        "但边界也很清楚：照明差、人车混行的路段，夜里骑反而更危险。"
    )
    recipe_value = {
        "schema_version": "0.5", "recipe_id": "voice-delivery-v6", "version": 6,
        "quality_anchor_audio_id": str(uuid4()), "quality_anchor_sha256": "a" * 64,
        "quality_anchor_evidence_reference": "audio:human-approved",
        "human_review": {"approved": True, "likeness": "pass", "naturalness": "pass",
            "emphasis": "pass", "pace": "pass", "pauses": "pass", "rhythm": "pass"},
        "reference_window": {
            "clip_id": str(uuid4()), "asset_id": str(uuid4()),
            "source_content_hash": "b" * 64, "start_ms": 0, "end_ms": 6000,
            "transcript_sha256": "c" * 64,
        },
        "runtime_label": "runtime", "runtime_parameters_sha256": "d" * 64,
        "composition_policy": "paragraph_spans",
        "pause_hierarchy": {
            "schema_version": "1.0",
            "levels": {
                "phrase_internal": "continuous_no_cut",
                "comma": "short_continuing_boundary",
                "sentence": "natural_close_longer_than_comma",
                "paragraph": "allow_one_beat",
                "key_point": "allow_one_thinking_beat",
                "ending": "natural_landing",
            },
            "invariants": {
                "sentence_outranks_comma": True,
                "global_speed_is_not_pause_control": True,
                "insert_unmeasured_silence": False,
            },
            "evidence_refs": ["user-voice-feedback"],
        },
        "seam_policy": {
            "scope": "paragraph_spans",
            "method": "pcm16_mono_24khz_10ms_rms_below_minus45dbfs",
            "eligible_boundary_kinds": ["terminal", "continuing"],
            "margin_ms_each_side": 60, "max_cut_ms": 300,
            "preserve_spoken_frames": True, "insert_silence": False,
            "evidence_reference": "v60",
        },
        "delivery_adjustments": {
            "schema_version": "1.4", "review_state": "pending_user_review",
            "syntax_rule_set_version": "zh-clause-binding-v1",
            "punctuation_overrides": [],
            "pronunciation_overrides": [],
            "generation_breaks": [],
            "copula_complement_break": {
                "enabled": True, "max_complement_chars": 12,
                "reason": "Bind short copular complements while preserving contrast boundaries.",
                "evidence_reference": "user-voice-feedback",
            },
            "contextual_pronunciation": {
                "enabled": True, "rule_set_version": "zh-common-polyphones-v1",
                "evidence_reference": "user-voice-feedback",
            },
            "contrastive_correction_break": {
                "enabled": True, "max_negated_chars": 48,
                "reason": "Close the negated member before the affirmed alternative.",
                "evidence_reference": "user-voice-feedback",
            },
            "predicate_completion_break": {
                "enabled": True, "max_clause_chars": 60,
                "reason": "Start the next clause after the completed resultative predicate.",
                "evidence_reference": "user-voice-feedback",
            },
        },
    }
    profile = VoiceProfile(
        name="Creator", provider="omnivoice", provider_profile_id="creator",
        reference_clip_ids=[uuid4()],
        consent=ConsentRecord(subject_name="Creator", basis="self", confirmed=True,
            confirmed_at=datetime.now(timezone.utc)),
        generation_recipe=recipe_value, created_at=datetime.now(timezone.utc),
    )
    recipe = parse_generation_recipe(profile)
    assert recipe is not None
    application = apply_recipe_delivery_adjustments(target, recipe)
    assert application.provider_text == (
        "这不是跟风。\n\n是城市夜间环境真的变了。\n\n"
        "一个具体场景：骑行道和步行道分开。\n\n护栏亮着暖光。\n\n"
        "但边界也很清楚：照明CHA4、人车混行的路段，夜里骑反而更危险。"
    )
    assert len(application.evidence["adjustments"]) == 3
    assert {
        item["rule_id"] for item in application.evidence["adjustments"]
        if item["kind"] == "syntax_boundary"
    } == {"contrastive_correction", "resultative_predicate_completion"}

    generated = application.provider_text
    generated_spans = []
    cursor = 0
    for part in generated.split("\n\n"):
        start = generated.index(part, cursor)
        generated_spans.append((start, start + len(part)))
        cursor = start + len(part)
    mapped = map_generation_segments_to_copy(
        target_text=target, generated_text=generated,
        generation_segments=[
            {"start_char": start, "end_char": end, "source_file": f"{index}.wav"}
            for index, (start, end) in enumerate(generated_spans)
        ],
        target_break_positions=application.target_break_positions,
    )
    assert [target[item["start_char"]:item["end_char"]] for item in mapped] == [
        target[start:end] for start, end in copy_spans_with_breaks(
            target, application.target_break_positions,
        )
    ]

    invalid = deepcopy(recipe_value)
    invalid["delivery_adjustments"]["contrastive_correction_break"]["max_negated_chars"] = 0
    with pytest.raises(VoiceRecipeError, match="recipe_invalid"):
        parse_generation_recipe(profile.model_copy(update={"generation_recipe": invalid}))
    valid_alias = deepcopy(recipe_value)
    valid_alias["delivery_adjustments"]["pronunciation_overrides"] = [{
        "copy_text": "斯多葛",
        "provider_text": "斯多格",
        "reason": "A provider-friendly homophone alias preserves the target sound.",
        "evidence_reference": "fixture",
    }]
    parsed_alias = parse_generation_recipe(profile.model_copy(update={"generation_recipe": valid_alias}))
    assert parsed_alias is not None
    invalid_alias = deepcopy(valid_alias)
    invalid_alias["delivery_adjustments"]["pronunciation_overrides"][0]["provider_text"] = "斯多各"
    with pytest.raises(VoiceRecipeError, match="recipe_invalid"):
        parse_generation_recipe(profile.model_copy(update={"generation_recipe": invalid_alias}))
    invalid = deepcopy(recipe_value)
    invalid["delivery_adjustments"]["pronunciation_overrides"] = [{
        "copy_text": "照明差",
        "provider_text": "照明cha4",
        "reason": "invalid lowercase pinyin must fail",
        "evidence_reference": "fixture",
    }]
    with pytest.raises(VoiceRecipeError, match="recipe_invalid"):
        parse_generation_recipe(profile.model_copy(update={"generation_recipe": invalid}))


def test_generation_mapping_ignores_break_already_present_at_paragraph_end() -> None:
    target = (
        "第一段结束。\n\n"
        "这不是压抑情绪，而是分清边界。\n\n"
        "第三段结束。"
    )
    paragraph_end = target.index("\n\n")
    contrast_end = target.index("而是")
    spans = copy_spans_with_breaks(target, (paragraph_end, contrast_end))
    assert [target[start:end] for start, end in spans] == [
        "第一段结束。",
        "这不是压抑情绪，",
        "而是分清边界。",
        "第三段结束。",
    ]


def test_reviewed_homophone_alias_applies_to_every_occurrence_without_changing_copy() -> None:
    from copy import deepcopy

    recipe_value = {
        "schema_version": "0.5",
        "recipe_id": "fixture-recipe",
        "version": 6,
        "quality_anchor_audio_id": str(uuid4()),
        "quality_anchor_sha256": "a" * 64,
        "quality_anchor_evidence_reference": "fixture",
        "human_review": {
            "approved": True,
            "likeness": "pass", "naturalness": "pass", "emphasis": "pass",
            "pace": "pass", "pauses": "pass", "rhythm": "pass",
        },
        "reference_window": {
            "schema_version": "0.1", "clip_id": str(uuid4()), "asset_id": str(uuid4()),
            "source_content_hash": "b" * 64, "start_ms": 0, "end_ms": 5_000,
            "transcript_sha256": "c" * 64, "source_timebase": "asset_pts",
        },
        "runtime_label": "fixture-runtime",
        "runtime_parameters_sha256": "d" * 64,
        "composition_policy": "paragraph_spans",
        "pause_hierarchy": {
            "schema_version": "1.0", "levels": {
                "phrase_internal": "continuous_no_cut", "comma": "short_continuing_boundary",
                "sentence": "natural_close_longer_than_comma", "paragraph": "allow_one_beat",
                "key_point": "allow_one_thinking_beat", "ending": "natural_landing",
            },
            "invariants": {
                "global_speed_is_not_pause_control": True, "sentence_outranks_comma": True,
                "insert_unmeasured_silence": False,
            },
            "evidence_refs": ["fixture"],
        },
        "seam_policy": {
            "scope": "paragraph_spans", "method": "pcm16_mono_24khz_10ms_rms_below_minus45dbfs",
            "eligible_boundary_kinds": ["terminal", "continuing"],
            "margin_ms_each_side": 60, "max_cut_ms": 300,
            "preserve_spoken_frames": True, "insert_silence": False,
            "evidence_reference": "fixture",
        },
        "delivery_adjustments": {
            "schema_version": "1.4", "review_state": "pending_user_review",
            "punctuation_overrides": [],
            "pronunciation_overrides": [{
                "copy_text": "斯多葛", "provider_text": "斯多格",
                "reason": "same-sound alias", "evidence_reference": "fixture",
            }],
            "generation_breaks": [],
            "copula_complement_break": {
                "enabled": False, "max_complement_chars": 12,
                "reason": "fixture", "evidence_reference": "fixture",
            },
            "contextual_pronunciation": {
                "enabled": False, "rule_set_version": "zh-common-polyphones-v1",
                "evidence_reference": "fixture",
            },
            "contrastive_correction_break": {
                "enabled": False, "max_negated_chars": 48,
                "reason": "fixture", "evidence_reference": "fixture",
            },
            "predicate_completion_break": {
                "enabled": False, "max_clause_chars": 60,
                "reason": "fixture", "evidence_reference": "fixture",
            },
            "syntax_rule_set_version": "zh-clause-binding-v1",
        },
    }
    profile = VoiceProfile(
        name="Creator", provider="omnivoice", provider_profile_id="creator",
        reference_clip_ids=[uuid4()],
        consent=ConsentRecord(
            subject_name="Creator", basis="self", confirmed=True,
            confirmed_at=datetime.now(timezone.utc),
        ),
        generation_recipe=recipe_value, created_at=datetime.now(timezone.utc),
    )
    recipe = parse_generation_recipe(profile)
    assert recipe is not None
    target = "斯多葛说冷静。斯多葛也提醒判断。"
    application = apply_recipe_delivery_adjustments(target, recipe)
    assert application.provider_text == "斯多格说冷静。斯多格也提醒判断。"


def test_reference_window_candidates_do_not_fall_back_to_original_asset_time(tmp_path: Path) -> None:
    source = tmp_path / "creator.media"
    source.write_bytes(b"media")
    asset = Asset(source_file=str(source), content_hash="b" * 64, duration_ms=20_000, width=320, height=180,
        fps=RationalFps(numerator=25, denominator=1), authorization_reference="creator-rights", imported_at=datetime.now(timezone.utc))
    clip = Clip(asset_id=asset.id, start_ms=0, end_ms=20_000, asset_duration_ms=20_000,
        transcript_source="faster-whisper:small", transcript_segments=[
            {"start_ms": 0, "end_ms": 2_000, "text": "未经核对的原片时间"},
            {"start_ms": 2_100, "end_ms": 4_500, "text": "分析坐标候选"},
            {"start_ms": 4_600, "end_ms": 7_000, "text": "没有派生音轨"},
        ])
    assert select_voice_reference_windows([clip], {asset.id: asset}, data_root=tmp_path) == ()
