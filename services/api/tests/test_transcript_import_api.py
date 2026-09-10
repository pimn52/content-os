from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import AssetRepository, AudioAssetRepository, ClipRepository, Database
from app.domain.models import Asset, AudioAsset, Clip, RationalFps, SourceKind
from app.main import create_app


def _seed(tmp_path: Path):
    db_path = tmp_path / "transcript-import.sqlite"
    source = tmp_path / "talking.mp4"
    source.write_bytes(b"video")
    audio_source = tmp_path / "narration.wav"
    audio_source.write_bytes(b"audio")
    db = Database(db_path)
    asset = Asset(
        source_file=str(source), content_hash="c" * 64, duration_ms=5_000,
        width=1_280, height=720, fps=RationalFps(numerator=30, denominator=1),
        authorization_reference="creator-owned", imported_at=datetime.now(timezone.utc),
    )
    AssetRepository(db).create(asset)
    ClipRepository(db).create(Clip(asset_id=asset.id, start_ms=0, end_ms=5_000, asset_duration_ms=5_000))
    audio = AudioAsset(
        source_kind=SourceKind.USER_ASSET, source_file=str(audio_source), content_hash="a" * 64,
        duration_ms=2_000, sample_rate=48_000, channels=1,
        authorization_reference="creator-voice", imported_at=datetime.now(timezone.utc),
    )
    AudioAssetRepository(db).create(audio)
    db.close()
    return db_path, asset, audio


def test_transcript_import_endpoint_persists_user_provided_timing_and_provenance(tmp_path: Path):
    db_path, asset, _ = _seed(tmp_path)
    subtitle = tmp_path / "creator-captions.srt"
    subtitle.write_text(
        "1\n00:00:00,100 --> 00:00:01,200\n第一句来自本人字幕\n\n"
        "2\n00:00:01,300 --> 00:00:02,450\n第二句来自本人字幕\n",
        encoding="utf-8",
    )

    with TestClient(create_app(data_path=db_path)) as client:
        response = client.post(
            f"/assets/{asset.id}/transcript-imports",
            json={"source_path": str(subtitle), "source_reference": "creator-captions.srt"},
        )
        assert response.status_code == 201, response.text
        assert response.json() == {
            "asset_id": str(asset.id),
            "source_reference": "creator-captions.srt",
            "segment_count": 2,
            "updated_clip_count": 1,
        }
        clip = client.get(f"/assets/{asset.id}/clips").json()[0]
        assert clip["transcript"] == "第一句来自本人字幕 第二句来自本人字幕"
        assert [(item["start_ms"], item["end_ms"]) for item in clip["transcript_segments"]] == [(100, 1_200), (1_300, 2_450)]
        assert clip["transcript_source"] == "creator-captions.srt"


def test_transcript_import_endpoint_reports_missing_clips_or_source(tmp_path: Path):
    db_path, asset, audio = _seed(tmp_path)
    subtitle = tmp_path / "captions.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,500\nprovided\n", encoding="utf-8")
    with TestClient(create_app(data_path=db_path)) as client:
        assert client.post(
            f"/assets/{asset.id}/transcript-imports",
            json={"source_path": str(tmp_path / "missing.srt"), "source_reference": "missing"},
        ).status_code == 404
        assert client.post(
            "/assets/00000000-0000-0000-0000-000000000000/transcript-imports",
            json={"source_path": str(subtitle), "source_reference": "provided"},
        ).status_code == 404


def test_audio_transcript_import_endpoint_persists_timing_for_new_narration(tmp_path: Path):
    db_path, _, audio = _seed(tmp_path)
    subtitle = tmp_path / "narration.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:00,800\n第一句旁白\n\n"
        "2\n00:00:00,900 --> 00:00:01,900\n第二句旁白\n",
        encoding="utf-8",
    )
    with TestClient(create_app(data_path=db_path)) as client:
        response = client.post(
            f"/audio-assets/{audio.id}/transcript-imports",
            json={"source_path": str(subtitle), "source_reference": "narration.srt"},
        )
        assert response.status_code == 201, response.text
        assert response.json() == {
            "audio_asset_id": str(audio.id),
            "source_reference": "narration.srt",
            "segment_count": 2,
        }
        stored = client.get(f"/audio-assets/{audio.id}").json()
        assert stored["transcript_source"] == "narration.srt"
        assert [(item["start_ms"], item["end_ms"]) for item in stored["transcript_segments"]] == [(0, 800), (900, 1_900)]
