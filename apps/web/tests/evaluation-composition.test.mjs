import test from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {componentHarness} from './hook-harness.mjs';
const file=fileURLToPath(new URL('../src/EvaluationCompositionPanel.tsx',import.meta.url));
function fixture(){
  const calls=[];let saved=[],runs=[];
  const selection={policy:'evaluation-composition-v1',purpose:'internal_evaluation',master_run_id:'M',talking_runs:{S:'T'},tail:'neutral_background'};
  const request=async(path,options)=>{
    calls.push({path,options});
    if(path.endsWith('/recoveries'))return [];
    if(path.endsWith('/candidates'))return {masters:[{id:'M',eligible:true,reasons:[],scenes:[{id:'S',label:'Exact copy'}]}],talking:[{id:'O',master_run_id:'M',scene_plan_id:'S',state:'admitted',current_eligible:true,talking_run_id:'T'}]};
    if(path.endsWith('/admissions'))return [{id:'U',revoked_at:null,review:{request_sha256:'fingerprint',selection}}];
    if(path.endsWith('/batches'))return [{id:'B',review:{master_run_id:'M'}}];
    if(path.endsWith('/preflight'))return {eligible:true,fingerprint:'fingerprint',missing_scenes:[],reasons:[]};
    if(path.endsWith('/selections')){if(options?.method==='POST')saved=[JSON.parse(options.body)];return saved;}
    if(path.endsWith('/runs')){if(options?.method==='POST')runs=[{id:'R',project_id:'P',state:'pending',reasons:[],current_eligible:true,technical_qa:null}];return runs;}
    throw new Error('unexpected request');
  };
  return {calls,request,create:changes=>componentHarness(file,'EvaluationCompositionPanel',{projectId:'P',draftVersion:1,scriptRevision:1,draftDirty:false,request,...changes})};
}
test('normal exact selection saves and reloads without generation, then starts same inspected use',async()=>{
  const f=fixture(),h=f.create();await h.flush();await h.change('完整旁白','M');await h.change('Exact copy','T');
  await h.click('检查成片方案');await h.click('保存素材选择');await h.click('开始本批内部成片');
  const start=f.calls.find(c=>c.path.endsWith('/runs')&&c.options?.method==='POST');
  const body=JSON.parse(start.options.body);assert.equal(body.selection.talking_runs.S,'T');assert.equal(body.admission_id,'U');assert.equal(body.batch_id,'B');
  assert.equal(body.expected_fingerprint,'fingerprint');assert.match(h.text(),/等待制作/);
  const fresh=f.create();await fresh.flush();assert.equal(fresh.input('完整旁白').props.value,'M');assert.equal(fresh.input('Exact copy').props.value,'T');
  assert.equal(f.calls.some(c=>/voice-drafts|evaluation-talking\/operations/.test(c.path)),false);
  h.unmount();fresh.unmount();
});
test('selection change or dirty draft invalidates inspected plan and prevents start',async()=>{
  const f=fixture(),h=f.create();await h.flush();await h.change('完整旁白','M');await h.change('Exact copy','T');await h.click('检查成片方案');
  assert.equal(h.find('button','开始本批内部成片').props.disabled,false);
  await h.change('Exact copy','');assert.equal(h.find('button','开始本批内部成片').props.disabled,true);
  h.setProps({draftDirty:true});await h.flush();assert.equal(h.find('button','检查成片方案').props.disabled,true);
  h.unmount();
});

test('operator recovery preserves the original allowance and reloads one successor',async()=>{
  const f=fixture(), submitted=[];
  let records=[{predecessor_id:'OLD',scene_plan_id:'S',successor_id:null,reasons:[],choices:[{
    selection:{policy:'evaluation-talking-calibration-v1',scene_plan_id:'S'},start_fingerprint:'normal-proof',fingerprint:'recovery-proof'}]}];
  const request=async(path,options)=>{
    if(path.endsWith('/recoveries'))return records;
    if(path.endsWith('/operations/OLD/recovery')){
      submitted.push({options,body:JSON.parse(options.body)});
      records=[{...records[0],successor_id:'NEW',choices:[]}];return {id:'NEW'};
    }
    return f.request(path,options);
  };
  const h=f.create({request});await h.flush();
  await h.change('恢复核对记录','review.json');await h.change('恢复记录摘要','a'.repeat(64));await h.change('操作员凭据','synthetic-key');
  const button=h.find('button','采用恢复核对并接续片段 1');assert.equal(button.props.disabled,false);
  button.props.onClick();button.props.onClick();await h.flush();
  assert.equal(submitted.length,1);assert.equal(submitted[0].body.start.expected_fingerprint,'normal-proof');
  assert.equal(submitted[0].body.start.scene_plan_id,'S');assert.equal(submitted[0].options.headers['x-content-os-execution-admission-key'],'synthetic-key');
  assert.match(h.text(),/已有接续任务，原额度保持不变/);assert.equal(h.input('操作员凭据').props.value,'');
  const reloaded=f.create({request});await reloaded.flush();assert.match(reloaded.text(),/已有接续任务，原额度保持不变/);
  assert.equal(submitted.length,1);h.unmount();reloaded.unmount();
});

