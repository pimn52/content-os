from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sys

import pytest

from app.db import AssetRepository, ClipRepository, Database, VoiceProfileRepository
from app.domain.models import Asset, Clip, ConsentRecord, Job, JobType, RationalFps, VoiceProfile
from app.jobs.handlers import AssetAnalysisJobHandler
from app.jobs.targets import AssetJobTargetStore
from app.jobs.store import JobStore
from app.providers.asr import ASRConfigurationError
from app.providers.talking import TalkingConfigurationError
from app.providers.vision import VisionConfigurationError
from app.runtime import resolve_local_executable
from app.worker_cli import _build_voice_provider, _resolve_omnivoice_reference_path, build_runner, parse_config, run
from app.providers.voice import VoiceInputError
from app.voice_performance import authorized_voice_reference_choices


def test_worker_defaults_and_type_selection(monkeypatch):
    monkeypatch.delenv("CONTENT_OS_DB_PATH", raising=False)
    monkeypatch.delenv("CONTENT_OS_FFMPEG", raising=False)
    config = parse_config(["--once", "--job-type", "analyze_asset"])
    assert config.once is True
    assert config.job_types == (JobType.ANALYZE_ASSET,)
    assert config.gpu_resource_key.startswith("gpu:")
    assert config.db_path == Path("content-os-data/content-os.sqlite3")
    assert config.ffmpeg == resolve_local_executable("ffmpeg")
    assert config.ffprobe == resolve_local_executable("ffprobe")


def test_omnivoice_selected_reference_rechecks_source_hash_before_inference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original authorized bytes")
    model = tmp_path / "model"
    model.mkdir()
    monkeypatch.setenv("CONTENT_OS_VOICE_PROVIDER", "omnivoice")
    monkeypatch.setenv("CONTENT_OS_OMNIVOICE_PYTHON", sys.executable)
    monkeypatch.setenv("CONTENT_OS_OMNIVOICE_MODEL", str(model))
    db_path = tmp_path / "selected-worker.sqlite"
    with Database(db_path) as db:
        asset = Asset(source_file=str(source), content_hash=hashlib.sha256(source.read_bytes()).hexdigest(), duration_ms=12_000, width=720, height=1280, fps=RationalFps(numerator=25, denominator=1), authorization_reference="source-rights", imported_at=datetime.now(timezone.utc))
        AssetRepository(db).create(asset)
        clip = Clip(asset_id=asset.id, start_ms=0, end_ms=12_000, asset_duration_ms=12_000, voice_candidate=True, transcript_segments=[{"start_ms": 0, "end_ms": 3_000, "text": "这是前半句"}, {"start_ms": 3_100, "end_ms": 6_000, "text": "这是后半句"}])
        ClipRepository(db).create(clip)
        profile = VoiceProfile(name="Creator", provider="omnivoice", provider_profile_id="creator", reference_clip_ids=[clip.id], consent=ConsentRecord(subject_name="Creator", basis="self", confirmed=True, confirmed_at=datetime.now(timezone.utc)), created_at=datetime.now(timezone.utc))
        VoiceProfileRepository(db).create(profile)
        selection, _ = authorized_voice_reference_choices(profile, [clip], {asset.id: asset}, data_root=tmp_path)[0]
        config = parse_config(["--once", "--db", str(db_path), "--job-type", "generate_voice"])
        provider = _build_voice_provider(config, db)
        source.write_bytes(b"changed source bytes")
        with pytest.raises(VoiceInputError, match="source changed"):
            provider.synthesize_with_reference(profile, "新文案", tmp_path / "output.wav", reference_window=selection)


