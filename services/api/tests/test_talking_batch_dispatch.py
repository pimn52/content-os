"""Batch ordinary Talking dispatch: no provider calls or real workspace data."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.budget import ProviderCallLedger
from app.db import (
    AssetRepository, ClipRepository, AudioAssetRepository, BudgetPolicyRepository, Database, IPProfileRepository,
    JobRepository, ProjectRepository, ProviderCallRepository,
)
from app.domain.models import (
    Asset, Clip, AudioAsset, BudgetPolicy, ConsentRecord, CostCategory, IPProfile, Job, JobStatus,
    JobType, Project, RationalFps, TalkingPerformanceBrief, TranscriptSegment, UsageCost,
)
from app.main import ProductionRunTalkingBatchDispatchRequest, create_app
from app.production_runs import (
    ProductionRunConflict, ProductionRunNotReady, ProductionRunService,
    TalkingPlanDependency, TalkingSourceBinding,
)


def test_short_source_cannot_restart_for_each_scene_and_no_work_is_enqueued(batch):
    clips = ClipRepository(batch.db)
    clip = clips.get(batch.choice["reference_clip_id"])
    clips.update(clip.model_copy(update={"end_ms":3200}))
    with pytest.raises(ProductionRunNotReady) as failure:
        batch.service.dispatch_all_talking(batch.project.id,batch.run_id,**batch.choice)
    assert failure.value.reasons == ("talking_source_coverage_insufficient_replan",)
    assert JobRepository(batch.db).list() == []
    assert ProviderCallRepository(batch.db).list() == []
    assert batch.service.get(batch.project.id,batch.run_id).talking_source_bindings == ()


def test_render_cannot_consume_old_jobs_that_restart_the_same_motion(batch):
    run = batch.service.dispatch_all_talking(batch.project.id,batch.run_id,**batch.choice)
    batch.service._require_talking_source_continuity(run)
    jobs=JobRepository(batch.db)
    job=jobs.get(run.talking_source_bindings[1].talking_job_id)
    jobs.update(job.model_copy(update={"payload":job.payload.model_copy(update={"reference_window_start_ms":0})}))
    with pytest.raises(ProductionRunNotReady) as failure:
        batch.service._require_talking_source_continuity(run)
    assert failure.value.reasons == ("talking_motion_restart_requires_replan",)


def test_other_video_occupancy_is_detected_before_any_batch_job(batch):
    from app.source_usage import SourceUsageService
    project = ProjectRepository(batch.db).create(batch.project.model_copy(update={"id":uuid4()}))
    clip = ClipRepository(batch.db).get(batch.choice["reference_clip_id"])
    claims = SourceUsageService(batch.db).resolve(clip.asset_id,0,8000,"另一视频")
    batch.db.connection.execute("""INSERT INTO production_runs
        (id,project_id,idempotency_key,preflight_fingerprint,draft_version,script_revision,
        style_tokens,repair_allowance,stage,created_at,visual_source_uses)
        VALUES(?,?,?,'other-video',1,1,'{}',1,'awaiting_master',?,?)""",
        (str(uuid4()),str(project.id),str(uuid4()),batch.now.isoformat(),json.dumps(claims)))
    with pytest.raises(ProductionRunNotReady,match="talking_source_coverage_insufficient_replan"):
        batch.service.dispatch_all_talking(batch.project.id,batch.run_id,**batch.choice)
    assert JobRepository(batch.db).list()==[]
    assert ProviderCallRepository(batch.db).list()==[]


def test_different_clip_ids_do_not_restart_the_same_physical_source(batch):
    clips=ClipRepository(batch.db)
    original=clips.get(batch.choice["reference_clip_id"])
    alias=clips.create(original.model_copy(update={"id":uuid4()}))
    for index,dependency in enumerate(batch.dependencies):
        start,end=batch.times[dependency.scene_plan_id]
        with batch.db.transaction(immediate=True):
            batch.service.bind_talking_source(batch.project.id,batch.run_id,scene_plan_id=dependency.scene_plan_id,
                **{**batch.choice,"reference_clip_id":original.id if index==0 else alias.id},
                master_start_ms=start,master_end_ms=end,brief=TalkingPerformanceBrief())
    for dependency in batch.dependencies:
        batch.service.dispatch_talking(batch.project.id,batch.run_id,scene_plan_id=dependency.scene_plan_id)
    assert [job.payload.reference_window_start_ms for job in JobRepository(batch.db).list()]==[0,2400]


def test_offscreen_narration_gap_does_not_consume_visible_reference_capacity(batch):
    # Two visible performances fit even when a long source demonstration separates them.
    clips = ClipRepository(batch.db)
    clip = clips.get(batch.choice["reference_clip_id"])
    clips.update(clip.model_copy(update={"end_ms": 6000}))
    first, second = batch.dependencies
    batch.times[second.scene_plan_id] = (20_000, 22_700)
    run = batch.service.dispatch_all_talking(batch.project.id, batch.run_id, **batch.choice)
    jobs = JobRepository(batch.db).list()
    assert len(jobs) == 2
    assert sorted(job.payload.reference_window_start_ms for job in jobs) == [0, 2400]
    batch.service._require_talking_source_continuity(run)


@pytest.fixture
def batch(monkeypatch):
    """Keep existing source/context gates stubbed; exercise real durable Job dispatch."""
    now = datetime.now(timezone.utc)
    db = Database()
    ip = IPProfileRepository(db).create(IPProfile(creator_name="创作者"))
    project = ProjectRepository(db).create(Project(
        ip_profile_id=ip.id, title="短视频", topic="码率", created_at=now,
        fps=RationalFps(numerator=30, denominator=1),
    ))
    master = AudioAssetRepository(db).create(AudioAsset(
        source_file="fixture.wav", content_hash="a" * 64, duration_ms=5100,
        sample_rate=24000, channels=1, authorization_reference="registered-at-import",
        imported_at=now, transcript_source="verified-asr:fixture",
        transcript_segments=[TranscriptSegment(start_ms=0,end_ms=2400,text="First."),
            TranscriptSegment(start_ms=2400,end_ms=5100,text="Second.")],
        metadata={"voice_generation":{"qa_state":"verified"}},
    ))
    run_id = uuid4()
    dependencies = [TalkingPlanDependency(scene_plan_id=uuid4(), scene_id=f"speech-{index}",
        visual_dependency_fingerprint=str(index + 1) * 64) for index in range(2)]
    db.connection.execute("""INSERT INTO production_runs
        (id, project_id, idempotency_key, preflight_fingerprint, draft_version,
         script_revision, style_tokens, repair_allowance, stage, created_at,
         talking_master_audio_id, talking_dependencies)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (str(run_id), str(project.id), f"fixture:{run_id}", "a" * 64, 1, 1,
         "{}", 1, "awaiting_talking", now.isoformat(), str(master.id),
         json.dumps([item.model_dump(mode="json") for item in dependencies])))
    service = ProductionRunService(db, ".")
    choice = dict(talking_profile_id=uuid4(), reference_clip_id=uuid4(), capability_profile_id=uuid4())
    source = AssetRepository(db).create(Asset(source_kind="user_asset",source_file="fixture.mp4",
        content_hash="b"*64,duration_ms=8000,width=1080,height=1920,fps=project.fps,
        authorization_reference="registered-at-import",imported_at=now))
    ClipRepository(db).create(Clip(id=choice["reference_clip_id"],asset_id=source.id,
        start_ms=0,end_ms=8000,asset_duration_ms=8000))
    times = {dependencies[0].scene_plan_id: (0, 2400), dependencies[1].scene_plan_id: (2400, 5100)}
    reads, binds = [], []

    def context(project_id, selected_run_id, *, scene_plan_id):
        assert project_id == project.id and selected_run_id == run_id
        reads.append(scene_plan_id)
        start, end = times[scene_plan_id]
        return SimpleNamespace(status="ready", speech_start_ms=start, speech_end_ms=end)

    def bind(project_id, selected_run_id, **values):
        assert db.connection.in_transaction
        binds.append(values)
        current = service.get(project_id, selected_run_id)
        binding = TalkingSourceBinding(
            scene_plan_id=values["scene_plan_id"], talking_profile_id=values["talking_profile_id"],
            reference_clip_id=values["reference_clip_id"], reference_asset_id=uuid4(),
            reference_asset_hash="b" * 64, reference_start_ms=0, reference_end_ms=8000,
            source_authorization_reference="registered-at-import", reference_assessment_reference="source-qa",
            performance_brief=values["brief"], capability_profile_id=values["capability_profile_id"],
            capability_evidence_reference="verified-local", license_evidence_reference="model-license",
            master_audio_id=master.id, master_start_ms=values["master_start_ms"],
            master_end_ms=values["master_end_ms"], authorization_reference="registered-at-import",
        )
        if not any(item.scene_plan_id == binding.scene_plan_id for item in current.talking_source_bindings):
            updated = (*current.talking_source_bindings, binding)
            db.connection.execute("UPDATE production_runs SET talking_source_bindings=? WHERE id=?",
                (json.dumps([item.model_dump(mode="json") for item in updated]), str(run_id)))
        return service.get(project_id, selected_run_id)

    def validated(project_id, selected_run_id, binding, **kwargs):
        assert db.connection.in_transaction
        start, end = times[binding.scene_plan_id]
        assert (binding.master_start_ms, binding.master_end_ms) == (start, end)
        profile = SimpleNamespace(id=binding.talking_profile_id, consent=ConsentRecord(
            subject_name="创作者", basis="self", confirmed=True, confirmed_at=now))
        capability = SimpleNamespace(id=binding.capability_profile_id, provider="fixture", model="fixture",
            runtime="local", machine_id="fixture", verified_parameters={"fresh_voice_talking_duration_ms": 3200})
        plan = SimpleNamespace(start_segment_index=dependencies.index(next(
            item for item in dependencies if item.scene_plan_id == binding.scene_plan_id)),
            end_segment_index=dependencies.index(next(
                item for item in dependencies if item.scene_plan_id == binding.scene_plan_id)) + 1)
        return binding, profile, master, capability, plan

    monkeypatch.setattr(service, "talking_source_context", context)
    monkeypatch.setattr(service, "bind_talking_source", bind)
    monkeypatch.setattr(service, "_validated_talking_dispatch", validated)
    yield SimpleNamespace(db=db, service=service, project=project, run_id=run_id,
        dependencies=dependencies, choice=choice, times=times, reads=reads, binds=binds,
        now=now, context=context, validated=validated)
    db.close()


