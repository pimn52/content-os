"""Retained-only production: synthetic local media/consent; no model or render."""
import hashlib
import json
from datetime import datetime, timezone, timedelta
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
    voice=voice.model_copy(update={"ip_profile_id":project.ip_profile_id})
    db.connection.execute("UPDATE voice_profiles SET payload=? WHERE id=?",(voice.model_dump_json(),str(voice.id)))
    scenes=[_scene(project,i,1000).model_copy(update=dict(voice_text=f'word{i}',preferred_sources=[SourceKind.AI_VIDEO],
        visual_requirement='creator_speaking',visual_requirement_reason='Synthetic explicit speaking requirement')) for i in range(5)]
    draft=ProjectDraft(project_id=pid,version=1,script_revision=1,script=copy,scenes=scenes,updated_at=now)
    ProjectDraftRepository(db).save(draft)
    key='synthetic-operator-key-for-tests-0001'
    monkeypatch.setenv('CONTENT_OS_EXECUTION_ADMISSION_KEY_HASHES',json.dumps({
        'fixture-operator':hashlib.sha256(key.encode()).hexdigest()}))
    from app.db import BudgetPolicyRepository
    from app.domain.models import BudgetPolicy
    BudgetPolicyRepository(db).save(BudgetPolicy(project_id=pid,currency='CNY',allow_unknown_cost=True,max_calls=1,updated_at=now))
    yield db,pid,asset,audio,talking,draft,key
    db.close()


def use_and_batch(service,pid,context,key,*,suffix=''):
    root=service.root;terms=root/f'synthetic-terms{suffix}.txt';terms.write_text(f'Synthetic fixture permission {suffix}, never runtime license evidence')
    evidence=dict(source='synthetic',version='fixture',terms_reference=terms.name,terms_sha256=hashlib.sha256(terms.read_bytes()).hexdigest(),
        applicability='synthetic exact test use',permits_intended_activity=True)
    def save(name,value):
        file=root/name;file.write_text(json.dumps(value),encoding='utf-8')
        from app.domain.evaluation_master import MasterAdoption
        return MasterAdoption(review_reference=name,review_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),idempotency_key=name)
    report=dict(policy='evaluation-composition-v1',purpose='internal_evaluation',operation='compose_retained_media_and_review',local_only=True,
        project_id=str(pid),selection=context['selection'].model_dump(mode='json'),request_sha256=context['request_sha256'],proof=context['proof'],
        intended_activity='Synthetic fixture only',
        license_evidence=[dict(evidence,admission_id=str(b.admission_id)) for b in context['scope'].bindings],
        findings=['No real rights or quality claims'],
        dependency_findings=[dict(evidence,dependency=d) for d in ['retained_media_composition','local_renderer_and_tools',*context['proof']['dependencies']]])
    use=service.adopt(pid,save('use'+suffix+'.json',report),key)
    assert use.actor_id == 'fixture-operator'
    batch=service.adopt_batch(pid,save(f'batch{suffix}.json',dict(policy='evaluation-composition-v1',project_id=str(pid),master_audio_id=str(context['audio'].id),
        revision_sha256=context['request_sha256'],selection=context['selection'].model_dump(mode='json'),
        authorization_reference=terms.name,authorization_sha256=evidence['terms_sha256'],scene_ids=[],max_renders=1,
        findings=['Synthetic one-render grant for this exact revision'])),key)
    assert batch['actor_id'] == 'fixture-operator'
    return use




def test_composition_fence_ignores_unrelated_commits_and_rejects_revision_change(legacy):
    import sqlite3
    db,pid,_,_,_,_,_=legacy
    service=EvaluationCompositionService(db,db.data_root)
    context=service._context(pid,service.retained_selection(pid,VisualStyleTokens()))
    other=sqlite3.connect(db.path,isolation_level=None)
    try:
        other.execute("INSERT INTO content_opportunities(id,dedupe_key,status,created_at,payload) VALUES ('unrelated','unrelated','new','now','{}')")
        assert service._fence(pid,context)["request_sha256"]==context["request_sha256"]
        other.execute("UPDATE project_drafts SET version=version+1,payload=json_set(payload,'$.version',version+1) WHERE project_id=?",(str(pid),))
    finally:
        other.close()
    with pytest.raises(UseAdmissionError):
        service._fence(pid,context)








