import runpy, tempfile, json, sys
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app
from app.providers.scene_planner import OpenAICompatibleScenePlanner
root=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path.cwd()
h=runpy.run_path(str(root/'services/api/tests/test_scene_plan_api.py'))
with tempfile.TemporaryDirectory() as t:
 p=Path(t)/'db.sqlite'; calls=[]
 class Transport:
  def post(self,*args):
   calls.append(1); return h['_runtime_plan_response']()
 with TestClient(create_app(p,scene_planner=OpenAICompatibleScenePlanner('dummy',transport=Transport()))) as c:
  c.put('/budget',json={'max_calls':1,'allow_unknown_cost':True})
  a=c.post('/projects',json={'title':'A','topic':'A'}).json()['id']
  b=c.post('/projects',json={'title':'B','topic':'B'}).json()['id']
  r1=c.post(f'/projects/{a}/scene-plan',json={'topic':'first'},headers={'Idempotency-Key':'same'})
  r2=c.post(f'/projects/{a}/scene-plan',json={'topic':'changed'},headers={'Idempotency-Key':'same'})
  print('repeat runtime:',r1.status_code,r2.status_code,'provider invocations',len(calls),'ledger records',len(c.get(f'/projects/{a}/provider-calls').json()))
  r3=c.post(f'/projects/{b}/scene-plan',json={'topic':'other project'})
  print('global max_calls=1 second project:',r3.status_code,'global snapshot',c.get('/budget').json()['snapshot'],'over_budget',c.get('/budget').json()['over_budget'])
f=runpy.run_path(str(root/'services/api/tests/test_video_spec_assembly.py'))
with tempfile.TemporaryDirectory() as t:
 db,_,project,scenes,assets,clips=f['_setup'](Path(t),f['RationalFps'](numerator=30,denominator=1))
 ap=Path(t)/'voice.wav';ap.write_bytes(b'placeholder')
 audio=f['AudioAsset'](source_kind=f['SourceKind'].USER_ASSET,source_file=str(ap),content_hash='d'*64,duration_ms=2000,sample_rate=48000,channels=1,authorization_reference='creator',imported_at=f['NOW'])
 f['AudioAssetRepository'](db).create(audio)
 assembler=f['VideoSpecAssembler'](f['AssetRepository'](db),f['ClipRepository'](db),audios=f['AudioAssetRepository'](db))
 s=scenes[0];spec=assembler.assemble(project,[s],{s.id:f['_candidate'](s,assets[0],clips[0])},narration_asset_ids={s.id:audio.id},narration_required=True)
 print('assembled visual end',spec.scenes[0].visual.clip_end_ms,'authorized Clip end',clips[0].end_ms)
 db.close()
