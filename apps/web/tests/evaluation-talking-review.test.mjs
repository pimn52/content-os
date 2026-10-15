import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { componentHarness } from './hook-harness.mjs';
const file=fileURLToPath(new URL('../src/EvaluationTalkingReview.tsx',import.meta.url));
const dimensions=['visible_sync','identity','artifacts','source_performance','continuity','publishability'];
function fixture(){
  const calls=[];let changed=0;
  let state={preview_asset_id:'A',preview_sha256:'a'.repeat(64),planned_origin_sha256:'b'.repeat(64),review_policy_version:2,readiness:'ready_for_human_continuity_review',blockers:[],continuity_review_state:'not_submitted',continuity_review_evidence_reference:null,review_concerns:[]};
  const request=async(path,options)=>{calls.push({path,options});if(options?.method==='POST')return {};return structuredClone(state);};
  const create=changes=>componentHarness(file,'EvaluationTalkingReview',{projectId:'P',operationId:'O',revision:'1',request,onChange:()=>changed++,...changes});
  return {calls,request,create,setState:values=>state={...state,...values},changes:()=>changed};
}
const passed=()=>({...Object.fromEntries(dimensions.map(key=>[key,'pass'])),verdict:'pass',evidence:'fixture-only:watched',inspected:'on'});
test('explicit six-pass exact preview review writes once and does not auto-admit',async()=>{
  const f=fixture(),h=f.create();await h.flush();assert.equal(f.calls.some(c=>c.options?.method==='POST'),false);
  assert.equal(h.nodes('select').every(node=>node.props.defaultValue==='unknown'),true);
  assert.equal(h.nodes('input').find(node=>node.props.type==='checkbox').props.style.width,'auto');
  await h.submit('保存人物六项判断',passed());
  const writes=f.calls.filter(c=>c.options?.method==='POST');assert.equal(writes.length,1);const body=JSON.parse(writes[0].options.body);
  assert.equal(body.review_policy_version,2);assert.equal(body.preview_asset_id,'A');assert.equal(body.preview_sha256,'a'.repeat(64));assert.equal(body.approved,true);assert.equal(f.changes(),1);
  assert.equal(writes[0].path,'/projects/P/evaluation-talking/operations/O/review');
});
test('unknown dimension or missing inspection never becomes approval',async()=>{
  const f=fixture(),h=f.create();await h.flush();await h.submit('保存人物六项判断',{...passed(),visible_sync:'unknown'});
  assert.match(h.text(),/未知不能/);await h.submit('保存人物六项判断',{...passed(),inspected:null});assert.equal(f.calls.some(c=>c.options?.method==='POST'),false);
});
test('negative review retains named failed dimension and concrete finding',async()=>{
  const f=fixture(),h=f.create();await h.flush();await h.submit('保存人物六项判断',{...passed(),verdict:'fail',artifacts:'fail',findings:'fixture visible artifact'});
  const body=JSON.parse(f.calls.find(c=>c.options?.method==='POST').options.body);assert.equal(body.approved,false);assert.deepEqual(body.scoped_findings,[{dimension:'artifacts',reason:'fixture visible artifact'}]);
});
test('saved approval only admits on explicit click; pending concern blocks admit and remains answerable',async()=>{
  const f=fixture();f.setState({continuity_review_state:'approved',continuity_review_evidence_reference:'saved proof'});
  const h=f.create();await h.flush();assert.equal(h.nodes('form').length,1);assert.equal(f.calls.some(c=>c.options?.method==='POST'),false);
  await h.click('将已审核人物片段准入为 TalkingRun');assert.equal(f.calls.find(c=>c.options?.method==='POST').path.endsWith('/admit'),true);
  f.setState({blockers:['talking_local_review_pending:K'],review_concerns:[{id:'K',finding:{dimension:'artifacts',reason:'inspect frame'},evidence_reference:'fixture',answer:null}]});
  const pending=f.create();await pending.flush();assert.equal(pending.find('button','将已审核人物片段准入为 TalkingRun').props.disabled,true);
  await pending.submit('保存问题复核',{verdict:'pass',reason:'checked frame',evidence:'fixture-only:answer'});
  const body=JSON.parse(f.calls.find(c=>c.path.endsWith('/concern-answers')).options.body);assert.equal(body.answers[0].concern_id,'K');assert.equal(body.preview_sha256,'a'.repeat(64));
});
test('policy1 and failed refresh remove submit authority without pretending history passed',async()=>{
  const f=fixture();f.setState({review_policy_version:1});const h=f.create();await h.flush();assert.equal(h.nodes('form').length,0);assert.match(h.text(),/材料不完整/);
  let fail=false;f.setState({review_policy_version:2});const other=f.create({request:(path,options)=>fail?Promise.reject(new Error('revoked')):f.request(path,options)});await other.flush();
  fail=true;await other.click('重新核对评审材料');assert.equal(other.nodes('form').length,0);assert.match(other.text(),/revoked/);
});
test('project change discards old review response and starts unknown judgments',async()=>{
  const f=fixture();let resolve;const pending=new Promise(r=>resolve=r);
  const h=f.create({request:(path,options)=>path.includes('/P/')?pending:f.request(path,options)});await h.flush();h.setProps({projectId:'new'});await h.flush();
  resolve({review_policy_version:1});await h.flush();assert.equal(h.nodes('form').length,2);assert.equal(h.nodes('select').every(node=>node.props.defaultValue==='unknown'),true);assert.equal(f.calls.some(c=>c.options?.method==='POST'),false);
});
test('concern registration requires a named defect and exact preview, preserving its key on retry',async()=>{
  const f=fixture(),h=f.create();await h.flush();
  await h.submit('登记预览问题',{dimension:'unknown',reason:'frame defect',evidence:'frame 10'});
  assert.equal(f.calls.some(c=>c.options?.method==='POST'),false);
  await h.submit('登记预览问题',{dimension:'artifacts',reason:'frame defect',evidence:'frame 10'});
  await h.submit('登记预览问题',{dimension:'artifacts',reason:'frame defect',evidence:'frame 10'});
  const writes=f.calls.filter(c=>c.path.endsWith('/concerns'));
  assert.equal(writes.length,2);
  const first=JSON.parse(writes[0].options.body),second=JSON.parse(writes[1].options.body);
  assert.deepEqual(first,second);assert.equal(first.preview_asset_id,'A');assert.equal(first.preview_sha256,'a'.repeat(64));
  assert.deepEqual(first.finding,{dimension:'artifacts',reason:'frame defect'});assert.ok(first.idempotency_key);
});
