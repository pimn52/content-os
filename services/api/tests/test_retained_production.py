"""Retained-only production: synthetic local media/consent; no model or render."""
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from app.db import (AssetRepository, AudioAssetRepository, ClipRepository, Database, JobRepository,
    ProjectDraftRepository, ProjectRepository, TalkingProfileRepository, VoiceProfileRepository)
from app.domain.models import (AudioAsset, Clip, ConsentRecord, Job, JobStatus, JobType, ProjectDraft,
    SourceKind, TalkingGenerationJobPayload, TalkingProfile, TranscriptSegment, VoiceHumanReview, VoiceProfile, VisualStyleTokens)
from app.domain.evaluation_composition import CompositionSelection
from app.evaluation_composition import EvaluationCompositionService
from app.execution_admission import UseAdmissionError
from app.execution_scope import ExecutionScopeService, require_evaluation_lane_closed
from app.production_preflight import build_production_preflight
from app.production_runs import ProductionRunService
from test_video_spec_assembly import _asset, _scene


@pytest.fixture
def legacy(tmp_path, admit_talking_run, monkeypatch):
    now=datetime.now(timezone.utc);db=Database(tmp_path/'legacy.sqlite3');db.data_root=tmp_path
    pid=uuid4();source=tmp_path/'synthetic.media';source.write_bytes(b'SYNTHETIC not a real approved video')
    asset=_asset(1,source_kind='ai_video').model_copy(update=dict(source_file=str(source),duration_ms=5020,
        content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),authorization_reference='synthetic-consent'))
    AssetRepository(db).create(asset)
    clip=ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=5020,asset_duration_ms=5020))
    consent=ConsentRecord(subject_name='Synthetic fixture',basis='self',confirmed=True,confirmed_at=now,authorization_reference='synthetic-consent')
    voice=VoiceProfile(name='Synthetic',provider='fixture',provider_profile_id='test',reference_clip_ids=[clip.id],consent=consent,created_at=now)
    talking=TalkingProfile(name='Synthetic',provider='fixture',reference_clip_ids=[clip.id],consent=consent,created_at=now)
    VoiceProfileRepository(db).create(voice);TalkingProfileRepository(db).create(talking)
    asset=AssetRepository(db).update(asset.model_copy(update={"metadata":{"talking_generation":{
        "talking_profile_id":str(talking.id),"provider":"fixture","qa_state":"verified","human_review_state":"approved"}}}))
    review=VoiceHumanReview(approved=True,evidence_reference='fixture-only',findings=['Synthetic approval, no real quality claim'],
        likeness='pass',naturalness='pass',emphasis='pass',pace='pass',pauses='pass',rhythm='pass',reviewed_at=now)
    audiofile=tmp_path/'synthetic.wav';audiofile.write_bytes(b'SYNTHETIC not real audio')
    audio=AudioAsset(source_file=str(audiofile),content_hash=hashlib.sha256(audiofile.read_bytes()).hexdigest(),
        duration_ms=5300,sample_rate=24000,channels=1,authorization_reference='synthetic-consent',imported_at=now,
        transcript_source='synthetic',transcript_segments=[TranscriptSegment(start_ms=i*1000,end_ms=(i+1)*1000,text=f'word{i}') for i in range(5)],
        metadata={'voice_generation':dict(project_id=str(pid),provider='fixture',model='fixture',voice_profile_id=str(voice.id),
            qa_state='verified',human_review_state='approved',human_review=review.model_dump(mode='json'))})
    AudioAssetRepository(db).create(audio)
    copy=' '.join(s.text for s in audio.transcript_segments)
    asset,run,_=admit_talking_run(db,pid,asset,clip,copy,audio=audio)
    run=run.model_copy(update={"authorized_reference_clip_id":clip.id})
    db.connection.execute("UPDATE talking_runs SET payload=? WHERE id=?",(run.model_dump_json(),str(run.id)))
    # Failed/old execution status is deliberately not present consumption QA.
    JobRepository(db).create(Job(id=run.child_evidence[0].job_id,project_id=pid,type=JobType.GENERATE_TALKING,
        status=JobStatus.FAILED,idempotency_key='historical',created_at=now,updated_at=now,
        payload=TalkingGenerationJobPayload(project_id=pid,talking_profile_id=talking.id,reference_clip_id=clip.id,
            narration_audio_id=audio.id,authorization_reference='synthetic-consent')))
    project=ProjectRepository(db).get(pid)
    scenes=[_scene(project,i,1000).model_copy(update=dict(voice_text=f'word{i}',preferred_sources=[SourceKind.AI_VIDEO],
        visual_requirement='creator_speaking',visual_requirement_reason='Synthetic explicit speaking requirement')) for i in range(5)]
    draft=ProjectDraft(project_id=pid,version=1,script_revision=1,script=copy,scenes=scenes,updated_at=now)
    ProjectDraftRepository(db).save(draft)
    key='synthetic-operator-key-000000000000000000000000000000'
    monkeypatch.setenv('CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES',json.dumps({'fixture':hashlib.sha256(key.encode()).hexdigest()}))
    yield db,pid,asset,audio,talking,draft,key
    db.close()


