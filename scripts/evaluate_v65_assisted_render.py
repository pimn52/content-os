"""One assisted, exact-copy VideoSpec and local render on the admitted TalkingRun."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID, uuid5

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from app.db import AudioAssetRepository, ClipRepository, Database, ProjectDraftRepository  # noqa: E402
from app.domain.models import CandidateAsset, Clip, CostCategory, ScenePlan, SourceKind, UsageCost, VisualIntent  # noqa: E402
from app.main import create_app  # noqa: E402
from app.media import FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402

DATA = ROOT / "content-os-data"
DB = DATA / "content-os.sqlite3"
PROJECT_ID = UUID("d45f42fb-06eb-4ec5-bded-66116ea6a872")
MASTER_ID = UUID("c1346d02-d967-4475-b5f4-4bdf7933b2d3")
RUN_ASSET_ID = UUID("2c98b774-f109-4377-a491-95a948d9b0ee")
RUN_CLIP_ID = UUID("f60981ff-5c88-4f8a-96e5-990b61d3de51")
EVIDENCE = DATA / "evaluation-evidence" / "v65-assisted-render"
MANIFEST = EVIDENCE / "manifest.json"
SCENES = (
    ("opening-and-method", "把长采访剪成短视频，最容易犯的错，是先找一句听起来很炸的话。可一句话离开上下文，观点可能就变了。我的做法是先找一个能独立回答的问题，再保留支撑答案的证据。", 0, 13_560, SourceKind.AI_VIDEO),
    ("editorial-structure", "开头把问题抛给观众，中间只放一条最有力的例子，最后交代这段话原本在讨论什么。", 13_560, 20_120, SourceKind.TYPOGRAPHY),
    ("responsible-edit", "这样剪出来的短视频，不只是抓眼球，也不会把受访者的意思剪歪。对内容负责，不只是避免断章取义，", 20_120, 28_200, SourceKind.AI_VIDEO),
    ("closing-judgment", "更要让观众带走一个完整的判断，而不是只带走一阵情绪。", 28_200, 32_560, SourceKind.TYPOGRAPHY),
)


def save(value: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_selections() -> tuple[list[ScenePlan], list[CandidateAsset]]:
    with Database(DB) as db:
        master = AudioAssetRepository(db).get(MASTER_ID)
        draft = ProjectDraftRepository(db).get(PROJECT_ID)
        full = ClipRepository(db).get(RUN_CLIP_ID)
        if master is None or draft is None or full is None or full.asset_id != RUN_ASSET_ID:
            raise ValueError("exact approved Master, draft or admitted TalkingRun Clip unavailable")
        if "".join(item[1] for item in SCENES) != draft.script:
            raise ValueError("assisted ScenePlan does not cover the exact persisted draft copy")
        if master.duration_ms != 32_560 or full.start_ms != 0 or full.end_ms != 32_400:
            raise ValueError("Master or TalkingRun timing changed")
        segments = master.transcript_segments
        if not segments or segments[-1].end_ms != 32_400:
            raise ValueError("persisted Master transcript timing changed")
        clips: dict[tuple[int, int], Clip] = {}
        for _, _, start, end, kind in SCENES:
            if kind is not SourceKind.AI_VIDEO:
                continue
            # The stored master boundary can round up to the next 30fps frame.
            # Keep one source frame of visual context so assembly never flashes
            # a one-frame typography fallback at a Talking/typography cut.
            visual_end = min(full.end_ms, end + 40)
            clip_id = uuid5(RUN_CLIP_ID, f"v65-master-aligned:{start}:{end}")
            prior = ClipRepository(db).get(clip_id)
            if prior is None:
                proposed = full.model_copy(update={
                    "id": clip_id, "start_ms": start, "end_ms": visual_end,
                    "visual_description": "V65 assisted editorial interval on the admitted, U-reviewed TalkingRun; aligns to V60 Master timestamps.",
                })
                with db.transaction():
                    prior = ClipRepository(db).create(proposed)
            elif prior.asset_id == RUN_ASSET_ID and prior.start_ms == start and prior.end_ms == end:
                with db.transaction():
                    prior = ClipRepository(db).update(prior.model_copy(update={"end_ms": visual_end}))
            if prior.asset_id != RUN_ASSET_ID or prior.start_ms != start or prior.end_ms != visual_end:
                raise ValueError("TalkingRun interval Clip identity conflict")
            clips[(start, end)] = prior
    scenes: list[ScenePlan] = []
    selections: list[CandidateAsset] = []
    for order, (name, copy, start, end, kind) in enumerate(SCENES):
        scene = ScenePlan(
            id=uuid5(PROJECT_ID, f"v65-assisted:{order}:{name}"), project_id=PROJECT_ID,
            scene_id=name, order=order, purpose="hook" if order == 0 else "close" if order == 3 else "explain",
            voice_text=copy, duration_target_ms=end - start,
            visual_intent=VisualIntent(description="Assisted selection on exact V60 Master; not product-generated ScenePlan"),
            preferred_sources=[kind], evidence_refs=["v65:assisted-scene-map", f"v60:master:{MASTER_ID}"],
        )
        clip = clips.get((start, end))
        candidate = CandidateAsset(
            scene_plan_id=scene.id, source_kind=kind,
            asset_id=RUN_ASSET_ID if clip is not None else None,
            clip_id=clip.id if clip is not None else None,
            match_score=1.0, why=["explicit assisted selection; exact Master timing"], recommended=True,
            estimated_cost=UsageCost(category=CostCategory.TALKING if clip is not None else CostCategory.TYPOGRAPHY,
                                     amount=0, currency="USD"),
        )
        scenes.append(scene)
        selections.append(candidate)
    return scenes, selections


def main() -> None:
    if MANIFEST.exists():
        raise ValueError("V65 one-render manifest already exists; do not replay")
    scenes, selections = build_selections()
    request = {
        "scenes": [scene.model_dump(mode="json") for scene in scenes],
        "selections": [candidate.model_dump(mode="json") for candidate in selections],
        "master_narration_asset_id": str(MASTER_ID),
    }
    with TestClient(create_app(DB)) as api:
        response = api.post(f"/projects/{PROJECT_ID}/video-spec", json=request)
        if response.status_code != 200:
            raise ValueError(f"normal VideoSpec refused V65: {response.status_code} {response.text}")
        spec = response.json()
        if spec["master_narration"]["audio_asset_id"] != str(MASTER_ID) or len(spec["scenes"]) != 4:
            raise ValueError("assembled VideoSpec changed exact Master or assisted scene count")
        kinds = [scene["visual"]["source_kind"] for scene in spec["scenes"]]
        if kinds != ["ai_video", "typography", "ai_video", "typography"]:
            raise ValueError("assembled visual route differs from reviewed Talking + typography plan")
        record = {
            "package": "V65", "status": "rendering", "project_id": str(PROJECT_ID),
            "master_audio_id": str(MASTER_ID), "talking_run_asset_id": str(RUN_ASSET_ID),
            "evidence_class": "assisted editorial ScenePlan, not product scene-planner output",
            "unrelated_real_media_excluded": True, "scene_intervals_ms": [[row[2], row[3]] for row in SCENES],
            "talking_visual_source_end_context_ms": 40,
            "video_spec": spec, "render_attempt_limit": 1,
        }
        save(record)
        response = api.post(f"/projects/{PROJECT_ID}/render", json={"video_spec": spec})
        if response.status_code != 200:
            record.update(status="blocked_render", render_error=response.text[:2_000])
            save(record)
            raise ValueError(f"normal render failed: {response.status_code} {response.text}")
        rendered = response.json()
    render_id = UUID(rendered["render_id"])
    media = DATA / "renders" / str(PROJECT_ID) / f"{render_id}.mp4"
    if not media.is_file():
        raise ValueError("normal render response has no output MP4")
    probe = FFProbeAdapter(resolve_local_executable("ffprobe")).probe(media)
    streams = probe.metadata.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if video is None or audio is None or video.get("width") != 1080 or video.get("height") != 1920 or not 30_000 <= probe.duration_ms <= 60_000:
        record.update(status="blocked_technical_qa", media=str(media), duration_ms=probe.duration_ms)
        save(record)
        raise ValueError("rendered MP4 failed playable vertical 30–60s technical gate")
    record.update(
        status="technical_qa_pass_visual_review_pending", render_id=str(render_id), media=str(media),
        sha256=sha256(media), duration_ms=probe.duration_ms,
        width=video["width"], height=video["height"], audio_stream_present=True,
    )
    save(record)
    print(json.dumps({key: record[key] for key in ("status", "render_id", "media", "sha256", "duration_ms", "width", "height")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
