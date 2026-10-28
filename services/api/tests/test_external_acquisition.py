from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.budget import BudgetLimitError
from app.db import BudgetPolicyRepository, JobRepository
from app.domain.models import BudgetPolicy, Job, JobStatus, JobType, MaterialRequest, SourceKind
from app.external_acquisition import BoundedExternalAcquirer, ExternalAcquisitionError, analyze_exact_job, reserve_production_tail
from app.jobs.runner import JobRunner
from app.jobs.store import JobStore
from app.media.planning import MaterialResolutionError
from test_external_media import Provider
from test_material_resolution import material_library


def fixture(tmp_path):
    db, project, asset, clip, original, vision, resolver = material_library(tmp_path)
    def scene(query, order=0, kind="stock"):
        return original.model_copy(update={"id": uuid4(), "scene_id": f"s-{order}", "order": order,
            "preferred_clip_id": None, "preferred_sources": [SourceKind(kind)],
            "material_request": MaterialRequest(source_kind=kind, query=query, reason="补齐画面")})
    calls = []
    candidate = Provider().video.model_copy(update={"width":1080,"height":1920,"duration_ms":10000})
    class Service:
        def search(self, project_id, query, key):
            calls.append(("search", query))
            return SimpleNamespace(call_id=uuid4(), candidates=[candidate.model_copy(update={"id":uuid4()})])
        def import_video(self, project_id, call_id, candidate_id, key, *, download_limits):
            calls.append(("import", str(candidate_id), download_limits))
            return SimpleNamespace(asset_id=asset.id, analysis_job_id=uuid4())
    acquirer = BoundedExternalAcquirer(db, tmp_path, ["stock-a"], lambda *a: Service(),
        lambda job_id: calls.append(("analyze", str(job_id))))
    return db, project, asset, scene, calls, acquirer, resolver


def test_queries_share_whole_video_limits_and_replay_without_network(tmp_path):
    db, project, asset, scene, calls, service, _ = fixture(tmp_path)
    try:
        scenes = [scene(" coffee  making "), scene("coffee making", 1), scene("city street", 2), scene("laptop", 3), scene("fourth", 4)]
        job_id = uuid4()
        result = service.acquire(project, scenes, job_id)
        assert set(result) == {"coffee making", "city street", "laptop"}
        assert all(v == [asset.id] for v in result.values())
        assert [c[1] for c in calls if c[0] == "search"] == ["coffee making", "city street", "laptop"]
        assert len([c for c in calls if c[0] == "import"]) == 3
        assert all(c[2]["max_bytes"] == 32*1024*1024 for c in calls if c[0] == "import")
        calls.clear()
        assert service.acquire(project, scenes, job_id) == result
        assert all(c[0] == "analyze" for c in calls)
        with pytest.raises(ExternalAcquisitionError):
            service.acquire(project, [scene("changed")], job_id)
    finally:
        db.close()


def test_failed_source_moves_to_different_source_without_retry_and_keeps_ai_separate(tmp_path):
    db, project, _, scene, calls, service, _ = fixture(tmp_path)
    original = service.service_factory
    def factory(source, project):
        if source == "broken":
            calls.append(("broken",))
            raise RuntimeError("not persisted")
        return original(source, project)
    service.sources = ("broken", "stock-a")
    service.service_factory = factory
    try:
        job_id = uuid4()
        scenes = [scene("coffee making"), scene("AI fantasy", 1, "ai_broll")]
        assert set(service.acquire(project, scenes, job_id)) == {"coffee making"}
        assert calls.count(("broken",)) == 1
        calls.clear()
        service.acquire(project, scenes, job_id)
        assert all(c[0] == "analyze" for c in calls)
        state = json.loads((tmp_path / "render-diagnostics" / str(job_id) / "external-materials.json").read_text(encoding="utf-8"))
        assert state["searches"][0]["reason"] == "external_search_failed"
        assert "not persisted" not in json.dumps(state)
    finally:
        db.close()


def test_unreadable_original_does_not_lock_query_to_first_source(tmp_path):
    db, project, asset, scene, calls, service, _ = fixture(tmp_path)
    original=service.service_factory
    class BrokenDownload:
        def search(self,*args):return original('a',project).search(*args)
        def import_video(self,*args,**kwargs):
            calls.append(('failed_original',))
            raise ValueError('source file unavailable')
    service.sources=('broken-original','backup')
    service.service_factory=lambda source,p: BrokenDownload() if source=='broken-original' else original(source,p)
    try:
        scenes=[scene('coffee hands')]
        job_id=uuid4()
        assert service.acquire(project,scenes,job_id)=={'coffee hands':[asset.id]}
        assert calls.count(('failed_original',))==1
        assert len([c for c in calls if c[0]=='search'])==2
        calls.clear()
        service.acquire(project,scenes,job_id)
        assert all(c[0]=='analyze' for c in calls)
    finally:db.close()


def test_expired_work_never_resets_acquisition_limits_after_restart(tmp_path):
    db, project, _, scene, calls, service, _ = fixture(tmp_path)
    try:
        job_id = uuid4()
        scenes = [scene("coffee making")]
        service.max_seconds = 0
        assert service.acquire(project, scenes, job_id) == {}
        service.max_seconds = 120
        assert service.acquire(project, scenes, job_id) == {}
        assert calls == []
    finally:
        db.close()