def test_one_source_choice_dispatches_exact_intervals_once_and_never_calls_provider(batch):
    run = batch.service.dispatch_all_talking(batch.project.id, batch.run_id, **batch.choice)
    jobs = JobRepository(batch.db).list()
    assert len(jobs) == 2
    assert all(job.type is JobType.GENERATE_TALKING and job.status is JobStatus.PENDING for job in jobs)
    assert batch.reads == [item.scene_plan_id for item in batch.dependencies]
    assert [(item["master_start_ms"], item["master_end_ms"]) for item in batch.binds] == [(0, 2400), (2400, 5100)]
    assert all(item.talking_job_id is not None for item in run.talking_source_bindings)
    assert all(job.payload.planned_context.run_id == batch.run_id for job in jobs)
    assert {job.idempotency_key for job in jobs} == {
        f"production-run:{batch.run_id}:talking:{item.scene_plan_id}" for item in batch.dependencies}
    assert ProviderCallRepository(batch.db).list() == []
    assert sorted(job.payload.reference_window_start_ms for job in jobs) == [0,2400]
    for job in jobs:
        batch.service.require_talking_job_admission(job,provider="fixture",model="fixture",runtime="local",machine_id="fixture")
    assert batch.service.dispatch_all_talking(batch.project.id, batch.run_id) == run
    assert [job.id for job in JobRepository(batch.db).list()] == [job.id for job in jobs]


