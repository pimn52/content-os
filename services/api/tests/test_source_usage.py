"""Source motion accounting across normal videos; no model/media calls."""
from datetime import datetime, timezone
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.db import (AssetRepository, ClipRepository, Database, IPProfileRepository,
    JobRepository, ProjectRepository, PublicationRepository, FeedbackRepository)
from app.domain.models import (Asset, Clip, ContentFeedback, IPProfile, Job, JobStatus,
    JobType, Project, PublicationRecord, RationalFps, RenderVideoJobPayload, VideoSpec, VideoScene, VideoVisual)
from app.domain.models import SourceKind
from app.source_usage import SourceUsageService, SourceUsageConflict


@pytest.fixture
def library():
    db = Database()
    now = datetime.now(timezone.utc)
    ip = IPProfileRepository(db).create(IPProfile(creator_name="创作者"))
    projects = [ProjectRepository(db).create(Project(ip_profile_id=ip.id,title=f"视频{i}",topic="科普",
        created_at=now,fps=RationalFps(numerator=30,denominator=1))) for i in range(2)]
    asset = AssetRepository(db).create(Asset(source_file="source.mp4",content_hash="a"*64,
        duration_ms=20000,width=1080,height=1920,fps=projects[0].fps,
        authorization_reference="入库已授权",imported_at=now))
    clip = ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=20000,asset_duration_ms=20000))
    yield db,projects,asset,clip,now
    db.close()


def run(db,project,now,*,uses=(),stage="awaiting_master",job=None):
    key=uuid4()
    db.connection.execute("""INSERT INTO production_runs(id,project_id,idempotency_key,preflight_fingerprint,
        draft_version,script_revision,style_tokens,repair_allowance,stage,render_job_id,created_at,visual_source_uses)
        VALUES(?,?,?,?,1,1,'{}',1,?,?,?,?)""",
        (str(key),str(project.id),str(key),str(key).replace('-','')*2,stage,
         str(job.id) if job else None,now.isoformat(),json.dumps(uses)))
    return key


def performance(service,asset,start,end,scene):
    return [{**use,"repeatable":False,"content_changed":True} for use in service.resolve(asset.id,start,end,scene)]


def test_same_bytes_different_clip_ids_and_crop_are_one_motion_source(library):
    db,projects,asset,clip,now=library
    crop = AssetRepository(db).create(asset.model_copy(update={"id":uuid4(),"content_hash":"b"*64,
        "duration_ms":4000,"metadata":{"source_derivation":{"source_asset_id":str(asset.id),
        "source_content_hash":asset.content_hash,"source_interval":{"start_ms":4000,"end_ms":8000}}}}))
    service=SourceUsageService(db)
    uses=service.resolve(asset.id,5000,7000,"原片")+service.resolve(crop.id,1000,3000,"裁剪片")
    assert uses[0]["source_hash"]==uses[1]["source_hash"]
    assert (uses[0]["start_ms"],uses[0]["end_ms"])==(uses[1]["start_ms"],uses[1]["end_ms"])
    service.require_available(projects[0].id,uses)  # 原样引用不因换过裁剪文件而被禁用。
    # Adjacent real intervals are usable, not a blanket whole-Asset ban.
    service.require_available(projects[0].id,service.resolve(asset.id,0,4000,"甲")+service.resolve(crop.id,0,4000,"乙"))


def test_generated_output_does_not_hide_repeated_source_motion(library):
    db,projects,asset,clip,now=library
    outputs=[]
    for i in range(2):
        outputs.append(AssetRepository(db).create(asset.model_copy(update={"id":uuid4(),
            "content_hash":str(i+2)*64,"source_kind":SourceKind.AI_VIDEO,"duration_ms":3000,
            "metadata":{"talking_generation":{"reference_clip_id":str(clip.id),"reference_window_start_ms":1000}}})))
    service=SourceUsageService(db)
    uses=[use for i,output in enumerate(outputs) for use in service.resolve(output.id,0,2000,str(i))]
    with pytest.raises(SourceUsageConflict,match="visual_source_repeated_in_video"):
        service.require_available(projects[0].id,uses)


