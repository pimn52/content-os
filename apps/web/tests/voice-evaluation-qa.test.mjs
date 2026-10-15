import test from 'node:test';
import assert from 'node:assert/strict';
import { componentHarness } from './hook-harness.mjs';
const file = decodeURI(new URL('../src/VoiceEvaluationQA.tsx', import.meta.url).pathname).replace(/^\/([A-Z]:)/, '$1');
function fixture(state = 'ready') {
  const calls = [];
  let status = { project_id:'p1', audio_id:'a1', state, reasons:state==='blocked'?['voice_qa_current_asr_admission_missing']:[],
    choices:state==='ready'?[{admission_id:'grant',label:'Local single assessment'}]:[], target_text:'exact retained text',technical_ready:false };
  const request = async (path,options) => {
    calls.push({path,options});
    if (options?.method==='POST') { status={...status,state:'queued',job_status:'pending',choices:[]}; return {project_id:'p1'}; }
    return structuredClone(status);
  };
  return {calls,request,create:props=>componentHarness(file,'VoiceEvaluationQA',{projectId:'p1',audioId:'a1',request,...props}),
    complete:outcome=>{ status={...status,state:'completed',job_status:'completed',call_status:'completed',qa_state:outcome,technical_ready:outcome==='verified'}; }};
}
test('selection never dispatches; explicit single submit binds exact audio/target/grant and reload reads outcome',async()=>{
  const f=fixture(), h=f.create();await h.flush();
  assert.equal(f.calls.filter(c=>c.options?.method==='POST').length,0);
  assert.equal(h.find('button','提交一次独立 QA').props.disabled,true);
  await h.change('独立 QA 准入','grant');
  assert.equal(f.calls.filter(c=>c.options?.method==='POST').length,0);
  const click=h.find('button','提交一次独立 QA').props.onClick;click();click();await h.flush();
  const posts=f.calls.filter(c=>c.options?.method==='POST');assert.equal(posts.length,1);
  const body=JSON.parse(posts[0].options.body);
  assert.equal(body.narration_audio_id,'a1');assert.equal(body.target_text,'exact retained text');
  assert.equal(body.use_admission_id,'grant');assert.equal(body.purpose,'internal_evaluation');
  f.complete('verified');await h.click('刷新独立 QA 状态');assert.match(h.text(),/声线、自然度与发布质量请查看各自人审和准入结果/);
  h.unmount();const reload=f.create();await reload.flush();assert.match(reload.text(),/技术 QA 已通过/);
  assert.equal(f.calls.filter(c=>c.options?.method==='POST').length,1);reload.unmount();
});
for (const outcome of ['verified','failed']) test('terminal '+outcome+' refresh notifies owner once without another POST',async()=>{
  const f=fixture(),received=[],h=f.create({onResult:id=>received.push(id)});await h.flush();
  assert.deepEqual(received,[]);f.complete(outcome);await h.click('刷新独立 QA 状态');
  assert.deepEqual(received,['a1']);await h.click('刷新独立 QA 状态');assert.deepEqual(received,['a1']);
  assert.equal(f.calls.filter(c=>c.options?.method==='POST').length,0);h.unmount();
});
for(const state of ['blocked','reconciliation_required']) test(state+' exposes reason without retry/submit',async()=>{
  const f=fixture(state),h=f.create();await h.flush();
  assert.equal(h.nodes('button').some(b=>String(b.props.children).includes('提交')),false);
  assert.match(h.text(),state==='blocked'?/voice_qa_current_asr_admission_missing/:/不会自动再次识别/);
  assert.equal(f.calls.filter(c=>c.options?.method==='POST').length,0);h.unmount();
});
test('old project response is discarded and failed technical result stays explicit',async()=>{
  let resolve;
  const request=path=>path.includes('/p1/')?new Promise(r=>{resolve=r;}):Promise.resolve({project_id:'p2',audio_id:'a2',state:'completed',reasons:[],choices:[],qa_state:'failed',technical_ready:false});
  const h=componentHarness(file,'VoiceEvaluationQA',{projectId:'p1',audioId:'a1',request});await h.flush();
  h.setProps({projectId:'p2',audioId:'a2'});await h.flush();
  resolve({project_id:'p1',audio_id:'a1',state:'ready',reasons:[],choices:[],technical_ready:true});await h.flush();
  assert.match(h.text(),/技术 QA 未通过/);assert.doesNotMatch(h.text(),/技术 QA 已通过/);h.unmount();
});
