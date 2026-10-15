import test from 'node:test';
import assert from 'node:assert/strict';
import { componentHarness } from './hook-harness.mjs';
const file = decodeURI(new URL('../src/VoiceDraftPanel.tsx', import.meta.url).pathname).replace(/^\/([A-Z]:)/, '$1');
const choiceControl = function ExecutionPurposeControl() {};
const audition = function AudioAudition() {};
const evaluationQA = function VoiceEvaluationQA() {};
function fixture(unknown = false, saved = false) {
  const storage = new Map(), calls = [];
  let state = { job_id: 'job-1', project_id: 'project-1', state: 'pending', job_status: 'pending', reasons: [], error_code: null, outputs: [], recovery_action: null };
  const request = async (path, options) => {
    calls.push({ path, options });
    if (path === '/voice-profiles') return [{ id: 'profile-1', name: 'Synthetic', provider: 'omnivoice', consent: { confirmed: true } }];
    if (path === '/execution-settings/capabilities') return [{ id: 'cap-1', capability: 'voice', provider: 'omnivoice', mode: 'local', model: 'synthetic', readiness: unknown ? 'configured' : 'verified', quality_status: unknown ? 'unknown' : 'verified' }];
    if (path.includes('/reference-windows')) return [{ selection: { source_content_hash: 'a'.repeat(64) }, transcript: 'synthetic reference', duration_ms: 1000 }];
    if (path.endsWith('/voice-jobs')) return { id: 'job-1', project_id: 'project-1' };
    if (path.endsWith('/voice-draft-jobs')) return saved && path.includes('/project-1/') ? [{ job_id: 'job-1', project_id: 'project-1', policy: 'voice-calibration-v1', job_status: state.job_status, created_at: '2026-10-11T00:00:00Z', text_preview: 'Saved private draft' }] : [];
    if (path.includes('/voice-draft-jobs/')) return structuredClone(state);
    throw new Error('unexpected fixture path ' + path);
  };
  const environment = { localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    modules: { './ExecutionPurposeControl': { ExecutionPurposeControl: choiceControl, commercialChoice: () => ({ purpose: 'commercial_production', use_admission_ids: [] }) }, './AudioAudition': { AudioAudition: audition }, './VoiceEvaluationQA': { VoiceEvaluationQA: evaluationQA }, './VoiceEvaluationHumanReview': { VoiceEvaluationHumanReview: function VoiceEvaluationHumanReview() {} } } };
  const create = props => componentHarness(file, 'VoiceDraftPanel', { projectId: 'project-1', request, ...props }, environment);
  return { create, calls, storage, request, complete: (generationBlocked = false, qa = undefined) => { state = { ...state, state: 'completed', job_status: 'completed', outputs: [{ audio_id: 'audio-1', qa_state: 'pending', qa_consumption: qa }], generation_eligibility: generationBlocked ? { state: 'blocked', reasons: ['provider_use_evidence_not_current'], dispatch_authorized: false } : undefined }; },
    completeQa: outcome => { state = {...state,outputs:state.outputs.map(a=>({...a,qa_state:outcome}))}; },
    block: () => { state = { ...state, state: 'blocked', reasons: ['provider_call_reconciliation_required'], recovery_action: 'reconcile_recorded_call' }; } };
}
for (const outcome of ['verified','failed']) test('independent '+outcome+' result refreshes the saved draft snapshot without TTS',async()=>{
  const f=fixture(false,true);f.complete();const h=f.create();await h.flush();await h.change('已保存的语音草稿','job-1');
  assert.match(h.text(),/技术 QA 状态：pending/);f.completeQa(outcome);
  h.nodes(evaluationQA)[0].props.onResult('audio-1');await h.flush();
  assert.match(h.text(),new RegExp('技术 QA 状态：'+outcome));assert.doesNotMatch(h.text(),/技术 QA 状态：pending/);
  assert.equal(f.calls.filter(c=>c.options?.method==='POST').length,0);h.unmount();
});
test('late result-triggered status refresh cannot restore a cleared draft',async()=>{
  const f=fixture(false,true);f.complete();let delay=false,resolve;
  const request=(path,options)=>delay&&path.includes('/voice-draft-jobs/job-1')?new Promise(r=>{resolve=r;}):f.request(path,options);
  const h=f.create({request});await h.flush();await h.change('已保存的语音草稿','job-1');
  delay=true;h.nodes(evaluationQA)[0].props.onResult('audio-1');await h.flush();
  await h.change('已保存的语音草稿','');
  resolve({job_id:'job-1',project_id:'project-1',state:'completed',job_status:'completed',reasons:[],outputs:[{audio_id:'audio-1',qa_state:'verified'}]});await h.flush();
  assert.equal(h.nodes(evaluationQA).length,0);assert.equal(h.nodes(audition).length,0);
  assert.equal(f.calls.filter(c=>c.options?.method==='POST').length,0);h.unmount();
});
async function select(h) {
  await h.flush();
  h.nodes(choiceControl)[0].props.onChange({ purpose: 'internal_evaluation', use_admission_ids: ['receipt-1'] }); await h.flush();
  await h.change('草稿声线', 'profile-1'); await h.change('精确 Voice 配置', 'cap-1'); await h.change('授权参考片段', '0');
  h.nodes('textarea')[0].props.onChange({ target: { value: 'fixture new words' } }); await h.flush();
  await h.change('本次生成授权引用', 'fixture authorization');
}

