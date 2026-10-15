"""One assisted R1-V render through unchanged production admission checks.

Uses a consistent SQLite backup, exact approved assets and the existing local
Remotion/FFmpeg installation. Never invents QA, rights or purpose approvals.
"""
from __future__ import annotations

import hashlib
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from fastapi.testclient import TestClient
from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database
from app.domain.models import VideoSpec
from app.main import create_app
from app.renderer.remotion import RemotionRenderer, SubprocessRenderCommandRunner

DATA = ROOT / "content-os-data"
EVIDENCE = DATA / "evaluation-evidence" / "r1-v-portrait-composition"
MANIFEST = EVIDENCE / "manifest.json"
FFMPEG = DATA / "latentsync-runtime310" / "Library" / "bin" / "ffmpeg.exe"
FFPROBE = FFMPEG.with_name("ffprobe.exe")
SOURCE_SHA = "4e5f49859a4cc441b09332791a4be658cbabeb5c6a770f3dbcd1e226a06bf3b8"


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def probe(path: Path) -> dict:
    result = subprocess.run([str(FFPROBE), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], check=True, capture_output=True)
    return json.loads(result.stdout)


def build_spec() -> VideoSpec:
    prior = json.loads((DATA / "evaluation-evidence" / "v68-edit-plan-render" / "manifest.json").read_text(encoding="utf-8"))
    value = copy.deepcopy(prior["video_spec"])
    # This is explicitly a revised assisted VideoSpec. Do not attach the old
    # EditPlan which declared inherited subtitles and a different presentation.
    value["edit_plan"] = None
    for scene in value["scenes"]:
        scene["style_tokens"] = {
            "name": "content-os-editorial", "background_color": "#f3efe5",
            "foreground_color": "#172033", "accent_color": "#ca4c2c",
            "font_family": '"Microsoft YaHei", "Noto Sans CJK SC", sans-serif',
        }
        if scene["visual"]["source_kind"] == "ai_video":
            scene["visual"].update(
                vertical_reframe_mode="bottom_crop_contain", source_bottom_crop_ratio=.15,
                vertical_reframe_evidence_reference="r1-v-source-strip:sha256:" + SOURCE_SHA,
            )
            scene.update(portrait_presentation="portrait_panel", subtitle_treatment="timed_captions",
                         graphic_treatment="headline", graphic_text="别先找金句\n先保住原意" if scene["start_frame"] == 0 else "抓眼球\n也要保住语境")
        if scene["scene_id"] == "closing-judgment":
            scene["graphic_text"] = "完整判断\n一阵情绪"
    spec = VideoSpec.model_validate(value)
    # No changes to inputs, source intervals, speech, timing or timed captions.
    for before, after in zip(prior["video_spec"]["scenes"], spec.scenes):
        for field in ("scene_id", "start_frame", "duration_frames", "caption", "captions", "narration_asset_id", "narration_start_ms", "narration_end_ms"):
            assert before[field] == after.model_dump(mode="json")[field], field
    return spec


def add_presentation_cues(props: dict, cues: list[dict]) -> None:
    """Assisted evaluation-only layout cues; never rewrite sources or audio."""
    if not cues:
        return
    videos = [source for source in props["sceneSources"].values() if source["kind"] == "video"]
    if len(videos) != 1 or videos[0].get("portraitPresentation") != "portrait_panel":
        raise ValueError("presentation cues require one continuous portrait panel")
    duration = next(scene["duration_frames"] for scene in props["videoSpec"]["scenes"] if scene["visual"]["source_kind"] == "ai_video")
    expected = 0
    for cue in cues:
        if cue["startFrame"] != expected or cue["durationFrames"] <= 0 or cue["treatment"] not in {"headline", "key_point", "contrast"}:
            raise ValueError("presentation cues must be positive, contiguous and semantic")
        expected += cue["durationFrames"]
    if expected != duration:
        raise ValueError("presentation cues must cover the continuous panel exactly")
    videos[0]["presentationCues"] = copy.deepcopy(cues)