def test_revision_batch_migration_preserves_old_run_and_allows_unique_audio_revision_slots(tmp_path):
    import sqlite3
    from app.db.migrations import CURRENT_SCHEMA_VERSION,apply_migrations
    path=tmp_path/'composition-v54.sqlite3';connection=sqlite3.connect(path,isolation_level=None)
    connection.execute('PRAGMA foreign_keys=ON')
    connection.executescript('''
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY NOT NULL,applied_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE projects(id TEXT PRIMARY KEY NOT NULL);
        CREATE TABLE production_runs(id TEXT PRIMARY KEY NOT NULL);
        CREATE TABLE audio_assets(id TEXT PRIMARY KEY NOT NULL);
        CREATE TABLE jobs(id TEXT PRIMARY KEY NOT NULL);
        CREATE TABLE evaluation_composition_batches(
            id TEXT PRIMARY KEY NOT NULL,project_id TEXT NOT NULL REFERENCES projects(id),
            master_run_id TEXT REFERENCES production_runs(id),authorization_sha256 TEXT NOT NULL UNIQUE,
            master_audio_id TEXT REFERENCES audio_assets(id),CHECK((master_run_id IS NULL)!=(master_audio_id IS NULL)),
            UNIQUE(project_id,master_run_id),UNIQUE(project_id,master_audio_id));
        CREATE TABLE evaluation_composition_owners(
            run_id TEXT PRIMARY KEY NOT NULL REFERENCES production_runs(id),project_id TEXT NOT NULL REFERENCES projects(id),
            job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),render_id TEXT NOT NULL UNIQUE,
            batch_id TEXT NOT NULL REFERENCES evaluation_composition_batches(id),request_sha256 TEXT NOT NULL,
            job_sha256 TEXT NOT NULL,scope_fingerprint TEXT NOT NULL);
        CREATE TABLE evaluation_composition_slots(
            batch_id TEXT NOT NULL REFERENCES evaluation_composition_batches(id),slot TEXT NOT NULL,
            operation_id TEXT NOT NULL UNIQUE,PRIMARY KEY(batch_id,slot));
        CREATE TABLE evaluation_composition_records(
            kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,payload_sha256 TEXT NOT NULL,PRIMARY KEY(kind,id));
        CREATE TABLE evaluation_talking_operations(id TEXT PRIMARY KEY NOT NULL);
        CREATE TABLE evaluation_talking_recoveries(
            predecessor_id TEXT PRIMARY KEY NOT NULL REFERENCES evaluation_talking_operations(id),
            successor_id TEXT NOT NULL UNIQUE REFERENCES evaluation_talking_operations(id),
            project_id TEXT NOT NULL REFERENCES projects(id),batch_id TEXT NOT NULL REFERENCES evaluation_composition_batches(id),
            slot TEXT NOT NULL,payload_sha256 TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(batch_id,slot));
    ''')
    connection.executemany('INSERT INTO schema_migrations(version) VALUES (?)',((v,) for v in range(1,55)))
    connection.execute("INSERT INTO projects VALUES ('p')")
    connection.execute("INSERT INTO audio_assets VALUES ('a')")
    connection.execute("INSERT INTO production_runs VALUES ('r')")
    connection.execute("INSERT INTO jobs VALUES ('j')")
    connection.execute("INSERT INTO evaluation_composition_batches VALUES ('b','p',NULL,?,'a')",('d'*64,))
    connection.execute("INSERT INTO evaluation_composition_owners VALUES ('r','p','j','render','b',?,?,?)",('e'*64,'f'*64,'a'*64))
    connection.execute("INSERT INTO evaluation_composition_slots VALUES ('b','render','r')")
    connection.execute("INSERT INTO evaluation_talking_operations VALUES ('pred')")
    connection.execute("INSERT INTO evaluation_talking_operations VALUES ('succ')")
    connection.execute("INSERT INTO evaluation_talking_recoveries VALUES ('pred','succ','p','b','talking:scene',?,'{}')",('7'*64,))

    assert apply_migrations(connection)==CURRENT_SCHEMA_VERSION
    assert connection.execute("SELECT visual_source_uses FROM production_runs").fetchone()[0]=="[]"
    assert tuple(connection.execute('SELECT id,revision_sha256 FROM evaluation_composition_batches').fetchone())==('b',None)
    assert tuple(connection.execute('SELECT run_id,job_id,batch_id FROM evaluation_composition_owners').fetchone())==('r','j','b')
    assert tuple(connection.execute('SELECT batch_id,slot,operation_id FROM evaluation_composition_slots').fetchone())==('b','render','r')
    assert tuple(connection.execute('SELECT predecessor_id,successor_id,batch_id FROM evaluation_talking_recoveries').fetchone())==('pred','succ','b')
    assert connection.execute('PRAGMA foreign_keys').fetchone()[0]==1
    connection.execute("INSERT INTO evaluation_composition_batches(id,project_id,authorization_sha256,master_audio_id,revision_sha256) VALUES ('b2','p',?,'a',?)",('1'*64,'2'*64))
    connection.execute("INSERT INTO evaluation_composition_batches(id,project_id,authorization_sha256,master_audio_id,revision_sha256) VALUES ('b3','p',?,'a',?)",('3'*64,'4'*64))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO evaluation_composition_batches(id,project_id,authorization_sha256,master_audio_id,revision_sha256) VALUES ('b4','p',?,'a',?)",('5'*64,'2'*64))
    assert connection.execute('PRAGMA foreign_key_check').fetchall()==[]
    connection.close()




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














