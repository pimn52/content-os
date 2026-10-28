"""已有选材被核验淘汰后，补缺沿用同一次外取台账。"""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.domain.models import MaterialRequest, SourceKind, VisualIntent
from app.external_acquisition import ExternalAcquisitionError
from app.media.planning import split_footage_clauses
from test_external_acquisition import fixture


def missing_scene(make_scene, query="camera settings", order=0):
    return make_scene(query,order).model_copy(update={"material_request":None,"preferred_clip_id":uuid4()})


def gap_request(scene, query="camera settings"):
    return scene.model_copy(update={"preferred_clip_id":None,
        "material_request":MaterialRequest(source_kind="stock",query=query,reason="原候选未通过用途核验")})


def state_file(tmp_path,job_id):
    return tmp_path/"render-diagnostics"/str(job_id)/"external-materials.json"


def test_empty_initial_request_can_acquire_one_gap_batch_and_restore_without_network(tmp_path):
    db,project,asset,make_scene,calls,service,_=fixture(tmp_path)
    try:
        original=missing_scene(make_scene)
        job=uuid4();scenes=[original];gap=gap_request(original)
        assert service.acquire(project,scenes,job)=={}
        initial=json.loads(state_file(tmp_path,job).read_text(encoding="utf8"))
        assert not calls
        result=service.acquire(project,scenes,job,supplement_for=["camera settings"],gap_scenes=[gap])
        assert result=={"camera settings":[asset.id]}
        kept=json.loads(state_file(tmp_path,job).read_text(encoding="utf8"))
        assert kept["fingerprint"]==initial["fingerprint"]
        assert kept["started_at"]==initial["started_at"]
        assert kept["supplement"]["import_limit"]==2
        assert len(kept["searches"])==len(kept["imports"])==1
        assert service.resolution_scenes(scenes,job)==(gap,)
        calls.clear()
        assert service.acquire(project,scenes,job)==result
        assert service.acquire(project,scenes,job,supplement_for=["camera settings"],gap_scenes=[gap])==result
        assert all(call[0]=="analyze" for call in calls)
        changed=gap_request(original,"different camera action")
        with pytest.raises(ExternalAcquisitionError,match="批次已改变"):
            service.acquire(project,scenes,job,supplement_for=["different camera action"],gap_scenes=[changed])
        changed_original=original.model_copy(update={"voice_text":"另一段内容。"})
        with pytest.raises(ExternalAcquisitionError,match="原计划已改变"):
            service.acquire(project,[changed_original],job)
        with pytest.raises(ExternalAcquisitionError,match="原计划已改变"):
            service.resolution_scenes([changed_original],job)
    finally:db.close()


def test_gap_on_existing_acquisition_shares_search_import_and_work_time_limits(tmp_path,monkeypatch):
    db,project,asset,make_scene,calls,service,_=fixture(tmp_path)
    clock=[0.0]
    monkeypatch.setattr("app.external_acquisition.time",SimpleNamespace(monotonic=lambda:clock[0]))
    factory=service.service_factory
    class TimedService:
        def search(self,*args):
            clock[0]+=5
            return factory("stock-a",project).search(*args)
        def import_video(self,*args,**kwargs):
            clock[0]+=5
            return factory("stock-a",project).import_video(*args,**kwargs)
    service.service_factory=lambda *args:TimedService()
    service.analyze=lambda identity:clock.__setitem__(0,clock[0]+1)
    service.max_searches=2;service.max_imports=2;service.max_seconds=30
    try:
        first=make_scene("first query")
        missing=[missing_scene(make_scene,order=index) for index in (1,2)]
        scenes=[first,*missing];job=uuid4()
        assert service.acquire(project,scenes,job)=={"first query":[asset.id]}
        initial=json.loads(state_file(tmp_path,job).read_text(encoding="utf8"))
        clock[0]+=600
        gaps=[gap_request(scene,f"missing query {index}") for index,scene in enumerate(missing)]
        result=service.acquire(project,scenes,job,supplement_for=[gap.material_request.query for gap in gaps],gap_scenes=gaps)
        kept=json.loads(state_file(tmp_path,job).read_text(encoding="utf8"))
        assert set(result)=={"first query","missing query 0"}
        assert len(kept["searches"])==len(kept["imports"])==2
        assert kept["supplement"]["import_limit"]==2
        assert kept["started_at"]==initial["started_at"]
        assert initial["acquisition_elapsed_seconds"]<kept["acquisition_elapsed_seconds"]<30
        assert kept["acquisition_limit_seconds"]==30
        calls.clear();service.max_seconds=120
        assert service.acquire(project,scenes,job)==result
        assert not calls
        assert json.loads(state_file(tmp_path,job).read_text(encoding="utf8"))["acquisition_limit_seconds"]==30
    finally:db.close()