def add_sparse_emphasis(props: dict, cues: list[dict]) -> None:
    """Assisted square-in-portrait callouts, separate from continuous cues.

    The caller verifies square source dimensions; this is not persisted EditPlan.
    """
    videos = [source for source in props["sceneSources"].values() if source["kind"] == "video"]
    spec = props["videoSpec"]
    if len(videos) != 1 or (spec["width"], spec["height"]) != (1080, 1920):
        raise ValueError("emphasis requires one square-in-portrait evaluation source")
    source = videos[0]
    if (source.get("portraitPresentation") != "full_canvas"
            or source.get("verticalReframeMode") != "contain"
            or source.get("sourceBottomCropRatio", 0) != 0
            or source.get("styleTokens", {}).get("name") != "content-os-subject-first"):
        raise ValueError("emphasis must preserve the selected contain presentation")
    duration = next(scene["duration_frames"] for scene in spec["scenes"] if scene["visual"]["source_kind"] == "ai_video")
    end = total = 0
    if len(cues) > 4:
        raise ValueError("too many emphasis cues")
    for cue in cues:
        start, length = cue["startFrame"], cue["durationFrames"]
        title = cue.get("title", "")
        if (type(start) is not int or type(length) is not int or start < end
                or not 0 < length <= 180 or start + length > duration
                or cue.get("treatment") != "key_point" or not isinstance(title, str)
                or not title.strip() or len(title.splitlines()) > 2
                or any(len(line) > 14 for line in title.splitlines())):
            raise ValueError("invalid, overlapping or oversized emphasis cue")
        end = start + length
        total += length
    if total > duration * .4:
        raise ValueError("emphasis must remain occasional")
    source["presentationCues"] = copy.deepcopy(cues)


