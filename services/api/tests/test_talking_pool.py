"""动作池的真实区间与调度契约；无推理、网络或生产库。"""
from datetime import datetime,timezone
from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db import (AssetRepository,ClipRepository,Database,TalkingProfileRepository,
    ProviderMachineCapabilityProfileRepository)
from app.domain.models import TalkingProfile,TalkingReferenceAssessment,Asset,Clip
from app.talking.pool import TalkingPool,build_talking_pool,allocate_talking_windows,TalkingPoolCapacityError
from test_planning_feasibility import _creator_project,_reference,_consent
from app.domain.models import ProviderMachineCapabilityProfile


def pool_fixture(db,project=None):
    if project is None:
        ip,project=_creator_project(db)
        ip_id=ip.id
    else:ip_id=project.ip_profile_id
    clips=[]
    for duration in (30000,12000,9000):
        identity=uuid4();path=Path(db.path).parent/f'{identity}.mp4'
        path.write_bytes(f'fixture silent motion {identity}'.encode())
        asset=AssetRepository(db).create(Asset(id=identity,source_file=str(path),duration_ms=duration,
            content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),has_audio=False,width=1080,height=1920,
            fps=project.fps,authorization_reference='入库授权',imported_at=datetime.now(timezone.utc),
            metadata={'ip_profile_id':str(ip_id)}))
        clip=ClipRepository(db).create(Clip(asset_id=asset.id,start_ms=0,end_ms=duration,asset_duration_ms=duration,
            talking_candidate=True,face_visibility=.9,mouth_visibility=.9,
            talking_reference_assessment=TalkingReferenceAssessment(burned_in_subtitles=False,evidence_reference='fixture:full-source')))
        clips.append(clip)
    overlap=ClipRepository(db).create(clips[0].model_copy(update={'id':uuid4(),'start_ms':5000,'end_ms':15000}))
    # 无评价的参考不能因同属于创作者就计作合格容量。
    ClipRepository(db).update(clips[2].model_copy(update={'talking_reference_assessment':None}))
    profile=TalkingProfileRepository(db).create(TalkingProfile(name='本人动作',provider='local',
        reference_clip_ids=[clip.id for clip in clips]+[overlap.id],consent=_consent(),created_at=datetime.now(timezone.utc)))
    capability=ProviderMachineCapabilityProfileRepository(db).save(ProviderMachineCapabilityProfile(
        capability='talking',mode='local',provider='local',model='model',runtime='runtime',machine_id='machine',
        readiness='verified',quality_status='verified',evidence_reference='evidence',provenance_source='fixture',
        commercial_status='commercial_safe',license_evidence_reference='license',updated_at=datetime.now(timezone.utc)))
    return project,clips,profile,capability


def test_pool_keeps_long_silent_sources_separates_total_and_continuous_and_deduplicates_overlap(tmp_path):
    with Database(tmp_path/'pool.sqlite') as db:
        project,clips,_,_=pool_fixture(db)
        changes=db.connection.total_changes
        pool=build_talking_pool(db,project.id,occupied=[])
        assert pool.usable_total_ms==42000 and pool.longest_continuous_ms==30000
        assert any(entry.clip.id==clips[0].id and entry.duration_ms==30000 for entry in pool.entries)
        assert db.connection.total_changes==changes
        allocations=allocate_talking_windows(pool,[20000,10000,10000])
        assert len({value.entry.asset.id for value in allocations})==2
        assert sum(value.narration_duration_ms for value in allocations)==40000
        with pytest.raises(TalkingPoolCapacityError):allocate_talking_windows(pool,[25000,20000])


