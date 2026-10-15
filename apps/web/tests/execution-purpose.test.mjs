import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { componentHarness } from './hook-harness.mjs';
const file = fileURLToPath(new URL('../src/ExecutionPurposeControl.tsx', import.meta.url));
const evaluation = {purpose: 'internal_evaluation', use_admission_ids: []};
const row = (id, changes = {}) => ({id, project_id: 'P', revoked_at: null, review: {intended_activity: `activity-${id}`, license_version: 'terms-v1', permitted_operations: ['generation'], identity: {capability: 'voice', provider: 'omnivoice', model: 'existing-local'}}, ...changes});
function setup(request, value = evaluation) {
  const changes = []; let h;
  h = componentHarness(file, 'ExecutionPurposeControl', {projectId: 'P', value, disabled: false, request,
    onChange: value => { changes.push(value); h.setProps({value}); }});
  return {h, changes};
}
test('existing project records show applicability without automatic permission selection; revoked and unsupported generation disabled', async () => {
  const s = setup(async () => [row('B'), row('A'), row('revoked', {revoked_at: 'today'}), row('review', {review: {...row('review').review, permitted_operations: ['review']}})]);
  await s.h.flush(); assert.equal(s.changes.length, 0);
  assert.match(s.h.text(), /待后端核验/); assert.match(s.h.text(), /尚未开放/);
  assert.equal(s.h.input('activity-revoked').props.disabled, true);
  assert.equal(s.h.input('activity-review').props.disabled, true);
  s.h.input('activity-B').props.onChange({target: {checked: true}}); await s.h.flush();
  s.h.input('activity-A').props.onChange({target: {checked: true}}); await s.h.flush();
  assert.equal(JSON.stringify(s.changes.at(-1).use_admission_ids), JSON.stringify(['A', 'B']));
  await s.h.click('刷新用途准入记录');
  assert.equal(s.changes.at(-1).use_admission_ids.length, 0);
  await s.h.change('新执行用途', 'commercial_production');
  assert.equal(s.changes.at(-1).purpose, 'commercial_production');
  assert.equal(s.changes.at(-1).use_admission_ids.length, 0);
});
test('commercial default does not request evaluation authority', async () => {
  const s = setup(() => { throw new Error('unexpected'); }, {purpose: 'commercial_production', use_admission_ids: []});
  await s.h.flush(); assert.equal(s.changes.length, 0); assert.doesNotMatch(s.h.text(), /读取失败/);
});
test('empty, error and mismatched project remain evaluation without fallback or permission', async () => {
  for (const request of [async () => [], async () => { throw new Error('offline'); }, async () => [row('A', {project_id: 'Q'})]]) {
    const s = setup(request); await s.h.flush();
    assert.equal(s.changes.length, 0); assert.equal(s.h.input('新执行用途').props.value, 'internal_evaluation');
    assert.equal(s.h.nodes('input').length, 0);
    assert.match(s.h.text(), /没有用途准入记录|offline|项目不匹配/);
  }
});
test('late old-project receipt response cannot replace new project records', async () => {
  let resolve; const s = setup(path => path.includes('/P/') ? new Promise(r => { resolve = r; }) : Promise.resolve([]));
  await s.h.flush(); assert.match(s.h.text(), /正在读取/);
  s.h.setProps({projectId: 'Q'}); await s.h.flush(); resolve([row('A')]); await s.h.flush();
  assert.equal(s.h.nodes('input').length, 0); assert.equal(s.changes.length, 0);
});