def test_transcribe_requires_runtime_key_but_analyze_does_not(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CONTENT_OS_ASR_API_KEY", raising=False)
    monkeypatch.delenv("CONTENT_OS_ASR_PROVIDER", raising=False)
    db = Database(tmp_path / "worker.sqlite")
    try:
        build_runner(parse_config(["--once", "--db", str(tmp_path / "worker.sqlite"), "--job-type", "analyze_asset"]), db)
        with pytest.raises(ASRConfigurationError, match="supplied at runtime"):
            build_runner(parse_config(["--once", "--db", str(tmp_path / "worker.sqlite"), "--job-type", "transcribe_audio"]), db)
    finally:
        db.close()


def test_transcribe_can_select_local_faster_whisper_without_api_key(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CONTENT_OS_ASR_API_KEY", raising=False)
    monkeypatch.setenv("CONTENT_OS_ASR_PROVIDER", "local")
    db = Database(tmp_path / "local-asr-worker.sqlite")
    try:
        runner = build_runner(parse_config(["--once", "--db", str(tmp_path / "local-asr-worker.sqlite"), "--job-type", "transcribe_audio"]), db)
        assert runner.handler_types == {JobType.TRANSCRIBE_AUDIO}
        assert runner._handlers[JobType.TRANSCRIBE_AUDIO]._provider.__class__.__name__ == "FasterWhisperASRProvider"
    finally:
        db.close()


def test_vision_requires_runtime_key_and_builds_only_index_handler(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CONTENT_OS_VISION_API_KEY", raising=False)
    monkeypatch.delenv("CONTENT_OS_EMBEDDING_API_KEY", raising=False)
    db = Database(tmp_path / "vision-worker.sqlite")
    try:
        config = parse_config(["--once", "--db", str(tmp_path / "vision-worker.sqlite"), "--job-type", "index_clips"])
        with pytest.raises(VisionConfigurationError, match="supplied at runtime"):
            build_runner(config, db)
        monkeypatch.setenv("CONTENT_OS_VISION_API_KEY", "runtime-only-test-key")
        monkeypatch.setenv("CONTENT_OS_EMBEDDING_API_KEY", "runtime-only-embedding-key")
        runner = build_runner(config, db)
        assert runner.handler_types == {JobType.INDEX_CLIPS}
    finally:
        db.close()


def test_talking_worker_refuses_unadmitted_provider(tmp_path: Path):
    db_path = tmp_path / "talking-worker.sqlite"
    db = Database(db_path)
    try:
        config = parse_config(["--once", "--db", str(db_path), "--job-type", "generate_talking"])
        with pytest.raises(TalkingConfigurationError, match="no Talking provider is currently admitted"):
            build_runner(config, db)
    finally:
        db.close()


def test_talking_worker_builds_explicit_latentsync_provider(tmp_path: Path, monkeypatch):
    repo = tmp_path / "latentsync"
    (repo / "scripts").mkdir(parents=True)
    (repo / "configs" / "unet").mkdir(parents=True)
    (repo / "scripts" / "inference.py").write_text("", encoding="utf-8")
    (repo / "configs" / "unet" / "stage2.yaml").write_text("", encoding="utf-8")
    checkpoint = tmp_path / "latentsync_unet.pt"
    checkpoint.write_bytes(b"weights")
    provider_ffmpeg = tmp_path / "latentsync-ffmpeg.exe"
    provider_ffmpeg.write_bytes(b"ffmpeg")
    monkeypatch.setenv("CONTENT_OS_TALKING_PROVIDER", "latentsync")
    monkeypatch.setenv("CONTENT_OS_LATENTSYNC_PYTHON", sys.executable)
    monkeypatch.setenv("CONTENT_OS_LATENTSYNC_REPO", str(repo))
    monkeypatch.setenv("CONTENT_OS_LATENTSYNC_CHECKPOINT", str(checkpoint))
    monkeypatch.setenv("CONTENT_OS_LATENTSYNC_FFMPEG", str(provider_ffmpeg))
    db_path = tmp_path / "talking-latentsync-worker.sqlite"
    db = Database(db_path)
    try:
        config = parse_config(["--once", "--db", str(db_path), "--job-type", "generate_talking"])
        runner = build_runner(config, db)
        handler = runner._handlers[JobType.GENERATE_TALKING]
        provider = handler._provider
        assert provider.__class__.__name__ == "LatentSyncProvider"
        assert provider.provider_name == "latentsync"
        assert provider.ffmpeg_command == (str(provider_ffmpeg),)
        assert handler._ffmpeg_command == config.ffmpeg
        assert provider.runtime_metadata.estimated_cost.amount == 0
    finally:
        db.close()


def test_voice_worker_builds_explicit_omnivoice_provider(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "python.exe"
    runtime.write_bytes(b"runtime")
    model = tmp_path / "omnivoice-snapshot"
    model.mkdir()
    monkeypatch.setenv("CONTENT_OS_VOICE_PROVIDER", "omnivoice")
    monkeypatch.setenv("CONTENT_OS_OMNIVOICE_PYTHON", str(runtime))
    monkeypatch.setenv("CONTENT_OS_OMNIVOICE_MODEL", str(model))
    db_path = tmp_path / "voice-omnivoice-worker.sqlite"
    db = Database(db_path)
    try:
        config = parse_config(["--once", "--db", str(db_path), "--job-type", "generate_voice"])
        runner = build_runner(config, db)
        provider = runner._handlers[JobType.GENERATE_VOICE]._provider
        assert provider.__class__.__name__ == "OmniVoiceProvider"
        assert provider.provider_name == "omnivoice"
        assert runner._resource_keys_by_type == {JobType.GENERATE_VOICE: config.gpu_resource_key}
    finally:
        db.close()


def test_omnivoice_worker_resolves_legacy_bare_reference_media_from_local_originals(tmp_path: Path, monkeypatch):
    """A pre-portable record may retain only its content-addressed filename."""
    runtime = tmp_path / "python.exe"
    runtime.write_bytes(b"runtime")
    model = tmp_path / "omnivoice-snapshot"
    model.mkdir()
    data_root = tmp_path / "data"
    original = data_root / "assets" / "originals" / ("a" * 64 + ".media")
    original.parent.mkdir(parents=True)
    original.write_bytes(b"reference")
    assert runtime.is_file() and model.is_dir()  # Deliberately no provider inference.
    assert _resolve_omnivoice_reference_path("a" * 64 + ".media", data_root) == original
    assert _resolve_omnivoice_reference_path("assets/originals/" + "a" * 64 + ".media", data_root) == original
    assert _resolve_omnivoice_reference_path("data/assets/originals/" + "a" * 64 + ".media", data_root) == original


def test_gpu_resource_key_can_be_overridden_for_one_local_machine(tmp_path: Path):
    config = parse_config([
        "--once", "--db", str(tmp_path / "worker.sqlite"), "--job-type", "generate_talking",
        "--gpu-resource-key", "gpu:asus-rtx3060-laptop-6gb",
    ])
    assert config.gpu_resource_key == "gpu:asus-rtx3060-laptop-6gb"


def test_once_uses_fake_runner_and_closes_database(tmp_path: Path, monkeypatch):
    calls = []

    class FakeRunner:
        handler_types = frozenset({JobType.ANALYZE_ASSET})
        def recover_expired(self):
            return []
        def run_once(self, **kwargs):
            calls.append("run")
            return None

    monkeypatch.setattr("app.worker_cli.build_runner", lambda config, db: FakeRunner())
    closed = []

    class RecordingDatabase:
        def __init__(self, path):
            self.path = path
        def close(self):
            closed.append(self.path)

    monkeypatch.setattr("app.worker_cli.Database", RecordingDatabase)
    config = parse_config(["--once", "--db", str(tmp_path / "worker.sqlite"), "--job-type", "analyze_asset"])
    assert run(config) == 0
    assert calls == ["run"]
    assert closed == [config.db_path]


@pytest.mark.parametrize("option", ["--lease-seconds", "--heartbeat-seconds", "--poll-seconds"])
@pytest.mark.parametrize("value", ["nan", "inf", "0"])
def test_worker_rejects_non_finite_or_non_positive_float_options(option, value):
    with pytest.raises(SystemExit):
        parse_config([option, value, "--job-type", "analyze_asset"])


def test_build_failure_still_closes_database(tmp_path: Path, monkeypatch):
    closed = []

    class RecordingDatabase:
        def __init__(self, path):
            self.path = path
        def close(self):
            closed.append(self.path)

    monkeypatch.setattr("app.worker_cli.Database", RecordingDatabase)
    monkeypatch.setattr("app.worker_cli.build_runner", lambda config, db: (_ for _ in ()).throw(ASRConfigurationError("safe config error")))
    config = parse_config(["--once", "--db", str(tmp_path / "worker.sqlite"), "--job-type", "analyze_asset"])
    with pytest.raises(ASRConfigurationError):
        run(config)
    assert closed == [config.db_path]


def test_build_runner_uses_asset_target_store_and_analyze_filter(tmp_path: Path):
    db = Database(tmp_path / "worker.sqlite")
    try:
        config = parse_config(["--once", "--db", str(tmp_path / "worker.sqlite"), "--job-type", "analyze_asset"])
        runner = build_runner(config, db)
        assert runner.handler_types == {JobType.ANALYZE_ASSET}
        assert runner._handlers[JobType.ANALYZE_ASSET]._target_store.__class__ is AssetJobTargetStore
        source = tmp_path / "asset.mp4"
        source.write_bytes(b"asset")
        asset = Asset(source_file=str(source), content_hash="f" * 64, duration_ms=1_000, width=10, height=10, fps=RationalFps(numerator=25, denominator=1), authorization_reference="rights", imported_at=datetime.now(timezone.utc))
        AssetRepository(db).create(asset)
        now = datetime.now(timezone.utc)
        job = Job(type=JobType.TRANSCRIBE_AUDIO, idempotency_key="pending-transcribe", created_at=now, updated_at=now)
        AssetJobTargetStore(db).enqueue(job, asset.id)
        assert runner.run_once(allowed_types=runner.handler_types) is None
        assert db.connection.execute("SELECT status FROM jobs WHERE id = ?", (str(job.id),)).fetchone()[0] == "pending"
    finally:
        db.close()


def test_analysis_handler_resolves_asset_through_target_store(tmp_path: Path):
    db = Database(tmp_path / "resolve.sqlite")
    try:
        source = tmp_path / "asset.mp4"
        source.write_bytes(b"asset")
        asset = Asset(source_file=str(source), content_hash="a" * 64, duration_ms=1_000, width=10, height=10, fps=RationalFps(numerator=25, denominator=1), authorization_reference="rights", imported_at=datetime.now(timezone.utc))
        AssetRepository(db).create(asset)
        now = datetime.now(timezone.utc)
        job = AssetJobTargetStore(db).enqueue(Job(type=JobType.ANALYZE_ASSET, idempotency_key="resolve", created_at=now, updated_at=now), asset.id)
        claimed = JobStore(db).claim("worker", timedelta(seconds=30), max_attempts=3)
        seen = []

        class Pipeline:
            def process(self, value):
                seen.append(value.id)

        handler = AssetAnalysisJobHandler(AssetJobTargetStore(db), AssetRepository(db), Pipeline())
        handler(claimed)
        assert seen == [asset.id]
    finally:
        db.close()


def test_render_worker_composes_audio_and_image_repositories(tmp_path: Path):
    db = Database(tmp_path / "render.sqlite")
    try:
        runner = build_runner(parse_config(["--once", "--db", str(tmp_path / "render.sqlite"), "--job-type", "render"]), db)
        renderer = runner._handlers[JobType.RENDER]._renderer
        assert renderer.audios is not None
        assert renderer.images is not None
    finally:
        db.close()