def test_known_cny_cost_still_obeys_amount_limit(legacy):
    from decimal import Decimal
    from app.budget import ProviderCallLedger, BudgetLimitError
    from app.db import BudgetPolicyRepository
    from app.domain.models import BudgetPolicy, UsageCost, CostCategory
    db,pid,*_=legacy
    BudgetPolicyRepository(db).save(BudgetPolicy(project_id=pid,currency='CNY',max_amount=Decimal('2'),max_calls=1,
        allow_unknown_cost=True,updated_at=datetime.now(timezone.utc)))
    ledger=ProviderCallLedger(db)
    ledger.check_reservation(project_id=pid,estimated_cost=UsageCost(category=CostCategory.RENDER,amount=Decimal('1'),currency='CNY'))
    with pytest.raises(BudgetLimitError):
        ledger.check_reservation(project_id=pid,estimated_cost=UsageCost(category=CostCategory.RENDER,amount=Decimal('3'),currency='CNY'))


def test_approved_display_copy_uses_word_positions_without_rewriting_asr(legacy):
    from copy import deepcopy
    from app.assembly.evaluation_composition import _approved_scene_captions
    from app.voice_qa import comparison_tokens
    _,_,_,audio,_,draft,_=legacy
    text='码率是每秒分给画面的数据量，提高码率不会恢复源里丢失的细节。'
    observed=text.translate(str.maketrans({'码':'碼','画':'畫','丢':'丟','源':'原'}))
    words=[dict(text=char,start_ms=i*100,end_ms=(i+1)*100) for i,char in enumerate(''.join(comparison_tokens(observed)))]
    end=words[-1]['end_ms']
    audio=audio.model_copy(update=dict(duration_ms=end,transcript_segments=[TranscriptSegment(start_ms=0,end_ms=end,text=observed)],
        metadata=dict(voice_generation=dict(qa_state='verified',target_text=text),voice_word_timing=dict(source_sha256=audio.content_hash,words=words))))
    original=deepcopy(audio.model_dump(mode='json'));scene=draft.scenes[0].model_copy(update={'voice_text':text})
    captions=_approved_scene_captions(audio,[scene],scene,0,end)
    assert ''.join(c.text for c in captions)==text
    assert all(c.start_ms in {w['start_ms'] for w in words} and c.end_ms in {w['end_ms'] for w in words} for c in captions)
    assert audio.model_dump(mode='json')==original
    audio.metadata['voice_word_timing']['source_sha256']='0'*64
    assert _approved_scene_captions(audio,[scene],scene,0,end)==[]






