"""One normal-path, EditPlan-preflighted render for the exact V60/V64 inputs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from app.domain.models import (  # noqa: E402
    EditPlan,
    EditPlanFallback,
    EditPlanScene,
    EditVisualRole,
    GraphicTreatment,
    SourceKind,
    SubtitleTreatment,
    TalkingFramingPolicy,
)
from app.main import create_app  # noqa: E402
from app.media import FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402
from evaluate_v65_assisted_render import (  # noqa: E402
    DATA,
    DB,
    MASTER_ID,
    PROJECT_ID,
    RUN_ASSET_ID,
    build_selections,
)

EVIDENCE = DATA / "evaluation-evidence" / "v68-edit-plan-render"
MANIFEST = EVIDENCE / "manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _save(value: dict[str, object]) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _edit_plan(scenes, selections) -> EditPlan:
    by_scene = {candidate.scene_plan_id: candidate for candidate in selections}
    plan_scenes: list[EditPlanScene] = []
    for scene in scenes:
        candidate = by_scene[scene.id]
        if candidate.source_kind is SourceKind.AI_VIDEO:
            plan_scenes.append(EditPlanScene(
                scene_plan_id=scene.id, scene_id=scene.scene_id, visual_role=EditVisualRole.TALKING,
                selected_source_kind=candidate.source_kind, selected_asset_id=candidate.asset_id,
                selected_clip_id=candidate.clip_id, framing_policy=TalkingFramingPolicy.FACE_SAFE_CONTAIN,
                # V66 established that the exact source has burned-in captions.
                # Retain them as source content instead of overlaying a second
                # timed-caption layer while the full frame remains visible.
                burned_in_subtitles="present", subtitle_treatment=SubtitleTreatment.NONE,
                fallback=EditPlanFallback(graphic_treatment=GraphicTreatment.HEADLINE, text="保住原意"),
            ))
            continue
        treatment, title = (
            (GraphicTreatment.KEY_POINT, "问题 · 例子 · 语境")
            if scene.scene_id == "editorial-structure"
            else (GraphicTreatment.CONTRAST, "完整判断，不是一阵情绪")
        )
        plan_scenes.append(EditPlanScene(
            scene_plan_id=scene.id, scene_id=scene.scene_id, visual_role=EditVisualRole.GRAPHIC,
            selected_source_kind=SourceKind.TYPOGRAPHY, graphic_treatment=treatment, graphic_text=title,
            subtitle_treatment=SubtitleTreatment.TIMED_CAPTIONS,
            fallback=EditPlanFallback(graphic_treatment=treatment, text=title),
        ))
    return EditPlan(project_id=PROJECT_ID, scenes=plan_scenes)


def main() -> None:
    if MANIFEST.exists():
        raise ValueError("V68 manifest already exists; the bounded render must not be replayed")
    scenes, selections = build_selections()
    plan = _edit_plan(scenes, selections)
    request = {
        "scenes": [scene.model_dump(mode="json") for scene in scenes],
        "selections": [candidate.model_dump(mode="json") for candidate in selections],
        "master_narration_asset_id": str(MASTER_ID),
        "edit_plan": plan.model_dump(mode="json"),
    }
    record: dict[str, object] = {
        "package": "V68", "status": "preflighting", "render_attempt_limit": 1,
        "project_id": str(PROJECT_ID), "master_audio_id": str(MASTER_ID),
        "talking_run_asset_id": str(RUN_ASSET_ID),
        "evidence_class": "assisted ScenePlan; normal EditPlan and normal VideoSpec assembly",
        "edit_plan": plan.model_dump(mode="json"),
    }
    _save(record)
    with TestClient(create_app(DB)) as api:
        preflight = api.post(f"/projects/{PROJECT_ID}/video-spec", json=request)
        if preflight.status_code != 200:
            record.update(status="blocked_preflight", error=preflight.text[:2_000])
            _save(record)
            raise ValueError(f"normal EditPlan preflight failed: {preflight.status_code} {preflight.text}")
        spec = preflight.json()
        if spec.get("edit_plan") != record["edit_plan"]:
            record.update(status="blocked_preflight", error="resolved VideoSpec did not preserve the accepted EditPlan")
            _save(record)
            raise ValueError("normal VideoSpec did not preserve the accepted EditPlan")
        talking = [scene for scene in spec["scenes"] if scene["visual"]["source_kind"] == "ai_video"]
        if any(scene["visual"]["vertical_reframe_mode"] != "contain" for scene in talking):
            record.update(status="blocked_preflight", error="Talking framing did not fail closed to contain")
            _save(record)
            raise ValueError("Talking framing did not fail closed to contain")
        record.update(status="rendering", video_spec=spec)
        _save(record)
        rendered = api.post(f"/projects/{PROJECT_ID}/render", json={"video_spec": spec})
        if rendered.status_code != 200:
            record.update(status="blocked_render", error=rendered.text[:2_000])
            _save(record)
            raise ValueError(f"one normal render failed: {rendered.status_code} {rendered.text}")
        result = rendered.json()
    media = DATA / "renders" / str(PROJECT_ID) / f"{result['render_id']}.mp4"
    if not media.is_file():
        record.update(status="blocked_missing_render", render_id=result["render_id"])
        _save(record)
        raise ValueError("normal render output file is missing")
    probe = FFProbeAdapter(resolve_local_executable("ffprobe")).probe(media)
    streams = probe.metadata.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    record.update(
        render_id=result["render_id"], media=str(media), sha256=_sha256(media), duration_ms=probe.duration_ms,
        width=None if video is None else video.get("width"), height=None if video is None else video.get("height"),
        audio_stream_present=audio is not None,
    )
    if video is None or audio is None or video.get("width") != 1080 or video.get("height") != 1920 or not 30_000 <= probe.duration_ms <= 60_000:
        record["status"] = "blocked_technical_qa"
        _save(record)
        raise ValueError("rendered MP4 failed vertical 30–60s technical QA")
    record["status"] = "technical_qa_pass_u_product_pending"
    _save(record)
    print(json.dumps({key: record[key] for key in ("status", "render_id", "media", "sha256", "duration_ms")}, ensure_ascii=False))


def finalize(render_id: str) -> None:
    """Finish technical evidence after an interrupted post-render shell window.

    This path never calls the API or renderer.  It is intentionally limited to
    the sole render ID already produced by the one-attempt main path.
    """
    if not MANIFEST.is_file():
        raise ValueError("V68 rendering manifest is unavailable")
    record = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if record.get("status") != "rendering" or record.get("render_attempt_limit") != 1:
        raise ValueError("V68 is not awaiting one-time render evidence finalization")
    media = DATA / "renders" / str(PROJECT_ID) / f"{render_id}.mp4"
    if not media.is_file():
        raise ValueError("the specified one-time render output is unavailable")
    probe = FFProbeAdapter(resolve_local_executable("ffprobe")).probe(media)
    streams = probe.metadata.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    record.update(
        render_id=render_id, media=str(media), sha256=_sha256(media), duration_ms=probe.duration_ms,
        width=None if video is None else video.get("width"), height=None if video is None else video.get("height"),
        audio_stream_present=audio is not None,
    )
    if video is None or audio is None or video.get("width") != 1080 or video.get("height") != 1920 or not 30_000 <= probe.duration_ms <= 60_000:
        record["status"] = "blocked_technical_qa"
        _save(record)
        raise ValueError("rendered MP4 failed vertical 30–60s technical QA")
    record["status"] = "technical_qa_pass_u_product_pending"
    _save(record)
    print(json.dumps({key: record[key] for key in ("status", "render_id", "media", "sha256", "duration_ms")}, ensure_ascii=False))


def record_human_review(*, approved: bool, evidence_reference: str, findings: list[str]) -> None:
    """Persist the immutable U-Product decision for the one rendered MP4."""
    if not MANIFEST.is_file():
        raise ValueError("V68 technical evidence is unavailable")
    record = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if record.get("status") != "technical_qa_pass_u_product_pending":
        raise ValueError("V68 is not awaiting a U-Product decision")
    media = Path(str(record.get("media", "")))
    expected_hash = record.get("sha256")
    if not media.is_file() or not isinstance(expected_hash, str) or _sha256(media) != expected_hash:
        raise ValueError("the reviewed V68 artifact is unavailable or its bytes changed")
    record["human_review"] = {
        "approved": approved,
        "evidence_reference": evidence_reference,
        "reviewed_sha256": expected_hash,
        "findings": findings,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "scope": "V68 exact final render only; Voice and TalkingRun human approvals remain separate",
    }
    record["status"] = "u_product_pass" if approved else "u_product_fail"
    _save(record)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "finalize":
        finalize(sys.argv[2])
    elif len(sys.argv) == 1:
        main()
    else:
        raise ValueError("usage: evaluate_v68_edit_plan_render.py [finalize RENDER_ID]")