def use_and_batch(service,pid,context,key):
    root=service.root;terms=root/'synthetic-terms.txt';terms.write_text('Synthetic fixture permission, never runtime license evidence')
    evidence=dict(source='synthetic',version='fixture',terms_reference=terms.name,terms_sha256=hashlib.sha256(terms.read_bytes()).hexdigest(),
        applicability='synthetic exact test use',permits_intended_activity=True)
    def save(name,value):
        file=root/name;file.write_text(json.dumps(value),encoding='utf-8')
        from app.domain.evaluation_master import MasterAdoption
        return MasterAdoption(review_reference=name,review_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),idempotency_key=name)
    report=dict(policy='evaluation-composition-v1',purpose='internal_evaluation',operation='compose_retained_media_and_review',local_only=True,
        project_id=str(pid),selection=context['selection'].model_dump(mode='json'),request_sha256=context['request_sha256'],proof=context['proof'],
        intended_activity='Synthetic fixture only',license_evidence=[],findings=['No real rights or quality claims'],
        dependency_findings=[dict(evidence,dependency=d) for d in ['retained_media_composition','local_renderer_and_tools',*context['proof']['dependencies']]])
    use=service.adopt(pid,save('use.json',report),key)
    service.adopt_batch(pid,save('batch.json',dict(policy='evaluation-composition-v1',project_id=str(pid),master_audio_id=str(context['audio'].id),
        authorization_reference=terms.name,authorization_sha256=evidence['terms_sha256'],scene_ids=[],max_renders=1,findings=['Synthetic one-render grant'])),key)
    return use


def test_ordinary_retained_preflight_start_no_generation_history_or_new_calls(legacy):
    db,pid,asset,audio,_,draft,key=legacy;service=EvaluationCompositionService(db,db.data_root);style=VisualStyleTokens()
    # An obsolete generation payload cannot become a media consumption dependency.
    db.connection.execute("UPDATE jobs SET payload=json_set(payload,'$.payload.planned_context',json('{\"obsolete\":true}'))")
    selection=service.retained_selection(pid,style);context=service._context(pid,selection)
    assert selection.master_run_id is None and selection.master_audio_id==audio.id
    assert len(selection.talking_runs)==5 and len(set(selection.talking_runs.values()))==1
    spec=context['spec'];assert len(spec.scenes)==6
    assert [(s.visual.clip_start_ms,s.visual.clip_end_ms) for s in spec.scenes[:5]]==[(i*1000,(i+1)*1000) for i in range(5)]
    assert spec.master_narration.end_ms==5300 and spec.scenes[-1].narration_start_ms==5000
    assert not spec.scenes[-1].caption and not spec.scenes[-1].captions and spec.scenes[-1].visual.asset_id is None
    plan=build_production_preflight(db,ProjectRepository(db).get(pid),draft,style_tokens=style,purpose='internal_evaluation',data_root=db.data_root)
    assert plan.stop_reasons==('composition_use_admission_required',)
    assert not any(a.required and a.kind in {'voice','talking','analysis'} for a in plan.actions)
    with pytest.raises(UseAdmissionError):service.current_use(pid,context)
    use=use_and_batch(service,pid,context,key)
    plan=build_production_preflight(db,ProjectRepository(db).get(pid),draft,style_tokens=style,purpose='internal_evaluation',data_root=db.data_root)
    assert plan.status=='ready_for_authorized_dispatch'
    runs=ProductionRunService(db,db.data_root)
    run=runs.start(pid,idempotency_key='normal',expected_fingerprint=plan.fingerprint,style_tokens=style,purpose='internal_evaluation')
    assert runs.start(pid,idempotency_key='different-key',expected_fingerprint=plan.fingerprint,style_tokens=style,purpose='internal_evaluation').id==run.id
    job=JobRepository(db).get(run.render_job_id)
    assert job.type==JobType.RENDER and job.payload.video_spec==spec
    assert run.talking_review_policy_version==1  # No policy2 history is manufactured.
    assert db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0]==2
    assert db.connection.execute('SELECT count(*) FROM provider_call_records').fetchone()[0]==0
    with pytest.raises(UseAdmissionError):require_evaluation_lane_closed(db,job)
    with pytest.raises(UseAdmissionError):ExecutionScopeService(db,db.data_root).prepare(pid,purpose='internal_evaluation')
    service.revoke(pid,use.id,'Synthetic permission withdrawn',key)
    with pytest.raises(UseAdmissionError):service.require_current_job(job)


@pytest.mark.parametrize('change',['hash','copy','consent','project','review'])
def test_current_media_safety_fails_without_fallback_generation(legacy,change):
    db,pid,asset,audio,talking,draft,_=legacy;service=EvaluationCompositionService(db,db.data_root)
    selection=service.retained_selection(pid,VisualStyleTokens())
    if change=='hash':
        from pathlib import Path
        Path(asset.source_file).write_bytes(b'changed')
    elif change=='copy':
        db.connection.execute("UPDATE project_drafts SET payload=json_set(payload,'$.script','changed') WHERE project_id=?",(str(pid),))
    elif change=='consent':
        db.connection.execute("UPDATE talking_profiles SET payload=json_set(payload,'$.consent.confirmed',json('false')) WHERE id=?",(str(talking.id),))
    elif change=='project':
        pid=uuid4()
    else:
        db.connection.execute("UPDATE talking_slice_series_continuity_reviews SET payload=json_set(payload,'$.approved',json('false'))")
    with pytest.raises((ValueError,OSError)):service._context(pid,selection)
    assert db.connection.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
    assert db.connection.execute('SELECT count(*) FROM provider_call_records').fetchone()[0]==0