@pytest.mark.parametrize("acceptable_replacement", [True, False])
@pytest.mark.parametrize("candidate_count", [2, 3])
def test_ordinary_plan_replaces_rejected_material_once_and_replays_without_new_calls(tmp_path, monkeypatch, acceptable_replacement, candidate_count):
    import hashlib
    from app.db import AssetRepository, ClipRepository
    from app.domain.models import Clip
    from app.video_production import VideoProductionService
    db, project, bad, make_scene, calls, acquirer, resolver = fixture(tmp_path)
    try:
        source = tmp_path / "alternative.mp4"
        source.write_bytes(b"different source fixture")
        good = AssetRepository(db).create(bad.model_copy(update={"id": uuid4(), "source_file": str(source),
            "source_kind": SourceKind.STOCK, "content_hash": hashlib.sha256(source.read_bytes()).hexdigest(), "metadata": {}}))
        ClipRepository(db).create(Clip(asset_id=good.id,start_ms=0,end_ms=good.duration_ms,
            asset_duration_ms=good.duration_ms,visual_description="coffee making"))
        candidates = [Provider().video.model_copy(update={"id": uuid4(), "width":1080,"height":1920,"duration_ms":10000}) for _ in range(candidate_count)]
        replacements = [good.id]
        if candidate_count == 3:
            other_source = tmp_path / "unusable-alternative.mp4"
            other_source.write_bytes(b"second candidate does not express required action")
            unusable = AssetRepository(db).create(good.model_copy(update={"id":uuid4(),"source_file":str(other_source),
                "content_hash":hashlib.sha256(other_source.read_bytes()).hexdigest(),"metadata":{}}))
            ClipRepository(db).create(Clip(asset_id=unusable.id,start_ms=0,end_ms=unusable.duration_ms,
                asset_duration_ms=unusable.duration_ms,visual_description="coffee making"))
            replacements = [unusable.id,good.id]
        identities = {str(candidate.id):asset_id for candidate,asset_id in zip(candidates,[bad.id,*replacements])}
        class Service:
            def search(self, *args):
                calls.append(("search",))
                return SimpleNamespace(call_id=uuid4(), candidates=candidates)
            def import_video(self, project_id, call_id, candidate_id, key, **kwargs):
                calls.append(("import",str(candidate_id)))
                return SimpleNamespace(asset_id=identities[str(candidate_id)],analysis_job_id=uuid4())
        acquirer.service_factory = lambda *a: Service()
        resolver.provider.accept = lambda item: acceptable_replacement and item["source_asset_id"] == str(good.id)
        resolver.prepare = lambda *a: ()
        scenes = [make_scene("coffee making")]
        draft = SimpleNamespace(scenes=scenes, script="原文不变",topic=project.topic,evidence_refs=[])
        monkeypatch.setattr("app.video_production.generate_scene_plan",lambda *a,**kw:SimpleNamespace(draft=draft))
        monkeypatch.setattr("app.video_production.save_project_draft",lambda *a,**kw:SimpleNamespace(scenes=kw["scenes"]))
        selected = []
        class ReachedPreflight(Exception): pass
        def preflight(db, project, draft, **kwargs):
            selected.extend(draft.scenes)
            raise ReachedPreflight()
        monkeypatch.setattr("app.video_production.build_production_preflight",preflight)
        service=VideoProductionService(db,tmp_path,material_resolver=resolver,material_acquirer=acquirer)
        service.advance=lambda *a:pytest.fail("不应启动语音、口型或渲染")
        job=SimpleNamespace(project_id=project.id,id=uuid4(),payload=SimpleNamespace(topic=project.topic,candidate_of_run_id=None))
        from app.db import ProjectDraftRepository
        from app.domain.models import ProjectDraft
        draft=ProjectDraftRepository(db).save(ProjectDraft(project_id=project.id,version=1,script_revision=1,
            script='\n\n'.join(scene.voice_text for scene in scenes),topic=project.topic,scenes=scenes,
            evidence_refs=[f'content_plan_job:{job.id}'],updated_at=datetime.now(timezone.utc)))
        # 本夹具没有更好的画面方案；调整返回原需求，产品必须保留实际缺口。
        monkeypatch.setattr("app.video_production.generate_scene_plan",lambda *a,**kw:SimpleNamespace(scenes=tuple(scenes)))
        expected = ReachedPreflight if acceptable_replacement else MaterialResolutionError
        with pytest.raises(expected): service.plan(job,object(),object())
        assert len(resolver.provider.calls) == 2
        assert all(item["source_asset_id"]==str(bad.id) for item in resolver.provider.calls[0][0])
        assert {item["source_asset_id"] for item in resolver.provider.calls[1][0]}=={str(identity) for identity in replacements}
        assert len([c for c in calls if c[0]=="search"]) == 1
        assert len([c for c in calls if c[0]=="import"]) == candidate_count
        if acceptable_replacement:
            assert ClipRepository(db).get(selected[0].preferred_clip_id).asset_id == good.id
        calls.clear()
        with pytest.raises(expected): service.plan(job,object(),object())
        assert len(resolver.provider.calls) == 2
        assert all(c[0]=="analyze" for c in calls)
        state=json.loads((tmp_path/"render-diagnostics"/str(job.id)/"external-materials.json").read_text(encoding="utf-8"))
        assert state["supplement"]["rejected_assets"] == {"coffee making":[str(bad.id)]}
        assert len(state["searches"])==1 and len(state["imports"])==candidate_count
        assert AssetRepository(db).get(bad.id) is not None  # 不封禁或删除原素材。
    finally: db.close()


