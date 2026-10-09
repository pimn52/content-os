import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { componentHarness } from './hook-harness.mjs';
const file = fileURLToPath(new URL('../src/PlanningPreferencesPanel.tsx', import.meta.url));

function setup() {
  const rule = { id: 'rule', rule: 'semantic_graphic_not_full_copy_v1', version: 1, state: 'candidate', classification: 'creator_preference', profile_version: 1,
    stop_reasons: [], current_stop_reasons: [], retained_case: { before: { caption_emphasis: ['full copy'] }, after: { caption_emphasis: ['takeaway'] }, passed: true } };
  let rules = [], calls = [], confirm = true, retainedFiles = [], useRules = [], useHistory = [], staleWriteResponse = false;
  const request = async (path, options) => {
    calls.push({ path, options });
    if (path.endsWith('retained-render-rejections')) return structuredClone(retainedFiles);
    if (path.endsWith('/versions')) return structuredClone(useHistory);
    if (path.endsWith('source-use-constraints') && options?.method === 'POST') {
      const body = JSON.parse(options.body), choice = retainedFiles.find(item => item.manifest_path === body.manifest_path);
      assert.equal(body.confirmed_source, true);
      const value = { id: 'constraint', version: 1, state: 'candidate', classification: 'configuration_use_limit',
        evidence_class: body.evidence_class, reason: body.reason, current_stop_reasons: [],
        original_review: structuredClone(choice.original_review), uses: structuredClone(choice.uses), adopted_use_ids: [] };
      useRules = [value]; useHistory = [structuredClone(value)]; return structuredClone(value);
    }
    if (path.includes('/source-use-constraints/')) {
      const body = JSON.parse(options.body), current = useRules[0];
      if (body.expected_version !== current.version) throw new Error('use_constraint_version_conflict_reload');
      const saved = { ...current, version: current.version + 1, state: body.enabled ? 'enabled' : 'disabled', adopted_use_ids: body.enabled ? body.use_ids : current.adopted_use_ids };
      useRules = [saved]; useHistory.push(structuredClone(saved)); return structuredClone(staleWriteResponse ? current : saved);
    }
    if (path.endsWith('source-use-constraints')) return structuredClone(useRules);
    if (path.endsWith('planning-preference-selection') && !options) return { profile_version: 1, rule: rule.rule };
    if (path.endsWith('planning-preference-selection') && options?.method === 'POST') { rules = [{ ...structuredClone(rule), authority_source: 'creator_selection', retained_case: null }]; return rules[0]; }
    if (options?.method === 'POST') { rules = [structuredClone(rule)]; return rules[0]; }
    if (options?.method === 'PUT') { const body = JSON.parse(options.body); assert.equal(body.expected_version, rules[0].version); rules[0] = { ...rules[0], state: body.enabled ? 'enabled' : 'disabled', version: rules[0].version + 1 }; return rules[0]; }
    if (path.endsWith('planning-observations')) return [{ id: 'obs', request: { finding: 'duplicate_text', reason: 'observed', evidence_reference: 'fixture', render_sha256: 'a'.repeat(64) } }];
    return structuredClone(rules);
  };
  const harness = componentHarness(file, 'PlanningPreferencesPanel', { projectId: 'A', request }, { window: { confirm: () => confirm } });
  return { harness, calls, setConfirm(value) { confirm = value; }, setRetained(value) { retainedFiles = value; },
    setUseRules(value) { useRules = value; }, setStaleWrite(value) { staleWriteResponse = value; },
    stop() { rules[0].current_stop_reasons = ['preference_render_evidence_changed']; } };
}

test('candidate never enables implicitly; explicit confirm enable/disable; reload retains state', async () => {
  const s = setup(), h = s.harness;
  await h.flush(); await h.click('分类并验证候选');
  assert.match(h.text(), /candidate/);
  assert.equal(s.calls.filter(c => c.options?.method === 'PUT').length, 0);
  await h.change('采纳 / 禁用理由', 'only this creator');
  s.setConfirm(false); await h.click('明确启用');
  assert.equal(s.calls.filter(c => c.options?.method === 'PUT').length, 0);
  s.setConfirm(true); await h.click('明确启用'); assert.match(h.text(), /v2 · enabled/);
  await h.click('刷新反馈'); assert.match(h.text(), /v2 · enabled/);
  await h.change('采纳 / 禁用理由', 'rollback'); await h.click('禁用并恢复'); assert.match(h.text(), /v3 · disabled/);
  await h.change('采纳 / 禁用理由', 'must reset'); h.setProps({ projectId: 'B' }); await h.flush();
  assert.equal(h.input('采纳 / 禁用理由').props.value, '');
});

