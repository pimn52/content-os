"""Project/IP/Voice selection only; no inference, ASR, media render or real data."""
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.db import AssetRepository, ClipRepository, JobRepository, Database
from app.domain.models import Asset, Clip, RationalFps, JobStatus
from app.jobs.handlers import VoiceGenerationJobHandler
from app.jobs.runner import JobExecutionError


def test_project_ip_default_voice_and_explicit_cross_ip_choice(tmp_path):
    with Database(tmp_path/'identity.sqlite3') as db, TestClient(create_app(tmp_path/'identity.sqlite3')) as api:
        tim=api.post('/projects',json={'title':'TIM','topic':'Bitrate','creator_name':'TIM'}).json()
        other=api.post('/projects',json={'title':'Other','topic':'Other','creator_name':'Other'}).json()
        assert tim['ip_profile_id']!=other['ip_profile_id']
        assert {p['creator_name'] for p in api.get('/ip-profiles').json()}=={'TIM','Other'}
        assert api.post('/projects',json={'title':'Ambiguous','topic':'x'}).status_code==422
        explicit=api.post('/projects',json={'title':'Selected','topic':'x','ip_profile_id':tim['ip_profile_id']})
        assert explicit.status_code==201 and explicit.json()['ip_profile_id']==tim['ip_profile_id']
        now=datetime.now(timezone.utc);profiles=[]
        for project in (tim,other):
            asset=Asset(source_file=str(tmp_path/'synthetic.media'),content_hash=uuid4().hex*2,duration_ms=6000,width=1080,height=1920,
                fps=RationalFps(numerator=30,denominator=1),has_audio=True,authorization_reference='synthetic-only',imported_at=now)
            AssetRepository(db).create(asset)
            clip=Clip(asset_id=asset.id,start_ms=0,end_ms=6000,asset_duration_ms=6000);ClipRepository(db).create(clip)
            value=api.post('/voice-profiles',json=dict(ip_profile_id=project['ip_profile_id'],name=project['title'],provider='fixture',
                provider_profile_id=project['id'],reference_clip_ids=[str(clip.id)],created_at=now.isoformat(),
                consent=dict(subject_name=project['title'],basis='explicit_authorization',confirmed=True,confirmed_at=now.isoformat(),authorization_reference='synthetic-consent')))
            assert value.status_code==201,value.text
            profiles.append(value.json())
        scoped=f"/ip-profile?ip_profile_id={tim['ip_profile_id']}"
        assert api.put(scoped,json={'creator_name':'TIM','default_voice_profile_id':profiles[1]['id']}).status_code==422
        assert api.put(scoped,json={'creator_name':'TIM','default_voice_profile_id':profiles[0]['id']}).status_code==200
        assert api.put(scoped,json={'creator_name':'TIM','domains':['video']}).json()['default_voice_profile_id']==profiles[0]['id']
        catalog=api.get(f"/voice-profiles?project_id={tim['id']}&default_only=true").json()
        assert [p['id'] for p in catalog]==[profiles[0]['id']]
        body={'idempotency_key':'default','text':'Synthetic words','authorization_reference':'synthetic-consent'}
        url=f"/projects/{tim['id']}/voice-jobs"
        default=api.post(url,json=body);assert default.status_code==201,default.text
        own=JobRepository(db).get(UUID(default.json()['id']))
        assert str(own.payload.voice_profile_id)==profiles[0]['id']
        cross=dict(body,idempotency_key='cross',voice_profile_id=profiles[1]['id'])
        assert api.post(url,json=cross).status_code==422
        accepted=api.post(url,json=dict(cross,cross_ip_voice_authorization_reference='explicit owner choice: Other narrates TIM project'))
        assert accepted.status_code==201,accepted.text
        saved=JobRepository(db).get(UUID(accepted.json()['id']))
        assert saved.payload.cross_ip_voice_authorization_reference=='explicit owner choice: Other narrates TIM project'
        # Even a manually altered pending payload must fail before a provider.
        class Provider:
            provider_name='fixture';model='fixture'
            def synthesize(self,*args,**kwargs):pytest.fail('identity mismatch reached inference')
        from app.db import VoiceProfileRepository,AudioAssetRepository
        handler=VoiceGenerationJobHandler(VoiceProfileRepository(db),AudioAssetRepository(db),None,Provider(),tmp_path)
        bad=own.model_copy(update={'status':JobStatus.RUNNING,'attempt':1,'payload':own.payload.model_copy(update={'voice_profile_id':UUID(profiles[1]['id'])})})
        with pytest.raises(JobExecutionError,match='cross_ip_voice_explicit_authorization_required'):handler(bad)
        assert db.connection.execute('SELECT count(*) FROM provider_call_records').fetchone()[0]==0
        assert api.post(url,json=body).json()['id']==str(own.id)