def test_gap_uses_exact_split_clause_and_restore_keeps_other_clause_unchanged(tmp_path):
    db,project,_,make_scene,_,service,_=fixture(tmp_path)
    try:
        original=missing_scene(make_scene).model_copy(update={"voice_text":"先打开相机设置。再检查视频参数。","duration_target_ms":16000})
        clauses=split_footage_clauses([original]);assert len(clauses)==2
        gap=gap_request(clauses[1]);job=uuid4()
        service.acquire(project,[original],job,supplement_for=["camera settings"],gap_scenes=[gap])
        assert service.resolution_scenes([original],job)==(clauses[0],gap)
    finally:db.close()


@pytest.mark.parametrize("invalid",["creator","stock-forbidden","unknown-scene","copy-changed","already-requested","ai-request"])
def test_gap_rejects_person_identity_source_and_script_changes_before_any_call(tmp_path,invalid):
    db,project,_,make_scene,calls,service,_=fixture(tmp_path)
    try:
        original=missing_scene(make_scene)
        if invalid=="creator":original=original.model_copy(update={"visual_requirement":"creator_speaking","visual_requirement_reason":"本人表达"})
        if invalid=="stock-forbidden":original=original.model_copy(update={"preferred_sources":[SourceKind.USER_ASSET],"fallback_sources":[]})
        if invalid=="already-requested":original=gap_request(original,"original query")
        gap=gap_request(original)
        if invalid=="unknown-scene":gap=gap.model_copy(update={"scene_id":"unrelated"})
        if invalid=="copy-changed":gap=gap.model_copy(update={"voice_text":"暗改旁白。"})
        if invalid=="ai-request":gap=gap.model_copy(update={"material_request":MaterialRequest(source_kind="ai_broll",query="camera settings",reason="不是外部图库")})
        job=uuid4()
        with pytest.raises(ExternalAcquisitionError,match="补缺只能"):
            service.acquire(project,[original],job,supplement_for=["camera settings"],gap_scenes=[gap])
        assert not calls and not state_file(tmp_path,job).exists()
    finally:db.close()


def test_gap_cannot_add_a_second_batch_after_existing_supplement(tmp_path):
    db,project,_,make_scene,calls,service,_=fixture(tmp_path)
    try:
        first=make_scene("first query");original=missing_scene(make_scene,order=1)
        scenes=[first,original];job=uuid4()
        service.acquire(project,scenes,job)
        service.acquire(project,scenes,job,supplement_for=["first query"])
        before=state_file(tmp_path,job).read_text(encoding="utf8");calls.clear()
        with pytest.raises(ExternalAcquisitionError,match="批次已改变"):
            service.acquire(project,scenes,job,supplement_for=["camera settings"],gap_scenes=[gap_request(original)])
        assert not calls and state_file(tmp_path,job).read_text(encoding="utf8")==before
    finally:db.close()


def test_old_ledger_keeps_original_fingerprint_when_gap_is_added(tmp_path):
    db,project,_,make_scene,_,service,_=fixture(tmp_path)
    try:
        scenes=[make_scene("first query"),missing_scene(make_scene,order=1)];job=uuid4()
        service.acquire(project,scenes,job)
        path=state_file(tmp_path,job);legacy=json.loads(path.read_text(encoding="utf8"))
        legacy.pop("scene_fingerprint")
        path.write_text(json.dumps(legacy),encoding="utf8")
        service.acquire(project,scenes,job,supplement_for=["camera settings"],gap_scenes=[gap_request(scenes[1])])
        kept=json.loads(path.read_text(encoding="utf8"))
        assert kept["fingerprint"]==legacy["fingerprint"]
        assert len(kept["searches"])==len(kept["imports"])==2
        assert kept["started_at"]==legacy["started_at"]
    finally:db.close()