@pytest.mark.parametrize("slots",[0,1],ids=["no-slot-before-search","failed-download-uses-last-slot"])
def test_no_search_is_dispatched_when_original_download_slots_are_exhausted(tmp_path,slots):
    db,project,_,make_scene,calls,acquirer,_=fixture(tmp_path)
    try:
        acquirer.max_imports=slots
        acquirer.sources=("first","unused-backup")
        candidate=Provider().video.model_copy(update={"width":1080,"height":1920,"duration_ms":10000})
        class Service:
            def __init__(self,source):self.source=source
            def search(self,*args):
                calls.append(("search",self.source))
                return SimpleNamespace(call_id=uuid4(),candidates=[candidate])
            def import_video(self,*args,**kwargs):
                calls.append(("import",self.source))
                raise ValueError("recorded failed candidate")
        acquirer.service_factory=lambda source,project:Service(source)
        job_id=uuid4();scenes=[make_scene("coffee making")]
        assert acquirer.acquire(project,scenes,job_id)=={}
        assert [call for call in calls if call[0]=="search"]==([] if slots==0 else [("search","first")])
        assert len([call for call in calls if call[0]=="import"])==slots
        calls.clear()
        assert acquirer.acquire(project,scenes,job_id)=={}
        assert not calls
    finally:db.close()


def test_supplement_shares_two_remaining_candidate_attempts_between_queries_without_expanding_global_limits(tmp_path):
    import hashlib
    from app.db import AssetRepository
    db,project,first,make_scene,calls,acquirer,_=fixture(tmp_path)
    try:
        source=tmp_path/"other-query.mp4"
        source.write_bytes(b"distinct second query file")
        second=AssetRepository(db).create(first.model_copy(update={"id":uuid4(),"source_file":str(source),
            "content_hash":hashlib.sha256(source.read_bytes()).hexdigest(),"metadata":{}}))
        scenes=[make_scene("first query",0),make_scene("second query",1)]
        candidates={query:[Provider().video.model_copy(update={"id":uuid4(),"width":1080,"height":1920,"duration_ms":10000})
            for _ in range(3)] for query in ("first query","second query")}
        identities={candidate.id:asset.id for query,asset in (("first query",first),("second query",second))
            for candidate in candidates[query]}
        class Service:
            def search(self,project_id,query,key):
                calls.append(("search",query))
                return SimpleNamespace(call_id=uuid4(),candidates=candidates[query])
            def import_video(self,project_id,call_id,candidate_id,key,**kwargs):
                calls.append(("import",candidate_id))
                return SimpleNamespace(asset_id=identities[candidate_id],analysis_job_id=uuid4())
        acquirer.service_factory=lambda *a:Service()
        job_id=uuid4()
        acquirer.max_imports=0
        assert acquirer.acquire(project,scenes,job_id)=={}
        assert not calls  # 首轮未外取，可代表先使用了库中的已有素材。
        acquirer.max_imports=3
        result=acquirer.acquire(project,scenes,job_id,supplement_for=["first query","second query"])
        assert result=={"first query":[first.id],"second query":[second.id]}
        assert [call for call in calls if call[0]=="search"]==[("search","first query"),("search","second query")]
        assert [call for call in calls if call[0]=="import"]==[("import",candidates["first query"][0].id),
            ("import",candidates["second query"][0].id)]
        state=json.loads((tmp_path/"render-diagnostics"/str(job_id)/"external-materials.json").read_text(encoding="utf-8"))
        assert state["supplement"]["import_limit"]==2 and len(state["imports"])==2
        calls.clear()
        assert acquirer.acquire(project,scenes,job_id,supplement_for=["first query","second query"])==result
        assert all(call[0]=="analyze" for call in calls)
    finally:db.close()


def test_three_initial_needs_keep_two_bounded_import_attempts_for_use_replacement(tmp_path):
    import hashlib
    from app.db import AssetRepository
    db,project,template,make_scene,calls,acquirer,_=fixture(tmp_path)
    try:
        assets=[]
        for i in range(5):
            path=tmp_path/f'bounded-{i}.mp4';path.write_bytes(f'explicit source {i}'.encode())
            assets.append(AssetRepository(db).create(template.model_copy(update={'id':uuid4(),'source_file':str(path),
                'content_hash':hashlib.sha256(path.read_bytes()).hexdigest(),'metadata':{}})))
        queries=['first need','second need','third need']
        prototype=Provider().video.model_copy(update={'width':1080,'height':1920,'duration_ms':10000})
        candidates={query:[prototype.model_copy(update={'id':uuid4()}) for _ in range(3)] for query in queries}
        identities={candidates[query][0].id:assets[i].id for i,query in enumerate(queries)}
        identities.update({candidates[queries[0]][1].id:assets[3].id,candidates[queries[0]][2].id:assets[4].id})
        class Service:
            def search(self,pid,query,key):return SimpleNamespace(call_id=uuid4(),candidates=candidates[query])
            def import_video(self,pid,cid,identity,key,**kwargs):
                calls.append(('bounded-import',identity))
                return SimpleNamespace(asset_id=identities[identity],analysis_job_id=uuid4())
        acquirer.service_factory=lambda *a:Service()
        scenes=[make_scene(query,i) for i,query in enumerate(queries)];job=uuid4()
        acquirer.acquire(project,scenes,job)
        assert len([call for call in calls if call[0]=='bounded-import'])==3
        acquirer.acquire(project,scenes,job,supplement_for=[queries[0]])
        assert len([call for call in calls if call[0]=='bounded-import'])==5
        acquirer.acquire(project,scenes,job,supplement_for=[queries[0]])
        assert len([call for call in calls if call[0]=='bounded-import'])==5
    finally:db.close()