test('completed historical draft retains audition while new generation is explicitly blocked', async () => {
  const f = fixture(false, true); f.complete(true);
  const h = f.create(); await h.flush(); await h.change('已保存的语音草稿', 'job-1');
  assert.equal(h.nodes(audition)[0].props.audioId, 'audio-1');
  assert.match(h.text(), /当前配置不能用于新的生成：provider_use_evidence_not_current/);
  assert.equal(f.calls.filter(value => value.options?.method === 'POST').length, 0); h.unmount();
});

for (const state of ['blocked', 'scope_check_passed']) test(`QA ${state} remains separate from audition and execution`, async () => {
  const f = fixture(false, true);
  f.complete(true, { state, purpose: 'commercial_production', reasons: state === 'blocked' ? ['evaluation_scope_not_commercial'] : [], execution_configuration: 'not_checked', dispatch_authorized: false });
  const h = f.create(); await h.flush(); await h.change('已保存的语音草稿', 'job-1');
  assert.equal(h.nodes(audition)[0].props.audioId, 'audio-1');
  assert.match(h.text(), /试听权限不代表 QA 可执行/);
  assert.match(h.text(), state === 'blocked' ? /用途检查未通过：evaluation_scope_not_commercial/ : /执行配置仍未检查/);
  assert.equal(f.calls.filter(value => value.options?.method === 'POST').length, 0); h.unmount();
});
test('saved backend draft is discoverable without bookmark and selection/reload never generates', async () => {
  const f = fixture(false, true); f.complete(); const h = f.create(); await h.flush();
  assert.match(h.text(), /Saved private draft/); assert.equal(h.nodes(audition).length, 0);
  await h.change('已保存的语音草稿', 'job-1');
  assert.equal(h.nodes(audition)[0].props.audioId, 'audio-1');
  assert.equal(f.storage.get('content-os:voice-draft:project-1'), 'job-1');
  await h.change('已保存的语音草稿', ''); assert.equal(h.nodes(audition).length, 0);
  await h.change('已保存的语音草稿', 'job-1'); h.unmount();
  const restored = f.create(); await restored.flush(); assert.equal(restored.nodes(audition)[0].props.audioId, 'audio-1');
  restored.setProps({ projectId: 'project-2' }); await restored.flush();
  assert.equal(restored.nodes(audition).length, 0); assert.doesNotMatch(restored.text(), /Saved private draft/);
  assert.equal(f.calls.filter(value => value.options?.method === 'POST').length, 0); restored.unmount();
});

test('late saved-draft list cannot cross project and list errors remain explicit', async () => {
  let resolveOld;
  const request = async path => {
    if (path === '/projects/project-1/voice-draft-jobs') return new Promise(resolve => { resolveOld = resolve; });
    if (path === '/projects/project-2/voice-draft-jobs') throw new Error('history unavailable');
    return [];
  };
  const f = fixture(), h = f.create({ request }); await h.flush();
  h.setProps({ projectId: 'project-2' }); await h.flush();
  resolveOld([{ job_id: 'old', project_id: 'project-1', text_preview: 'Old private text' }]); await h.flush();
  assert.doesNotMatch(h.text(), /Old private text/); assert.match(h.text(), /草稿列表读取失败：history unavailable/);
  assert.equal(h.nodes(audition).length, 0); h.unmount();
});

