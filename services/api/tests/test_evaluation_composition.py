"""Real retained resolvers and SQLite; inherited model children are synthetic."""
import hashlib
import json
from uuid import UUID, uuid4
from datetime import timedelta
from types import SimpleNamespace
import pytest
from app.db import JobRepository, ProviderCallRepository
from app.domain.models import JobStatus, execution_canonical_sha256 as digest
from app.domain.evaluation_composition import CompositionSelection, CompositionStart
from app.domain.evaluation_talking import TalkingCalibrationStart
from app.evaluation_composition import EvaluationCompositionService
from app.execution_scope import read_job_scope, require_evaluation_lane_closed
from app.execution_admission import UseAdmissionError
from test_execution_admission import OPERATOR_KEY
from test_evaluation_talking_normal_inputs import normal_inputs, bound_job, child_case, setup
from test_voice_evaluation_qa import qa_case as raw_qa_case
from test_evaluation_talking_dual_normal import talking_case, original_talking_case, candidate
from test_evaluation_talking_calibration import calibration_flow
from test_evaluation_talking_normal_flow import save


@pytest.fixture(autouse=True)
def speaking_fixture(monkeypatch):
    # The inherited permission fixture exports four PCM samples (1ms), which
    # cannot represent a video frame. Export one second of synthetic PCM.
    import wave
    frames = wave.Wave_write.writeframes
    monkeypatch.setattr(wave.Wave_write,"writeframes",lambda self,data:frames(self,data*6000 if len(data)==8 else data))
    import test_evaluation_master as module
    original = module._scene
    monkeypatch.setattr(module,"_scene",lambda *a,**kw:original(*a,**kw).model_copy(update={
        "visual_requirement":"creator_speaking","visual_requirement_reason":"Synthetic explicit creator speaking requirement"}))


@pytest.fixture
def qa_case(raw_qa_case):
    from app.domain.asr_child import ASRChildResult
    q=raw_qa_case
    q.set_result(ASRChildResult.model_validate_json(json.dumps(dict(text=q.c.job.payload.text,language="zh",
        segments=[dict(start_ms=0,end_ms=q.audio.duration_ms,text=q.c.job.payload.text)],words=[]))))
    return q


@pytest.fixture
def retained(calibration_flow,monkeypatch):
    import test_evaluation_talking_normal_flow as normal
    c = calibration_flow
    with monkeypatch.context() as preparation:
        preparation.setattr(normal,"TalkingStart",TalkingCalibrationStart)
        normal.test_normal_owned_handler_through_preview_review_admission_with_real_authority(c,preparation)
    result = json.loads(c.db.connection.execute("SELECT payload FROM evaluation_talking_results WHERE role='admission'").fetchone()[0])
    c.composition = EvaluationCompositionService(c.db,c.root)
    c.compose_selection = CompositionSelection(master_run_id=c.parent.id,talking_runs={c.selection.scene_plan_id:UUID(result["run_id"])})
    from app.db import BudgetPolicyRepository
    budgets=BudgetPolicyRepository(c.db)
    for scope in (None,c.project):
        budgets.save(budgets.get_by_project(scope).model_copy(update={"max_calls":4}))
    return c


def batch(c, scenes=()):
    authorization = c.root/"composition-authorization.txt"
    authorization.write_text("Synthetic explicit internal-only batch; no actual execution permission",encoding="utf-8")
    report = dict(policy="evaluation-composition-v1",project_id=str(c.project),master_run_id=str(c.parent.id),
        authorization_reference=authorization.name,authorization_sha256=hashlib.sha256(authorization.read_bytes()).hexdigest(),
        scene_ids=[str(x) for x in scenes],max_renders=1,findings=["Synthetic operator-bound batch"])
    return c.composition.adopt_batch(c.project,save(c.root,"composition-batch.json",report),OPERATOR_KEY)


def adoption(c):
    state = c.composition.preflight(c.project,c.compose_selection)
    assert state["eligible"],state
    terms = c.root/"composition-terms.txt"
    terms.write_text("Synthetic explicit retained composition/review permission for all test origins and tools",encoding="utf-8")
    evidence = dict(source="synthetic",version="test-v1",terms_reference=terms.name,
        terms_sha256=hashlib.sha256(terms.read_bytes()).hexdigest(),applicability="Synthetic exact retained local composition and review",permits_intended_activity=True)
    report = dict(policy="evaluation-composition-v1",purpose="internal_evaluation",operation="compose_retained_media_and_review",local_only=True,
        project_id=str(c.project),selection=c.compose_selection.model_dump(mode="json"),request_sha256=state["fingerprint"],proof=state["proof"],
        intended_activity="Synthetic internal-only composition",findings=["No actual licence or runtime evidence"],
        license_evidence=[dict(evidence,admission_id=b["admission_id"]) for b in state["proof"]["scope"]["bindings"]],
        dependency_findings=[dict(evidence,dependency=k) for k in ("retained_media_composition","local_renderer_and_tools",*state["proof"]["dependencies"])])
    use = c.composition.adopt(c.project,save(c.root,"composition-use.json",report),OPERATOR_KEY)
    grant = batch(c)
    return CompositionStart(selection=c.compose_selection,expected_fingerprint=state["fingerprint"],idempotency_key="composition-once",
        admission_id=use.id,batch_id=UUID(grant["id"]))


