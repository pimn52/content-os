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
test('ordinary editor selects, saves and reloads actual diagram treatments', async () => {
  const s = setup({evidence_refs: ['source:case'], visual_requirement: 'explanatory', preferred_sources: ['typography']});
  await s.h.flush();
  await s.h.change('额外图文', 'diagram');
  for (const treatment of ['diagram_flow', 'diagram_loss', 'diagram_factors']) {
    await s.h.change('图文形式', treatment);
    await textarea(s, 3, '输入信息\n输出信息');
    const restored = setup(JSON.parse(JSON.stringify(s.scene()))); await restored.h.flush();
    assert.equal(restored.h.input('额外图文').props.value, 'diagram');
    assert.equal(restored.h.input('图文形式').props.value, treatment);
    assert.deepEqual(Array.from(restored.scene().graphic_plan.points), ['输入信息', '输出信息']);
    assert.equal(restored.scene().graphic_plan.reason, null);
  }
});

async function textarea(s, index, value) {
  s.h.nodes('textarea')[index].props.onChange({target: {value}}); await s.h.flush();
}
test('ordinary editor preserves evidence, no-graphics reason and reloads exact explicit choice', async () => {
  const s = setup(); await s.h.flush();
  await s.h.change('画面需求', 'action_evidence');
  await textarea(s, 1, 'Must show the real operation');
  await textarea(s, 2, 'source:screen-recording\nresearch:scope');
  await s.h.change('额外图文', 'none');
  await textarea(s, 3, 'Do not obscure the operation');
  assert.equal(s.scene().visual_requirement, 'action_evidence');
  assert.equal(s.scene().visual_requirement_reason, 'Must show the real operation');
  assert.deepEqual(Array.from(s.scene().evidence_refs), ['source:screen-recording', 'research:scope']);
  assert.equal(s.scene().graphic_plan.kind, 'none');
  const restored = setup(JSON.parse(JSON.stringify(s.scene()))); await restored.h.flush();
  assert.equal(restored.h.input('额外图文').props.value, 'none');
  assert.equal(restored.h.nodes('textarea')[3].props.value, 'Do not obscure the operation');
  assert.match(restored.h.text(), /不能用字卡悄悄填补/);
});
test('switching graphic kind clears incompatible point/treatment fields and keeps source references', async () => {
  const s = setup({evidence_refs: ['source:case']}); await s.h.flush();
  await s.h.change('额外图文', 'text_card');
  await s.h.change('图文形式', 'contrast');
  await textarea(s, 3, 'Before\nAfter');
  assert.equal(s.scene().graphic_plan.points.length, 2);
  await s.h.change('额外图文', 'unsupported');
  assert.equal(s.scene().graphic_plan.treatment, null);
  assert.equal(s.scene().graphic_plan.points.length, 0);
  await textarea(s, 3, 'Need an animated diagram');
  assert.equal(s.scene().graphic_plan.reason, 'Need an animated diagram');
  await s.h.change('额外图文', 'legacy');
  assert.equal(s.scene().graphic_plan, null);
  assert.deepEqual(Array.from(s.scene().evidence_refs), ['source:case']);
});
test('scene purpose and visual intent can be corrected and reloaded in the ordinary editor', async () => {
  const s = setup({purpose: '旧场景目的', visual_intent: {subject: '旧主体', action: '旧动作', framing: '全景', description: '旧描述'}, evidence_refs: []}); await s.h.flush();
  await s.h.change('场景目的', '解释平台二次压缩');
  await s.h.change('画面主体', '竖屏信息图');
  await s.h.change('画面动作', '展示上传到压缩结果的变化');
  await s.h.change('取景方式', '全屏竖版');
  s.h.nodes('textarea')[0].props.onChange({target: {value: '左右展示源画质与平台压缩后的细节损失'}}); await s.h.flush();
  assert.equal(s.scene().purpose, '解释平台二次压缩');
  assert.deepEqual({...s.scene().visual_intent}, {subject: '竖屏信息图', action: '展示上传到压缩结果的变化', framing: '全屏竖版', description: '左右展示源画质与平台压缩后的细节损失'});
  const restored = setup(JSON.parse(JSON.stringify(s.scene()))); await restored.h.flush();
  assert.equal(restored.h.input('场景目的').props.value, '解释平台二次压缩');
  assert.equal(restored.h.input('画面主体').props.value, '竖屏信息图');
  assert.equal(restored.h.nodes('textarea')[0].props.value, '左右展示源画质与平台压缩后的细节损失');
});
test('explanatory scene can choose semantic graphics as its actual routed source', async () => {
  const s = setup({evidence_refs: [], visual_requirement: 'explanatory', preferred_sources: ['user_asset'], fallback_sources: []});
  await s.h.flush();
  await s.h.change('主画面来源', 'typography');
  assert.deepEqual(Array.from(s.scene().preferred_sources), ['typography']);
  assert.deepEqual(Array.from(s.scene().fallback_sources), []);
  const restored = setup(JSON.parse(JSON.stringify(s.scene()))); await restored.h.flush();
  assert.equal(restored.h.input('主画面来源').props.value, 'typography');
});