@pytest.mark.parametrize("missing,backup_available",[(None,True),("first query",True),("second query",True),("second query",False)],
    ids=["symmetric-primary","first-needs-backup","second-needs-backup","backup-empty-reallocate"])
def test_shared_first_file_allocates_supplement_to_uncovered_query_before_second_alternative(tmp_path,missing,backup_available):
    import hashlib
    from app.db import AssetRepository
    db,project,shared,make_scene,calls,acquirer,_=fixture(tmp_path)
    try:
        queries=("first query","second query")
        assets={}
        for name in (*queries,"spare"):
            source=tmp_path/(name.replace(" ","-")+".mp4")
            source.write_bytes(("distinct original "+name).encode())
            assets[name]=AssetRepository(db).create(shared.model_copy(update={"id":uuid4(),"source_file":str(source),
                "content_hash":hashlib.sha256(source.read_bytes()).hexdigest(),"metadata":{}}))
        common=Provider().video.model_copy(update={"id":uuid4(),"width":1080,"height":1920,"duration_ms":10000})
        first={query:common.model_copy(update={"id":uuid4()}) for query in queries}
        second={query:common.model_copy(update={"id":uuid4()}) for query in queries}
        identities={common.id:shared.id,**{first[query].id:assets[query].id for query in queries},
            **{second[query].id:assets["spare"].id for query in queries}}
        class Service:
            def __init__(self,source):self.source=source
            def search(self,project_id,query,key):
                calls.append(("search",self.source,query))
                if self.source=="primary":
                    candidates=[common] if query==missing else [common,first[query],second[query]]
                else:
                    candidates=[first[query]] if backup_available else []
                return SimpleNamespace(call_id=uuid4(),candidates=candidates)
            def import_video(self,project_id,call_id,candidate_id,key,**kwargs):
                calls.append(("import",candidate_id))
                return SimpleNamespace(asset_id=identities[candidate_id],analysis_job_id=uuid4())
        acquirer.sources=("primary","backup")
        acquirer.service_factory=lambda source,project:Service(source)
        scenes=[make_scene(query,index) for index,query in enumerate(queries)]
        job_id=uuid4()
        assert acquirer.acquire(project,scenes,job_id)=={query:[shared.id] for query in queries}
        assert len([call for call in calls if call[0]=="search"])==2
        assert [call for call in calls if call[0]=="import"]==[("import",common.id)]
        result=acquirer.acquire(project,scenes,job_id,supplement_for=queries)
        searches=[call for call in calls if call[0]=="search"]
        assert len(searches)==3
        # 用途否定后先换尚未查询的来源；无缓存替代的需求优先取得该机会。
        assert searches[-1]==("search","backup",missing or queries[0])
        if backup_available:
            assert result=={query:[assets[query].id,shared.id] for query in queries}
            imported={call[1] for call in calls if call[0]=="import"}
            assert imported=={common.id,first[queries[0]].id,first[queries[1]].id}
        else:
            # 已把备用源给零候选需求试过，仍无可用文件时，余槽才回给另一需求。
            covered=next(query for query in queries if query!=missing)
            assert set(result[covered])=={shared.id,assets[covered].id,assets["spare"].id}
            assert result[missing]==[shared.id]
        statefile=tmp_path/"render-diagnostics"/str(job_id)/"external-materials.json"
        state=json.loads(statefile.read_text(encoding="utf-8"))
        assert len(state["imports"])==3 and len(state["searches"])<=3
        assert state["supplement"]["import_limit"]==3
        calls.clear()
        assert acquirer.acquire(project,scenes,job_id,supplement_for=queries)==result
        assert all(call[0]=="analyze" for call in calls)
    finally:db.close()


def test_semantic_supplement_keeps_original_time_and_download_limits(tmp_path):
    db,project,_,make_scene,calls,acquirer,_=fixture(tmp_path)
    try:
        job_id=uuid4();scenes=[make_scene("coffee making")]
        acquirer.acquire(project,scenes,job_id)
        calls.clear()
        path=tmp_path/"render-diagnostics"/str(job_id)/"external-materials.json"
        state=json.loads(path.read_text(encoding="utf-8"))
        state["started_at"]=(datetime.now(timezone.utc)-timedelta(seconds=121)).isoformat()
        path.write_text(json.dumps(state),encoding="utf-8")
        assert acquirer.acquire(project,scenes,job_id,supplement_for=["coffee making"]) is None
        assert all(c[0]=="analyze" for c in calls)
        assert json.loads(path.read_text(encoding="utf-8"))["started_at"]==state["started_at"]
    finally: db.close()


