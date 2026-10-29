"""真实配时后补充人物画面方案，保留原稿、旁白和原取材台账。"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db import BudgetPolicyRepository
from app.domain.models import BudgetPolicy, SourceKind, VisualIntent
from app.external_acquisition import ExternalAcquisitionError, reserve_production_tail
from app.budget import BudgetLimitError
from app.media.planning import VisualMaterialResolver
from app.providers.scene_planner import ScenePlannerProviderResponseError
from test_scene_visual_revision import _revision_fixture, _planner
from test_external_acquisition_gaps import measured_fixture, gap_request


def test_capacity_failure_with_existing_alternatives_skips_revision(tmp_path, monkeypatch):
    from app.production_runs import ProductionRunNotReady
    from app.talking.pool import TalkingPoolCapacityError
    from app.video_production import VideoProductionService

    project, scenes, _, _ = _revision_fixture()
    scenes = [scene.model_copy(update={"supporting_visual_intent": VisualIntent(subject="设备", action="操作")})
        if scene.visual_requirement == "creator_speaking" else scene for scene in scenes]
    draft = SimpleNamespace(version=1, script_revision=1, script="稿件", scenes=scenes)
    master = SimpleNamespace()
    service = VideoProductionService(None, tmp_path, planner=object(), planner_cost_estimator=object(),
        material_resolver=object())
    for name, value in (("ProjectRepository", project), ("ProjectDraftRepository", draft),
                        ("AudioAssetRepository", master)):
        monkeypatch.setattr(f"app.video_production.{name}", lambda db, value=value: SimpleNamespace(get=lambda _: value))
    pool = SimpleNamespace(entries=(object(),),occupancy_state='known',summary=lambda **_: {"max_narration_duration_ms": 1000, "longest_continuous_ms": 1000})
    monkeypatch.setattr("app.talking.pool.build_talking_pool", lambda *a, **kw: pool)
    monkeypatch.setattr("app.video_production._split_talking_master_clauses", lambda *a, **kw: scenes)
    monkeypatch.setattr("app.assembly.video_spec._master_narration_intervals", lambda *a: [(s, 0, 1000) for s in scenes])
    def fail_schedule(*args, **kwargs):
        raise TalkingPoolCapacityError()
    monkeypatch.setattr("app.talking.scheduling.schedule_talking_scenes", fail_schedule)
    calls = []
    monkeypatch.setattr(service, "_motion_revision", lambda *a: calls.append("revision"))
    monkeypatch.setattr(service, "_reserve_planning_tail", lambda *a, **kw: calls.append("reservation"))
    row = {"project_id": str(project.id), "draft_version": 1, "script_revision": 1,
        "voice_audio_id": str(uuid4()), "id": str(uuid4()), "start_job_id": str(uuid4())}
    with pytest.raises(ProductionRunNotReady) as error:
        service._prepare_measured_plan(row)
    assert error.value.reasons == ("talking_motion_capacity_insufficient",)
    assert calls == []
    assert not (tmp_path / "render-diagnostics").exists()


def test_model_only_adds_explained_footage_alternative_and_preserves_content():
    project, originals, _, _ = _revision_fixture()
    context={"visual_revision":{"kind":"talking_capacity","scenes":[s.model_dump(mode="json") for s in originals]}}
    document={"scenes":[
        {"scene_id":"creator","supporting_visual_intent":{"subject":"拍摄设备","action":"调节设置","framing":None,"description":None},
            "fallback_sources":["stock"],"replaceability_reason":"观点可由相关操作画面辅助讲述"},
        {"scene_id":"evidence","supporting_visual_intent":None,"fallback_sources":[],"replaceability_reason":None}]}
    planner,requests=_planner(document)
    result=planner.plan(project,script="\n\n".join(s.voice_text for s in originals),context=context)
    assert len(requests)==1 and result.scenes[1]==originals[1]
    assert result.scenes[0].model_dump(exclude={"supporting_visual_intent","fallback_sources"})==originals[0].model_dump(exclude={"supporting_visual_intent","fallback_sources"})
    assert dict(result.replaceability_reasons)=={"creator":"观点可由相关操作画面辅助讲述"}
    document["scenes"][0]["replaceability_reason"]=None
    with pytest.raises(ScenePlannerProviderResponseError):_planner(document)[0].plan(project,context=context)
    document["scenes"][0]["replaceability_reason"]="理由"
    document["scenes"][0]["fallback_sources"]=["typography"]
    with pytest.raises(ScenePlannerProviderResponseError):_planner(document)[0].plan(project,context=context)


def test_measured_motion_revision_uses_original_ledger_and_exact_master(tmp_path,monkeypatch):
    db,project,asset,amended,calls,acquirer,master,measured=measured_fixture(tmp_path,monkeypatch)
    try:
        original=amended.model_copy(update={"supporting_visual_intent":None})
        job=uuid4()
        acquirer.remember_original_scenes(project,[original],job)
        path=tmp_path/"render-diagnostics"/str(job)
        (path/"visual-revision-attempt.json").write_text(json.dumps({"kind":"talking_capacity",
            "master_audio_id":str(master.id),"master_content_hash":master.content_hash}),encoding="utf-8")
        evidence={"master_audio_id":str(master.id),"master_content_hash":master.content_hash,
            "scenes":[amended.model_dump(mode="json")],"reasons":{original.scene_id:"可用关联录像辅助解释"}}
        (path/"motion-revision.json").write_text(json.dumps(evidence),encoding="utf-8")
        gap=gap_request(measured[1],"camera menu set bitrate")
        result=acquirer.acquire(project,[original],job,visual_scenes=[amended],measured_scenes=measured,
            master_audio_id=master.id,gap_scenes=[gap],supplement_for=[gap.material_request.query])
        assert result=={gap.material_request.query:[asset.id]}
        assert acquirer.resolution_scenes([original],job)[1]==gap
        state=json.loads((path/"external-materials.json").read_text(encoding="utf-8"))
        assert state["motion_revision"] and state["master_content_hash"]==master.content_hash
        evidence["master_content_hash"]="changed"
        (path/"motion-revision.json").write_text(json.dumps(evidence),encoding="utf-8")
        with pytest.raises(ExternalAcquisitionError):acquirer.resolution_scenes([original],job)
    finally:db.close()


def test_visual_revision_keeps_prior_selected_clip_and_admitted_content(tmp_path,monkeypatch):
    db,project,asset,amended,_,acquirer,_,_=measured_fixture(tmp_path,monkeypatch)
    try:
        original=amended.model_copy(update={"supporting_visual_intent":None})
        footage=original.model_copy(update={"id":uuid4(),"scene_id":"evidence","order":1,
            "visual_requirement":"action_evidence","supporting_visual_intent":None,
            "preferred_sources":[SourceKind.STOCK],"fallback_sources":[],"preferred_clip_id":None})
        selected=footage.model_copy(update={"preferred_clip_id":uuid4()})
        assert acquirer._validated_visuals([original,footage],[amended,selected],motion=True)==(amended,selected)
        changed=selected.model_copy(update={"viewer_takeaway":"改写主张"})
        with pytest.raises(ExternalAcquisitionError):
            acquirer._validated_visuals([original,footage],[amended,changed],motion=True)
    finally:db.close()


def test_after_master_budget_reserves_actual_talking_count(tmp_path,monkeypatch):
    db,project,_,_,_,_,_,_=measured_fixture(tmp_path,monkeypatch)
    try:
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id,max_calls=5,
            allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
        reserve_production_tail(db,project,"stock_search",after_master=True,remaining_talking_calls=1)
        with pytest.raises(BudgetLimitError):reserve_production_tail(db,project,"stock_search",after_master=True,remaining_talking_calls=2)
        with pytest.raises(BudgetLimitError):reserve_production_tail(db,project,"stock_search",revision_reserve_calls=4)
        resolver=object.__new__(VisualMaterialResolver);resolver.db=db
        resolver._reserve_tail(project,uuid4(),"source-analysis","test",after_master=True,remaining_talking_calls=3)
        with pytest.raises(BudgetLimitError):resolver._reserve_tail(project,uuid4(),"source-analysis","test",after_master=True,remaining_talking_calls=4)
    finally:db.close()