@pytest.fixture
def portrait(legacy):
    from app.media import MediaImporter,ProbeMetadata,VerticalSourceDerivationService
    from app.domain.models import VerifiedVerticalDerivationRequest,SourceCropWindow,VerifiedVerticalDerivationEvidence,RationalFps
    from pathlib import Path
    db,pid,asset,_,_,_,_=legacy
    asset=AssetRepository(db).update(asset.model_copy(update={'width':1280,'height':720}))
    class Probe:
        def probe(self,path):return ProbeMetadata(duration_ms=5000,width=1080,height=1080,fps=RationalFps(numerator=30,denominator=1),has_audio=False,metadata={})
    crop=SourceCropWindow(x=0,y=0,width=500,height=500)
    clip=ClipRepository(db).list_by_asset(asset.id)[0]
    evidence=VerifiedVerticalDerivationEvidence(source_asset_id=asset.id,source_clip_id=clip.id,source_content_hash=asset.content_hash,
        covered_start_ms=0,covered_end_ms=5000,crop_window=crop,face_head_clearance_verified=True,crop_stability_verified=True,
        source_subtitle_clearance_verified=True,method='human_full_interval_review',method_version='synthetic-v1',
        coverage='full_interval_continuous',confidence='reviewed',evidence_reference='Synthetic fixture, no actual crop-safety claim')
    request=VerifiedVerticalDerivationRequest(idempotency_key='synthetic-square',source_asset_id=asset.id,source_clip_id=clip.id,
        start_ms=0,end_ms=5000,crop_window=crop,output_height=1080,source_subtitle_state='not_observed',evidence=evidence)
    service=VerticalSourceDerivationService(db,db.data_root,MediaImporter(db,db.data_root,Probe()),runner=lambda argv:Path(argv[-1]).write_bytes(b'synthetic derived media'))
    return service.derive(request)




@pytest.mark.parametrize('invalid',['bytes','evidence','interval'])
def test_invalid_portrait_falls_back_to_reviewed_original_without_new_media(legacy,portrait,invalid):
    from pathlib import Path
    db,pid,original,_,_,_,_=legacy
    if invalid=='bytes':Path(portrait.asset.source_file).write_bytes(b'changed')
    else:
        p=portrait.asset.metadata['source_derivation']
        if invalid=='evidence':p['evidence']['face_head_clearance_verified']=False
        else:p['source_interval']['end_ms']=4999
        AssetRepository(db).update(portrait.asset)
    service=EvaluationCompositionService(db,db.data_root)
    context=service._context(pid,service.retained_selection(pid,VisualStyleTokens(name='content-os-subject-first')))
    assert all(v['asset'].id==original.id for v in context['inputs'].values())
    assert all(s.subtitle_treatment.value=='none' for s in context['spec'].scenes[:5])
    assert db.connection.execute('SELECT count(*) FROM provider_call_records').fetchone()[0]==0


@pytest.mark.parametrize("fps",[(30,1),(30000,1001)])
def test_absolute_timing_full_master_and_neutral_tail(fps,tmp_path):
    from app.assembly.evaluation_composition import assemble_retained_composition
    from app.domain.models import AudioAsset, RationalFps, VisualStyleTokens, TranscriptSegment, Clip, CandidateAsset
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
    for scene in scenes:
        item=inputs[scene.id]
        item['candidate']=CandidateAsset(scene_plan_id=scene.id,source_kind='ai_video',asset_id=item['asset'].id,clip_id=item['clip'].id,
            match_score=1,why=['synthetic'],estimated_cost={'category':'render','amount':'0','currency':'USD'})
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


def test_retained_composition_derives_comparison_crop_from_the_scene_interval():
    from app.assembly.evaluation_composition import assemble_retained_composition
    from app.domain.models import (AudioAsset,CandidateAsset,CostCategory,RationalFps,TranscriptSegment,
        UsageCost,VisualIntent,VisualStyleTokens)
    from test_video_spec_assembly import _project
    project=_project(RationalFps(numerator=30,denominator=1))
    scene=_scene(project,0,1001).model_copy(update={"visual_intent":VisualIntent(
        subject="bitrate comparison",action="side-by-side comparison",description="compare the same source image")})
    asset=_asset(1).model_copy(update={"duration_ms":5000,"width":1280,"height":720})
    clip=Clip(asset_id=asset.id,start_ms=0,end_ms=5000,asset_duration_ms=5000)
    asset=asset.model_copy(update={"metadata":{"visual_analysis":{str(clip.id):{
        "source_content_hash":asset.content_hash,"clip_interval":{"start_ms":0,"end_ms":1034},
        "comparison_layout":"side_by_side_equal_halves","subtitle_regions":[[.3,.9,.4,.05]],
        "evidence_reference":"synthetic-used-scene-interval","coverage":"full_interval_continuous",
        "covered_start_ms":0,"covered_end_ms":1034,
    }}}})
    audio=AudioAsset(source_file="synthetic.wav",content_hash="a"*64,duration_ms=1001,sample_rate=24000,channels=1,
        authorization_reference="synthetic",imported_at=project.created_at,transcript_source="synthetic",
        transcript_segments=[TranscriptSegment(start_ms=0,end_ms=1001,text=scene.voice_text)])
    candidate=CandidateAsset(scene_plan_id=scene.id,source_kind=SourceKind.USER_ASSET,asset_id=asset.id,clip_id=clip.id,
        match_score=.9,why=["synthetic comparison"],estimated_cost={"category":CostCategory.USER_ASSET,"amount":"0","currency":"USD"})
    spec=assemble_retained_composition(project,audio,[(scene,0,1001)],
        {scene.id:{"asset":asset,"clip":clip,"candidate":candidate}},
        VisualStyleTokens(name="content-os-portrait-comparison"))
    plan_scene=spec.edit_plan.scenes[0]
    assert spec.scenes[0].duration_frames==31
    assert plan_scene.portrait_presentation.value=="stacked_comparison"
    assert plan_scene.source_bottom_crop_ratio==pytest.approx(74/720)
    assert plan_scene.source_subtitle_evidence_reference=="synthetic-used-scene-interval"
    assert spec.scenes[0].visual.source_bottom_crop_ratio==plan_scene.source_bottom_crop_ratio
    assert spec.scenes[0].visual.vertical_reframe_evidence_reference==plan_scene.source_subtitle_evidence_reference


