from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import AssetRepository, Database
from app.domain.models import Asset, Job, JobType, RationalFps
from app.jobs.handlers import AssetAnalysisJobHandler
from app.jobs.targets import AssetJobTargetStore
from app.jobs.store import JobStore
from app.providers.asr import ASRConfigurationError
from app.worker_cli import build_runner, parse_config, run


def test_worker_defaults_and_type_selection(monkeypatch):
    monkeypatch.delenv("CONTENT_OS_DB_PATH", raising=False)
    config = parse_config(["--once", "--job-type", "analyze_asset"])
    assert config.once is True
    assert config.job_types == (JobType.ANALYZE_ASSET,)
    assert config.db_path == Path("content-os-data/content-os.sqlite3")


def test_transcribe_requires_runtime_key_but_analyze_does_not(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CONTENT_OS_ASR_API_KEY", raising=False)
    db = Database(tmp_path / "worker.sqlite")
    try:
        build_runner(parse_config(["--once", "--db", str(tmp_path / "worker.sqlite"), "--job-type", "analyze_asset"]), db)
        with pytest.raises(ASRConfigurationError, match="supplied at runtime"):
            build_runner(parse_config(["--once", "--db", str(tmp_path / "worker.sqlite"), "--job-type", "transcribe_audio"]), db)
    finally:
        db.close()


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


def test_build_runner_uses_asset_target_store_and_analyze_filter(tmp_path: Path, monkeypatch):
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


def test_signal_callback_stops_polling(monkeypatch, tmp_path: Path):
    captured = {}

    def fake_signal(signum, callback):
        previous = captured.get(signum)
        captured[signum] = callback
        return previous

    monkeypatch.setattr("app.worker_cli.signal.signal", fake_signal)
    monkeypatch.setattr("app.worker_cli.run", lambda config, stop_event: (stop_event.set() or 0))
    from app.worker_cli import main
    assert main(["--once", "--db", str(tmp_path / "worker.sqlite"), "--job-type", "analyze_asset"]) == 0
    assert captured
