import test from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {componentHarness} from './hook-harness.mjs';
const file = fileURLToPath(new URL('../src/ScenePresentationEditor.tsx', import.meta.url));
function setup(scene = {evidence_refs: []}) {
  const calls = [];
  let current = scene;
  const h = componentHarness(file, 'ScenePresentationEditor', {scene: current, disabled: false,
    onChange: patch => {calls.push(patch); current = {...current, ...patch}; h.setProps({scene: current});}});
  return {h, calls, scene: () => current};
}
async function textarea(s, index, value) {
  s.h.nodes('textarea')[index].props.onChange({target: {value}}); await s.h.flush();
}
test('ordinary editor preserves evidence, no-graphics reason and reloads exact explicit choice', async () => {
  const s = setup(); await s.h.flush();
  await s.h.change('画面需求', 'action_evidence');
  await textarea(s, 0, 'Must show the real operation');
  await textarea(s, 1, 'source:screen-recording\nresearch:scope');
  await s.h.change('额外图文', 'none');
  await textarea(s, 2, 'Do not obscure the operation');
  assert.equal(s.scene().visual_requirement, 'action_evidence');
  assert.equal(s.scene().visual_requirement_reason, 'Must show the real operation');
  assert.deepEqual(Array.from(s.scene().evidence_refs), ['source:screen-recording', 'research:scope']);
  assert.equal(s.scene().graphic_plan.kind, 'none');
  const restored = setup(JSON.parse(JSON.stringify(s.scene()))); await restored.h.flush();
  assert.equal(restored.h.input('额外图文').props.value, 'none');
  assert.equal(restored.h.nodes('textarea')[2].props.value, 'Do not obscure the operation');
  assert.match(restored.h.text(), /不能用字卡悄悄填补/);
});
test('switching graphic kind clears incompatible point/treatment fields and keeps source references', async () => {
  const s = setup({evidence_refs: ['source:case']}); await s.h.flush();
  await s.h.change('额外图文', 'text_card');
  await s.h.change('图文形式', 'contrast');
  await textarea(s, 2, 'Before\nAfter');
  assert.equal(s.scene().graphic_plan.points.length, 2);
  await s.h.change('额外图文', 'unsupported');
  assert.equal(s.scene().graphic_plan.treatment, null);
  assert.equal(s.scene().graphic_plan.points.length, 0);
  await textarea(s, 2, 'Need an animated diagram');
  assert.equal(s.scene().graphic_plan.reason, 'Need an animated diagram');
  await s.h.change('额外图文', 'legacy');
  assert.equal(s.scene().graphic_plan, null);
  assert.deepEqual(Array.from(s.scene().evidence_refs), ['source:case']);
});
