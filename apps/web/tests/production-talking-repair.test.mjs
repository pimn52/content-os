import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { componentHarness } from './hook-harness.mjs';

const file = fileURLToPath(new URL('../src/ProductionTalkingPanel.tsx', import.meta.url));
const basePlan = {
  fingerprint: 'current-plan', failure_kind: 'quality', action: 'scoped_fix_required', stop_reasons: [], findings: [],
  master_audio_id: 'original-master', master_start_ms: 240, master_end_ms: 960, remaining_run_repairs: 1,
  max_provider_calls: 1, external_charge_ceiling: '0', currency: 'USD', review_scope: 'fresh_child_qa_and_policy_required_whole_result_review',
  assessment_state: 'unknown', responsible_stage: 'voice', scope: { scene_id: 'scene-1', start_ms: 240, end_ms: 960 },
  preserves: { master_audio_id: 'original-master', master_audio_sha256: 'original-master-hash', talking_job_ids: ['original-talking-job'],
    talking_output_asset_ids: ['original-talking-output'], scene_plan_ids: ['original-scene-plan'], render_job_id: 'original-render-job' },
  required_capability: ['upstream_voice_qa_diagnosis', 'consumed_voice_dependency_repair'],
};

test('Talking repair shows the upstream owner and retained exact work without regenerating unknown failures', async () => {
  for (const action of ['scoped_fix_required', 'replan', 'regenerate_scene']) {
    let submits = 0;
    const h = componentHarness(file, 'RepairControls', { scene: { scene_plan_id: 'scene-plan-1', scene_id: 'scene-1' },
      plan: { ...basePlan, action }, reason: 'specific quality defect', records: [], canRequestPlan: true, busy: false,
      onLoad() {}, onReason() {}, onSubmit() { submits += 1; } });
    await h.flush();
    assert.match(h.text(), /责任工序：声音/); assert.match(h.text(), /待补证据，尚未定位可执行修复/);
    assert.match(h.text(), /场景 scene-1 · 240–960 ms/); assert.match(h.text(), /核对本场景上游声音的质量检查失败/);
    assert.match(h.text(), /声音修改后更新受影响的人物画面与成片/);
    for (const id of ['original-master', 'original-master-hash', 'original-talking-job', 'original-talking-output', 'original-scene-plan', 'original-render-job']) assert.ok(h.text().includes(id));
    assert.match(h.text(), /保留主旁白和其它合格场景/);
    assert.equal(h.nodes('form').length, 0); assert.equal(submits, 0); h.unmount();
  }
});

test('known Talking repair retains its one-scene action while blocked plans cannot submit', async () => {
  let submits = 0;
  const h = componentHarness(file, 'RepairControls', { scene: { scene_plan_id: 'scene-plan-1', scene_id: 'scene-1' },
    plan: { ...basePlan, action: 'regenerate_scene', assessment_state: 'known', responsible_stage: 'talking', required_capability: [] },
    reason: 'known decoded output defect', records: [], canRequestPlan: true, busy: false,
    onLoad() {}, onReason() {}, onSubmit() { submits += 1; } });
  await h.flush(); assert.match(h.text(), /责任工序：人物画面/);
  assert.equal(h.find('button', '只重生成此场景').props.disabled, false);
  await h.submit('明确确认'); assert.equal(submits, 1);
  h.setProps({ plan: { ...basePlan, action: 'regenerate_scene', assessment_state: 'known', stop_reasons: ['current_source_changed'] } });
  await h.flush(); assert.equal(h.find('button', '只重生成此场景').props.disabled, true); h.unmount();
});

test('Talking submission handler independently rejects an explicitly unknown repair plan', async () => {
  const calls = [], confirmations = [];
  const run = { id: 'run-1', project_id: 'project-1', status: 'awaiting_talking_dependencies', preflight_fingerprint: 'current-plan',
    talking_master_audio_id: 'original-master', talking_dependencies: [{ scene_plan_id: 'scene-plan-1', scene_id: 'scene-1' }],
    talking_source_bindings: [{ scene_plan_id: 'scene-plan-1', talking_job_id: 'original-talking-job' }],
    talking_qa_dependencies: [{ scene_plan_id: 'scene-plan-1', generation_job_id: 'original-talking-job', state: 'generation_failed' }],
    talking_preview_dependencies: [], waiting_stop_reasons: [], voice_qa_auto_stop_reasons: [] };
  const request = async (path, options) => {
    calls.push({ path, options });
    if (path.includes('/talking-repair-plan?')) return { ...basePlan, action: 'regenerate_scene' };
    if (['/talking-profiles', '/execution-settings/capabilities', '/assets'].includes(path) || path.endsWith('/talking-repairs')) return [];
    throw new Error(`Unexpected fixture request: ${path}`);
  };
  const h = componentHarness(file, 'ProductionTalkingPanel', { projectId: 'project-1', run, request,
    confirmAction: message => { confirmations.push(message); return true; }, onRunChange() {} });
  const walk = node => node == null || typeof node !== 'object' ? [] : Array.isArray(node) ? node.flatMap(walk) : [node, ...walk(node.props?.children)];
  const controls = () => h.nodes('article').flatMap(walk).find(node => typeof node.type === 'function' && node.type.name === 'RepairControls');
  await h.flush(); assert.ok(controls()); controls().props.onLoad(); await h.flush();
  controls().props.onReason('explicit defect'); await h.flush();
  assert.equal(controls().props.plan.assessment_state, 'unknown');
  controls().props.onSubmit({ preventDefault() {} }); await h.flush();
  assert.equal(confirmations.length, 0); assert.equal(calls.filter(call => call.options?.method === 'POST').length, 0); h.unmount();
});