def test_selected_playback_never_slides_to_other_picture_or_truncates_duration(library):
    from app.domain.models import CandidateAsset, UsageCost, CostCategory
    db, projects, asset, clip, now = library
    service = SourceUsageService(db)
    candidate = CandidateAsset(scene_plan_id=uuid4(), source_kind=asset.source_kind, asset_id=asset.id, clip_id=clip.id,
        playback_start_ms=1000, playback_end_ms=5000, match_score=1, why=['selected picture'],
        estimated_cost=UsageCost(category=CostCategory.USER_ASSET, amount=None))
    occupied = performance(service, asset, 1000, 2000, 'earlier')
    assert service.allocate_candidate(candidate, 'current', 1000, occupied, prefer_unused=True) is None
    with pytest.raises(SourceUsageConflict, match='playback_interval_invalid'):
        service.candidate_uses(candidate, 'too-long', 4001)
    with pytest.raises(SourceUsageConflict, match='playback_interval_invalid'):
        service.candidate_uses(candidate.model_copy(update={'playback_end_ms':clip.end_ms + 1}), 'outside', 1000)


def test_different_video_reserves_exact_interval_not_whole_asset(library):
    db,projects,asset,clip,now=library
    service=SourceUsageService(db)
    first=run(db,projects[0],now,uses=service.resolve(asset.id,2000,5000,"a"))
    service.require_available(projects[0].id,service.resolve(asset.id,2000,5000,"a"),run_id=first)
    with pytest.raises(SourceUsageConflict,match="visual_source_reserved_by_other_video"):
        service.require_available(projects[1].id,performance(service,asset,4000,6000,"b"))
    service.require_available(projects[1].id,service.resolve(asset.id,4000,6000,"reference"))
    service.require_available(projects[1].id,service.resolve(asset.id,5000,7000,"b"))


@pytest.mark.parametrize("state",["cancelled","rejected","failed"])
def test_abandoned_or_rejected_video_releases_but_published_history_remains(library,state):
    db,projects,asset,clip,now=library
    service=SourceUsageService(db)
    key=run(db,projects[0],now,uses=service.resolve(asset.id,1000,3000,"a"))
    if state=="cancelled":
        db.connection.execute("UPDATE production_runs SET stage='cancelled' WHERE id=?",(str(key),))
    elif state=="rejected":
        FeedbackRepository(db).create(ContentFeedback(project_id=projects[0].id,output_version=f"run:{key}",
            accepted=False,rejection_reason="动作重复",created_at=now))
    else:
        spec=video(projects[0],asset,clip,1000,3000)
        job=JobRepository(db).create(Job(project_id=projects[0].id,type=JobType.RENDER,status=JobStatus.FAILED,
            idempotency_key=str(uuid4()),created_at=now,updated_at=now,
            payload=RenderVideoJobPayload(project_id=projects[0].id,render_id=uuid4(),video_spec=spec)))
        db.connection.execute("UPDATE production_runs SET stage='render',render_job_id=? WHERE id=?",(str(job.id),str(key)))
    requested=performance(service,asset,1000,3000,"b")
    service.require_available(projects[1].id,requested)
    PublicationRepository(db).create(PublicationRecord(project_id=projects[0].id,output_version=f"run:{key}",
        platform="抖音",published_at=now))
    with pytest.raises(SourceUsageConflict,match="visual_source_already_published"):
        service.require_available(projects[1].id,requested)


def video(project,asset,clip,start,end):
    return VideoSpec(project_id=project.id,format="vertical",width=1080,height=1920,fps=project.fps,
        scenes=[VideoScene(scene_id="a",start_frame=0,duration_frames=(end-start)*30//1000,
            visual=VideoVisual(source_kind="user_asset",authorization_reference="入库已授权",
                asset_id=asset.id,clip_id=clip.id,clip_start_ms=start,clip_end_ms=end,source_duration_ms=asset.duration_ms))])