def test_shared_candidate_rejected_only_for_second_query_gets_new_material_and_recovers_without_new_calls(tmp_path, monkeypatch):
    import hashlib
    from app.db import AssetRepository, ClipRepository, ProviderCallRepository, ProjectDraftRepository
    from app.domain.models import Clip
    from app.media.visual_facts import scene_visual_key
    from app.project_drafts import save_project_draft
    from app.video_production import VideoProductionService
    db, project, shared, make_scene, calls, acquirer, resolver = fixture(tmp_path)
    try:
        shared = AssetRepository(db).update(shared.model_copy(update={"source_kind":SourceKind.STOCK}))
        source = tmp_path / "second-purpose.mp4"
        source.write_bytes(b"different file for second expression")
        replacement = AssetRepository(db).create(shared.model_copy(update={"id":uuid4(),"source_file":str(source),
            "content_hash":hashlib.sha256(source.read_bytes()).hexdigest(),"metadata":{}}))
        ClipRepository(db).create(Clip(asset_id=replacement.id,start_ms=0,end_ms=replacement.duration_ms,
            asset_duration_ms=replacement.duration_ms,visual_description="second query fixture"))
        first = make_scene("pour coffee",0).model_copy(update={"voice_text":"倒入咖啡。"})
        second = make_scene("stir drink",1).model_copy(update={"voice_text":"搅拌杯中饮品。"})
        scenes = [first,second]
        common = Provider().video.model_copy(update={"id":uuid4(),"width":1080,"height":1920,"duration_ms":10000})
        alternative = common.model_copy(update={"id":uuid4()})
        identities = {common.id:shared.id,alternative.id:replacement.id}
        class Service:
            def search(self, project_id, query, key):
                calls.append(("search",query))
                return SimpleNamespace(call_id=uuid4(),candidates=[common,alternative])
            def import_video(self, project_id, call_id, candidate_id, key, **kwargs):
                calls.append(("import",str(candidate_id)))
                return SimpleNamespace(asset_id=identities[candidate_id],analysis_job_id=uuid4())
        acquirer.service_factory = lambda *a:Service()
        resolver.provider.provider_name="contract-fixture"
        resolver.provider.model="fixture-video"
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id,max_calls=10,
            allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
        resolver.provider.accept=lambda item:True
        original = resolver.provider.analyze_intervals
        def analyze(videos, requests):
            result = original(videos, requests)
            for observed, video in zip(result.clips,videos):
                for match in observed.matches:
                    accepted = ((match.scene_key == scene_visual_key(first) and video["source_asset_id"] == str(shared.id))
                        or (match.scene_key == scene_visual_key(second) and video["source_asset_id"] == str(replacement.id)))
                    match.supported=accepted
                    match.score=.95 if accepted else .1
                    match.quality_adequate=True
            return result
        resolver.provider.analyze_intervals=analyze
        resolver.prepare=lambda *a:()
        plans=[]
        def generate(db,project,*args,**kwargs):
            plans.append(kwargs["origin_job_id"])
            draft=save_project_draft(db,project,script="".join(scene.voice_text for scene in scenes),topic=project.topic,
                scenes=scenes,routes=(),confirmed=(),video_spec=None,
                evidence_refs=(f"content_plan_job:{kwargs['origin_job_id']}",),accept_scene_plan_with_new_script=True)
            return SimpleNamespace(draft=draft)
        monkeypatch.setattr("app.video_production.generate_scene_plan",generate)
        selected=[]
        class ReachedPreflight(Exception):pass
        def preflight(db, project, draft, **kwargs):
            selected[:] = draft.scenes
            raise ReachedPreflight()
        monkeypatch.setattr("app.video_production.build_production_preflight",preflight)
        production=VideoProductionService(db,tmp_path,material_resolver=resolver,material_acquirer=acquirer)
        job=SimpleNamespace(project_id=project.id,id=uuid4(),payload=SimpleNamespace(topic=project.topic,candidate_of_run_id=None))
        with pytest.raises(ReachedPreflight):production.plan(job,object(),object())
        assert ClipRepository(db).get(selected[0].preferred_clip_id).asset_id == shared.id
        assert ClipRepository(db).get(selected[1].preferred_clip_id).asset_id == replacement.id
        assert [call[0] for call in calls].count("search")==2
        assert [call[0] for call in calls].count("import")==2
        assert len(resolver.provider.calls)==2
        assert all(video["source_asset_id"]==str(replacement.id) for video in resolver.provider.calls[1][0])
        state=json.loads((tmp_path/"render-diagnostics"/str(job.id)/"external-materials.json").read_text(encoding="utf-8"))
        assert state["supplement"]["queries"]==["stir drink"]
        assert state["supplement"]["rejected_assets"]=={"stir drink":[str(shared.id)]}
        assert len(ProviderCallRepository(db).list_for_project(project.id))==2
        persisted=ProjectDraftRepository(db).get(project.id)
        assert all(scene.material_request is None for scene in persisted.scenes)
        assert persisted.scenes[1].preferred_clip_id==selected[1].preferred_clip_id
        calls.clear()
        # 新建服务对象模拟产品恢复；沿同一任务和持久记录，不增加下载或核验账目。
        resumed_acquirer=BoundedExternalAcquirer(db,tmp_path,acquirer.sources,acquirer.service_factory,acquirer.analyze)
        resumed=VideoProductionService(db,tmp_path,material_resolver=resolver,material_acquirer=resumed_acquirer)
        with pytest.raises(ReachedPreflight):resumed.plan(job,object(),object())
        assert not calls and len(plans)==1
        assert len(resolver.provider.calls)==2
        assert len(ProviderCallRepository(db).list_for_project(project.id))==2
        assert ClipRepository(db).get(selected[0].preferred_clip_id).asset_id==shared.id
        # 不能借另一任务的补检标识绕开原核验、哈希或占用检查。
        state["supplement"]["analysis_scope_id"]=str(uuid4())
        (tmp_path/"render-diagnostics"/str(job.id)/"external-materials.json").write_text(json.dumps(state),encoding="utf-8")
        with pytest.raises(ExternalAcquisitionError,match="补检恢复记录"):
            resumed.plan(job,object(),object())
        assert not calls and len(resolver.provider.calls)==2
    finally:db.close()