test('stale evidence holds enable; late load cannot cross project scope', async () => {
  const s = setup(), h = s.harness;
  await h.flush(); await h.click('分类并验证候选'); s.stop(); await h.click('刷新反馈');
  await h.change('采纳 / 禁用理由', 'no override'); assert.equal(h.find('button', '明确启用').props.disabled, true);
  assert.match(h.text(), /preference_render_evidence_changed/);
  let resolve;
  const delayed = new Promise(r => { resolve = r; });
  const request = async path => path.startsWith('/projects/A') ? delayed : [];
  const fresh = componentHarness(file, 'PlanningPreferencesPanel', { projectId: 'A', request });
  await fresh.flush(); fresh.setProps({ projectId: 'B' }); await fresh.flush();
  resolve([{ id: 'old', request: { finding: 'OLD_PROJECT', reason: 'old' } }]); await fresh.flush();
  assert.doesNotMatch(fresh.text(), /OLD_PROJECT/);
});

test('direct creator selection needs no fake observation or regression and does not auto-enable', async () => {
  const s = setup(), h = s.harness;
  await h.flush(); await h.change('采纳 / 禁用理由', 'my selected presentation direction');
  await h.click('保存我的短句偏好候选');
  assert.match(h.text(), /创作者主动选择/);
  assert.doesNotMatch(h.text(), /留存案例：/);
  assert.equal(s.calls.filter(c => c.options?.method === 'PUT').length, 0);
  const selection = s.calls.find(c => c.path.endsWith('planning-preference-selection') && c.options);
  assert.equal(selection.options.method, 'POST');
  assert.equal(JSON.parse(selection.options.body).observation_id, undefined);
  await h.change('采纳 / 禁用理由', 'explicit adoption'); await h.click('明确启用');
  assert.match(h.text(), /v2 · enabled/);
});

function retainedCase(evidenceClass = 'assisted_test') {
  return [{ manifest_path: 'evaluation-evidence/case/manifest.json', expected_manifest_sha256: 'a'.repeat(64),
    evidence_level: 'unconfirmed_retained_document_not_policy_or_qa', original_review: {
      approved: false, reviewed_sha256: 'b'.repeat(64), evidence_reference: 'review:whole-render',
      reviewed_at: '2026-10-01T12:00:00Z', scope: 'exact complete render only', findings: ['Original horizontal composition rejected.'] },
    uses: [
      { use_id: 'exact-a', scene_id: 'opening-and-method', source_label: 'creator-interview.mp4', start_ms: 0, end_ms: 13600,
        scope_authority: 'proposed_future_use_not_independent_scene_rejection', presentation: { width: 1080, height: 1920, visual_role: 'talking', framing_policy: 'face_safe_contain', portrait_presentation: 'full_canvas', burned_in_subtitles: 'present', subtitle_treatment: 'none', graphic_treatment: 'none', graphic_text: null } },
      { use_id: 'unknown-b', scene_id: 'responsible-edit', source_label: 'creator-interview.mp4', start_ms: 20120, end_ms: 28240,
        scope_authority: 'proposed_future_use_not_independent_scene_rejection', presentation: null },
    ], evidenceClass }];
}

test('retained rejection needs source confirmation, then a separately selected exact-use adoption', async () => {
  const s = setup(), h = s.harness; s.setRetained(retainedCase()); await h.flush();
  assert.match(h.text(), /exact complete render only/);
  assert.match(h.text(), /Original horizontal composition rejected/);
  assert.doesNotMatch(h.text(), /未来用法设置/);
  await h.change('保留的拒绝成片', 'evaluation-evidence/case/manifest.json');
  await h.change('证据来源', 'assisted_test');
  await h.change('采纳 / 禁用理由', 'I confirmed this retained rejection and its future scope');
  s.setConfirm(false); await h.click('确认来源并加入待确认拒绝');
  assert.equal(s.calls.some(c => c.path.endsWith('source-use-constraints') && c.options?.method === 'POST'), false);
  s.setConfirm(true); await h.click('确认来源并加入待确认拒绝');
  assert.ok(s.calls.some(c => c.path.endsWith('source-use-constraints') && c.options?.method === 'POST'), h.text());
  assert.match(h.text(), /待明确范围/);
  const intake = s.calls.find(c => c.path.endsWith('source-use-constraints') && c.options?.method === 'POST');
  const body = JSON.parse(intake.options.body);
  assert.equal(body.confirmed_source, true); assert.equal(body.expected_manifest_sha256, 'a'.repeat(64));
  assert.equal(body.evidence_class, 'assisted_test'); assert.equal(body.use_ids, undefined);
  assert.equal(h.find('button', '明确采纳所选未来避用范围').props.disabled, true);
});