@pytest.mark.parametrize("normal_inputs",["calibration"],indirect=True)
def test_retained_selection_adoption_normal_run_and_default_denials(retained,monkeypatch):
    c = retained
    calls = ProviderCallRepository(c.db).list_all()
    parent = tuple(c.db.connection.execute("SELECT * FROM production_runs WHERE id=?",(str(c.parent.id),)).fetchone())
    origins = list(c.db.connection.execute("SELECT * FROM execution_use_content_origins ORDER BY content_hash,subject_kind,subject_id"))
    missing = c.compose_selection.model_copy(update={"talking_runs":{}})
    state = c.composition.preflight(c.project,missing)
    assert not state["eligible"] and len(state["missing_scenes"])==1
    payload = adoption(c)
    value = c.composition.create(c.project,payload)
    assert value["current_eligible"],value
    assert c.composition.create(c.project,payload.model_copy(update={"idempotency_key":"alias"}))["id"] == value["id"]
    job = JobRepository(c.db).get(UUID(value["render_job_id"]))
    assert str(job.id) != value["render_id"]
    assert read_job_scope(c.db,job).purpose == "internal_evaluation"
    with pytest.raises(UseAdmissionError):require_evaluation_lane_closed(c.db,job)
    from app.production_runs import ProductionRunService
    normal = ProductionRunService(c.db,c.root).get(c.project,UUID(value["id"]))
    assert normal.evaluation_composition["id"] == value["id"]
    assert tuple(c.db.connection.execute("SELECT * FROM production_runs WHERE id=?",(str(c.parent.id),)).fetchone()) == parent
    assert ProviderCallRepository(c.db).list_all() == calls
    # New real consumer Run retains the selected inputs' noncommercial use
    # restriction; no original audio/asset/producer payload is reparented.
    assert len(list(c.db.connection.execute("SELECT * FROM execution_use_content_origins"))) >= len(origins)
    # The exact concrete renderer and owned Worker run; the command runner and
    # measurement are synthetic. Real FFmpeg measurement is covered separately.
    from app.db import AssetRepository, ClipRepository, AudioAssetRepository
    from app.renderer.remotion import RemotionRenderer
    from app.jobs.evaluation_composition_handler import EvaluationCompositionHandler
    from app.jobs.runner import JobRunner
    from app.jobs.store import JobStore
    from test_remotion_renderer import _Runner, _renderer_project
    from app import presentation_repair
    runner = _Runner()
    renderer = RemotionRenderer(AssetRepository(c.db),ClipRepository(c.db),audios=AudioAssetRepository(c.db),
        renderer_dir=_renderer_project(c.root/"composition-renderer"),runner=runner)
    def measured(path,spec):
        return dict(state="verified",sha256=hashlib.sha256(path.read_bytes()).hexdigest(),video_frames=sum(s.duration_frames for s in spec.scenes),scope="synthetic_technical_only")
    monkeypatch.setattr(presentation_repair,"measure_render_output",measured)
    from app.jobs.handlers import RenderVideoJobHandler
    from app.db import ProjectRepository
    handler = RenderVideoJobHandler(ProjectRepository(c.db),renderer,c.root/"renders")
    import app.jobs.evaluation_composition_handler as owned_module
    original_error = owned_module.JobExecutionError
    def fixture_error(code,message,**kw):
        import traceback
        return original_error(code,traceback.format_exc()[-1900:],**kw)
    monkeypatch.setattr(owned_module,"JobExecutionError",fixture_error)
    completed = JobRunner(JobStore(c.db),{job.type:handler},worker_id="composition-fixture",lease_duration=timedelta(minutes=5),max_attempts=1).run_once(job_id=job.id)
    assert completed.status == JobStatus.COMPLETED,completed.error_message
    assert len(runner.calls)==2  # One visual render, one direct Master mux.
    props = runner.calls[0][3]
    assert props["audioMode"] == "master_narration" and "masterNarration" not in props
    assert "--muted" in runner.calls[0][0]
    assert all("narrationSrc" not in s for s in props["sceneSources"].values())
    assert c.composition.get(c.project,UUID(value["id"]))["state"] == "awaiting_u_product"
    assert c.composition.create(c.project,payload.model_copy(update={"idempotency_key":"after-complete"}))["id"] == value["id"]
    assert len(runner.calls)==2  # Replay dispatches neither render nor mux.
    path = c.composition.media(c.project,UUID(value["id"]),"internal_evaluation")
    import asyncio,httpx
    from fastapi import FastAPI
    from app.evaluation_composition_api import router
    api=FastAPI();api.include_router(router);api.state.database=c.db;api.state.data_root=c.root
    async def read(method="GET",suffix="?purpose=internal_evaluation",headers=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url="http://synthetic") as client:
            return await client.request(method,f"/projects/{c.project}/evaluation-composition/runs/{value['id']}/media"+suffix,headers=headers)
    full=asyncio.run(read());assert full.status_code==200 and full.content==path.read_bytes()
    assert asyncio.run(read("HEAD")).content==b""
    part=asyncio.run(read(headers={"Range":"bytes=0-3"}));assert part.status_code==206 and part.content==full.content[:4]
    assert asyncio.run(read(suffix="")).status_code==403
    assert asyncio.run(read(suffix="?purpose=commercial_production")).status_code==403
    from app.media.audio_access import require_ordinary_media
    with pytest.raises(UseAdmissionError):require_ordinary_media(c.db,c.root,path,kind="render",subject_id=UUID(value["render_id"]))
    with pytest.raises(UseAdmissionError):c.composition.media(c.project,UUID(value["id"]),"commercial_production")
    # A completed result can repair a lost Job completion without a new render.
    JobRepository(c.db).update(completed.model_copy(update={"status":JobStatus.FAILED}))
    assert c.composition.reconcile(c.project,UUID(value["id"]))["state"] == "awaiting_u_product"
    assert len(runner.calls)==2
    c.composition.revoke(c.project,payload.admission_id,"Synthetic revocation",OPERATOR_KEY)
    assert not c.composition.get(c.project,UUID(value["id"]))["current_eligible"]
    assert asyncio.run(read(headers={"If-None-Match":full.headers["etag"]})).status_code==403