def test_automatic_pool_dispatch_uses_two_silent_sources_and_replays_without_regeneration(batch):
    from test_talking_pool import pool_fixture
    from app.db import TalkingProfileRepository
    _,clips,profile,_=pool_fixture(batch.db,project=batch.project)
    for clip in clips[:2]:
        ClipRepository(batch.db).update(clip.model_copy(update={'end_ms':3000}))
    profile=profile.model_copy(update={'reference_clip_ids':[clip.id for clip in clips[:2]]})
    batch.db.connection.execute('UPDATE talking_profiles SET payload=? WHERE id=?',(profile.model_dump_json(),str(profile.id)))
    batch.db.connection.execute('UPDATE production_runs SET automatic=1 WHERE id=?',(str(batch.run_id),))
    def validate(duration,reference):
        if reference<duration+200:raise ValueError('context needed')
    batch.service.talking_provider=SimpleNamespace(provider_name='local',model='model',validate_reference_duration=validate)
    result=batch.service.dispatch_all_talking(batch.project.id,batch.run_id)
    jobs=JobRepository(batch.db).list()
    assert len(jobs)==2 and len({job.payload.reference_clip_id for job in jobs})==2
    assert sorted(job.payload.reference_window_end_ms-job.payload.reference_window_start_ms for job in jobs)==[2600,2900]
    assert ProviderCallRepository(batch.db).list()==[]
    assert batch.service.dispatch_all_talking(batch.project.id,batch.run_id)==result
    assert len(JobRepository(batch.db).list())==2


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED])
def test_existing_job_is_never_regenerated_and_unique_binding_can_supply_source(batch, status):
    first = batch.dependencies[0].scene_plan_id
    with batch.db.transaction(immediate=True):
        batch.service.bind_talking_source(batch.project.id, batch.run_id, scene_plan_id=first,
            master_start_ms=0, master_end_ms=2400, brief=TalkingPerformanceBrief(), **batch.choice)
    initial = batch.service.dispatch_talking(batch.project.id, batch.run_id, scene_plan_id=first)
    first_job = JobRepository(batch.db).get(initial.talking_source_bindings[0].talking_job_id)
    JobRepository(batch.db).update(first_job.model_copy(update={"status": status}))
    batch.reads.clear()
    result = batch.service.dispatch_all_talking(batch.project.id, batch.run_id)
    assert batch.reads == [batch.dependencies[1].scene_plan_id]
    assert len(JobRepository(batch.db).list()) == 2
    assert result.talking_source_bindings[0].talking_job_id == first_job.id
    assert JobRepository(batch.db).get(first_job.id).status is status


