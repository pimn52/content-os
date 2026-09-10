from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import AssetRepository
from app.domain.models import Asset, AudioAsset, RationalFps, ScenePlan, SourceKind, VisualIntent
from app.main import create_app


def test_browser_upload_imports_one_video_without_source_path(tmp_path: Path) -> None:
    calls: list[tuple[bytes, str, str, str]] = []

    class FakeImporter:
        def import_stream(self, stream, filename, authorization_reference, *, source_kind):
            calls.append((stream.read(), filename, authorization_reference, source_kind.value))
            return Asset(
                id=uuid4(), source_file=str(tmp_path / "stored.media"), content_hash="b" * 64,
                duration_ms=1_000, width=320, height=180, fps=RationalFps(numerator=25, denominator=1),
                authorization_reference=authorization_reference, imported_at="2026-09-09T00:00:00Z",
            )

    with TestClient(create_app(tmp_path / "upload.sqlite", media_importer_factory=lambda db, root: FakeImporter())) as client:
        response = client.post(
            "/uploads",
            files={"file": ("phone-clip.mp4", b"video bytes", "video/mp4")},
            data={"authorization_reference": "creator-owned", "source_kind": "user_asset"},
        )
        rejected = client.post(
            "/uploads",
            files={"file": ("notes.txt", b"not a video", "text/plain")},
            data={"authorization_reference": "creator-owned"},
        )

    assert response.status_code == 201
    assert response.json()["authorization_reference"] == "creator-owned"
    assert calls == [(b"video bytes", "phone-clip.mp4", "creator-owned", "user_asset")]
    assert rejected.status_code == 422


def test_browser_upload_imports_authorized_audio_without_source_path(tmp_path: Path) -> None:
    calls: list[tuple[bytes, str, str, str, str | None]] = []

    class FakeAudioImporter:
        def import_stream(self, stream, filename, authorization_reference, *, source_kind, language):
            calls.append((stream.read(), filename, authorization_reference, source_kind.value, language))
            return AudioAsset(
                id=uuid4(), source_kind=source_kind, source_file=str(tmp_path / "stored.wav"), content_hash="c" * 64,
                duration_ms=1_200, sample_rate=48_000, channels=1,
                authorization_reference=authorization_reference, imported_at="2026-09-09T00:00:00Z", language=language,
            )

    with TestClient(create_app(tmp_path / "audio-upload.sqlite", audio_importer_factory=lambda db, root: FakeAudioImporter())) as client:
        response = client.post(
            "/audio-uploads",
            files={"file": ("scene-01.wav", b"audio bytes", "audio/wav")},
            data={"authorization_reference": "creator-voice", "source_kind": "user_asset", "language": "zh-CN"},
        )
        rejected = client.post(
            "/audio-uploads",
            files={"file": ("notes.txt", b"not audio", "text/plain")},
            data={"authorization_reference": "creator-voice"},
        )

    assert response.status_code == 201
    assert response.json()["authorization_reference"] == "creator-voice"
    assert response.json()["language"] == "zh-CN"
    assert calls == [(b"audio bytes", "scene-01.wav", "creator-voice", "user_asset", "zh-CN")]
    assert rejected.status_code == 422


def test_browser_video_upload_binds_confirmed_shoot_task(tmp_path: Path) -> None:
    calls: list[tuple[bytes, str]] = []

    class FakeImporter:
        def __init__(self, db):
            self.db = db

        def import_stream(self, stream, filename, authorization_reference, *, source_kind):
            calls.append((stream.read(), filename))
            asset = Asset(
                id=uuid4(), source_file=str(tmp_path / "captured.media"), content_hash="d" * 64,
                duration_ms=3_200, width=1080, height=1920, fps=RationalFps(numerator=30, denominator=1),
                authorization_reference=authorization_reference, imported_at="2026-09-09T00:00:00Z",
            )
            AssetRepository(self.db).create(asset)
            return asset

    with TestClient(create_app(tmp_path / "shoot-upload.sqlite", media_importer_factory=lambda db, root: FakeImporter(db))) as client:
        project = client.post("/projects", json={"title": "Capture", "topic": "补拍"}).json()
        scene = ScenePlan(
            project_id=project["id"], scene_id="scene-1", order=0, purpose="b-roll",
            voice_text="展示操作画面", duration_target_ms=3_000,
            visual_intent=VisualIntent(subject="手机", action="滑动屏幕", framing="close"),
            preferred_sources=[SourceKind.USER_ASSET], fallback_sources=[SourceKind.CAPTURE],
        )
        assert client.put(f"/projects/{project['id']}/draft", json={"scenes": [scene.model_dump(mode="json")]}).status_code == 200
        task = client.post(f"/projects/{project['id']}/shoot-tasks", json={
            "scene_plan_id": str(scene.id), "scene_id": scene.scene_id,
            "what_to_shoot": "拍摄手机，滑动屏幕。", "framing": "close",
            "duration_ms": 3_000, "requires_speaking": False,
        })
        upload = client.post(
            "/uploads",
            files={"file": ("capture.mp4", b"video bytes", "video/mp4")},
            data={"authorization_reference": "creator-capture", "shoot_task_id": task.json()["id"]},
        )
        bound = client.get(f"/projects/{project['id']}/shoot-tasks")

    assert upload.status_code == 201
    assert calls == [(b"video bytes", "capture.mp4")]
    assert bound.status_code == 200
    assert bound.json()[0]["status"] == "fulfilled"
    assert bound.json()[0]["asset_id"] == upload.json()["id"]


def test_private_access_token_protects_api_but_keeps_static_entry_public(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONTENT_OS_ACCESS_TOKEN", "local-private-token")
    with TestClient(create_app(tmp_path / "access.sqlite")) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/projects").status_code == 401
        authorized = client.get("/projects", headers={"Authorization": "Bearer local-private-token"})
        assert authorized.status_code == 200
        assert client.get("/", follow_redirects=False).status_code == 307