def test_occupancy_splits_only_known_available_intervals_and_identity_is_enforced(tmp_path):
    with Database(tmp_path/'occupied.sqlite') as db:
        project,clips,_,_=pool_fixture(db)
        source=AssetRepository(db).get(clips[0].asset_id)
        pool=build_talking_pool(db,project.id,occupied=[{'source_hash':source.content_hash,'start_ms':10000,'end_ms':20000}])
        assert pool.usable_total_ms==32000 and pool.longest_continuous_ms==12000
        other=AssetRepository(db).get(clips[1].asset_id)
        AssetRepository(db).update(other.model_copy(update={'metadata':{'ip_profile_id':str(uuid4())}}))
        assert build_talking_pool(db,project.id,occupied=[]).usable_total_ms==30000


def test_model_segment_ceiling_does_not_shrink_source_pool_or_copy(tmp_path):
    with Database(tmp_path/'model.sqlite') as db:
        project,_,_,capability=pool_fixture(db)
        ProviderMachineCapabilityProfileRepository(db).save(capability.model_copy(update={'verified_parameters':{'fresh_voice_talking_duration_ms':10000}}))
        def validate(duration,reference):
            if reference<duration+200:raise ValueError('context needed')
        provider=SimpleNamespace(provider_name='local',model='model',validate_reference_duration=validate)
        pool=build_talking_pool(db,project.id,talking_provider=provider,occupied=[])
        summary=pool.summary(talking_provider=provider)
        assert summary['usable_total_ms']==42000 and summary['longest_continuous_ms']==30000
        assert summary['max_narration_duration_ms']==10000
        values=allocate_talking_windows(pool,[9800,9800],talking_provider=provider)
        assert all(value.needed_reference_ms==10000 for value in values)
        with pytest.raises(TalkingPoolCapacityError):allocate_talking_windows(pool,[10001],talking_provider=provider)


def test_overlapping_short_window_does_not_block_a_feasible_forward_batch(tmp_path):
    with Database(tmp_path/'overlap.sqlite') as db:
        project,clips,_,_=pool_fixture(db)
        pool=build_talking_pool(db,project.id,occupied=[])
        long=next(entry for entry in pool.entries if entry.clip.id==clips[0].id)
        short=next(entry for entry in pool.entries if entry.clip.id!=clips[0].id and entry.asset.id==long.asset.id)
        pool=TalkingPool((replace(long,end_ms=10000,source_end_ms=10000),
            replace(short,end_ms=7000,source_end_ms=7000)))
        def validate(duration,reference):
            if reference<duration+200:raise ValueError('context needed')
        provider=SimpleNamespace(provider_name='local',model='model',validate_reference_duration=validate)
        values=allocate_talking_windows(pool,[1800,7800],talking_provider=provider)
        assert [(value.start_ms,value.end_ms) for value in values]==[(0,2000),(2000,10000)]
        assert [value.needed_reference_ms for value in values]==[2000,8000]


def test_ordinary_api_exposes_motion_pool_without_creating_any_jobs_or_calls(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.db import JobRepository,ProviderCallRepository
    path=tmp_path/'api.sqlite'
    with Database(path) as db:project,_,_,_=pool_fixture(db)
    with TestClient(create_app(path)) as client:
        result=client.get(f'/projects/{project.id}/talking-pool')
        assert result.status_code==200,result.text
        assert result.json()['usable_total_ms']==42000 and result.json()['longest_continuous_ms']==30000
        assert client.get(f'/projects/{uuid4()}/talking-pool').status_code==404
    with Database(path) as db:
        assert JobRepository(db).list()==[] and ProviderCallRepository(db).list()==[]


def test_changed_source_is_excluded_while_other_qualified_sources_remain_usable(tmp_path):
    with Database(tmp_path/'changed.sqlite') as db:
        project,clips,_,_=pool_fixture(db)
        source=AssetRepository(db).get(clips[0].asset_id)
        Path(source.source_file).write_bytes(b'replaced without fresh qualification')
        pool=build_talking_pool(db,project.id,data_root=tmp_path,occupied=[])
        assert pool.usable_total_ms==pool.longest_continuous_ms==12000
