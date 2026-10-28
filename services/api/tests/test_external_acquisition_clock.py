"""同一稿件只累计获取工作的时间；QA 等候不重置资源，也不占获取窗口。"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID,uuid4

from app.db import AssetRepository,JobRepository
from app.domain.models import Job,JobStatus,JobType,SourceKind
from app.external_acquisition import BoundedExternalAcquirer
from test_external_acquisition import fixture
from test_external_media import Provider


def timed_supply(tmp_path,monkeypatch,*,limit=120):
    db,project,template,make_scene,calls,service,_=fixture(tmp_path)
    clock=[0.0]
    monkeypatch.setattr("app.external_acquisition.time",SimpleNamespace(monotonic=lambda:clock[0]))
    sources=[template]
    for index in range(2):
        path=tmp_path/f"replacement-{index}.mp4";path.write_bytes(f"distinct source {index}".encode())
        sources.append(AssetRepository(db).create(template.model_copy(update={"id":uuid4(),
            "source_file":str(path),"content_hash":hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_kind":SourceKind.STOCK,"metadata":{}})))
    candidate=Provider().video.model_copy(update={"width":1080,"height":1920,"duration_ms":10000})
    candidates=[candidate.model_copy(update={"id":uuid4()}) for _ in sources]
    assets={candidate.id:asset.id for candidate,asset in zip(candidates,sources)}
    class Service:
        def search(self,pid,query,key):
            calls.append(("search",key));clock[0]+=5
            return SimpleNamespace(call_id=uuid4(),candidates=candidates)
        def import_video(self,pid,cid,identity,key,*,download_limits):
            calls.append(("import",identity,download_limits["deadline"]-clock[0]));clock[0]+=10
            return SimpleNamespace(asset_id=assets[identity],analysis_job_id=uuid4())
    service.service_factory=lambda *a:Service()
    def analyze(identity):calls.append(("analyze",identity));clock[0]+=2
    service.analyze=analyze;service.max_seconds=limit
    job_id=uuid4();scenes=[make_scene("coffee making")]
    statefile=tmp_path/"render-diagnostics"/str(job_id)/"external-materials.json"
    return db,project,scenes,calls,service,clock,job_id,statefile,sources


def test_same_job_qa_wait_excluded_but_work_and_acquisition_counts_accumulate(tmp_path,monkeypatch):
    db,project,scenes,calls,service,clock,job,path,sources=timed_supply(tmp_path,monkeypatch)
    try:
        assert service.acquire(project,scenes,job)=={"coffee making":[sources[0].id]}
        first=json.loads(path.read_text(encoding="utf8"));initial=first["acquisition_elapsed_seconds"]
        assert initial==19 and not first["acquisition_in_progress"]
        clock[0]+=600  # 同一次制作在获取与补检之间等待动态用途 QA。
        result=service.acquire(project,scenes,job,supplement_for=["coffee making"])
        assert set(result["coffee making"])=={asset.id for asset in sources}
        current=json.loads(path.read_text(encoding="utf8"))
        assert current["started_at"]==first["started_at"]
        assert current["acquisition_elapsed_seconds"]==49 and current["acquisition_limit_seconds"]==120
        assert len(current["searches"])==1 and len(current["imports"])==3
        assert len([call for call in calls if call[0]=="search"])==1
        assert len([call for call in calls if call[0]=="import"])==3
        clock[0]+=24*3600;before=list(calls)
        assert service.acquire(project,scenes,job,supplement_for=["coffee making"])==result
        assert all(call[0]=="analyze" for call in calls[len(before):])
    finally:db.close()


def test_real_work_exhaustion_and_deadline_survive_wait_and_service_restart(tmp_path,monkeypatch):
    db,project,scenes,calls,service,clock,job,path,_=timed_supply(tmp_path,monkeypatch,limit=20)
    try:
        service.acquire(project,scenes,job)
        clock[0]+=1000
        result=service.acquire(project,scenes,job,supplement_for=["coffee making"])
        imports=[call for call in calls if call[0]=="import"]
        assert len(imports)==2 and imports[1][2]==1  # 补检仅余一秒，未获得新的20秒窗口。
        assert json.loads(path.read_text(encoding="utf8"))["acquisition_elapsed_seconds"]>=20
        clock[0]+=10000;before=list(calls)
        resumed=BoundedExternalAcquirer(db,tmp_path,service.sources,service.service_factory,service.analyze,
            max_seconds=120)  # 新服务和更宽配置不重置该任务原先20秒配额。
        assert resumed.acquire(project,scenes,job,supplement_for=["coffee making"])==result
        assert all(call[0]=="analyze" for call in calls[len(before):])
        assert json.loads(path.read_text(encoding="utf8"))["acquisition_limit_seconds"]==20
    finally:db.close()


def test_unsettled_hard_interruption_preserves_files_and_conservatively_reserves_unknown_time(tmp_path,monkeypatch):
    db,project,scenes,calls,service,clock,job,path,sources=timed_supply(tmp_path,monkeypatch)
    try:
        initial=service.acquire(project,scenes,job)
        state=json.loads(path.read_text(encoding="utf8"))
        state.update(acquisition_in_progress=True,acquisition_elapsed_seconds=7,complete=False)
        path.write_text(json.dumps(state),encoding="utf8")
        clock[0]+=86400;before=len(calls)
        assert service.acquire(project,scenes,job)==initial
        assert service.acquire(project,scenes,job,supplement_for=["coffee making"]) is None
        assert all(call[0]=="analyze" for call in calls[before:])
        kept=json.loads(path.read_text(encoding="utf8"))
        assert kept["acquisition_elapsed_seconds"]>=120 and len(kept["imports"])==1
        assert kept["imports"][0]["asset_id"]==str(sources[0].id)
    finally:db.close()


def test_legacy_completed_initial_supply_migrates_recorded_durations_not_days_waiting(tmp_path,monkeypatch):
    db,project,scenes,calls,service,clock,job,path,_=timed_supply(tmp_path,monkeypatch)
    try:
        service.acquire(project,scenes,job)
        state=json.loads(path.read_text(encoding="utf8"))
        for key in ("acquisition_elapsed_seconds","acquisition_in_progress","acquisition_limit_seconds"):
            state.pop(key,None)
        start=datetime.now(timezone.utc)-timedelta(days=10)
        state["started_at"]=(start-timedelta(days=2)).isoformat()
        state["searches"][0].update(started_at=start.isoformat(),completed_at=(start+timedelta(seconds=5)).isoformat())
        state["imports"][0].update(started_at=(start+timedelta(seconds=5)).isoformat(),
            completed_at=(start+timedelta(seconds=15)).isoformat())
        JobRepository(db).create(Job(id=UUID(state["imports"][0]["analysis_job_id"]),type=JobType.ANALYZE_ASSET,
            status=JobStatus.COMPLETED,idempotency_key="legacy-analysis-clock",created_at=start+timedelta(seconds=15),
            started_at=start+timedelta(seconds=15),updated_at=start+timedelta(seconds=17)))
        path.write_text(json.dumps(state),encoding="utf8")
        clock[0]+=20*86400
        result=service.acquire(project,scenes,job,supplement_for=["coffee making"])
        kept=json.loads(path.read_text(encoding="utf8"))
        assert result is not None and len(kept["imports"])==3 and len(kept["searches"])==1
        assert kept["started_at"]==state["started_at"]
        assert kept["acquisition_time_migration"]=="recorded_operation_durations"
        assert 17<kept["acquisition_elapsed_seconds"]<120
    finally:db.close()


def test_legacy_missing_duration_is_not_silently_treated_as_new_free_window(tmp_path,monkeypatch):
    db,project,scenes,calls,service,clock,job,path,_=timed_supply(tmp_path,monkeypatch)
    try:
        result=service.acquire(project,scenes,job)
        state=json.loads(path.read_text(encoding="utf8"))
        for key in ("acquisition_elapsed_seconds","acquisition_in_progress","acquisition_limit_seconds"):
            state.pop(key,None)
        path.write_text(json.dumps(state),encoding="utf8")
        before=len(calls);clock[0]+=86400
        assert service.acquire(project,scenes,job)==result
        assert service.acquire(project,scenes,job,supplement_for=["coffee making"]) is None
        assert all(call[0]=="analyze" for call in calls[before:])
        assert json.loads(path.read_text(encoding="utf8"))["acquisition_time_migration"]=="unknown_duration_reserved"
    finally:db.close()