test('only checked reconstructable uses may be adopted; reload wins over stale write body', async () => {
  const s = setup(), h = s.harness; s.setRetained(retainedCase()); await h.flush();
  await h.change('保留的拒绝成片', 'evaluation-evidence/case/manifest.json');
  await h.change('证据来源', 'assisted_test'); await h.change('采纳 / 禁用理由', 'explicit product adoption of this exact scope');
  await h.click('确认来源并加入待确认拒绝');
  assert.equal(h.find('button', '明确采纳所选未来避用范围').props.disabled, true);
  const boxes = h.nodes('input').filter(input => input.props.type === 'checkbox');
  assert.equal(boxes.length, 2); assert.equal(boxes[0].props.disabled, false); assert.equal(boxes[1].props.disabled, true);
  boxes[0].props.onChange({ target: { checked: true } }); await h.flush();
  await h.change('采纳 / 禁用理由', 'explicitly adopt only the checked interval');
  s.setStaleWrite(true);
  await h.click('明确采纳所选未来避用范围');
  assert.match(h.text(), /v2 · 已采纳/);
  const adoption = s.calls.find(c => c.path.includes('/source-use-constraints/constraint') && c.options?.method === 'PUT');
  const body = JSON.parse(adoption.options.body);
  assert.deepEqual(body.use_ids, ['exact-a']); assert.equal(body.expected_version, 1); assert.match(body.reason, /explicitly adopt only the checked interval/);
  await h.click('查看采纳与禁用历史'); assert.match(h.text(), /v1 · 候选/); assert.match(h.text(), /v2 · 已采纳/);
  await h.change('采纳 / 禁用理由', 'explicitly disable this scoped preference');
  s.setStaleWrite(false); await h.click('禁用并恢复原推荐');
  assert.match(h.text(), /v3 · 已禁用/);
});

test('fixture and changed-evidence records stay visible but cannot be adopted', async () => {
  const fixture = setup(), h = fixture.harness;
  fixture.setRetained(retainedCase()); await h.flush();
  await h.change('保留的拒绝成片', 'evaluation-evidence/case/manifest.json');
  await h.change('证据来源', 'fixture'); await h.change('采纳 / 禁用理由', 'test evidence only');
  await h.click('确认来源并加入待确认拒绝');
  assert.equal(h.find('button', '明确采纳所选未来避用范围').props.disabled, true);
  assert.equal(fixture.calls.some(c => c.path.includes('/source-use-constraints/constraint') && c.options?.method === 'PUT'), false);
  assert.match(h.text(), /测试夹具（不可采纳为规则）/);

  const held = setup(), heldHarness = held.harness;
  held.setUseRules([{ id: 'held', version: 2, state: 'candidate', classification: 'configuration_use_limit',
    evidence_class: 'assisted_test', reason: 'prior evidence changed', current_stop_reasons: ['use_constraint_retained_evidence_changed'],
    adopted_use_ids: [], original_review: retainedCase()[0].original_review, uses: retainedCase()[0].uses }]);
  await heldHarness.flush(); await heldHarness.change('采纳 / 禁用理由', 'no override');
  assert.equal(heldHarness.find('button', '明确采纳所选未来避用范围').props.disabled, true);
  assert.match(heldHarness.text(), /use_constraint_retained_evidence_changed/);
});

test('fresh project load clears prior selected evidence and user scope', async () => {
  const s = setup(), h = s.harness; s.setRetained(retainedCase()); await h.flush();
  await h.change('保留的拒绝成片', 'evaluation-evidence/case/manifest.json');
  await h.change('证据来源', 'assisted_test'); await h.change('采纳 / 禁用理由', 'prior creator choice');
  h.setProps({ projectId: 'other-project' }); await h.flush();
  assert.equal(h.input('保留的拒绝成片').props.value, '');
  assert.equal(h.input('证据来源').props.value, '');
  assert.equal(h.input('采纳 / 禁用理由').props.value, '');
  assert.doesNotMatch(h.text(), /creator-interview\.mp4/);
});
