import test from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {componentHarness} from './hook-harness.mjs';
const file=fileURLToPath(new URL('../src/EvaluationCompositionPanel.tsx',import.meta.url));
function fixture(){
  const calls=[];
  const request=async(path,options)=>{calls.push({path,options});return {};};
  return {calls,request,create:changes=>componentHarness(file,'EvaluationCompositionPanel',{projectId:'P',draftVersion:1,scriptRevision:1,draftDirty:false,request,...changes})};
}
test('operator adoption is separate from ordinary selection/start and clears credentials',async()=>{
  const f=fixture();let adopted=0;const h=f.create({onAdopt:()=>{adopted++;}});await h.flush();
  assert.equal(f.calls.length,0);
  await h.change('核对记录','use.json');await h.change('记录摘要','a'.repeat(64));await h.change('操作员凭据','synthetic-key');
  await h.click('采用核对结果');
  assert.equal(f.calls.length,1);assert.match(f.calls[0].path,/evaluation-composition\/admissions$/);
  assert.equal(JSON.parse(f.calls[0].options.body).review_reference,'use.json');
  assert.equal(adopted,1);assert.equal(h.input('操作员凭据').props.value,'');
  assert.equal(h.nodes('select').length,1);h.unmount();
});
test('dirty or changed project cannot adopt stale permission',async()=>{
  const f=fixture();const h=f.create();await h.flush();
  await h.change('核对记录','use.json');await h.change('记录摘要','a'.repeat(64));await h.change('操作员凭据','synthetic-key');
  h.setProps({draftDirty:true});await h.flush();
  assert.equal(h.find('button','采用核对结果').props.disabled,true);
  h.setProps({projectId:'Q',draftDirty:false});await h.flush();
  assert.equal(h.input('操作员凭据').props.value,'');assert.equal(f.calls.length,0);h.unmount();
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
  const h=f.create({request,enableGenerationRecovery:true});await h.flush();
  await h.change('恢复核对记录','review.json');await h.change('恢复记录摘要','a'.repeat(64));await h.change('操作员凭据','synthetic-key');
  const button=h.find('button','采用恢复核对并接续片段 1');assert.equal(button.props.disabled,false);
  button.props.onClick();button.props.onClick();await h.flush();
  assert.equal(submitted.length,1);assert.equal(submitted[0].body.start.expected_fingerprint,'normal-proof');
  assert.equal(submitted[0].body.start.scene_plan_id,'S');assert.equal(submitted[0].options.headers['x-content-os-execution-admission-key'],'synthetic-key');
  assert.match(h.text(),/已有接续任务，原额度保持不变/);assert.equal(h.input('操作员凭据').props.value,'');
  const reloaded=f.create({request,enableGenerationRecovery:true});await reloaded.flush();assert.match(reloaded.text(),/已有接续任务，原额度保持不变/);
  assert.equal(submitted.length,1);h.unmount();reloaded.unmount();
});