def test_render_worker_cannot_bypass_cross_video_claim(library):
    from app.jobs.handlers import RenderVideoJobHandler
    from app.jobs.runner import JobExecutionError
    db,projects,asset,clip,now=library
    service=SourceUsageService(db)
    run(db,projects[0],now,uses=service.resolve(asset.id,1000,3000,"a"))
    altered=AssetRepository(db).create(asset.model_copy(update={"id":uuid4(),"content_hash":"b"*64,
        "source_kind":SourceKind.AI_VIDEO,"metadata":{"talking_generation":{"reference_clip_id":str(clip.id),"reference_window_start_ms":0}}}))
    altered_clip=ClipRepository(db).create(clip.model_copy(update={"id":uuid4(),"asset_id":altered.id}))
    job=Job(project_id=projects[1].id,type=JobType.RENDER,status=JobStatus.RUNNING,
        idempotency_key=str(uuid4()),created_at=now,updated_at=now,
        payload=RenderVideoJobPayload(project_id=projects[1].id,render_id=uuid4(),video_spec=video(projects[1],altered,altered_clip,1000,3000)))
    class NeverRender:
        data_root=".tmp/source-usage"
        def render(self,*args,**kwargs):
            pytest.fail("冲突必须在实际渲染之前发现")
    with pytest.raises(JobExecutionError) as failure:
        RenderVideoJobHandler(ProjectRepository(db),NeverRender(),".tmp/source-usage/renders")(job)
    assert failure.value.code=="visual_source_reserved_by_other_video"


def test_two_database_connections_cannot_reserve_the_same_motion(tmp_path,library):
    db,projects,asset,clip,now=library
    path=tmp_path/"race.sqlite3"
    import sqlite3
    target=sqlite3.connect(path)
    db.connection.backup(target);target.close()
    with Database(path) as setup:
        keys=[run(setup,project,now) for project in projects]
    def claim(index):
        with Database(path,startup_mode="existing") as connection:
            try:
                with connection.transaction(immediate=True):
                    service=SourceUsageService(connection)
                    service.save(projects[index].id,keys[index],performance(service,asset,0,2000,"a"))
                return "reserved"
            except SourceUsageConflict:
                return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim,range(2)))==["conflict","reserved"]


def test_same_interval_in_another_creator_is_not_a_global_ban(library):
    db,projects,asset,clip,now=library
    service=SourceUsageService(db)
    run(db,projects[0],now,uses=service.resolve(asset.id,0,2000,"a"))
    other=IPProfileRepository(db).create(IPProfile(creator_name="另一创作者"))
    ProjectRepository(db).update(projects[1].model_copy(update={"ip_profile_id":other.id}))
    service.require_available(projects[1].id,service.resolve(asset.id,0,2000,"b"))


def test_render_usage_records_exact_interval_once_and_publication_consumes_it(library):
    from app.db import AssetUsageRepository
    db,projects,asset,clip,now=library
    service=SourceUsageService(db)
    job=Job(project_id=projects[0].id,type=JobType.RENDER,idempotency_key=str(uuid4()),created_at=now,updated_at=now,
        payload=RenderVideoJobPayload(project_id=projects[0].id,render_id=uuid4(),video_spec=video(projects[0],asset,clip,1000,3000)))
    service.record_render(job);service.record_render(job)
    events=AssetUsageRepository(db).list_for_project(projects[0].id)
    assert len(events)==1 and (events[0].source_start_ms,events[0].source_end_ms)==(1000,3000)
    assert ClipRepository(db).get(clip.id).used_count==1
    PublicationRepository(db).create(PublicationRecord(project_id=projects[0].id,
        output_version=events[0].output_version,platform="抖音",published_at=now))
    with pytest.raises(SourceUsageConflict,match="visual_source_already_published"):
        service.require_available(projects[1].id,performance(service,asset,1000,3000,"b"))
    service.require_available(projects[1].id,service.resolve(asset.id,3000,5000,"b"))