@pytest.mark.parametrize("normal_inputs",["calibration"],indirect=True)
def test_composition_tamper_scope_alias_failure_and_once_only_boundaries(retained,monkeypatch):
    c = retained
    payload = adoption(c)
    missing = c.compose_selection.model_copy(update={"talking_runs":{uuid4():uuid4()}})
    assert not c.composition.preflight(c.project,missing)["eligible"]
    assert not c.composition.preflight(uuid4(),c.compose_selection)["eligible"]
    with pytest.raises(UseAdmissionError):
        c.composition.adopt(c.project,save(c.root,"untrusted.json",{}),None)
    value = c.composition.create(c.project,payload)
    job = JobRepository(c.db).get(UUID(value["render_job_id"]))
    binding = c.composition.records.get("binding",value["id"])
    changed = json.loads(json.dumps(binding));changed["proof"]["audio_sha256"]="f"*64
    c.composition.records.save("binding",value["id"],changed,update=True)
    with pytest.raises(UseAdmissionError):c.composition.require_current_job(job)
    c.composition.records.save("binding",value["id"],binding,update=True)
    scope_row = tuple(c.db.connection.execute("SELECT * FROM execution_use_scopes WHERE subject_kind='render' AND subject_id=?",(value["render_id"],)).fetchone())
    c.db.connection.execute("DELETE FROM execution_use_scopes WHERE subject_kind='render' AND subject_id=?",(value["render_id"],))
    with pytest.raises(UseAdmissionError):read_job_scope(c.db,job)
    c.db.connection.execute("INSERT INTO execution_use_scopes VALUES (?,?,?,?,?,?)",scope_row)
    # Generic callbacks cannot consume the owned internal Job.
    from app.jobs.runner import JobRunner
    from app.jobs.store import JobStore
    result = JobRunner(JobStore(c.db),{job.type:lambda _j:pytest.fail("generic handler must remain closed")},worker_id="generic",lease_duration=timedelta(minutes=5),max_attempts=1).run_once(job_id=job.id)
    assert result.status == JobStatus.FAILED
    assert c.composition.records.get("result",value["id"]) is None
    assert JobRunner(JobStore(c.db),{},worker_id="no-retry",lease_duration=timedelta(minutes=5),max_attempts=1).run_once(job_id=job.id) is None
    assert c.composition.create(c.project,payload.model_copy(update={"idempotency_key":"after-failure"}))["id"] == value["id"]
    with pytest.raises(UseAdmissionError):c.composition.media(c.project,UUID(value["id"]),"internal_evaluation")
    # A new grant/key cannot reset this same Master batch.
    report=json.loads((c.root/"composition-batch.json").read_text());report["scene_ids"]=[str(c.selection.scene_plan_id)]
    with pytest.raises(UseAdmissionError,match="already_bound"):
        c.composition.adopt_batch(c.project,save(c.root,"changed-batch.json",report),OPERATOR_KEY)
    terms=c.root/"composition-terms.txt";terms.write_bytes(terms.read_bytes()+b" changed")
    with pytest.raises(UseAdmissionError,match="evidence_changed"):c.composition.create(c.project,payload)


