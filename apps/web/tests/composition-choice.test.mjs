import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { componentHarness } from './hook-harness.mjs';
const file = fileURLToPath(new URL('../src/ProductionPanel.tsx', import.meta.url));
const modules = Object.fromEntries(['ProductionVoicePanel', 'ProductionTalkingPanel', 'PresentationRecoveryPanel','AutomaticProductionPanel'].map(name => [`./${name}`, { [name]: () => null }]));
const plan = { project_id: 'P', draft_version: 1, script_revision: 1, fingerprint: 'a'.repeat(64), status: 'blocked', evidence_level: 'fixture', cost_estimate: { known_amount: '0', currency: 'USD', unknown_cost_count: 0 }, estimated_local_runtime_ms: null, estimated_user_active_minutes: null, stop_reasons: [], authorization_needs: [], actions: [], scenes: [] };
function setup(preflightOverride, saved = []) {
  const calls = [];
  const h = componentHarness(file, 'ProductionPanel', { projectId: 'P', draftVersion: 1, scriptRevision: 1, hasScenes: true,
    request: async (path, options) => { if (path.includes('/production-runs?') && !options) return saved;
      const body = options?.body && JSON.parse(options.body); calls.push({ path, body, options });
      if (path.endsWith('production-preflight')) return preflightOverride ? preflightOverride() : plan;
      return { id: 'R', project_id: 'P', status: 'cancelled', talking_dependencies: [], waiting_stop_reasons: [], voice_qa_auto_stop_reasons: [] }; },
  }, { modules });
  return { h, calls };
}
test('current draft restores its latest backend Run without a browser bookmark', async () => {
  const saved = [{id:'R',project_id:'P',draft_version:1,script_revision:1,stage:'render',created_at:'2026-10-22T00:00:00Z'},
    {id:'OLD',project_id:'P',draft_version:0,script_revision:1,stage:'render',created_at:'2026-10-21T00:00:00Z'}];
  const s = setup(undefined, saved); await s.h.flush();
  assert.equal(s.h.input('当前草稿的已有执行').props.value, 'R');
  assert.equal(s.calls.filter(call => call.path.endsWith('/production-runs/R')).length, 1);
  assert.equal(s.calls.some(call => call.path.endsWith('/production-runs/OLD')), false);
  s.h.unmount();
});
test('normal preflight and start carry identical subject-first tokens; changing choice requires fresh preflight', async () => {
  const s = setup(); await s.h.flush(); await s.h.change('新执行的成片画面呈现', 'subject_first'); await s.h.click('预检整条方案');
  const before = s.calls[0].body.style_tokens; assert.equal(before.name, 'content-os-subject-first');
  await s.h.click('保存 / 启动'); assert.deepEqual(s.calls[1].body.style_tokens, before);
  assert.equal(s.calls[1].body.expected_fingerprint, plan.fingerprint); assert.equal(s.calls[1].body.talking_review_policy_version, 1);
  await s.h.change('新执行的成片画面呈现', 'default'); assert.equal(s.h.nodes('button').some(node => String(node.props.children).includes('保存 / 启动')), false);
  await s.h.click('预检整条方案'); assert.deepEqual(s.calls[2].body.style_tokens, {});
});
test('late preflight cannot reinstate a changed presentation; project change clears choice', async () => {
  let resolve; const pending = new Promise(r => { resolve = r; }); const s = setup(() => pending);
  await s.h.flush(); await s.h.click('预检整条方案'); await s.h.change('新执行的成片画面呈现', 'subject_first');
  resolve(plan); await s.h.flush(); assert.equal(s.h.nodes('button').some(node => String(node.props.children).includes('保存 / 启动')), false);
  assert.equal(s.h.find('button', '预检整条方案').props.disabled, false);
  s.h.setProps({ projectId: 'Q' }); await s.h.flush(); assert.equal(s.h.input('新执行的成片画面呈现').props.value, 'default');
});
test('normal editorial choice carries the existing profile through preflight and start', async () => {
  const s = setup(); await s.h.flush(); await s.h.change('新执行的成片画面呈现', 'editorial');
  await s.h.click('预检整条方案'); await s.h.click('保存 / 启动');
  assert.equal(s.calls[0].body.style_tokens.name, 'content-os-editorial');
  assert.deepEqual(s.calls[1].body.style_tokens, s.calls[0].body.style_tokens);
  assert.equal(s.calls[1].body.expected_fingerprint, plan.fingerprint);
});

test('comparison-stack choice persists through preflight and start', async () => {
  const s = setup(); await s.h.flush(); await s.h.change('新执行的成片画面呈现', 'comparison_stack');
  await s.h.click('预检整条方案'); await s.h.click('保存 / 启动');
  assert.equal(s.calls[0].body.style_tokens.name, 'content-os-portrait-comparison');
  assert.deepEqual(s.calls[1].body.style_tokens, s.calls[0].body.style_tokens);
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