def test_worker_uses_one_render_handler_independent_of_legacy_policy(tmp_path,monkeypatch):
    from app.db import Database
    from app.worker_cli import parse_config,build_runner
    from app.domain.models import JobType
    from app.jobs.handlers import RenderVideoJobHandler
    cfg=parse_config(['--db',str(tmp_path/'worker.sqlite3'),'--data-root',str(tmp_path),'--once','--job-type','render','--max-attempts','1'])
    monkeypatch.setenv('CONTENT_OS_RENDER_EXECUTION_POLICY','evaluation-composition-v1')
    with Database(cfg.db_path) as db:
        runner=build_runner(cfg,db)
        assert type(runner._handlers[JobType.RENDER].__wrapped__) is RenderVideoJobHandler
        monkeypatch.setenv('CONTENT_OS_RENDER_EXECUTION_POLICY','unknown')
        assert type(build_runner(cfg,db)._handlers[JobType.RENDER].__wrapped__) is RenderVideoJobHandler


@pytest.mark.parametrize("change", [None, "stale", "missing", "inside_word", "unverified", "overlap"])
def test_verified_position_preserving_words_map_scenes_without_rewriting_asr(change):
    from types import SimpleNamespace
    from app.production_runs import _unique_scene_speech_span
    from app.domain.models import AudioAsset, TranscriptSegment
    tokens = [f"word{i}" for i in range(80)]
    copy = " ".join(tokens)
    observed = list(tokens); observed[10] = "recognized"
    words = [dict(start_ms=i*100, end_ms=(i+1)*100, text=t) for i,t in enumerate(observed)]
    metadata = dict(voice_generation=dict(target_text=copy, qa_state="verified"),
        voice_word_timing=dict(source_sha256="a"*64, words=words))
    scenes = [SimpleNamespace(id=uuid4(), voice_text=" ".join(tokens[:40])),
        SimpleNamespace(id=uuid4(), voice_text=" ".join(tokens[40:]))]
    if change == "stale": metadata["voice_word_timing"]["source_sha256"] = "b"*64
    elif change == "missing": words.pop(10)
    elif change == "inside_word": words[39:41] = [dict(start_ms=3900,end_ms=4100,text=" ".join(observed[39:41]))]
    elif change == "unverified": metadata["voice_generation"]["qa_state"] = "pending"
    elif change == "overlap": words[2]["start_ms"] = 150
    audio = AudioAsset(source_file="fixture.wav",content_hash="a"*64,duration_ms=8200,
        sample_rate=24000,channels=1,authorization_reference="synthetic",imported_at=datetime.now(timezone.utc),
        transcript_source="fixture-asr",transcript_segments=[TranscriptSegment(start_ms=0,end_ms=8000,text=" ".join(observed))],metadata=metadata)
    before = audio.model_dump_json()
    assert _unique_scene_speech_span(audio,scenes,scenes[0].id) == ((0,4000) if change is None else None)
    if change is None: assert _unique_scene_speech_span(audio,scenes,scenes[1].id) == (4000,8000)
    assert audio.model_dump_json() == before
