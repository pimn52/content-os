import test from 'node:test';
import assert from 'node:assert/strict';
import { componentHarness } from './hook-harness.mjs';
const file=decodeURI(new URL('../src/VoiceEvaluationHumanReview.tsx',import.meta.url).pathname).replace(/^\/([A-Z]:)/,'$1');
const labels=['声线相似度','自然度','重音','语速','停顿','节奏'];
function fixture(eligible=true) {
  const calls=[];
  let state={project_id:'p1',audio_id:'a1',state:eligible?'eligible':'blocked',current_eligible:eligible,
    reasons:eligible?[]:['voice_human_review_verified_terminal_qa_required'],record:null,
    expected_qa_fingerprint:eligible?'f1':undefined,audio_sha256:'a'.repeat(64),target_text:'exact unchanged words'};
  const request=async(path,options)=>{
    calls.push({path,options});
    if(options?.method==='POST'){
      const review=JSON.parse(options.body);
      state={...state,state:'recorded',record:{id:'record1',project_id:'p1',audio_id:'a1',audio_sha256:state.audio_sha256,
        review:{...review,reviewed_at:'2026-10-12T00:00:00Z'}}};return structuredClone(state.record);
    }
    return structuredClone(state);
  };
  return {calls,request,create:props=>componentHarness(file,'VoiceEvaluationHumanReview',{projectId:'p1',audioId:'a1',qaState:'verified',request,...props}),
    change:next=>{state={...state,...next};}};
}
async function fill(h,rejected=false){
  for(const label of labels)await h.change(label,rejected&&label==='自然度'?'needs_revision':'pass');
  await h.change('人审证据引用','explicit-user-feedback');
  h.find('label','具体判断或硬伤').props.children.find(node=>node?.type==='textarea').props.onChange({target:{value:'exact explicit feedback'}});await h.flush();
  h.input('确认上述判断').props.onChange({target:{checked:true}});await h.flush();
}
for(const rejected of [false,true])test('explicit '+(rejected?'negative':'positive')+' six-dimension review saves once and reloads without provider work',async()=>{
  const f=fixture(),h=f.create();await h.flush();
  for(const label of labels)assert.equal(h.input(label).props.value,'');
  assert.equal(h.find('button','保存这条音频的人审').props.disabled,true);
  assert.equal(f.calls.some(c=>c.options?.method==='POST'),false);
  await fill(h,rejected);const click=h.find('button','保存这条音频的人审').props.onClick;click();click();await h.flush();
  const posts=f.calls.filter(c=>c.options?.method==='POST');assert.equal(posts.length,1);
  const body=JSON.parse(posts[0].options.body);assert.equal(body.approved,!rejected);
  assert.equal(body.expected_qa_fingerprint,'f1');assert.equal(body.purpose,'internal_evaluation');
  assert.match(h.text(),rejected?/需要修改/:/六项通过/);assert.match(h.text(),/生产使用仍需单独准入/);
  assert.equal(h.nodes('select').length,0);h.unmount();
  const reload=f.create();await reload.flush();assert.match(reload.text(),rejected?/需要修改/:/六项通过/);
  assert.equal(f.calls.filter(c=>c.options?.method==='POST').length,1);reload.unmount();
});
test('pending or failed technical QA is blocked with no review submit',async()=>{
  const f=fixture(false),h=f.create({qaState:'failed'});await h.flush();
  assert.equal(h.nodes('select').length,0);assert.match(h.text(),/verified_terminal_qa_required/);
  assert.equal(f.calls.filter(c=>c.options?.method==='POST').length,0);h.unmount();
});
test('a changed technical fingerprint resets judgments and confirmation',async()=>{
  const f=fixture(),h=f.create();await h.flush();await fill(h);
  f.change({expected_qa_fingerprint:'f2'});await h.click('刷新人审记录');
  for(const label of labels)assert.equal(h.input(label).props.value,'');
  assert.equal(h.input('确认上述判断').props.checked,false);
  assert.equal(h.find('button','保存这条音频的人审').props.disabled,true);h.unmount();
});
test('project change discards late old responses and resets the form',async()=>{
  let resolve;const request=path=>path.includes('/p1/')?new Promise(r=>{resolve=r;}):Promise.resolve({project_id:'p2',audio_id:'a2',state:'blocked',current_eligible:false,reasons:['revoked'],record:null});
  const h=componentHarness(file,'VoiceEvaluationHumanReview',{projectId:'p1',audioId:'a1',qaState:'verified',request});await h.flush();
  assert.match(h.text(),/人审记录：读取中/);assert.doesNotMatch(h.text(),/尚未记录/);
  h.setProps({projectId:'p2',audioId:'a2'});await h.flush();
  resolve({project_id:'p1',audio_id:'a1',state:'eligible',current_eligible:true,reasons:[],record:null,expected_qa_fingerprint:'old'});await h.flush();
  assert.match(h.text(),/revoked/);assert.equal(h.nodes('select').length,0);h.unmount();
});
test('historical approval remains visible when current authority is revoked',async()=>{
  const f=fixture(),h=f.create();await h.flush();await fill(h);await h.click('保存这条音频的人审');
  f.change({current_eligible:false,state:'blocked',reasons:['provider_use_revoked'],expected_qa_fingerprint:undefined});await h.click('刷新人审记录');
  assert.match(h.text(),/六项通过/);assert.match(h.text(),/当前记录权限：不可用/);
  assert.match(h.text(),/provider_use_revoked/);assert.equal(h.nodes('select').length,0);h.unmount();
});
test('a failed refresh removes stale submit authority',async()=>{
  const f=fixture();let fail=false;const request=(...args)=>fail?Promise.reject(new Error('offline')):f.request(...args);
  const h=f.create({request});await h.flush();await fill(h);fail=true;await h.click('刷新人审记录');
  assert.match(h.text(),/offline/);assert.equal(h.nodes('select').length,0);h.unmount();
});