def test_normal_preflight_keeps_original_reference_and_reports_actual_occupancy(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_production_preflight import _create_draft
    path=tmp_path/"normal.sqlite3"
    now=datetime.now(timezone.utc)
    with TestClient(create_app(path)) as client:
        project_id,scene=_create_draft(client)
        with Database(path) as db:
            project=ProjectRepository(db).get(project_id)
            other=ProjectRepository(db).create(project.model_copy(update={"id":uuid4()}))
            asset=AssetRepository(db).create(Asset(source_file="fixture.mp4",content_hash="a"*64,duration_ms=12000,
                width=1080,height=1920,fps=project.fps,authorization_reference="入库授权",imported_at=now))
            ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=12000,asset_duration_ms=12000,
                transcript=scene.voice_text,visual_description="creator showing a workflow",quality_score=.9))
            run(db,other,now,uses=SourceUsageService(db).resolve(asset.id,0,4000,"other"))
        response=client.post(f"/projects/{project_id}/production-preflight",json={})
        assert response.status_code==200,response.text
        selected=response.json()["scenes"][0]["selected_candidate"]
        assert selected["asset_id"]==str(asset.id)
        assert selected["playback_start_ms"] in (None,0)
        assert any("source occupancy 0.33" in reason for reason in selected["why"])
        assert client.get(f"/projects/{project_id}/provider-calls").json()==[]


def test_render_revision_keeps_same_video_source_but_other_video_still_cannot_reuse(library):
    db,projects,asset,clip,now=library
    service=SourceUsageService(db)
    key=run(db,projects[0],now)
    jobs=[]
    for suffix in ("render","render-after:old-job"):
        jobs.append(JobRepository(db).create(Job(project_id=projects[0].id,type=JobType.RENDER,status=JobStatus.COMPLETED,
            idempotency_key=f"production-run:{key}:{suffix}",created_at=now,updated_at=now,
            payload=RenderVideoJobPayload(project_id=projects[0].id,render_id=uuid4(),video_spec=video(projects[0],asset,clip,1000,3000)))))
    db.connection.execute("UPDATE production_runs SET stage='render',render_job_id=? WHERE id=?",(str(jobs[1].id),str(key)))
    service.record_render(jobs[0])
    PublicationRepository(db).create(PublicationRecord(project_id=projects[0].id,
        output_version=f"render:{jobs[0].payload.render_id}",platform="抖音",published_at=now))
    service.require_render(jobs[1])
    with pytest.raises(SourceUsageConflict,match="visual_source_already_published"):
        service.require_available(projects[1].id,performance(service,asset,1000,3000,"b"))


def test_pending_versions_hold_as_one_work_and_explicit_selection_releases_discarded_versions(library):
    db,projects,asset,clip,now=library
    service=SourceUsageService(db)
    jobs=[]
    work_id=None
    for start in (1000,6000):
        job=JobRepository(db).create(Job(project_id=projects[0].id,type=JobType.RENDER,status=JobStatus.COMPLETED,
            idempotency_key=str(uuid4()),created_at=now,updated_at=now,payload=RenderVideoJobPayload(
                project_id=projects[0].id,render_id=uuid4(),video_spec=video(projects[0],asset,clip,start,start+2000))))
        key=run(db,projects[0],now,stage="render",job=job)
        work_id=work_id or key
        db.connection.execute("UPDATE production_runs SET work_id=? WHERE id=?",(str(work_id),str(key)))
        service.record_render(job)
        jobs.append(job)
    first=performance(service,asset,1000,3000,"other")
    second=performance(service,asset,6000,8000,"other")
    with pytest.raises(SourceUsageConflict,match="reserved_by_other_video"):
        service.require_available(projects[1].id,first+second)
    assert service.clip_occupancy_degree(clip,service.occupied(projects[1].id))==.2
    assert service.choice_impact(projects[0].id,jobs[0].payload.render_id)["discard_render_ids"]==[str(jobs[1].payload.render_id)]
    service.choose(projects[0].id,jobs[0].payload.render_id,"selected")
    assert service.clip_occupancy_degree(clip,service.occupied(projects[1].id))==.1
    with pytest.raises(SourceUsageConflict,match="reserved_by_other_video"):
        service.require_available(projects[1].id,first)
    service.choose(projects[0].id,jobs[1].payload.render_id,"selected")
    service.require_available(projects[1].id,first)
    assert service.choice(projects[0].id,jobs[0].payload.render_id)=="discarded"
    with pytest.raises(SourceUsageConflict,match="reserved_by_other_video"):
        service.require_available(projects[1].id,second)
    service.choose(projects[0].id,jobs[1].payload.render_id,"discarded")
    service.require_available(projects[1].id,second)
    assert service.clip_occupancy_degree(clip,service.occupied(projects[1].id))==0
    PublicationRepository(db).create(PublicationRecord(project_id=projects[0].id,
        output_version=f"render:{jobs[1].payload.render_id}",platform="抖音",published_at=now))
    with pytest.raises(SourceUsageConflict,match="already_published"):
        service.require_available(projects[1].id,second)