def recorded_acquisition(tmp_path, *, search_only=False):
    from app.db import Database
    from app.external_media import ExternalMediaService
    from app.media import MediaImporter
    from test_external_media import _project
    from test_pexels_media import Probe
    path=tmp_path/"recorded.sqlite"
    project=_project(path)
    db=Database(path,startup_mode="existing")
    provider=Provider()
    provider.video=provider.video.model_copy(update={"width":1080,"height":1920,"duration_ms":8000})
    download=provider.download
    provider.download=lambda candidate,destination,**kwargs:download(candidate,destination)
    media=ExternalMediaService(db,tmp_path,provider,MediaImporter(db,tmp_path,Probe()))
    analyzed=[]
    acquirer=BoundedExternalAcquirer(db,tmp_path,[provider.provider_name],lambda *a:media,analyzed.append)
    from app.domain.models import ScenePlan,VisualIntent
    scene=ScenePlan(project_id=project.id,scene_id="one",order=0,purpose="恢复夹具",voice_text="测试素材。",
        duration_target_ms=3000,visual_intent=VisualIntent(subject="fixture"),preferred_sources=["stock"],
        material_request=MaterialRequest(source_kind="stock",query="fixture material",reason="恢复夹具"))
    job_id=uuid4()
    if search_only:
        class SearchCheckpointCrash(BaseException):pass
        search=media.search
        def crash_after_recording(*args,**kwargs):
            search(*args,**kwargs)
            raise SearchCheckpointCrash()
        media.search=crash_after_recording
        with pytest.raises(SearchCheckpointCrash):
            acquirer.acquire(project,[scene],job_id)
        result={}
    else:
        result=acquirer.acquire(project,[scene],job_id)
    statefile=tmp_path/"render-diagnostics"/str(job_id)/"external-materials.json"
    state=json.loads(statefile.read_text(encoding="utf-8"))
    state["complete"]=False
    state["started_at"]=(datetime.now(timezone.utc)-timedelta(seconds=121)).isoformat()
    state["acquisition_elapsed_seconds"]=121  # 真实已用工作时间耗尽，不再用稿件等待时间冒充过期。
    entry=state["searches" if search_only else "imports"][0]
    entry["status"]="started"
    for key in (("call_id","candidates") if search_only else ("asset_id","analysis_job_id")):
        entry.pop(key,None)
    statefile.write_text(json.dumps(state),encoding="utf-8")
    acquirer.service_factory=lambda *a:pytest.fail("过期恢复不得构造网络服务或发起新请求")
    analyzed.clear()
    return db,project,scene,job_id,result,provider,acquirer,analyzed,statefile


@pytest.mark.parametrize("search_only",[True,False],ids=["search-checkpoint","download-checkpoint"])
def test_expired_acquisition_restores_completed_ledger_without_network_or_new_calls(tmp_path,search_only):
    from app.db import ProviderCallRepository
    db,project,scene,job_id,first,provider,acquirer,analyzed,statefile=recorded_acquisition(tmp_path,search_only=search_only)
    try:
        calls=ProviderCallRepository(db).list()
        assert all(call.status=="completed" for call in calls)
        result=acquirer.acquire(project,[scene],job_id)
        assert result==first
        recovered=json.loads(statefile.read_text(encoding="utf-8"))
        if search_only:
            assert recovered["searches"][0]["status"]=="completed"
            assert recovered["searches"][0]["candidates"]==[str(provider.video.id)]
            assert not recovered["imports"] and not analyzed
        else:
            assert recovered["imports"][0]["status"]=="analyzed" and analyzed
        assert ProviderCallRepository(db).list()==calls
        assert provider.search_count==1 and provider.download_count==(0 if search_only else 1)
        assert acquirer.acquire(project,[scene],job_id)==result
        assert ProviderCallRepository(db).list()==calls
    finally:db.close()