test('explicit normal draft submit carries exact scope/reference and reload retains pending audition without quality controls', async () => {
  const f = fixture(), h = f.create(); await h.flush();
  assert.equal(h.find('button', '创建一条评估草稿').props.disabled, true);
  await select(h); await h.click('创建一条评估草稿');
  const posts = f.calls.filter(c => c.options?.method === 'POST'); assert.equal(posts.length, 1);
  const body = JSON.parse(posts[0].options.body);
  assert.equal(body.purpose, 'internal_evaluation'); assert.equal(body.execution_capability_profile_id, 'cap-1');
  assert.deepEqual(body.use_admission_ids, ['receipt-1']); assert.equal(body.reference_window.source_content_hash, 'a'.repeat(64));
  assert.ok(body.idempotency_key.length <= 500); assert.ok(!body.idempotency_key.includes(body.text));
  assert.equal(f.storage.get('content-os:voice-draft:project-1'), 'job-1');
  f.complete(); await h.click('刷新草稿状态');
  assert.equal(h.nodes(audition)[0].props.audioId, 'audio-1'); assert.match(h.text(), /人审单独记录，生产使用仍需准入/);
  h.unmount(); const restored = f.create(); await restored.flush();
  assert.equal(restored.nodes(audition)[0].props.audioId, 'audio-1');
  assert.equal(restored.nodes(choiceControl)[0].props.value.purpose, 'commercial_production');
  restored.unmount();
});
test('explicit status recovery shows existing-call reconciliation and offers no generation retry', async () => {
  const f = fixture(); f.storage.set('content-os:voice-draft:project-1', 'job-1'); f.block();
  const h = f.create(); await h.flush(); assert.match(h.text(), /reconcile_recorded_call/);
  assert.equal(f.calls.filter(c => c.options?.method === 'POST').length, 0);
  assert.equal(h.nodes('button').some(b => String(b.props.children).includes('重试')), false);
  h.unmount();
});
test('switching project clears purpose, choices, current task and old preview', async () => {
  const f = fixture(), h = f.create(); await select(h); await h.click('创建一条评估草稿');
  f.complete(); await h.click('刷新草稿状态'); h.setProps({ projectId: 'project-2' }); await h.flush();
  assert.equal(h.nodes(audition).length, 0); assert.equal(h.input('草稿声线').props.value, '');
  assert.deepEqual(JSON.parse(JSON.stringify(h.nodes(choiceControl)[0].props.value)), { purpose: 'commercial_production', use_admission_ids: [] });
  assert.equal(h.find('button', '创建一条评估草稿').props.disabled, true); h.unmount();
});

test('first validation is explicit, resets receipts and submits unknown quality without promotion', async () => {
  const f = fixture(true), h = f.create(); await select(h);
  assert.equal(h.find('button', '创建一条评估草稿').props.disabled, true);
  await h.change('草稿验证方式', 'voice-calibration-v1');
  assert.deepEqual(JSON.parse(JSON.stringify(h.nodes(choiceControl)[0].props.value.use_admission_ids)), []);
  h.nodes(choiceControl)[0].props.onChange({purpose: 'internal_evaluation', use_admission_ids: ['calibration-receipt']}); await h.flush();
  assert.equal(h.find('button', '创建一条评估草稿').props.disabled, false);
  await h.click('创建一条评估草稿');
  const body = JSON.parse(f.calls.find(c => c.options?.method === 'POST').options.body);
  assert.equal(body.draft_policy, 'voice-calibration-v1'); assert.ok(body.idempotency_key.length <= 500);
  assert.match(h.text(), /不自动确认声线质量/);
  h.setProps({projectId: 'project-2'}); await h.flush();
  assert.equal(h.input('草稿验证方式').props.value, 'voice-draft-v1'); h.unmount();
});