@pytest.mark.parametrize("role",["meme","signature"])
def test_original_meme_can_repeat_for_any_person_but_new_lip_performance_cannot(library,role):
    db,projects,asset,clip,now=library
    AssetRepository(db).update(asset.model_copy(update={"metadata":{"reuse_role":role,"r1_identity":"other"}}))
    service=SourceUsageService(db)
    uses=service.resolve(asset.id,0,2000,"a")
    service.require_available(projects[0].id,uses+service.resolve(asset.id,0,2000,"b"))
    service.record_render(Job(project_id=projects[0].id,type=JobType.RENDER,idempotency_key=str(uuid4()),created_at=now,updated_at=now,
        payload=RenderVideoJobPayload(project_id=projects[0].id,render_id=uuid4(),video_spec=video(projects[0],asset,clip,0,2000))))
    from app.db import AssetUsageRepository
    event=AssetUsageRepository(db).list_for_project(projects[0].id)[0]
    PublicationRepository(db).create(PublicationRecord(project_id=projects[0].id,output_version=event.output_version,platform="抖音",published_at=now))
    service.require_available(projects[1].id,uses)
    output=AssetRepository(db).create(asset.model_copy(update={"id":uuid4(),"content_hash":"b"*64,"duration_ms":2000,
        "metadata":{"reuse_role":role,"talking_generation":{"reference_clip_id":str(clip.id),"reference_window_start_ms":0}}}))
    with pytest.raises(SourceUsageConflict,match="already_published"):
        service.require_available(projects[1].id,service.resolve(output.id,0,2000,"talking"))
    AssetRepository(db).update(asset.model_copy(update={"metadata":{"reuse_role":role,"r1_identity":"creator"}}))
    service.require_available(projects[1].id,service.resolve(asset.id,0,2000,"a")+service.resolve(asset.id,0,2000,"b"))


def test_explicit_draft_ignores_cross_output_occupancy_but_not_performance_restarts(library):
    db,projects,asset,clip,now=library
    service=SourceUsageService(db)
    run(db,projects[0],now,uses=performance(service,asset,0,2000,"a"))
    ProjectRepository(db).update(projects[1].model_copy(update={"delivery_intent":"draft"}))
    use=performance(service,asset,0,2000,"b")
    service.require_available(projects[1].id,use)
    with pytest.raises(SourceUsageConflict,match="repeated_in_video"):
        service.require_available(projects[1].id,use+use)


def test_occupancy_degree_unions_intervals_instead_of_counting_versions(library):
    db,projects,asset,clip,now=library
    service=SourceUsageService(db)
    used=service.resolve(asset.id,0,2000,"a")+service.resolve(asset.id,1000,3000,"b")
    assert service.clip_occupancy_degree(clip,used*5)==.15
