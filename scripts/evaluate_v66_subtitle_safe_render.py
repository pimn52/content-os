"""One bounded subtitle-safe render of V65's exact reviewed media inputs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from app.domain.models import SourceKind, VerticalReframeMode, VideoSpec  # noqa: E402
from app.main import create_app  # noqa: E402
from app.media import FFProbeAdapter  # noqa: E402
from app.runtime import resolve_local_executable  # noqa: E402

DATA = ROOT / "content-os-data"
DB = DATA / "content-os.sqlite3"
V65_MANIFEST = DATA / "evaluation-evidence" / "v65-assisted-render" / "manifest.json"
EVIDENCE = DATA / "evaluation-evidence" / "v66-subtitle-safe-render"
MANIFEST = EVIDENCE / "manifest.json"
TITLES = {
    "editorial-structure": "问题 · 例子 · 语境",
    "closing-judgment": "留下完整的判断",
}
CROP_RATIO = 0.15
CROP_REFERENCE = "v66:source-frame-preflight:3s-6s-12s-21s-24s"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save(value: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def corrected_spec() -> tuple[VideoSpec, dict]:
    v65 = json.loads(V65_MANIFEST.read_text(encoding="utf-8"))
    if v65.get("status") != "blocked_visual_qa" or v65.get("render_attempt_limit") != 1:
        raise ValueError("exact blocked V65 source is unavailable")
    original = VideoSpec.model_validate(v65["video_spec"])
    if len(original.scenes) != 4 or original.master_narration is None:
        raise ValueError("V65 VideoSpec has unexpected shape")
    kinds = [scene.visual.source_kind for scene in original.scenes]
    if kinds != [SourceKind.AI_VIDEO, SourceKind.TYPOGRAPHY, SourceKind.AI_VIDEO, SourceKind.TYPOGRAPHY]:
        raise ValueError("V65 visual route changed")
    corrected = []
    for scene in original.scenes:
        if scene.visual.source_kind is SourceKind.AI_VIDEO:
            visual = scene.visual.model_copy(update={
                "vertical_reframe_mode": VerticalReframeMode.CENTER_CROP,
                "vertical_reframe_evidence_reference": CROP_REFERENCE,
                "source_bottom_crop_ratio": CROP_RATIO,
            })
            corrected.append(scene.model_copy(update={"visual": visual}))
        else:
            title = TITLES.get(scene.scene_id)
            if title is None:
                raise ValueError("unexpected typography scene")
            corrected.append(scene.model_copy(update={"caption": title}))
    spec = VideoSpec.model_validate(original.model_copy(update={"scenes": corrected}).model_dump(mode="json"))
    for before, after in zip(original.scenes, spec.scenes, strict=True):
        if (
            before.scene_id != after.scene_id or before.start_frame != after.start_frame
            or before.duration_frames != after.duration_frames
            or before.narration_asset_id != after.narration_asset_id
            or before.narration_start_ms != after.narration_start_ms
            or before.narration_end_ms != after.narration_end_ms
            or before.captions != after.captions
        ):
            raise ValueError("V66 changed scene timing, Master interval or actual transcript captions")
        if before.visual.source_kind is SourceKind.AI_VIDEO:
            if before.caption != after.caption or before.visual.asset_id != after.visual.asset_id or before.visual.clip_id != after.visual.clip_id:
                raise ValueError("V66 changed Talking media or copy")
        elif before.visual != after.visual or after.caption != TITLES[before.scene_id]:
            raise ValueError("V66 typography adjustment exceeds title deduplication")
    if original.master_narration != spec.master_narration:
        raise ValueError("V66 changed the approved Master")
    return spec, v65


def main() -> None:
    if MANIFEST.exists():
        raise ValueError("V66 manifest already exists; do not repeat the bounded render")
    spec, v65 = corrected_spec()
    source = DATA / "assets" / "originals" / "4e5f49859a4cc441b09332791a4be658cbabeb5c6a770f3dbcd1e226a06bf3b8.media"
    if sha256(source) != "4e5f49859a4cc441b09332791a4be658cbabeb5c6a770f3dbcd1e226a06bf3b8":
        raise ValueError("admitted TalkingRun source bytes changed")
    preflight = [DATA / "evaluation-evidence" / "v65-assisted-render" / f"preflight-crop-{second}s.png"
                 for second in (3, 6, 12, 21, 24)]
    if any(not path.is_file() for path in preflight):
        raise ValueError("source subtitle/face preflight frames unavailable")
    record = {
        "package": "V66", "status": "rendering", "source_v65_render_sha256": v65["sha256"],
        "talking_run_sha256": sha256(source), "crop_ratio": CROP_RATIO,
        "crop_evidence_reference": CROP_REFERENCE,
        "preflight_frames": [{"path": str(path), "sha256": sha256(path)} for path in preflight],
        "typography_titles": TITLES,
        "stable": "same Master, TalkingRun, four scene timings and transcript captions as V65",
        "render_attempt_limit": 1, "video_spec": spec.model_dump(mode="json"),
    }
    save(record)
    with TestClient(create_app(DB)) as api:
        response = api.post(f"/projects/{spec.project_id}/render", json={"video_spec": record["video_spec"]})
        if response.status_code != 200:
            record.update(status="blocked_render", error=response.text[:2_000])
            save(record)
            raise ValueError(f"one normal render failed: {response.status_code} {response.text}")
        rendered = response.json()
    media = DATA / "renders" / str(spec.project_id) / f"{rendered['render_id']}.mp4"
    if not media.is_file():
        record.update(status="blocked_missing_render", render_id=rendered["render_id"])
        save(record)
        raise ValueError("normal render output file missing")
    probe = FFProbeAdapter(resolve_local_executable("ffprobe")).probe(media)
    streams = probe.metadata.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    record.update(render_id=rendered["render_id"], media=str(media), sha256=sha256(media),
                  duration_ms=probe.duration_ms,
                  width=None if video is None else video.get("width"),
                  height=None if video is None else video.get("height"),
                  audio_stream_present=audio is not None)
    if video is None or audio is None or video.get("width") != 1080 or video.get("height") != 1920 or not 30_000 <= probe.duration_ms <= 60_000:
        record["status"] = "blocked_technical_qa"
        save(record)
        raise ValueError("rendered MP4 failed vertical 30–60s technical QA")
    record["status"] = "technical_qa_pass_visual_review_pending"
    save(record)
    print(json.dumps({key: record[key] for key in ("status", "render_id", "media", "sha256", "duration_ms")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