def test_unresolved_later_interval_rolls_back_new_bindings_and_jobs(batch, monkeypatch):
    def context(project_id, run_id, *, scene_plan_id):
        if scene_plan_id == batch.dependencies[1].scene_plan_id:
            return SimpleNamespace(status="unresolved")
        return batch.context(project_id, run_id, scene_plan_id=scene_plan_id)
    monkeypatch.setattr(batch.service, "talking_source_context", context)
    with pytest.raises(ProductionRunNotReady, match="talking_scene_interval_unresolved:speech-1"):
        batch.service.dispatch_all_talking(batch.project.id, batch.run_id, **batch.choice)
    assert JobRepository(batch.db).list() == []
    assert batch.service.get(batch.project.id, batch.run_id).talking_source_bindings == ()


def test_existing_binding_cannot_silently_change_source_or_timing(batch, monkeypatch):
    first = batch.dependencies[0].scene_plan_id
    with batch.db.transaction(immediate=True):
        batch.service.bind_talking_source(batch.project.id, batch.run_id, scene_plan_id=first,
            master_start_ms=0, master_end_ms=2400, brief=TalkingPerformanceBrief(), **batch.choice)
    with pytest.raises(ProductionRunConflict, match="source differs"):
        batch.service.dispatch_all_talking(batch.project.id, batch.run_id,
            **{**batch.choice, "reference_clip_id": uuid4()})
    monkeypatch.setattr(batch.service, "talking_source_context", lambda *args, **kwargs:
        SimpleNamespace(status="ready", speech_start_ms=100, speech_end_ms=2400))
    with pytest.raises(ProductionRunConflict, match="interval changed"):
        batch.service.dispatch_all_talking(batch.project.id, batch.run_id)
    assert JobRepository(batch.db).list() == []


