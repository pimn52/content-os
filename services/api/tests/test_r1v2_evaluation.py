"""Bounded assisted-timeline checks, separate from real render admission."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from evaluate_r1v_portrait_composition import add_presentation_cues, add_sparse_emphasis


def test_assisted_cues_cannot_change_source_audio_or_spec() -> None:
    props = {
        "videoSpec": {"scenes": [{"duration_frames": 972, "visual": {"source_kind": "ai_video"}}]},
        "sceneSources": {"whole": {"kind": "video", "src": "authorized.mp4", "trimBefore": 0,
            "trimAfter": 972, "portraitPresentation": "portrait_panel"}},
        "masterNarration": {"src": "approved.wav", "startFrame": 0}, "audioMode": "master_narration",
    }
    original = copy.deepcopy(props)
    cues = [{"startFrame": 0, "durationFrames": 972, "treatment": "headline", "title": "Title", "text": "Point", "label": "01"}]
    add_presentation_cues(props, cues)
    assert props["sceneSources"]["whole"].pop("presentationCues") == cues
    assert props == original


def sparse_props():
    return {"videoSpec": {"width": 1080, "height": 1920, "scenes": [
        {"duration_frames": 972, "visual": {"source_kind": "ai_video"}}]},
        "sceneSources": {"whole": {"kind": "video", "src": "square.mp4",
            "portraitPresentation": "full_canvas", "verticalReframeMode": "contain",
            "sourceBottomCropRatio": 0, "styleTokens": {"name": "content-os-subject-first"}}},
        "masterNarration": {"src": "approved.wav"}}


def test_sparse_emphasis_preserves_gaps_source_master_and_spec():
    props = sparse_props()
    original = copy.deepcopy(props)
    cues = [{"startFrame": 90, "durationFrames": 72, "title": "Preserve meaning", "treatment": "key_point"}]
    # Two shorter lines fit the declared upper-margin layout.
    cues[0]["title"] = "Preserve\nmeaning"
    add_sparse_emphasis(props, cues)
    stored = props["sceneSources"]["whole"].pop("presentationCues")
    assert stored == cues
    assert props == original
    cues[0]["title"] = "Changed"
    assert stored[0]["title"] == "Preserve\nmeaning"


@pytest.mark.parametrize("cues", [
    [{"startFrame": -1, "durationFrames": 72, "title": "Point", "treatment": "key_point"}],
    [{"startFrame": 950, "durationFrames": 72, "title": "Point", "treatment": "key_point"}],
    [{"startFrame": 0, "durationFrames": 181, "title": "Point", "treatment": "key_point"}],
    [{"startFrame": 0, "durationFrames": 72, "title": "a" * 15, "treatment": "key_point"}],
    [{"startFrame": 0, "durationFrames": 72, "title": "a\nb\nc", "treatment": "key_point"}],
    [{"startFrame": 0, "durationFrames": 72, "title": "Point", "treatment": "unknown"}],
    [{"startFrame": n, "durationFrames": 180, "title": "Point", "treatment": "key_point"} for n in (0, 180, 360)],
    [{"startFrame": n, "durationFrames": 72, "title": "Point", "treatment": "key_point"} for n in (90, 120)],
])
def test_invalid_sparse_cues_fail_without_partial_mutation(cues):
    props = sparse_props()
    original = copy.deepcopy(props)
    with pytest.raises(ValueError):
        add_sparse_emphasis(props, cues)
    assert props == original


def test_sparse_cues_reject_cropped_source():
    props = sparse_props()
    props["sceneSources"]["whole"]["sourceBottomCropRatio"] = .15
    with pytest.raises(ValueError):
        add_sparse_emphasis(props, [])


@pytest.mark.parametrize("cues", [
    [{"startFrame": 1, "durationFrames": 972, "treatment": "headline"}],
    [{"startFrame": 0, "durationFrames": 971, "treatment": "headline"}],
    [{"startFrame": 0, "durationFrames": 972, "treatment": "unknown"}],
    [{"startFrame": 0, "durationFrames": 0, "treatment": "headline"}],
])
def test_assisted_cues_fail_before_render_for_gaps_short_coverage_or_unknown_treatment(cues) -> None:
    props = {"videoSpec": {"scenes": [{"duration_frames": 972, "visual": {"source_kind": "ai_video"}}]},
        "sceneSources": {"whole": {"kind": "video", "portraitPresentation": "portrait_panel"}}}
    original = copy.deepcopy(props)
    with pytest.raises(ValueError):
        add_presentation_cues(props, cues)
    assert props == original