@pytest.mark.parametrize("search_only",[True,False],ids=["search","download"])
@pytest.mark.parametrize("status",["running","failed"])
def test_expired_incomplete_provider_record_is_not_retried_or_treated_as_completed(tmp_path,search_only,status):
    from app.db import ProviderCallRepository
    db,project,scene,job_id,_,provider,acquirer,analyzed,statefile=recorded_acquisition(tmp_path,search_only=search_only)
    try:
        repo=ProviderCallRepository(db)
        call=next(value for value in repo.list() if value.operation==("stock_search" if search_only else "stock_import"))
        repo.update(call.model_copy(update={"status":status,"result_payload":None,
            "completed_at":None if status=="running" else call.completed_at,
            "error_code":"fixture_recorded_failure" if status=="failed" else None}))
        before=repo.list()
        assert acquirer.acquire(project,[scene],job_id)=={}
        assert not analyzed and repo.list()==before
        state=json.loads(statefile.read_text(encoding="utf-8"))
        assert state["searches" if search_only else "imports"][0]["status"]=="started"
        assert provider.search_count==1 and provider.download_count==(0 if search_only else 1)
    finally:db.close()


def test_expired_completed_import_with_changed_file_stops_without_new_network(tmp_path):
    from app.db import AssetRepository,ProviderCallRepository
    db,project,scene,job_id,first,provider,acquirer,analyzed,statefile=recorded_acquisition(tmp_path)
    try:
        asset=AssetRepository(db).get(first[scene.material_request.query][0])
        from pathlib import Path
        Path(asset.source_file).write_bytes(b"changed source")
        before=ProviderCallRepository(db).list()
        assert acquirer.acquire(project,[scene],job_id)=={}
        assert not analyzed and ProviderCallRepository(db).list()==before
        assert provider.search_count==provider.download_count==1
    finally:db.close()


def test_import_failure_not_retried_and_geometric_filter_does_not_admit_wrong_format(tmp_path):
    db, project, _, scene, calls, service, _ = fixture(tmp_path)
    provider = Provider()
    provider.video = provider.video.model_copy(update={"width":1080,"height":1920,"duration_ms":10000})
    class Service:
        def search(self, *args):
            return SimpleNamespace(call_id=uuid4(), candidates=[provider.video.model_copy(update={"width":1920,"height":1080}), provider.video])
        def import_video(self, *args, **kwargs):
            calls.append(("failed_import",))
            raise ValueError("fixture failure")
    service.service_factory = lambda *a: Service()
    try:
        job_id = uuid4()
        scenes = [scene("coffee making")]
        assert service.acquire(project, scenes, job_id) == {}
        assert service.acquire(project, scenes, job_id) == {}
        assert len(calls) == 1
        state = json.loads((tmp_path / "render-diagnostics" / str(job_id) / "external-materials.json").read_text(encoding="utf-8"))
        assert len(state["imports"]) == 1 and state["imports"][0]["status"] == "failed"
    finally:
        db.close()


def test_tail_budget_is_checked_before_new_external_reservation(tmp_path):
    db, project, _, _, _, _, _ = fixture(tmp_path)
    try:
        repo = BudgetPolicyRepository(db)
        repo.save(BudgetPolicy(project_id=project.id, max_calls=4, allow_unknown_cost=True, updated_at=datetime.now(timezone.utc)))
        with pytest.raises(BudgetLimitError, match="后续"):
            reserve_production_tail(db, project)
        repo.save(BudgetPolicy(project_id=project.id, max_calls=5, allow_unknown_cost=True, updated_at=datetime.now(timezone.utc)))
        reserve_production_tail(db, project)
    finally:
        db.close()


@pytest.mark.parametrize('operation,required',[('stock_search',8),('web_search',9),('stock_import',7)])
@pytest.mark.parametrize('revision_calls',[2,4])
def test_automatic_acquisition_preserves_one_visual_revision_and_qa(tmp_path,operation,required,revision_calls):
    db,project,_,_,_,_,_=fixture(tmp_path)
    try:
        repo=BudgetPolicyRepository(db)
        required+=revision_calls-2
        policy=BudgetPolicy(project_id=project.id,max_calls=required-1,allow_unknown_cost=True,updated_at=datetime.now(timezone.utc))
        repo.save(policy)
        with pytest.raises(BudgetLimitError):reserve_production_tail(db,project,operation,revision_reserve_calls=revision_calls)
        repo.save(policy.model_copy(update={'max_calls':required}))
        reserve_production_tail(db,project,operation,revision_reserve_calls=revision_calls)
    finally:db.close()


def test_search_reserves_file_and_production_before_spending_and_keeps_completed_work_available(tmp_path):
    from app.db import ProjectRepository,ProviderCallRepository
    from app.external_media import ExternalMediaService
    from app.media.importer import MediaImporter
    from test_external_media import Probe
    db,project,_,_,_,_,_=fixture(tmp_path)
    try:
        project=ProjectRepository(db).update(project.model_copy(update={'presentation_style':'creator_led'}))
        budgets=BudgetPolicyRepository(db)
        policy=BudgetPolicy(project_id=project.id,max_calls=6,allow_unknown_cost=True,updated_at=datetime.now(timezone.utc))
        budgets.save(policy)
        provider=Provider()
        service=ExternalMediaService(db,tmp_path,provider,MediaImporter(db,tmp_path,Probe()),
            reservation_guard=lambda operation:reserve_production_tail(db,project,operation))
        with pytest.raises(BudgetLimitError):service.search(project.id,'new topic','search')
        assert provider.search_count==0 and ProviderCallRepository(db).list()==[]
        budgets.save(policy.model_copy(update={'max_calls':7}))
        found=service.search(project.id,'new topic','search')
        saved=service.import_video(project.id,found.call_id,found.candidates[0].id,'import')
        assert provider.search_count==provider.download_count==1
        assert len(ProviderCallRepository(db).list())==2
        budgets.save(policy.model_copy(update={'max_calls':2}))
        assert service.search(project.id,'new topic','search').call_id==found.call_id
        assert service.import_video(project.id,found.call_id,found.candidates[0].id,'import').asset_id==saved.asset_id
        assert provider.search_count==provider.download_count==1 and len(ProviderCallRepository(db).list())==2
    finally:db.close()