@pytest.mark.parametrize("fps",[(30,1),(30000,1001)])
def test_absolute_timing_full_master_and_neutral_tail(fps,tmp_path):
    from app.assembly.evaluation_composition import assemble_retained_composition
    from app.domain.models import AudioAsset, RationalFps, VisualStyleTokens, TranscriptSegment, Clip
    from test_video_spec_assembly import _project,_scene,_asset
    from app.renderer.remotion import RemotionRenderer
    from app.db import Database, AssetRepository, ClipRepository
    from test_remotion_renderer import _renderer_project
    project=_project(RationalFps(numerator=fps[0],denominator=fps[1]))
    scenes=[_scene(project,i) for i in range(2)]
    audio=AudioAsset(source_file="synthetic.wav",content_hash="a"*64,duration_ms=2300,sample_rate=24000,channels=1,
        authorization_reference="synthetic",imported_at=project.created_at,transcript_source="synthetic-timing",
        transcript_segments=[TranscriptSegment(start_ms=0,end_ms=1001,text=scenes[0].voice_text),TranscriptSegment(start_ms=1001,end_ms=2100,text=scenes[1].voice_text)])
    inputs={s.id:dict(asset=_asset(i+1,source_kind="ai_video"),clip=Clip(asset_id=uuid4(),start_ms=0,end_ms=3000,asset_duration_ms=3000)) for i,s in enumerate(scenes)}
    spec=assemble_retained_composition(project,audio,[(scenes[0],0,1001),(scenes[1],1001,2100)],inputs,VisualStyleTokens())
    assert spec.master_narration.end_ms==2300
    assert sum(s.duration_frames for s in spec.scenes)==(2300*fps[0]+1000*fps[1]-1)//(1000*fps[1])
    tail=spec.scenes[-1]
    assert (tail.narration_start_ms,tail.narration_end_ms)==(2100,2300)
    assert not tail.caption and not tail.captions and tail.subtitle_treatment.value=="none"
    with Database(tmp_path/"tail.sqlite3") as db:
        renderer=RemotionRenderer(AssetRepository(db),ClipRepository(db),renderer_dir=_renderer_project(tmp_path/"renderer"))
        prepared=renderer._validate_scene(tail,spec.fps,project_id=project.id,master_audio_id=audio.id,validate_scene_narration=False)
        props=renderer._stage_props(spec.model_copy(update={"scenes":[tail]}),{tail.scene_id:prepared},tmp_path,"tail",None)
        assert props["sceneSources"][tail.scene_id]["text"]==""
        assert props["sceneSources"][tail.scene_id]["graphicTreatment"]=="none"


def test_db_fence_tolerates_only_heartbeat_renewal(tmp_path):
    from datetime import datetime,timezone
    from app.db import Database
    from app.domain.models import Job,JobType
    from app.jobs.store import JobStore
    with Database(tmp_path/"fence.sqlite3") as db:
        service=EvaluationCompositionService(db,tmp_path)
        now=datetime.now(timezone.utc)
        job=Job(type=JobType.RENDER,created_at=now,updated_at=now,idempotency_key="fence")
        JobRepository(db).create(job)
        store=JobStore(db)
        claimed=store.claim("fixture",timedelta(minutes=5),max_attempts=1,job_id=job.id)
        before=service._token()
        assert store.heartbeat(claimed.id,"fixture",timedelta(minutes=5))
        assert service._token()==before
        db.connection.execute("UPDATE jobs SET lease_owner='other' WHERE id=?",(str(job.id),))
        assert service._token()!=before


def test_worker_uses_one_render_handler_independent_of_legacy_policy(tmp_path,monkeypatch):
    from app.db import Database
    from app.worker_cli import parse_config,build_runner
    from app.domain.models import JobType
    from app.jobs.handlers import RenderVideoJobHandler
    cfg=parse_config(['--db',str(tmp_path/'worker.sqlite3'),'--data-root',str(tmp_path),'--once','--job-type','render','--max-attempts','1'])
    monkeypatch.setenv('CONTENT_OS_RENDER_EXECUTION_POLICY','evaluation-composition-v1')
    with Database(cfg.db_path) as db:
        runner=build_runner(cfg,db)
        assert type(runner._handlers[JobType.RENDER]) is RenderVideoJobHandler
        monkeypatch.setenv('CONTENT_OS_RENDER_EXECUTION_POLICY','unknown')
        assert type(build_runner(cfg,db)._handlers[JobType.RENDER]) is RenderVideoJobHandler