const savedPlan = { version: 7, scriptRevision: 3, text: '  Saved full copy\n\nSecond scene.  ', hasScenes: true, dirty: false };
test('saved-plan workflow uses exact full copy, snapshot and existing QA/review recovery without another POST', async () => {
  const f=fixture(), h=f.create({planSource:savedPlan});await select(h);
  assert.equal(h.nodes('textarea')[0].props.value,savedPlan.text);assert.equal(h.nodes('textarea')[0].props.readOnly,true);
  await h.click('创建一条评估草稿');const posts=f.calls.filter(c=>c.options?.method==='POST');const body=JSON.parse(posts[0].options.body);
  assert.equal(body.text,savedPlan.text);assert.deepEqual(body.saved_plan_source,{version:7,script_revision:3});
  f.complete();await h.click('刷新草稿状态');assert.equal(h.nodes(evaluationQA).length,1);assert.equal(h.nodes(audition).length,1);
  h.unmount();const restored=f.create({planSource:savedPlan});await restored.flush();assert.equal(restored.nodes(evaluationQA).length,1);
  assert.equal(f.calls.filter(c=>c.options?.method==='POST').length,1);restored.unmount();
});
test('dirty/missing saved plan and changing version require saved input and fresh explicit receipts', async () => {
  const f=fixture(),h=f.create({planSource:{...savedPlan,dirty:true}});await select(h);
  assert.equal(h.find('button','创建一条评估草稿').props.disabled,true);assert.match(h.text(),/请先保存完整文案与场景方案/);
  h.setProps({planSource:{...savedPlan,hasScenes:false}});await h.flush();assert.equal(h.find('button','创建一条评估草稿').props.disabled,true);
  h.setProps({planSource:{...savedPlan,version:8}});await h.flush();assert.equal(h.nodes(choiceControl)[0].props.value.use_admission_ids.length,0);
  assert.equal(h.find('button','创建一条评估草稿').props.disabled,true);assert.equal(f.calls.filter(c=>c.options?.method==='POST').length,0);h.unmount();
});
test('late saved-plan creation cannot restore stale selection after copy or project changes',async()=>{
  const f=fixture();let resolve;const request=(path,options)=>options?.method==='POST'?new Promise(r=>{resolve=r;}):f.request(path,options);
  const h=f.create({request,planSource:savedPlan});await select(h);await h.click('创建一条评估草稿');
  h.setProps({planSource:{...savedPlan,text:'Changed full copy',version:8}});await h.flush();
  resolve({id:'old-job',project_id:'project-1'});await h.flush();assert.equal(h.nodes(audition).length,0);
  assert.equal(f.storage.has('content-os:voice-draft:project-1'),false);assert.equal(h.nodes(choiceControl)[0].props.value.use_admission_ids.length,0);h.unmount();
});
test('historical target mismatch stays reviewable and standalone creation preserves the old request',async()=>{
  const f=fixture(false,true);f.complete();const request=async(path,options)=>{const v=await f.request(path,options);return path.includes('/voice-draft-jobs/job-1')?{...v,target_text:'Older copy'}:v;};
  const h=f.create({request,planSource:savedPlan});await h.flush();await h.change('已保存的语音草稿','job-1');
  assert.match(h.text(),/此历史草稿与当前方案不匹配/);assert.equal(h.nodes(evaluationQA).length,1);
  await h.change('旁白文案来源','standalone');await select(h);await h.click('创建一条评估草稿');
  const body=JSON.parse(f.calls.find(c=>c.options?.method==='POST').options.body);assert.equal(body.saved_plan_source,undefined);assert.equal(body.text,'fixture new words');h.unmount();
});

test('asynchronously loaded plan does not discard same-project voice configuration discovery',async()=>{
  const f=fixture();let resolve;const request=(path,options)=>path==='/voice-profiles'?new Promise(r=>{resolve=r;}):f.request(path,options);
  const h=f.create({request,planSource:{...savedPlan,hasScenes:false,text:''}});await h.flush();
  h.setProps({planSource:savedPlan});await h.flush();resolve([{id:'profile-1',name:'Late authorized voice',provider:'omnivoice',consent:{confirmed:true}}]);await h.flush();
  assert.match(h.text(),/Late authorized voice/);assert.equal(f.calls.filter(c=>c.options?.method==='POST').length,0);h.unmount();
});
