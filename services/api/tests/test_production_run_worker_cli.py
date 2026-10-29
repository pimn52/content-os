"""Production workers consume only durable references and never retry a failure."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from threading import Event
from uuid import UUID, uuid4

import pytest

from app.db import Database, IPProfileRepository, JobRepository, ProjectRepository
from app.domain.models import IPProfile, Job, JobStatus, JobType, Project, RationalFps, TalkingQaJobPayload
from app.jobs.runner import JobExecutionError, JobRunner
from app.jobs.store import JobStore
from app.worker_cli import parse_config, run


def _fixture(path):
    db = Database(path)
    now = datetime.now(timezone.utc)
    ip = IPProfileRepository(db).create(IPProfile(creator_name="Creator"))
    project = ProjectRepository(db).create(Project(ip_profile_id=ip.id, title="Video", topic="Topic",
        fps=RationalFps(numerator=25, denominator=1), created_at=now))
    return db, project, now


def _job(db, project, now, key, job_type=JobType.GENERATE_TALKING):
    payload = None if job_type is not JobType.VERIFY_TALKING else TalkingQaJobPayload(
        project_id=project.id, run_id=uuid4(), scene_plan_id=uuid4(), generation_job_id=uuid4(),
        output_asset_id=uuid4(), output_sha256="a" * 64, master_audio_id=uuid4(), master_sha256="b" * 64,
        master_start_ms=0, master_end_ms=1000,
    )
    return JobStore(db).enqueue(Job(project_id=project.id, type=job_type,
        idempotency_key=key, created_at=now, updated_at=now, payload=payload))


def _production(db, project, now, bindings, *, run_id=None, stage="awaiting_talking", render_job_id=None):
    run_id = run_id or uuid4()
    db.connection.execute("""INSERT INTO production_runs
        (id, project_id, idempotency_key, preflight_fingerprint, draft_version, script_revision,
         style_tokens, repair_allowance, stage, created_at, talking_source_bindings, render_job_id)
        VALUES (?, ?, ?, ?, 1, 1, '{}', 0, ?, ?, ?, ?)""",
        (str(run_id), str(project.id), str(run_id), run_id.hex * 2, stage, now.isoformat(),
         json.dumps(bindings), None if render_job_id is None else str(render_job_id)))
    return run_id


def _config(path, run_id, *, once=False):
    return parse_config(["--db", str(path), "--production-run-id", str(run_id),
        "--job-type", "generate_talking", "--job-type", "verify_talking", "--job-type", "render",
        "--poll-seconds", "0.01", *(["--once"] if once else [])])


def _install_runner(monkeypatch, handler, *, completed=None):
    def forbidden(*_, **__):
        pytest.fail("a production-scoped worker must not run bulk recovery/reconciliation")

    class Service:
        def __init__(self, *args):
            pass

        def reconcile_completed_voice_job(self, *args, **kwargs):
            pass

        def reconcile_completed_talking_job(self, *args, **kwargs):
            pass

        def reconcile_completed_talking_qa_job(self, *args, **kwargs):
            pass

        reconcile_completed_voice_jobs_once = forbidden
        reconcile_completed_talking_jobs_once = forbidden
        reconcile_completed_talking_qa_jobs_once = forbidden

    def build(config, db):
        assert config.max_attempts == 1
        runner = JobRunner(JobStore(db), {job_type: handler for job_type in config.job_types},
            worker_id=config.worker_id, lease_duration=timedelta(seconds=30), max_attempts=config.max_attempts,
            on_completed=None if completed is None else lambda job: completed(db, job))
        from app.video_production import VideoProductionService
        runner.production_service=VideoProductionService(db,config.data_root)
        runner.production_service.runs=Service(db,config.data_root)
        monkeypatch.setattr(runner, "recover_expired", forbidden)
        return runner

    monkeypatch.setattr("app.worker_cli.ProductionRunService", Service)
    monkeypatch.setattr("app.worker_cli.build_runner", build)
    return Service


def test_scope_parser_supports_resident_and_once_with_one_attempt():
    run_id = uuid4()
    config = parse_config(["--production-run-id", str(run_id), "--max-attempts", "9"])
    assert config.production_run_id == run_id and not config.once and config.max_attempts == 1
    assert set(config.job_types) == {JobType.GENERATE_VOICE, JobType.VERIFY_VOICE,
        JobType.GENERATE_TALKING, JobType.VERIFY_TALKING, JobType.RENDER}
    assert parse_config(["--production-run-id", str(run_id), "--once"]).once
    with pytest.raises(SystemExit):
        parse_config(["--production-run-id", str(run_id), "--job-id", str(uuid4()), "--once"])
    with pytest.raises(SystemExit):
        parse_config(["--production-run-id", "invalid"])
    with pytest.raises(SystemExit):
        parse_config(["--production-run-id", str(run_id), "--job-type", "analyze_asset"])


def test_ordinary_resident_worker_only_claims_production_references_and_explicit_media_types(tmp_path,monkeypatch):
    path=tmp_path/"ordinary.sqlite3"
    with _fixture(path)[0] as db:
        project=ProjectRepository(db).list()[0]
        now=datetime.now(timezone.utc)
        orphan=_job(db,project,now,"orphan-talking")
        media=_job(db,project,now,"ordinary-media",JobType.ANALYZE_ASSET)
    seen=[]
    _install_runner(monkeypatch,lambda job:seen.append(job.id))
    defaults=parse_config(["--production-only"])
    assert JobType.PLAN_CONTENT in defaults.job_types and defaults.max_attempts==1
    config=parse_config(["--db",str(path),"--production-only","--once","--job-type","generate_talking","--job-type","analyze_asset"])
    assert run(config)==0
    assert seen==[media.id]
    with Database(path) as db:
        assert JobStore(db).get(orphan.id).attempt==0


def test_once_never_claims_same_type_legacy_or_other_run_jobs(tmp_path, monkeypatch):
    path = tmp_path / "scoped.sqlite"
    db, project, now = _fixture(path)
    with db:
        legacy = _job(db, project, now - timedelta(hours=2), "legacy")
        other = _job(db, project, now - timedelta(hours=1), "other")
        target = _job(db, project, now, "target")
        _production(db, project, now, [{"talking_job_id": str(other.id)}])
        run_id = _production(db, project, now, [{"talking_job_id": str(target.id)}])
    seen = []
    _install_runner(monkeypatch, lambda job: seen.append(job.id))
    config = _config(path, run_id, once=True)
    assert run(config) == 0 and run(config) == 0
    assert seen == [target.id]
    with Database(path) as db:
        for job in (legacy, other):
            preserved = JobStore(db).get(job.id)
            assert preserved.status is JobStatus.PENDING and preserved.attempt == 0


def test_resident_reopens_callback_created_qa_and_render_until_reviewable_output(tmp_path, monkeypatch):
    path = tmp_path / "chain.sqlite"
    db, project, now = _fixture(path)
    with db:
        source = _job(db, project, now, "talking")
        run_id = _production(db, project, now, [{"talking_job_id": str(source.id)}])
        legacy = _job(db, project, now - timedelta(hours=1), "legacy-render", JobType.RENDER)
    seen = []

    def completed(db, job):
        if job.type is JobType.GENERATE_TALKING:
            qa = _job(db, project, now, "qa", JobType.VERIFY_TALKING)
            db.connection.execute("UPDATE production_runs SET talking_source_bindings = ? WHERE id = ?",
                (json.dumps([{"talking_job_id": str(source.id), "talking_qa_job_id": str(qa.id)}]), str(run_id)))
        elif job.type is JobType.VERIFY_TALKING:
            render = _job(db, project, now, "render", JobType.RENDER)
            db.connection.execute("UPDATE production_runs SET stage = 'render', render_job_id = ? WHERE id = ?",
                (str(render.id), str(run_id)))

    _install_runner(monkeypatch, lambda job: seen.append(job.type), completed=completed)
    assert run(_config(path, run_id)) == 0
    assert seen == [JobType.GENERATE_TALKING, JobType.VERIFY_TALKING, JobType.RENDER]
    with Database(path) as db:
        assert JobStore(db).get(legacy.id).status is JobStatus.PENDING
        render_id = db.connection.execute("SELECT render_job_id FROM production_runs WHERE id = ?", (str(run_id),)).fetchone()[0]
        assert JobStore(db).get(UUID(render_id)).status is JobStatus.COMPLETED


@pytest.mark.parametrize("field,job_type", [("voice_job_id", JobType.GENERATE_VOICE),
                                           ("voice_qa_job_id", JobType.VERIFY_VOICE)])
def test_voice_and_voice_qa_use_current_run_references(tmp_path, monkeypatch, field, job_type):
    path = tmp_path / "voice.sqlite"
    db, project, now = _fixture(path)
    with db:
        legacy = _job(db, project, now - timedelta(hours=1), "legacy", job_type)
        target = _job(db, project, now, "target", job_type)
        run_id = _production(db, project, now, [], stage="voice")
        db.connection.execute(f"UPDATE production_runs SET {field} = ? WHERE id = ?", (str(target.id), str(run_id)))
    seen = []
    _install_runner(monkeypatch, lambda job: seen.append(job.id))
    config = parse_config(["--db", str(path), "--production-run-id", str(run_id),
                           "--job-type", job_type.value, "--once"])
    assert run(config) == 0 and seen == [target.id]
    with Database(path) as db:
        assert JobStore(db).get(legacy.id).status is JobStatus.PENDING


def test_retryable_failure_stops_run_and_restart_without_claiming_later_work(tmp_path, monkeypatch):
    path = tmp_path / "failure.sqlite"
    db, project, now = _fixture(path)
    with db:
        first = _job(db, project, now, "first")
        later = _job(db, project, now, "later")
        run_id = _production(db, project, now, [{"talking_job_id": str(first.id)}, {"talking_job_id": str(later.id)}])
    seen = []

    def failing(job):
        seen.append(job.id)
        raise JobExecutionError("provider_failed", "Failure evidence retained", retryable=True)

    _install_runner(monkeypatch, failing)
    config = replace(_config(path, run_id), max_attempts=9)
    assert run(config) == 1 and run(config) == 1
    assert seen == [first.id]
    with Database(path) as db:
        failed, pending = JobStore(db).get(first.id), JobStore(db).get(later.id)
        assert failed.status is JobStatus.FAILED and failed.attempt == 1
        assert failed.error_code == "provider_failed"
        assert pending.status is JobStatus.PENDING and pending.attempt == 0


def test_automatic_once_returns_failure_without_claiming_later_work(tmp_path, monkeypatch):
    path = tmp_path / "automatic-failure.sqlite"
    db, project, now = _fixture(path)
    with db:
        first = _job(db, project, now, "first")
        later = _job(db, project, now, "later")
        run_id = _production(db, project, now, [{"talking_job_id": str(first.id)}, {"talking_job_id": str(later.id)}])
        db.connection.execute("UPDATE production_runs SET automatic=1 WHERE id=?", (str(run_id),))
    seen = []

    def failing(job):
        seen.append(job.id)
        raise JobExecutionError("provider_failed", "Failure evidence retained", retryable=True)

    _install_runner(monkeypatch, failing)
    config = _config(path, run_id, once=True)
    assert run(config) == 1 and run(config) == 1
    assert seen == [first.id]
    with Database(path) as db:
        assert JobStore(db).get(first.id).attempt == 1
        assert JobStore(db).get(later.id).attempt == 0


@pytest.mark.parametrize("selected,expected", [
    (JobType.GENERATE_TALKING, {JobType.GENERATE_TALKING, JobType.VERIFY_TALKING}),
    (JobType.VERIFY_VOICE, {JobType.GENERATE_VOICE, JobType.VERIFY_VOICE}),
])
def test_startup_replays_only_the_current_worker_stage_group(tmp_path, monkeypatch, selected, expected):
    path = tmp_path / "startup.sqlite"
    db, project, now = _fixture(path)
    with db:
        voice = _job(db, project, now, "voice", JobType.GENERATE_VOICE)
        voice_qa = _job(db, project, now, "voice-qa", JobType.VERIFY_VOICE)
        talking = _job(db, project, now, "talking")
        talking_qa = _job(db, project, now, "talking-qa", JobType.VERIFY_TALKING)
        run_id = _production(db, project, now, [{"talking_job_id": str(talking.id), "talking_qa_job_id": str(talking_qa.id)}])
        db.connection.execute("UPDATE production_runs SET automatic=1,voice_job_id=?,voice_qa_job_id=? WHERE id=?",
            (str(voice.id), str(voice_qa.id), str(run_id)))
        for job in (voice, voice_qa, talking, talking_qa):
            JobRepository(db).update(job.model_copy(update={"status": JobStatus.COMPLETED}))
    seen = []

    def build(config, db):
        from app.video_production import VideoProductionService
        runner = JobRunner(JobStore(db), {selected: lambda _: pytest.fail("no generation should run")},
            worker_id=config.worker_id, lease_duration=timedelta(seconds=30), max_attempts=1)
        runner.production_service = VideoProductionService(db, config.data_root)
        monkeypatch.setattr(runner.production_service, "completed", lambda job, types: seen.append((job.type, set(types))))
        return runner

    monkeypatch.setattr("app.worker_cli.build_runner", build)
    config = parse_config(["--db", str(path), "--once", "--job-type", selected.value])
    assert run(config) == 0
    assert {kind for kind, _ in seen} == expected
    assert all(types == {selected} for _, types in seen)
    with Database(path) as db:
        assert db.connection.execute("SELECT voice_qa_auto_stop_reasons FROM production_runs WHERE id=?",
            (str(run_id),)).fetchone()[0] == '[]'


@pytest.mark.parametrize("case,allowed", [
    ("failed_parent", False), ("cancelled_parent", False), ("unknown_stop", False),
    ("active", True), ("manual", True), ("media", True),
])
def test_targeted_job_uses_central_admission_before_claim(tmp_path, monkeypatch, case, allowed):
    path = tmp_path / "target-admission.sqlite"
    db, project, now = _fixture(path)
    job_type = JobType.ANALYZE_ASSET if case == "media" else JobType.GENERATE_VOICE
    with db:
        target = _job(db, project, now, "target", job_type)
        if case != "media":
            parent = _job(db, project, now, "start", JobType.PLAN_CONTENT)
            parent_status = JobStatus.CANCELLED if case == "cancelled_parent" else JobStatus.FAILED if case in {"failed_parent", "manual"} else JobStatus.COMPLETED
            JobRepository(db).update(parent.model_copy(update={"status": parent_status}))
            run_id = _production(db, project, now, [], stage="voice")
            db.connection.execute("UPDATE production_runs SET automatic=?,start_job_id=?,voice_job_id=?,waiting_stop_reasons=? WHERE id=?",
                (int(case != "manual"), str(parent.id), str(target.id),
                 json.dumps(["automatic_stop:unclassified_failure"] if case == "unknown_stop" else []), str(run_id)))
    seen = []
    _install_runner(monkeypatch, lambda job: seen.append(job.id))
    config = parse_config(["--db", str(path), "--job-id", str(target.id), "--once",
        "--max-attempts", "1", "--job-type", job_type.value])
    assert run(config) == (0 if allowed else 1)
    assert seen == ([target.id] if allowed else [])
    with Database(path) as db:
        current = JobStore(db).get(target.id)
        assert current.attempt == int(allowed)
        assert current.status is (JobStatus.COMPLETED if allowed else JobStatus.PENDING)


def test_completed_target_stays_a_successful_noop_for_a_sealed_run(tmp_path, monkeypatch):
    path = tmp_path / "completed-target.sqlite"
    db, project, now = _fixture(path)
    with db:
        parent = _job(db, project, now, "start", JobType.PLAN_CONTENT)
        target = _job(db, project, now, "render", JobType.RENDER)
        JobRepository(db).update(parent.model_copy(update={"status": JobStatus.FAILED}))
        JobRepository(db).update(target.model_copy(update={"status": JobStatus.COMPLETED}))
        run_id = _production(db, project, now, [], stage="render", render_job_id=target.id)
        db.connection.execute("UPDATE production_runs SET automatic=1,start_job_id=? WHERE id=?",
            (str(parent.id), str(run_id)))
    _install_runner(monkeypatch, lambda _: pytest.fail("completed Job must not execute"))
    config = parse_config(["--db", str(path), "--job-id", str(target.id), "--once",
        "--max-attempts", "1", "--job-type", "render"])
    assert run(config) == 0 and run(config) == 0
    with Database(path) as db:
        current = JobStore(db).get(target.id)
        assert current.status is JobStatus.COMPLETED and current.attempt == 0


@pytest.mark.parametrize("case,allowed", [("failed_parent", False), ("unknown_stop", False), ("manual", True)])
def test_unscoped_worker_checks_the_center_before_executing_a_production_handler(tmp_path, monkeypatch, case, allowed):
    from app.worker_cli import build_runner
    path = tmp_path / "unscoped-admission.sqlite"
    db, project, now = _fixture(path)
    with db:
        parent = _job(db, project, now, "start", JobType.PLAN_CONTENT)
        target = _job(db, project, now, "voice", JobType.GENERATE_VOICE)
        JobRepository(db).update(parent.model_copy(update={"status":
            JobStatus.COMPLETED if case == "unknown_stop" else JobStatus.FAILED}))
        run_id = _production(db, project, now, [], stage="voice")
        db.connection.execute("UPDATE production_runs SET automatic=?,start_job_id=?,voice_job_id=?,waiting_stop_reasons=? WHERE id=?",
            (int(case != "manual"), str(parent.id), str(target.id),
             json.dumps(["automatic_stop:unclassified_failure"] if case == "unknown_stop" else []), str(run_id)))
    seen = []
    monkeypatch.setattr("app.worker_cli._build_voice_provider", lambda *args: object())
    monkeypatch.setattr("app.worker_cli.VoiceGenerationJobHandler", lambda *args: lambda job: seen.append(job.id))

    def build(config, db):
        runner = build_runner(config, db)
        monkeypatch.setattr(runner.production_service, "completed", lambda *args: None)
        return runner

    monkeypatch.setattr("app.worker_cli.build_runner", build)
    config = parse_config(["--db", str(path), "--data-root", str(tmp_path), "--once",
        "--max-attempts", "1", "--job-type", "generate_voice"])
    assert run(config) == 0
    assert seen == ([target.id] if allowed else [])
    with Database(path) as db:
        current = JobStore(db).get(target.id)
        assert current.attempt == 1
        assert current.status is (JobStatus.COMPLETED if allowed else JobStatus.FAILED)
        assert current.error_code == (None if allowed else "production_stopped")


def test_expired_scoped_lease_stops_without_rerunning_or_recovering_unrelated_job(tmp_path, monkeypatch):
    path = tmp_path / "expired.sqlite"
    db, project, now = _fixture(path)
    with db:
        target = _job(db, project, now, "target")
        unrelated = _job(db, project, now, "unrelated")
        store = JobStore(db)
        for job in (target, unrelated):
            store.claim("interrupted", timedelta(seconds=1), max_attempts=3,
                        job_id=job.id, now=now - timedelta(minutes=1))
        run_id = _production(db, project, now, [{"talking_job_id": str(target.id)}])
    _install_runner(monkeypatch, lambda _: pytest.fail("an interrupted production attempt must not execute again"))
    assert run(_config(path, run_id)) == 1
    with Database(path) as db:
        assert JobStore(db).get(target.id).status is JobStatus.FAILED
        preserved = JobStore(db).get(unrelated.id)
        assert preserved.status is JobStatus.RUNNING and preserved.attempt == 1


def test_automatic_once_reports_a_claim_exhausted_lease_without_claiming_later_work(tmp_path, monkeypatch):
    path = tmp_path / "automatic-expired.sqlite"
    db, project, now = _fixture(path)
    with db:
        target = _job(db, project, now, "target")
        later = _job(db, project, now, "later")
        JobStore(db).claim("interrupted", timedelta(seconds=1), max_attempts=3,
            job_id=target.id, now=now - timedelta(minutes=1))
        run_id = _production(db, project, now, [{"talking_job_id": str(target.id)}, {"talking_job_id": str(later.id)}])
        db.connection.execute("UPDATE production_runs SET automatic=1 WHERE id=?", (str(run_id),))
    _install_runner(monkeypatch, lambda _: pytest.fail("exhausted or later work must not execute"))
    assert run(_config(path, run_id, once=True)) == 1
    with Database(path) as db:
        assert JobStore(db).get(target.id).status is JobStatus.FAILED
        assert JobStore(db).get(later.id).attempt == 0


def test_idle_resident_wait_is_interruptible(tmp_path, monkeypatch):
    path = tmp_path / "idle.sqlite"
    db, project, now = _fixture(path)
    with db:
        run_id = _production(db, project, now, [])
    _install_runner(monkeypatch, lambda _: pytest.fail("no referenced job is ready"))
    stop = Event()

    def end_wait(timeout):
        stop.set()
        return True

    monkeypatch.setattr(stop, "wait", end_wait)
    assert run(_config(path, run_id), stop_event=stop) == 0