def test_visual_revision_gap_keeps_original_admission_and_shared_ledger(tmp_path):
    db,project,asset,make_scene,calls,service,_=fixture(tmp_path)
    try:
        original=missing_scene(make_scene)
        revised=original.model_copy(update={"visual_intent":VisualIntent(subject="camera menu",action="set bitrate")})
        gap=gap_request(revised,"camera menu bitrate");job=uuid4()
        service.acquire(project,[original],job)
        initial=json.loads(state_file(tmp_path,job).read_text(encoding="utf8"))
        assert service.acquire(project,[original],job,visual_scenes=[revised],gap_scenes=[gap],
            supplement_for=[gap.material_request.query])=={"camera menu bitrate":[asset.id]}
        kept=json.loads(state_file(tmp_path,job).read_text(encoding="utf8"))
        assert kept["fingerprint"]==initial["fingerprint"] and kept["scene_fingerprint"]==initial["scene_fingerprint"]
        assert kept["started_at"]==initial["started_at"]
        assert kept["acquisition_limit_seconds"]==initial["acquisition_limit_seconds"]
        assert kept["acquisition_elapsed_seconds"]>=initial["acquisition_elapsed_seconds"]
        assert len(kept["searches"])==len(kept["imports"])==1
        assert service.resolution_scenes([original],job)==(gap,)
        calls.clear()
        assert service.acquire(project,[original],job)=={"camera menu bitrate":[asset.id]}
        assert all(call[0]=="analyze" for call in calls)
        with pytest.raises(ExternalAcquisitionError,match="调整已登记"):
            service.acquire(project,[original],job,visual_scenes=[revised.model_copy(update={
                "visual_intent":VisualIntent(subject="another visual")})])
    finally:db.close()


def test_visual_revision_reuses_remaining_slots_instead_of_resetting_initial_acquisition(tmp_path):
    db,project,_,make_scene,calls,service,_=fixture(tmp_path)
    try:
        original=make_scene("initial query");job=uuid4()
        service.max_searches=1;service.max_imports=1
        service.acquire(project,[original],job)
        initial=json.loads(state_file(tmp_path,job).read_text(encoding="utf8"));calls.clear()
        revised=original.model_copy(update={"material_request":None,"visual_intent":VisualIntent(subject="revised camera view")})
        gap=gap_request(revised,"revised query")
        assert service.acquire(project,[original],job,visual_scenes=[revised],gap_scenes=[gap],
            supplement_for=[gap.material_request.query]) is None
        kept=json.loads(state_file(tmp_path,job).read_text(encoding="utf8"))
        assert not any(call[0] in {"search","import"} for call in calls)
        assert len(kept["searches"])==len(kept["imports"])==kept["supplement"]["import_limit"]==1
        assert kept["started_at"]==initial["started_at"]
    finally:db.close()


def test_visual_revision_cannot_add_another_supplement_after_initial_gap(tmp_path):
    db,project,_,make_scene,calls,service,_=fixture(tmp_path)
    try:
        original=missing_scene(make_scene);job=uuid4()
        gap=gap_request(original)
        service.acquire(project,[original],job,supplement_for=[gap.material_request.query],gap_scenes=[gap])
        revised=original.model_copy(update={"visual_intent":VisualIntent(subject="revised camera view")})
        revised_gap=gap_request(revised,"revised query")
        before=state_file(tmp_path,job).read_text(encoding="utf8");calls.clear()
        assert service.acquire(project,[original],job,visual_scenes=[revised],gap_scenes=[revised_gap],
            supplement_for=[revised_gap.material_request.query]) is None
        assert not calls and state_file(tmp_path,job).read_text(encoding="utf8")==before
    finally:db.close()


@pytest.mark.parametrize("invalid",["voice","claim","requirement","request","identity"])
def test_visual_revision_rejects_content_or_admission_change_before_call(tmp_path,invalid):
    db,project,_,make_scene,calls,service,_=fixture(tmp_path)
    try:
        original=missing_scene(make_scene)
        change={"voice":{"voice_text":"改写旁白。"},"claim":{"viewer_takeaway":"改写主张"},
            "requirement":{"visual_requirement":"creator_speaking","visual_requirement_reason":"暗改人物"},
            "request":{"preferred_clip_id":None,"preferred_sources":[SourceKind.STOCK,SourceKind.AI_BROLL],
                "material_request":MaterialRequest(source_kind="ai_broll",query="new AI request",reason="增加生成请求")},
            "identity":{"scene_id":"changed"}}[invalid]
        revised=original.model_copy(update=change);job=uuid4()
        with pytest.raises(ExternalAcquisitionError,match="不能改变原稿"):
            service.acquire(project,[original],job,visual_scenes=[revised])
        assert not calls and not state_file(tmp_path,job).exists()
    finally:db.close()