def test_local_analysis_targets_exact_child_and_does_not_claim_backlog_or_retry(tmp_path):
    db, _, _, _, _, _, _ = fixture(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        repo = JobRepository(db)
        old = repo.create(Job(type=JobType.ANALYZE_ASSET, idempotency_key="old", created_at=now, updated_at=now))
        child = repo.create(Job(type=JobType.ANALYZE_ASSET, idempotency_key="child", created_at=now, updated_at=now))
        called = []
        runner = JobRunner(JobStore(db), {JobType.ANALYZE_ASSET: lambda j: called.append(j.id)},
            worker_id="exact", lease_duration=timedelta(seconds=60), max_attempts=1)
        analyze_exact_job(db, runner, child.id)
        analyze_exact_job(db, runner, child.id)
        assert called == [child.id] and repo.get(old.id).status is JobStatus.PENDING
    finally:
        db.close()


def test_failed_child_accepts_only_completed_manual_replacement_without_auto_retry(tmp_path):
    from app.jobs.targets import AssetJobTargetStore
    db, _, asset, _, _, _, _ = fixture(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        targets = AssetJobTargetStore(db)
        failed = targets.enqueue(Job(type=JobType.ANALYZE_ASSET, idempotency_key="failed", created_at=now, updated_at=now), asset.id)
        attempted = []
        def fail(job):
            attempted.append(job.id)
            raise ValueError("local analysis fixture failure")
        runner = JobRunner(JobStore(db), {JobType.ANALYZE_ASSET: fail},
            worker_id="exact-failure", lease_duration=timedelta(seconds=60), max_attempts=1)
        for _ in range(2):
            with pytest.raises(ExternalAcquisitionError):
                analyze_exact_job(db, runner, failed.id)
        assert attempted == [failed.id]
        replacement = targets.enqueue(Job(type=JobType.ANALYZE_ASSET, idempotency_key="manual", created_at=now, updated_at=now), asset.id)
        good = JobRunner(JobStore(db), {JobType.ANALYZE_ASSET: lambda j: None},
            worker_id="manual", lease_duration=timedelta(seconds=60), max_attempts=1)
        analyze_exact_job(db, good, replacement.id)
        analyze_exact_job(db, runner, failed.id)
        assert attempted == [failed.id]
    finally:
        db.close()


def test_external_pool_still_requires_real_interval_matches(tmp_path):
    db, project, asset, make_scene, _, _, resolver = fixture(tmp_path)
    try:
        scene = make_scene("unmatched query")
        resolver.provider.accept = lambda v: False
        with pytest.raises(MaterialResolutionError):
            resolver.resolve(project, [scene], uuid4(), acquired_assets={"unmatched query": [asset.id]})
        assert len(resolver.provider.calls) == 1
    finally:
        db.close()


@pytest.mark.parametrize("video_supported", [True, False])
def test_ordinary_plan_checks_purchased_work_only_after_material_qa(tmp_path, monkeypatch, video_supported):
    from app.video_production import VideoProductionService
    db, project, _, make_scene, calls, acquirer, resolver = fixture(tmp_path)
    try:
        scene = make_scene("coffee making")
        draft = SimpleNamespace(scenes=[scene])
        resolver.prepare = lambda *a: ()
        resolver.provider.accept = lambda v: False
        resolver.provider.supports_video_input = video_supported
        monkeypatch.setattr("app.video_production.generate_scene_plan", lambda *a, **kw: SimpleNamespace(draft=draft))
        service = VideoProductionService(db, tmp_path, material_resolver=resolver, material_acquirer=acquirer)
        service.advance = lambda *a: pytest.fail("素材未过用途核验，不应进入语音、口型或渲染")
        job = SimpleNamespace(project_id=project.id, id=uuid4(), payload=SimpleNamespace(topic=project.topic, candidate_of_run_id=None))
        from app.db import ProjectDraftRepository
        from app.domain.models import ProjectDraft
        ProjectDraftRepository(db).save(ProjectDraft(project_id=project.id,version=1,script_revision=1,
            script=scene.voice_text,topic=project.topic,scenes=[scene],
            evidence_refs=[f'content_plan_job:{job.id}'],updated_at=datetime.now(timezone.utc)))
        monkeypatch.setattr("app.video_production.generate_scene_plan",lambda *a,**kw:SimpleNamespace(scenes=(scene,)))
        with pytest.raises(MaterialResolutionError):
            service.plan(job, object(), object())
        if not video_supported:
            assert calls == []
        else:
            assert any(c[0] == "import" for c in calls) and len(resolver.provider.calls) == 1
    finally:
        db.close()
