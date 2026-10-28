"""Measured-plan supplement handoff, using local providers and a real ledger."""
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.costs import UnknownProviderCostEstimator
from app.db import AssetRepository, AudioAssetRepository, BudgetPolicyRepository, ClipRepository, ProjectDraftRepository, ProviderCallRepository
from app.domain.models import BudgetPolicy, Clip, CostCategory, ProjectDraft, SourceKind
from app.external_acquisition import reserve_production_tail
from app.media.planning import VisualMaterialResolver
from app.media.visual_facts import scene_visual_key
from app.provider_execution import ProviderExecutionService, RuntimeProviderIdentity
from app.video_production import VideoProductionService
from test_external_acquisition_gaps import measured_fixture, state_file
from test_external_media import Provider
from test_material_resolution import FixtureVideoExtractor, FixtureVideoProbe, FixtureVideoVision


def test_measured_supplement_handoff_uses_one_ledger_and_preserves_master(tmp_path, monkeypatch):
    db, project, bad, original, calls, acquirer, master, measured = measured_fixture(tmp_path, monkeypatch)
    try:
        source = tmp_path / 'replacement.mp4'
        source.write_bytes(b'local replacement fixture')
        good = AssetRepository(db).create(bad.model_copy(update={'id': uuid4(), 'source_file': str(source),
            'source_kind': SourceKind.STOCK, 'content_hash': hashlib.sha256(source.read_bytes()).hexdigest(), 'metadata': {}}))
        BudgetPolicyRepository(db).save(BudgetPolicy(project_id=project.id, max_calls=6,
            allow_unknown_cost=True, updated_at=datetime.now(timezone.utc)))
        vision = FixtureVideoVision(accept=lambda item: item['source_asset_id'] == str(good.id))
        vision.provider_name, vision.model = 'fixture', 'vision'
        resolver = VisualMaterialResolver(db, tmp_path, vision, FixtureVideoExtractor(tmp_path/'proxies'),
            FixtureVideoProbe(), UnknownProviderCostEstimator())
        bad_windows = {str(clip.id): (bad, clip) for clip in [Clip(id=uuid4(), asset_id=bad.id,
            start_ms=i*3000, end_ms=i*3000+3000, asset_duration_ms=bad.duration_ms) for i in range(6)]}
        new_clip = Clip(id=uuid4(), asset_id=good.id, start_ms=0, end_ms=3000, asset_duration_ms=good.duration_ms)
        def windows(project, scenes, acquired_assets=None):
            requests = [{'scene_key':scene_visual_key(s), 'voice_text':s.voice_text, 'purpose':s.purpose,
                'visual_intent':s.visual_intent.model_dump(mode='json'), 'visual_requirement':s.visual_requirement,
                'visual_requirement_reason':s.visual_requirement_reason} for s in scenes if s.scene_id == measured[1].scene_id]
            return ({str(new_clip.id):(good,new_clip)} if acquired_assets else bad_windows), requests
        resolver._windows = windows
        candidate = Provider().video.model_copy(update={'id':uuid4(), 'width':1080, 'height':1920, 'duration_ms':10000})
        executor = ProviderExecutionService(db, UnknownProviderCostEstimator())
        runtime = RuntimeProviderIdentity('fixture', 'stock')
        actions = []
        class LocalStock:
            def execute(self, project_id, key, operation, doc, result):
                def action():
                    actions.append(operation)
                    return result
                return executor.execute(project_id=project_id, idempotency_key=key, operation=operation,
                    provider=runtime, input_source='fixture:stock', category=CostCategory.STOCK,
                    input_document=doc, action=action, encode_result=lambda v:v, decode_result=lambda v:v,
                    error_code=lambda exc:'fixture_failed', reservation_guard=lambda:reserve_production_tail(
                        db, project, operation, after_master=True, remaining_talking_calls=1))
            def search(self, project_id, query, key):
                self.execute(project_id,key,'stock_search',{'query':query},{'candidate':str(candidate.id)})
                call=ProviderCallRepository(db).get_by_idempotency_key(key)
                return SimpleNamespace(call_id=call.id,candidates=[candidate])
            def import_video(self, project_id, call_id, candidate_id, key, *, download_limits):
                value=self.execute(project_id,key,'stock_import',{'candidate':str(candidate_id)},
                    {'asset_id':str(good.id),'analysis_job_id':str(uuid4())})
                return SimpleNamespace(asset_id=UUID(value['asset_id']),analysis_job_id=UUID(value['analysis_job_id']))
        acquirer.service_factory=lambda *args:LocalStock()
        job = SimpleNamespace(id=uuid4(),payload=SimpleNamespace(topic=project.topic))
        draft=ProjectDraftRepository(db).save(ProjectDraft(project_id=project.id, version=1, script_revision=1,
            topic=project.topic, script=original.voice_text, scenes=[original], updated_at=datetime.now(timezone.utc)))
        acquirer.acquire(project,[original],job.id)
        initial=json.loads(state_file(tmp_path,job.id).read_text(encoding='utf8'))
        service=VideoProductionService(db,tmp_path,material_resolver=resolver,material_acquirer=acquirer)
        scope=uuid4()
        extra={'after_master':True,'remaining_talking_calls':1,'supplement_reserve_calls':3}
        result=service._resolve_with_supplement(project,measured,job,job.id,scope,None,extra,
            measured_scenes=measured,master_audio_id=master.id)
        assert ClipRepository(db).get(result[1].preferred_clip_id).asset_id == good.id
        records=ProviderCallRepository(db).list_for_project(project.id)
        assert [r.operation for r in records] == ['vision','stock_search','stock_import','vision']
        assert all(r.status=='completed' for r in records) and len(vision.calls)==2
        assert actions==['stock_search','stock_import'] and 6-len(records)==2
        state=json.loads(state_file(tmp_path,job.id).read_text(encoding='utf8'))
        assert state['fingerprint']==initial['fingerprint'] and state['started_at']==initial['started_at']
        assert len(state['searches'])==len(state['imports'])==1
        assert state['master_audio_id']==str(master.id) and state['master_content_hash']==master.content_hash
        assert AudioAssetRepository(db).get(master.id)==master
        assert hashlib.sha256((tmp_path/'master.wav').read_bytes()).hexdigest()==master.content_hash
        assert ProjectDraftRepository(db).get(project.id)==draft
        again=service._resolve_with_supplement(project,measured,job,job.id,scope,None,extra,
            measured_scenes=measured,master_audio_id=master.id)
        assert again[1].preferred_clip_id==result[1].preferred_clip_id
        assert len(ProviderCallRepository(db).list_for_project(project.id))==4 and len(vision.calls)==2
        assert actions==['stock_search','stock_import']
        replay=json.loads(state_file(tmp_path,job.id).read_text(encoding='utf8'))
        assert replay['fingerprint']==state['fingerprint'] and replay['started_at']==state['started_at']
        assert len(replay['searches'])==len(replay['imports'])==1
    finally:
        db.close()
