"""Replan the approved whole Run; graphics change without splitting speech."""
from __future__ import annotations

import copy
import json
import shutil
from uuid import UUID

import evaluate_r1v_portrait_composition as existing
from app.domain.models import VideoSpec

EVIDENCE = existing.DATA / "evaluation-evidence" / "r1-v2-whole-run"
WHOLE_CLIP_ID = UUID("f60981ff-5c88-4f8a-96e5-990b61d3de51")
# Frames follow the approved Master's previous semantic narration boundaries.
# These are explicitly assisted presentation choices, retained in the manifest
# and exact render props. They are not product EditPlan automation or new QA.
CUES = [
    {"startFrame": 0, "durationFrames": 407, "label": "01 / 保住原意", "title": "别先找金句\n先保住原意", "treatment": "headline", "text": "观点独立成立\n证据留在语境里"},
    {"startFrame": 407, "durationFrames": 197, "label": "02 / 三步结构", "title": "先让答案成立\n再让观众记住", "treatment": "key_point", "text": "问题 · 例子 · 语境", "stepFrames": [0, 60, 119]},
    {"startFrame": 604, "durationFrames": 242, "label": "03 / 对内容负责", "title": "抓眼球\n也要保住语境", "treatment": "headline", "text": "避免断章取义\n保留完整的判断"},
    {"startFrame": 846, "durationFrames": 126, "label": "04 / 留下判断", "title": "让观众带走\n完整判断", "treatment": "contrast", "text": "完整判断\n一阵情绪"},
]


def build_spec() -> VideoSpec:
    previous = existing.build_spec()
    value = copy.deepcopy(previous.model_dump(mode="json"))
    talking = copy.deepcopy(value["scenes"][0])
    master = value["master_narration"]
    talking.update(scene_id="whole-approved-talking", start_frame=0, duration_frames=972,
        narration_start_ms=0, narration_end_ms=32400,
        caption=" ".join(segment["text"] for segment in master["transcript_segments"]),
        captions=copy.deepcopy(master["transcript_segments"]))
    talking["visual"].update(clip_id=str(WHOLE_CLIP_ID), clip_start_ms=0, clip_end_ms=32400)
    # Preserve all32560ms of approved audio, including the160ms tail beyond the
    # admitted source. An explicit graphic hold avoids extending its Clip.
    hold = copy.deepcopy(value["scenes"][-1])
    hold.update(scene_id="closing-audio-tail", start_frame=972, duration_frames=5,
        narration_start_ms=32400, narration_end_ms=32560, caption=None, captions=[],
        graphic_text="完整判断\n一阵情绪")
    hold["style_tokens"]["name"] = "content-os-editorial-ending"
    value["scenes"] = [talking, hold]
    spec = VideoSpec.model_validate(value)
    assert spec.master_narration == previous.master_narration
    assert [c.model_dump() for c in spec.scenes[0].captions] == [c.model_dump() for c in previous.master_narration.transcript_segments]
    return spec


def main() -> None:
    if (EVIDENCE / "manifest.json").exists() or (EVIDENCE / "evaluation.sqlite3").exists():
        raise RuntimeError("R1-V2 evidence already exists; no automatic replay")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    shutil.copy2(existing.EVIDENCE / "source-sheet.png", EVIDENCE / "source-sheet.png")
    existing.main(package="R1-V2", build=build_spec, evidence=EVIDENCE, presentation_cues=CUES)


if __name__ == "__main__":
    main()