def test_missing_or_partial_source_choice_never_dispatches(batch):
    with pytest.raises(ProductionRunNotReady, match="motion_capacity_insufficient"):
        batch.service.dispatch_all_talking(batch.project.id, batch.run_id)
    with pytest.raises(ProductionRunNotReady, match="selection_incomplete"):
        batch.service.dispatch_all_talking(batch.project.id, batch.run_id, talking_profile_id=uuid4())
    assert JobRepository(batch.db).list() == []


def _policy(batch, scope, limit):
    BudgetPolicyRepository(batch.db).save(BudgetPolicy(project_id=scope,
        max_calls=limit, allow_unknown_cost=True, updated_at=batch.now))


@pytest.mark.parametrize("scope", ["global", "project"])
def test_batch_call_limit_is_checked_before_any_work(batch, scope):
    _policy(batch, None if scope == "global" else batch.project.id, 1)
    with pytest.raises(ProductionRunNotReady, match="talking_batch_budget_call_limit"):
        batch.service.dispatch_all_talking(batch.project.id, batch.run_id, **batch.choice)
    assert JobRepository(batch.db).list() == []
    assert batch.service.get(batch.project.id, batch.run_id).talking_source_bindings == ()


def _pending(batch, project_id=None, *, status=JobStatus.PENDING, attempt=0, scheduled=True):
    job = JobRepository(batch.db).create(Job(project_id=project_id or batch.project.id,
        type=JobType.GENERATE_VOICE, status=status, attempt=attempt,
        idempotency_key=f"fixture:pending:{uuid4()}", created_at=batch.now, updated_at=batch.now))
    if scheduled:
        batch.db.connection.execute("""INSERT INTO production_runs
            (id, project_id, idempotency_key, preflight_fingerprint, draft_version,
             script_revision, style_tokens, repair_allowance, stage, created_at, voice_job_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid4()), str(job.project_id), f"scheduled:{job.id}", uuid4().hex * 2,
             1, 1, "{}", 1, "voice", batch.now.isoformat(), str(job.id)))
    return job


def _call(batch, job):
    return ProviderCallLedger(batch.db).reserve(project_id=job.project_id,
        idempotency_key=f"job:{job.id}:tts:{job.attempt}:voice", operation="tts", mode="runtime",
        provider="fixture", model="fixture", input_source="fixture",
        estimated_cost=UsageCost(category=CostCategory.VOICE, amount=Decimal(0), currency="USD"))


def test_queued_unaccounted_work_consumes_remaining_call_capacity(batch):
    _policy(batch, batch.project.id, 2)
    _pending(batch)
    with pytest.raises(ProductionRunNotReady, match="talking_batch_budget_call_limit"):
        batch.service.dispatch_all_talking(batch.project.id, batch.run_id, **batch.choice)
    assert len(JobRepository(batch.db).list()) == 1


def test_running_call_already_in_ledger_is_not_counted_twice(batch):
    _policy(batch, batch.project.id, 3)
    job = _pending(batch, status=JobStatus.RUNNING, attempt=1)
    _call(batch, job)
    result = batch.service.dispatch_all_talking(batch.project.id, batch.run_id, **batch.choice)
    assert len(result.talking_source_bindings) == 2
    assert len(JobRepository(batch.db).list()) == 3
    assert len(ProviderCallRepository(batch.db).list()) == 1


def test_global_budget_includes_other_projects_queued_work(batch):
    other = ProjectRepository(batch.db).create(batch.project.model_copy(update={"id": uuid4()}))
    _policy(batch, None, 2)
    _pending(batch, other.id)
    with pytest.raises(ProductionRunNotReady, match="talking_batch_budget_call_limit"):
        batch.service.dispatch_all_talking(batch.project.id, batch.run_id, **batch.choice)
    _policy(batch, None, 3)
    _policy(batch, batch.project.id, 2)
    assert batch.service.dispatch_all_talking(batch.project.id, batch.run_id, **batch.choice) is not None


def test_previous_attempt_does_not_hide_unaccounted_queued_retry(batch):
    _policy(batch, batch.project.id, 3)
    job = _pending(batch, status=JobStatus.PENDING, attempt=1)
    _call(batch, job)
    with pytest.raises(ProductionRunNotReady, match="talking_batch_budget_call_limit"):
        batch.service.dispatch_all_talking(batch.project.id, batch.run_id, **batch.choice)


def test_orphaned_and_cancelled_run_pending_jobs_do_not_reserve_product_budget(batch):
    _policy(batch, None, 2)
    _pending(batch, scheduled=False)
    job = _pending(batch)
    batch.db.connection.execute("UPDATE production_runs SET stage='cancelled' WHERE voice_job_id=?", (str(job.id),))
    assert batch.service.dispatch_all_talking(batch.project.id, batch.run_id, **batch.choice) is not None
    assert len(JobRepository(batch.db).list()) == 4


def test_historical_accounted_call_still_counts_without_active_run(batch):
    _policy(batch, None, 2)
    job = _pending(batch, status=JobStatus.RUNNING, attempt=1, scheduled=False)
    _call(batch, job)
    with pytest.raises(ProductionRunNotReady, match="talking_batch_budget_call_limit"):
        batch.service.dispatch_all_talking(batch.project.id, batch.run_id, **batch.choice)


def test_current_run_pending_talking_is_included_before_dispatching_remainder(batch):
    _policy(batch, batch.project.id, 1)
    first = batch.dependencies[0].scene_plan_id
    with batch.db.transaction(immediate=True):
        batch.service.bind_talking_source(batch.project.id, batch.run_id, scene_plan_id=first,
            master_start_ms=0, master_end_ms=2400, brief=TalkingPerformanceBrief(), **batch.choice)
    batch.service.dispatch_talking(batch.project.id, batch.run_id, scene_plan_id=first)
    with pytest.raises(ProductionRunNotReady, match="talking_batch_budget_call_limit"):
        batch.service.dispatch_all_talking(batch.project.id, batch.run_id)
    assert len(JobRepository(batch.db).list()) == 1


def test_batch_endpoint_has_ordinary_request_contract_and_missing_run_is_404(tmp_path):
    selection = {key: str(uuid4()) for key in ("talking_profile_id", "reference_clip_id", "capability_profile_id")}
    assert ProductionRunTalkingBatchDispatchRequest.model_validate(selection).brief.require_no_burned_subtitles
    with TestClient(create_app(tmp_path / "batch-api.sqlite")) as client:
        path = f"/projects/{uuid4()}/production-runs/{uuid4()}/talking-batch-dispatch"
        assert client.post(path, json=selection).status_code == 404
        assert client.post(path, json={"talking_profile_id": str(uuid4())}).status_code == 422
        assert client.post(path, json={**selection, "authorization_reference": "must-not-repeat"}).status_code == 422
