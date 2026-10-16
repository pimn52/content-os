import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { componentHarness } from './hook-harness.mjs';
const file = fileURLToPath(new URL('../src/ProductionPanel.tsx', import.meta.url));
const modules = Object.fromEntries(['ProductionVoicePanel', 'ProductionTalkingPanel', 'PresentationRecoveryPanel', 'EvaluationMasterPanel', 'EvaluationTalkingPanel', 'EvaluationCompositionPanel'].map(name => [`./${name}`, { [name]: () => null }]));
function PurposeControl() {}
modules['./ExecutionPurposeControl'] = { ExecutionPurposeControl: PurposeControl, commercialChoice: () => ({purpose: 'commercial_production', use_admission_ids: []}) };
const plan = { project_id: 'P', draft_version: 1, script_revision: 1, fingerprint: 'a'.repeat(64), status: 'blocked', evidence_level: 'fixture', cost_estimate: { known_amount: '0', currency: 'USD', unknown_cost_count: 0 }, estimated_local_runtime_ms: null, estimated_user_active_minutes: null, stop_reasons: [], authorization_needs: [], actions: [], scenes: [] };
function setup(preflightOverride) {
  const calls = [];
  const h = componentHarness(file, 'ProductionPanel', { projectId: 'P', draftVersion: 1, scriptRevision: 1, hasScenes: true,
    request: async (path, options) => { const body = options?.body && JSON.parse(options.body); calls.push({ path, body });
      if (path.endsWith('production-preflight')) return preflightOverride ? preflightOverride() : plan;
      return { id: 'R', project_id: 'P', status: 'cancelled', talking_dependencies: [], waiting_stop_reasons: [], voice_qa_auto_stop_reasons: [] }; },
  }, { modules });
  return { h, calls };
}
test('normal preflight and start carry identical subject-first tokens; changing choice requires fresh preflight', async () => {
  const s = setup(); await s.h.flush(); await s.h.change('成片人物呈现', 'subject_first'); await s.h.click('预检整条方案');
  const before = s.calls[0].body.style_tokens; assert.equal(before.name, 'content-os-subject-first');
  await s.h.click('保存 / 启动'); assert.deepEqual(s.calls[1].body.style_tokens, before);
  assert.equal(s.calls[1].body.expected_fingerprint, plan.fingerprint); assert.equal(s.calls[1].body.talking_review_policy_version, 2);
  await s.h.change('成片人物呈现', 'default'); assert.equal(s.h.nodes('button').some(node => String(node.props.children).includes('保存 / 启动')), false);
  await s.h.click('预检整条方案'); assert.deepEqual(s.calls[2].body.style_tokens, {});
});
test('late preflight cannot reinstate a changed presentation; project change clears choice', async () => {
  let resolve; const pending = new Promise(r => { resolve = r; }); const s = setup(() => pending);
  await s.h.flush(); await s.h.click('预检整条方案'); await s.h.change('成片人物呈现', 'subject_first');
  resolve(plan); await s.h.flush(); assert.equal(s.h.nodes('button').some(node => String(node.props.children).includes('保存 / 启动')), false);
  assert.equal(s.h.find('button', '预检整条方案').props.disabled, false);
  s.h.setProps({ projectId: 'Q' }); await s.h.flush(); assert.equal(s.h.input('成片人物呈现').props.value, 'default');
});

test('normal editorial choice carries the existing profile through preflight and start', async () => {
  const s = setup(); await s.h.flush(); await s.h.change('成片人物呈现', 'editorial');
  await s.h.click('预检整条方案'); await s.h.click('保存 / 启动');
  assert.equal(s.calls[0].body.style_tokens.name, 'content-os-editorial');
  assert.deepEqual(s.calls[1].body.style_tokens, s.calls[0].body.style_tokens);
  assert.equal(s.calls[1].body.expected_fingerprint, plan.fingerprint);
});

test('unsaved editorial changes invalidate the inspected plan and block preflight until save', async () => {
  const s = setup(); await s.h.flush(); await s.h.click('预检整条方案');
  s.h.setProps({draftDirty: true}); await s.h.flush();
  assert.match(s.h.text(), /先保存/);
  assert.equal(s.h.find('button', '预检整条方案').props.disabled, true);
  assert.equal(s.h.nodes('button').some(node => String(node.props.children).includes('保存 / 启动')), false);
  assert.equal(s.calls.length, 1);
  s.h.setProps({draftDirty: false, draftVersion: 2}); await s.h.flush();
  assert.equal(s.h.find('button', '预检整条方案').props.disabled, false);
  assert.equal(s.h.nodes('button').some(node => String(node.props.children).includes('保存 / 启动')), false);
});

test('purpose and selected admission travel to preflight, invalidate old results and keep evaluation start closed', async () => {
  const s = setup(); await s.h.flush(); await s.h.click('预检整条方案');
  const change = value => s.h.nodes(PurposeControl)[0].props.onChange(value);
  change({purpose: 'internal_evaluation', use_admission_ids: ['receipt-A']}); await s.h.flush();
  assert.equal(s.h.nodes('button').some(node => String(node.props.children).includes('保存 / 启动')), false);
  await s.h.click('预检整条方案');
  assert.equal(s.calls[1].body.purpose, 'internal_evaluation');
  assert.deepEqual(s.calls[1].body.use_admission_ids, ['receipt-A']);
  assert.equal(s.h.find('button', '保存 / 启动').props.disabled, true);
  // Even a direct handler call cannot dispatch the structurally closed lane.
  s.h.find('button', '保存 / 启动').props.onClick(); await s.h.flush(); assert.equal(s.calls.length, 2);
  change({purpose: 'commercial_production', use_admission_ids: []}); await s.h.flush();
  await s.h.click('预检整条方案'); await s.h.click('保存 / 启动');
  assert.equal(s.calls[3].body.purpose, s.calls[2].body.purpose);
  assert.deepEqual(s.calls[3].body.use_admission_ids, []);
});

test('late preflight cannot restore changed authority; project changes reset to default commercial', async () => {
  let resolve; const s = setup(() => new Promise(r => { resolve = r; })); await s.h.flush();
  await s.h.click('预检整条方案');
  s.h.nodes(PurposeControl)[0].props.onChange({purpose: 'internal_evaluation', use_admission_ids: ['A']}); await s.h.flush();
  resolve(plan); await s.h.flush();
  assert.equal(s.h.nodes('button').some(node => String(node.props.children).includes('保存 / 启动')), false);
  s.h.setProps({projectId: 'Q'}); await s.h.flush();
  assert.equal(s.h.nodes(PurposeControl)[0].props.value.purpose, 'commercial_production');
  assert.equal(s.h.nodes(PurposeControl)[0].props.value.use_admission_ids.length, 0);
});


test('approved retained media uses ordinary preflight/start without generation panels',async()=>{
  const s=setup(()=>({...plan,status:'ready_for_authorized_dispatch',retained_media:{master_audio_id:'A',talking_runs:{S:'T'}}}));
  await s.h.flush();s.h.nodes(PurposeControl)[0].props.onChange({purpose:'internal_evaluation',use_admission_ids:[]});await s.h.flush();
  await s.h.click('预检整条方案');assert.equal(s.h.find('button','保存 / 启动').props.disabled,false);
  await s.h.click('保存 / 启动');assert.equal(s.calls.length,2);
  assert.match(s.calls[1].path,/production-runs$/);assert.equal(s.calls[1].body.purpose,'internal_evaluation');
  assert.equal(s.calls.some(c=>/evaluation-master|evaluation-talking|voice-drafts|evaluation-composition/.test(c.path)),false);
  s.h.unmount();
});