def main(*, package: str = "R1-V", build=build_spec, evidence: Path = EVIDENCE, presentation_cues: list[dict] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    manifest = evidence / "manifest.json"
    cues = presentation_cues or []
    if manifest.exists():
        raise RuntimeError(f"{package} evidence already exists; the one-render budget cannot be replayed")
    if not FFMPEG.is_file() or not FFPROBE.is_file() or not (evidence / "source-sheet.png").is_file():
        raise RuntimeError("existing full FFmpeg or reviewed source contact sheet unavailable")
    os.environ["CONTENT_OS_FFMPEG"] = str(FFMPEG)
    os.environ["CONTENT_OS_DATA_ROOT"] = str(DATA)
    evidence.mkdir(parents=True, exist_ok=True)
    backup = evidence / "evaluation.sqlite3"
    if backup.exists():
        raise RuntimeError("evaluation backup already exists; do not overwrite evidence")
    with sqlite3.connect((DATA / "content-os.sqlite3").as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    spec = build()
    db = Database(backup)
    db.data_root = DATA
    asset = AssetRepository(db).get(UUID("2c98b774-f109-4377-a491-95a948d9b0ee"))
    master = AudioAssetRepository(db).get(spec.master_narration.audio_asset_id)
    assert asset and master
    def local(path: str) -> Path:
        value = Path(path)
        return value if value.is_absolute() else DATA / value
    assert asset.content_hash == SOURCE_SHA and sha(local(asset.source_file)) == SOURCE_SHA
    assert sha(local(master.source_file)) == master.content_hash
    record = {
        "package": package, "status": "preflighting", "evidence_level": "assisted VideoSpec/presentation cues; normal renderer admission; no normal UI or automatic EditPlan claim",
        "presentation_cues": cues,
        "work_class": "I", "model": "gpt-6.1-sol", "reasoning": "medium", "usage_cost": "unknown",
        "initial_render_limit": 1, "render_attempts": 0, "dependencies_or_licenses_changed": False,
        "source_sha256": SOURCE_SHA, "master_sha256": master.content_hash,
        "source_strip_evidence": {"reference": "r1-v-source-strip:sha256:" + SOURCE_SHA,
            "contact_sheet": str(evidence / "source-sheet.png"), "sha256": sha(evidence / "source-sheet.png"),
            "samples": "one frame per second, 0 through31s, original1280x720",
            "transform": "crop bottom108px only; full1280px width and612px upper area retained",
            "limits": "sampled visual inspection; no detector or independent whole-interval likeness/face-safety approval"},
        "video_spec": spec.model_dump(mode="json"), "ffmpeg": str(FFMPEG), "ffprobe": str(FFPROBE),
    }
    def save():
        manifest.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    save()

    class EvidenceRunner(SubprocessRenderCommandRunner):
        def run(self, argv, *, cwd, timeout_seconds):
            if "-vf" in argv:
                record["crop_argv"] = argv
            else:
                props = Path(next(item.split("=", 1)[1] for item in argv if item.startswith("--props=")))
                layout = json.loads(props.read_text(encoding="utf-8"))
                add_presentation_cues(layout, cues)
                props.write_text(json.dumps(layout, ensure_ascii=False), encoding="utf-8")
                (evidence / "render-props.json").write_bytes(props.read_bytes())
                record["render_attempts"] += 1
                if record["render_attempts"] > 1:
                    raise RuntimeError("initial render budget exhausted")
                record["render_argv"] = argv
            save()
            completed = super().run(argv, cwd=cwd, timeout_seconds=timeout_seconds)
            (evidence / ("crop.log" if "-vf" in argv else "render.log")).write_bytes(completed.stdout + completed.stderr)
            if "-vf" in argv and completed.returncode == 0:
                info = probe(Path(argv[-1]))
                record["crop_probe"] = info
                stream = next(s for s in info["streams"] if s["codec_type"] == "video")
                assert (stream["width"], stream["height"], stream["r_frame_rate"]) == (1280, 612, "25/1")
                assert abs(float(stream["duration"]) - 32.36) < .001
                save()
            return completed
    try:
        def renderer_factory(database):
            return RemotionRenderer(AssetRepository(database), ClipRepository(database), audios=AudioAssetRepository(database), renderer_dir=ROOT / "apps" / "renderer", runner=EvidenceRunner(), timeout_seconds=600)
        renderer = renderer_factory(db)
        renderer._validate_master_narration(spec, spec.fps)
        for scene in spec.scenes:
            renderer._validate_scene(scene, spec.fps, project_id=spec.project_id, master_audio_id=spec.master_narration.audio_asset_id, validate_scene_narration=False)
        db.close()
        record["status"] = "rendering"
        save()
        with TestClient(create_app(backup, render_output_root=evidence / "renders", renderer_factory=renderer_factory)) as api:
            result = api.post(f"/projects/{spec.project_id}/render", json={"video_spec": spec.model_dump(mode="json")})
            if result.status_code != 200:
                raise RuntimeError(f"normal render refused: {result.status_code} {result.text[:1000]}")
            response = result.json()
        media = evidence / "renders" / str(spec.project_id) / (response["render_id"] + ".mp4")
        record.update(render_id=response["render_id"], media=str(media), sha256=sha(media), probe=probe(media))
        streams = record["probe"]["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        audio = next(s for s in streams if s["codec_type"] == "audio")
        assert (video["width"], video["height"], video["nb_frames"], video["r_frame_rate"]) == (1080, 1920, "977", "30/1")
        assert 32.5 < float(audio["duration"]) < 32.7
        decode = subprocess.run([str(FFMPEG), "-nostdin", "-v", "error", "-xerror", "-i", str(media), "-f", "null", "-"], capture_output=True, timeout=120)
        (evidence / "decode.log").write_bytes(decode.stdout + decode.stderr)
        assert decode.returncode == 0
        sheet = evidence / "output-sheet.png"
        subprocess.run([str(FFMPEG), "-nostdin", "-v", "error", "-i", str(media), "-vf", "fps=1/3,scale=270:480,tile=4x3", "-frames:v", "1", str(sheet)], check=True, capture_output=True)
        for second in (3, 15, 18, 24, 30):
            subprocess.run([str(FFMPEG), "-nostdin", "-v", "error", "-ss", str(second), "-i", str(media), "-frames:v", "1", str(evidence / f"frame-{second:02d}.png")], check=True, capture_output=True)
        assert sha(local(asset.source_file)) == SOURCE_SHA and sha(local(master.source_file)) == master.content_hash
        record.update(status="technical_qa_pass_visual_inspection_pending", full_decode_pass=True, contact_sheet=str(sheet), contact_sheet_sha256=sha(sheet))
        save()
        print(json.dumps({key: record[key] for key in ("status", "media", "sha256", "render_attempts")}, ensure_ascii=False))
    except Exception as exc:
        record.update(status="blocked_or_failed", error=f"{type(exc).__name__}: {exc}")
        save()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
