import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { componentHarness } from './hook-harness.mjs';

const file = fileURLToPath(new URL('../src/ProductionTalkingPanel.tsx', import.meta.url));
const run = (changes = {}) => ({
  id: 'run-1', project_id: 'project-1', status: 'awaiting_talking_dependencies', preflight_fingerprint: 'plan-1',
  render_job_id: null, voice_job_id: null, voice_audio_id: 'audio-1', voice_qa_job_id: null,
  waiting_stop_reasons: [], voice_qa_auto_stop_reasons: [], talking_master_audio_id: 'audio-1',
  talking_dependencies: ['scene-1', 'scene-2'].map(id => ({ scene_plan_id: id, scene_id: id })),
  talking_source_bindings: [], talking_qa_dependencies: [], talking_preview_dependencies: [], ...changes,
});
const binding = (sceneId, jobId = null, changes = {}) => ({
  scene_plan_id: sceneId, talking_profile_id: 'profile-1', reference_clip_id: 'clip-1',
  capability_profile_id: 'cap-1', talking_job_id: jobId, ...changes,
});
function fixture({ initial = run(), confirmed = true, failure, response } = {}) {
  const calls = [], updates = [];
  const profile = { id: 'profile-1', name: '创作者', provider: 'local', reference_clip_ids: ['clip-1'], consent: { confirmed } };
  const asset = { id: 'asset-1', content_hash: 'source-hash', width: 1080, height: 1920, metadata: {} };
  const clip = { id: 'clip-1', asset_id: asset.id, start_ms: 0, end_ms: 4000 };
  const capability = { id: 'cap-1', capability: 'talking', provider: 'local' };
  const complete = response ?? { ...initial, talking_source_bindings: initial.talking_dependencies.map(scene => binding(scene.scene_plan_id, `job-${scene.scene_plan_id}`)) };
  const request = async (path, options) => {
    calls.push({ path, options });
    if (path === '/talking-profiles') return [profile];
    if (path === '/execution-settings/capabilities') return [capability];
    if (path === '/assets') return [asset];
    if (path === '/assets/asset-1/clips') return [clip];
    if (path.endsWith('/talking-repairs')) return [];
    if (path.endsWith('/talking-batch-dispatch')) {
      if (failure) throw new Error(failure);
      return complete;
    }
    throw new Error(`unexpected fixture request: ${path}`);
  };
  let h;
  h = componentHarness(file, 'ProductionTalkingPanel', {
    projectId: 'project-1', run: initial, request,
    onRunChange: next => { updates.push(next); h.setProps({ run: next }); },
  });
  return { h, calls, updates };
}
const posts = f => f.calls.filter(call => call.options?.method === 'POST');
async function selectSource(h) {
  await h.change('Talking Profile', 'profile-1');
  await h.change('Profile 授权来源 Clip', 'clip-1');
  await h.change('Talking 能力档案', 'cap-1');
}

test('one global selection dispatches all scenes without per-scene reading or confirmation', async () => {
  const f = fixture();
  await f.h.flush();
  assert.equal(f.h.nodes('select').length, 3);
  assert.equal(f.h.find('button', '制作全部人物画面').props.disabled, true);
  assert.equal(f.h.nodes('details').length, 2);
  await selectSource(f.h);
  const button = f.h.find('button', '制作全部人物画面');
  button.props.onClick(); button.props.onClick();
  await f.h.flush();
  assert.equal(posts(f).length, 1);
  assert.equal(posts(f)[0].path, '/projects/project-1/production-runs/run-1/talking-batch-dispatch');
  assert.deepEqual(JSON.parse(posts(f)[0].options.body), {
    talking_profile_id: 'profile-1', reference_clip_id: 'clip-1', capability_profile_id: 'cap-1',
    brief: { require_no_burned_subtitles: true },
  });
  assert.equal(f.calls.some(call => /talking-source-context|talking-source-bind$|talking-dispatch$/.test(call.path)), false);
  assert.equal(f.updates.length, 1);
  assert.equal(f.h.nodes('button').some(node => node.props.children === '制作全部人物画面'), false);
  f.h.unmount();
});

test('a unique existing source group can finish pending scenes without reselecting', async () => {
  const f = fixture({ initial: run({ talking_source_bindings: [binding('scene-1', 'existing-job')] }) });
  await f.h.flush();
  assert.match(f.h.text(), /待制作 1 段/);
  assert.match(f.h.text(), /沿用此执行已绑定/);
  await f.h.click('制作全部人物画面');
  assert.deepEqual(JSON.parse(posts(f)[0].options.body), {});
  assert.equal(posts(f).length, 1);
  f.h.unmount();
});

test('existing failed or pending jobs are never offered batch regeneration', async () => {
  const f = fixture({ initial: run({
    talking_source_bindings: [binding('scene-1', 'pending-job')],
    talking_qa_dependencies: [{ scene_plan_id: 'scene-2', generation_job_id: 'failed-job', state: 'generation_failed', output_asset_id: null, qa_job_id: null }],
  }) });
  await f.h.flush();
  assert.equal(f.h.nodes('button').some(node => node.props.children === '制作全部人物画面'), false);
  assert.equal(posts(f).length, 0);
  f.h.unmount();
});

test('partial choices, absent consent, and ambiguous existing groups cannot batch dispatch', async () => {
  const f = fixture({ initial: run({ talking_source_bindings: [binding('scene-1'), binding('scene-2', null, { reference_clip_id: 'other-clip' })] }), confirmed: false });
  await f.h.flush();
  assert.equal(f.h.find('button', '制作全部人物画面').props.disabled, true);
  await f.h.change('Talking Profile', 'profile-1');
  assert.equal(f.h.find('button', '制作全部人物画面').props.disabled, true);
  await f.h.change('Profile 授权来源 Clip', 'clip-1');
  await f.h.change('Talking 能力档案', 'cap-1');
  assert.equal(f.h.find('button', '制作全部人物画面').props.disabled, true);
  assert.equal(posts(f).length, 0);
  f.h.unmount();
});

test('a refused batch retains recovery controls and does not retry automatically', async () => {
  const f = fixture({ failure: '预算不足，未派发新任务' });
  await f.h.flush(); await selectSource(f.h);
  await f.h.click('制作全部人物画面');
  assert.match(f.h.text(), /预算不足，未派发新任务/);
  assert.equal(posts(f).length, 1);
  assert.equal(f.updates.length, 0);
  assert.equal(f.h.nodes('details').length, 2);
  assert.equal(f.h.find('button', '制作全部人物画面').props.disabled, false);
  f.h.unmount();
});

test('batch response from a different run is rejected', async () => {
  const f = fixture({ response: run({ id: 'other-run' }) });
  await f.h.flush(); await selectSource(f.h);
  await f.h.click('制作全部人物画面');
  assert.match(f.h.text(), /动作返回的 Run 与当前范围不一致/);
  assert.equal(f.updates.length, 0);
  f.h.unmount();
});