def test_ordinary_visual_revision_uses_common_acquisition_and_current_visual_qa(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    from app.db import ProjectDraftRepository
    from app.domain.models import ProjectDraft
    from app.media.planning import MaterialResolutionError
    from app.video_production import VideoProductionService
    db,project,asset,make_scene,calls,acquirer,_=fixture(tmp_path)
    try:
        original=missing_scene(make_scene)
        revised=original.model_copy(update={"visual_intent":VisualIntent(subject="camera menu",action="set bitrate")})
        draft=ProjectDraftRepository(db).save(ProjectDraft(project_id=project.id,version=1,script_revision=1,
            script=original.voice_text,topic=project.topic,scenes=[original],updated_at=datetime.now(timezone.utc)))
        job=SimpleNamespace(id=uuid4(),payload=SimpleNamespace(topic=project.topic))
        acquirer.acquire(project,[original],job.id)
        initial=json.loads(state_file(tmp_path,job.id).read_text(encoding="utf8"))
        attempts=[]
        def resolve(project,scenes,scope,**kwargs):
            attempts.append((tuple(scenes),scope,kwargs))
            if len(attempts)==1:raise MaterialResolutionError(f"material_scene_unresolved:{original.scene_id}")
            assert scenes[0].voice_text==original.voice_text
            assert scenes[0].visual_intent==revised.visual_intent
            assert kwargs["acquired_assets"]=={"camera menu set bitrate":[asset.id]}
            assert scenes[0].material_request.query=="camera menu set bitrate"
            return tuple(scenes)
        resolver=SimpleNamespace(revision_candidates=lambda *a:(original.preferred_clip_id,),resolve=resolve,
            check_external_configuration=lambda scenes:None,max_analysis_batches=1)
        production=VideoProductionService(db,tmp_path,material_resolver=resolver,material_acquirer=acquirer)
        monkeypatch.setattr("app.video_production.generate_scene_plan",lambda *a,**kw:SimpleNamespace(scenes=(revised,)))
        resolved=production._revise_visuals(project,draft,job,job.id,None,object(),object(),
            MaterialResolutionError(f"material_scene_unresolved:{original.scene_id}"))
        assert resolved[0].visual_intent==revised.visual_intent and len(attempts)==2
        assert ProjectDraftRepository(db).get(project.id)==draft
        kept=json.loads(state_file(tmp_path,job.id).read_text(encoding="utf8"))
        assert kept["fingerprint"]==initial["fingerprint"] and kept["started_at"]==initial["started_at"]
        assert kept["supplement"]["import_limit"]==2
        assert len([call for call in calls if call[0]=="search"])==1
    finally:db.close()


def test_selected_or_person_only_plan_records_original_for_later_cutaway_without_network(tmp_path):
    from datetime import datetime,timezone
    from app.db import ProjectDraftRepository
    from app.domain.models import ProjectDraft
    from app.video_production import VideoProductionService
    db,project,_,make_scene,calls,acquirer,_=fixture(tmp_path)
    try:
        original=missing_scene(make_scene).model_copy(update={"visual_requirement":"creator_speaking",
            "visual_requirement_reason":"本人讲解","preferred_sources":[SourceKind.AI_VIDEO],
            "fallback_sources":[SourceKind.STOCK],"supporting_visual_intent":VisualIntent(subject="camera menu")})
        job=SimpleNamespace(id=uuid4(),project_id=project.id,
            payload=SimpleNamespace(topic=project.topic,candidate_of_run_id=None))
        ProjectDraftRepository(db).save(ProjectDraft(project_id=project.id,version=1,script_revision=1,
            topic=project.topic,script=original.voice_text,scenes=[original],updated_at=datetime.now(timezone.utc),
            evidence_refs=[f'content_plan_job:{job.id}']))
        def halt(*args,**kwargs):raise RuntimeError('fixture_after_record')
        resolver=SimpleNamespace(check_configuration=lambda *args:None,check_external_configuration=lambda *args:None,
            selected_materials_ready=lambda *args:True,resolve=halt)
        production=VideoProductionService(db,tmp_path,material_resolver=resolver,material_acquirer=acquirer)
        production._require_talking_coverage=lambda *args:None
        with pytest.raises(RuntimeError,match='fixture_after_record'):
            production.plan(job,object(),object())
        assert acquirer.original_scenes(job.id)==(original,)
        assert not calls
    finally:db.close()


def measured_fixture(tmp_path,monkeypatch):
    """本地合约夹具：保留真实稿件/QA/文件核对，只替代无关的Profile选择。"""
    import hashlib
    from datetime import datetime,timezone
    from app.db import AudioAssetRepository
    from app.domain.models import AudioAsset,TranscriptSegment
    from app.project_drafts import _split_talking_master_clauses
    db,project,asset,make_scene,calls,acquirer,_=fixture(tmp_path)
    original=make_scene("unused").model_copy(update={"preferred_clip_id":None,"material_request":None,
        "voice_text":"先说观点。再看画面。","duration_target_ms":6000,
        "preferred_sources":[SourceKind.AI_VIDEO],"fallback_sources":[SourceKind.USER_ASSET,SourceKind.STOCK],
        "visual_requirement":"creator_speaking","visual_requirement_reason":"本人讲解",
        "supporting_visual_intent":VisualIntent(subject="camera menu",action="set bitrate")})
    source=tmp_path/"master.wav";source.write_bytes(b"fixture master audio bytes")
    master=AudioAssetRepository(db).create(AudioAsset(source_file=str(source),content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        duration_ms=3000,sample_rate=24000,channels=1,authorization_reference="fixture profile consent",
        imported_at=datetime.now(timezone.utc),transcript_source="fixture independent QA",
        transcript_segments=[TranscriptSegment(start_ms=0,end_ms=1500,text="先说观点。"),TranscriptSegment(start_ms=1500,end_ms=3000,text="再看画面。")],
        metadata={"voice_generation":{"project_id":str(project.id),"target_text":original.voice_text,"qa_state":"verified",
            "human_review_state":"approved","human_review":{"approved":True,"evidence_reference":"fixture only",
                "findings":["fixture quality judgment"],"likeness":"pass","naturalness":"pass","emphasis":"pass",
                "pace":"pass","pauses":"pass","rhythm":"pass","reviewed_at":datetime.now(timezone.utc).isoformat()}}}))
    monkeypatch.setattr("app.voice_plan.require_project_voice_audio",lambda *a:None)
    parts=_split_talking_master_clauses(db,project,original.voice_text,[original],master=master,max_segment_ms=1500)
    assert len(parts)==2
    measured=[parts[0],parts[1].model_copy(update={"visual_intent":original.supporting_visual_intent,
        "supporting_visual_intent":None,"preferred_clip_id":None,"preferred_sources":original.fallback_sources,
        "fallback_sources":[],"graphic_plan":None,"material_request":None,"visual_requirement":"explanatory",
        "visual_requirement_reason":"沿用原稿允许的切题实拍覆盖同一段完整旁白，保留其它本人出镜。"})]
    return db,project,asset,original,calls,acquirer,master,measured


def test_measured_cutaway_acquisition_binds_exact_master_and_restores_same_ledger(tmp_path,monkeypatch):
    db,project,asset,original,calls,acquirer,master,measured=measured_fixture(tmp_path,monkeypatch)
    try:
        job=uuid4();acquirer.acquire(project,[original],job)
        assert acquirer.original_scenes(job)==(original,)
        initial=json.loads(state_file(tmp_path,job).read_text(encoding="utf8"))
        gap=gap_request(measured[1],"camera menu set bitrate")
        assert acquirer.acquire(project,[original],job,measured_scenes=measured,master_audio_id=master.id,
            gap_scenes=[gap],supplement_for=[gap.material_request.query])=={gap.material_request.query:[asset.id]}
        kept=json.loads(state_file(tmp_path,job).read_text(encoding="utf8"))
        assert kept["started_at"]==initial["started_at"] and kept["fingerprint"]==initial["fingerprint"]
        assert kept["master_audio_id"]==str(master.id) and kept["master_content_hash"]==master.content_hash
        assert kept["supplement"]["import_limit"]==2 and len(kept["searches"])==len(kept["imports"])==1
        assert acquirer.resolution_scenes([original],job)==(measured[0],gap)
        calls.clear()
        assert acquirer.acquire(project,[original],job)=={gap.material_request.query:[asset.id]}
        assert all(call[0]=="analyze" for call in calls)
    finally:db.close()


def test_person_only_initial_plan_reflows_to_stock_and_uses_original_ledger(tmp_path,monkeypatch):
    from app.media.planning import MaterialResolutionError
    from app.video_production import VideoProductionService
    db,project,asset,original,calls,acquirer,master,measured=measured_fixture(tmp_path,monkeypatch)
    try:
        job=SimpleNamespace(id=uuid4(),payload=SimpleNamespace(topic=project.topic))
        acquirer.remember_original_scenes(project,[original],job.id)
        assert not calls
        attempts=[]
        def resolve(project,scenes,scope,**kwargs):
            attempts.append((tuple(scenes),scope,kwargs))
            if len(attempts)==1:raise MaterialResolutionError(f'material_scene_unresolved:{measured[1].scene_id}')
            assert kwargs['acquired_assets']=={'camera menu set bitrate':[asset.id]}
            assert scenes[1].material_request.query=='camera menu set bitrate'
            return tuple(scenes)
        resolver=SimpleNamespace(resolve=resolve,check_external_configuration=lambda *args:None)
        production=VideoProductionService(db,tmp_path,material_resolver=resolver,material_acquirer=acquirer)
        result=production._resolve_with_supplement(project,measured,job,job.id,uuid4(),None,{},
            measured_scenes=measured,master_audio_id=master.id)
        assert ''.join(scene.voice_text for scene in result)==original.voice_text
        state=json.loads(state_file(tmp_path,job.id).read_text(encoding='utf8'))
        assert state['master_audio_id']==str(master.id) and len(state['searches'])==len(state['imports'])==1
        assert [call[0] for call in calls if call[0] in {'search','import'}]==['search','import']
        assert any(call[0]=='analyze' for call in calls)
    finally:db.close()


@pytest.mark.parametrize("invalid",["copy","timing","claim","intent","source","master-project","qa","file","old-ledger"])
def test_measured_cutaway_rejects_unbound_changes_before_network(tmp_path,monkeypatch,invalid):
    from app.db import AudioAssetRepository
    db,project,_,original,calls,acquirer,master,measured=measured_fixture(tmp_path,monkeypatch)
    try:
        job=uuid4();acquirer.acquire(project,[original],job)
        path=state_file(tmp_path,job)
        if invalid=="copy":measured[1]=measured[1].model_copy(update={"voice_text":"改写观点。"})
        elif invalid=="timing":measured[1]=measured[1].model_copy(update={"duration_target_ms":1400})
        elif invalid=="claim":measured[1]=measured[1].model_copy(update={"viewer_takeaway":"新主张"})
        elif invalid=="intent":measured[1]=measured[1].model_copy(update={"visual_intent":VisualIntent(subject="unplanned visual")})
        elif invalid=="source":measured[1]=measured[1].model_copy(update={"preferred_sources":[SourceKind.AI_BROLL]})
        elif invalid in {"master-project","qa"}:
            generation={**master.metadata["voice_generation"],**({"project_id":str(uuid4())} if invalid=="master-project" else {"qa_state":"pending"})}
            AudioAssetRepository(db).update(master.model_copy(update={"metadata":{"voice_generation":generation}}))
        elif invalid=="file":(tmp_path/"master.wav").write_bytes(b"changed master bytes")
        elif invalid=="old-ledger":
            state=json.loads(path.read_text(encoding="utf8"));state.pop("original_scenes")
            path.write_text(json.dumps(state),encoding="utf8")
        before=path.read_text(encoding="utf8");calls.clear()
        with pytest.raises(ExternalAcquisitionError):
            acquirer.acquire(project,[original],job,measured_scenes=measured,master_audio_id=master.id,
                gap_scenes=[gap_request(measured[1])],supplement_for=["camera settings"])
        assert not calls and path.read_text(encoding="utf8")==before
    finally:db.close()
