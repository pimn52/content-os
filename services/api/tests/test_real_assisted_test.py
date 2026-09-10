from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.domain.models import AnalysisResultBundle
from scripts.real_assisted_test import _build_analysis_bundle, _extract_openai_text, _json_from_model, _provider_config, _validate_analysis


def test_assisted_test_parses_openai_compatible_text_and_validates_shape() -> None:
    value = _json_from_model('```json\n{"summary":"画面概述","segments":[{"start_fraction":0.05,"end_fraction":0.2,"confidence":0.8},{"start_fraction":0.2,"end_fraction":0.5,"confidence":0.7}]}\n```')
    assert value["summary"] == "画面概述"
    json_text = '{"summary":"x"}'
    assert _extract_openai_text({"choices": [{"message": {"content": json_text}}]}) == json_text
    assert _validate_analysis(value) == value


def test_assisted_test_provider_config_never_falls_back_across_provider(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CONTENT_OS_LLM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY or CONTENT_OS_LLM_API_KEY"):
        _provider_config("openai-compatible", None, None)


def test_assisted_test_builds_replayable_bundle_from_hash_bound_model_segments(tmp_path) -> None:
    asset_id = uuid4()
    frame = tmp_path / "frame02.jpg"
    frame.write_bytes(b"jpeg")
    items = [{
        "filename": "source.mp4",
        "content_hash": "a" * 64,
        "frames": [(0.20, frame)],
        "analysis": {
            "summary": "画面概述",
            "segments": [
                {"start_fraction": 0.10, "end_fraction": 0.40, "visual_description": "人在桌前", "on_screen_text": ["标题"], "confidence": 0.8},
                {"start_fraction": 0.40, "end_fraction": 0.90, "visual_description": "屏幕内容", "on_screen_text": [], "confidence": 0.7},
            ],
            "topic_candidates": [],
        },
    }]
    bundle = _build_analysis_bundle(
        items,
        provider="openai-compatible",
        model="vision-test-model",
        asset_by_hash={"a" * 64: {"id": str(asset_id), "duration_ms": 10_000}},
        analyzed_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    validated = AnalysisResultBundle.model_validate(bundle)
    assert validated.mode == "assisted_test"
    assert len(validated.results) == 2
    assert validated.results[0].asset_id == asset_id
    assert validated.results[0].transcript is None
    assert validated.results[0].available_subtitles == ["标题"]
    assert validated.results[0].keyframes[0].reference == str(frame.resolve())
    repeated = _build_analysis_bundle(items, provider="openai-compatible", model="vision-test-model", asset_by_hash={"a" * 64: {"id": str(asset_id), "duration_ms": 10_000}}, analyzed_at=datetime(2026, 9, 9, tzinfo=timezone.utc))
    assert repeated["input_hash"] == bundle["input_hash"]
    assert repeated["results"][0]["clip_id"] == bundle["results"][0]["clip_id"]


def test_assisted_test_does_not_bind_unknown_source_by_filename() -> None:
    item = {
        "filename": "same-name.mp4",
        "content_hash": "b" * 64,
        "frames": [],
        "analysis": {"summary": "x", "segments": [{"start_fraction": 0.1, "end_fraction": 0.2, "confidence": 0.5}, {"start_fraction": 0.2, "end_fraction": 0.3, "confidence": 0.5}]},
    }
    with pytest.raises(ValueError, match="not imported"):
        _build_analysis_bundle([item], provider="anthropic", model="vision-model", asset_by_hash={"c" * 64: {"id": str(uuid4()), "duration_ms": 1_000}})
