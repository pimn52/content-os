import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash, webcrypto } from 'node:crypto';
import { transformSync } from 'esbuild';
import { componentHarness } from './hook-harness.mjs';
const source = readFileSync(new URL('./recovery-state.ts', import.meta.url), 'utf8');
const { code } = transformSync(source, { loader: 'ts', format: 'esm' });
const { createRecoveryFixture, recoveryIds, renderHashes, manualComparison } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
const media = Object.fromEntries(['before', 'after'].map(kind => [kind, readFileSync(new URL(`./media/recovery-${kind}.mp4`, import.meta.url))]));
const paths = { voice: new URL('../src/ProductionVoicePanel.tsx', import.meta.url).pathname.replace(/^\/(\w:)/, '$1'), presentation: new URL('../src/PresentationRecoveryPanel.tsx', import.meta.url).pathname.replace(/^\/(\w:)/, '$1') };
function setup(kind, storageMap = new Map()) {
  const storage = { getItem: key => storageMap.get(key) ?? null, setItem: (key, value) => storageMap.set(key, String(value)), removeItem: key => storageMap.delete(key) };
  const fixture = createRecoveryFixture(storage, kind); if (!storage.getItem(fixture.key)) fixture.seed();
  return { fixture, storage, storageMap };
}
function mount(kind, f, extra = {}) {
  let harness;
  const props = { projectId: f.fixture.projectId, draftVersion: 1, scriptRevision: 1, run: f.fixture.load().run,
    request: f.fixture.request, confirmAction: () => true, onRunChange: run => harness.setProps({ run }) };
  harness = componentHarness(decodeURI(paths[kind]), kind === 'voice' ? 'ProductionVoicePanel' : 'PresentationRecoveryPanel', props,
    { localStorage: f.storage, fetch: path => Promise.resolve(new Response(String(path).endsWith(recoveryIds.oldRender) ? media.before : media.after)), ...extra });
  return harness;
}
async function observe(harness, finding = 'duplicate_text') {
  await harness.change('场景', 'scene-1'); await harness.change('所见问题', finding);
  await harness.change('观察理由', 'fixture observed duplicate'); await harness.change('证据引用', 'fixture:00:00');
  await harness.submit('登记此 Render');
}
test('Voice UI replaces failed take, keeps pending/completed history and exposes a fresh six-dimension review after QA', async () => {
  const f = setup('voice'); const h = mount('voice', f); await h.flush();
  await h.click('检查 Voice 修复方案');
  assert.match(h.text(), /whole|完整|整段/); assert.match(h.text(), /剩余额度 1/);
  await h.change('本次整段修复理由', 'fixture technical recovery'); await h.submit('按此方案创建整段');
  assert.match(h.text(), /Voice 修复历史/); assert.match(h.text(), new RegExp(recoveryIds.newJob));
  assert.equal(h.nodes('form').length, 0); assert.equal(f.fixture.load().run.voice_audio_id, null);
  f.fixture.complete(); h.setProps({ run: f.fixture.load().run }); await h.flush();
  await h.click('推进独立 Voice QA');
  assert.match(h.text(), new RegExp(recoveryIds.newAudio)); assert.match(h.text(), new RegExp(recoveryIds.qa));
  const review = h.find('form', '六维 U-Voice');
  assert.equal(h.nodes('select').length, 6); assert.ok(h.nodes('select').every(node => node.props.defaultValue === ''));
  assert.match(h.text(), /Voice 修复历史/);
  await h.submit('六维 U-Voice', { likeness: 'pass', naturalness: 'pass', emphasis: 'pass', pace: 'pass', pauses: 'pass', rhythm: 'pass', findings: 'synthetic only', evidence_reference: 'fixture:review' });
  assert.equal(f.fixture.load().review.evidence_reference, 'fixture:review');
  assert.match(h.text(), /已存在不可覆盖/); assert.equal(h.nodes('form').length, 0);
  assert.deepEqual(f.fixture.load().operations, ['read_voice_plan', 'apply_voice_repair', 'advance_voice_qa', 'submit_new_u_voice']);
  h.unmount(); const next = mount('voice', setup('voice', f.storageMap)); await next.flush();
  assert.match(next.text(), /已存在不可覆盖/); assert.match(next.text(), /Voice 修复历史/);
  assert.ok(review); next.unmount();
});
test('presentation UI binds the exact playable fixture hash, displays the semantic candidate and preserves unreviewed successor on reload', async () => {
  for (const kind of ['before', 'after']) assert.equal(createHash('sha256').update(media[kind]).digest('hex'), renderHashes[kind]);
  const f = setup('presentation'); const h = mount('presentation', f); await h.flush(); await observe(h);
  assert.equal(f.fixture.load().observations[0].request.render_sha256, renderHashes.before);
  await h.click('读取修复候选'); assert.match(h.text(), /Fixture complete narration/); assert.match(h.text(), /Fixture short point/);
  assert.match(h.text(), /Voice 0 次 · Talking 0 次/); assert.match(h.text(), new RegExp(recoveryIds.oldAudio));
  await h.change('本次修复理由', 'fixture typography revision'); await h.submit('按此方案建立新 Render');
  assert.equal(f.fixture.load().run.render_job_id, recoveryIds.newJob); assert.match(h.text(), /not_submitted/);
  assert.equal(h.nodes('form').length, 0); f.fixture.complete(); h.unmount();
  const next = mount('presentation', setup('presentation', f.storageMap)); await next.flush();
  assert.match(next.text(), new RegExp(renderHashes.after)); assert.match(next.text(), /not_submitted/); assert.match(next.text(), /技术 QA：verified/);
  assert.equal(next.input('观察理由').props.value, ''); assert.equal(next.input('场景').props.value, '');
  assert.deepEqual(f.fixture.load().operations, ['record_presentation_observation', 'read_presentation_plan', 'apply_presentation_repair']);
  assert.equal(manualComparison.historical_action_count, null); assert.equal(manualComparison.automatic_binding_actions, 0);
  await observe(next); await next.click('读取修复候选'); assert.match(next.text(), /run_repair_allowance_exhausted/);
  assert.ok(!next.nodes('button').some(node => String(node.props.children).includes('按此方案建立新 Render')));
  next.unmount();
});
test('unknown/source findings expose replan links and no apply control; authorization and allowance are visible stops', async () => {
  const f = setup('presentation'); const h = mount('presentation', f); await h.flush(); await observe(h, 'unknown_subtitles');
  await h.click('读取修复候选'); assert.match(h.text(), /presentation_source_interval_or_layout_replan_required/);
  assert.equal(h.nodes('form').filter(node => JSON.stringify(node.props.children).includes('本次修复理由')).length, 0);
  assert.ok(h.nodes('a').some(node => node.props.href === '#production-preflight')); h.unmount();
  const v = setup('voice'); v.fixture.setDenied(true); const vh = mount('voice', v); await vh.flush(); await vh.click('检查 Voice 修复方案');
  assert.match(vh.text(), /voice_repair_consent_changed/); assert.equal(vh.nodes('form').length, 0); vh.unmount();
});
test('Run/input switch cancels pending Voice plan and unlocks controls even after returning to the original identity', async () => {
  const f = setup('voice'); let resolve;
  const waiting = new Promise(value => { resolve = value; });
  const original = f.fixture.load().run;
  const request = (path, options) => path.endsWith('/voice-repair-plan') ? waiting : f.fixture.request(path, options);
  const h = mount('voice', f); h.setProps({ request }); await h.flush();
  await h.click('检查 Voice 修复方案'); h.setProps({ run: { ...original, preflight_fingerprint: 'changed' } }); await h.flush();
  h.setProps({ run: original }); await h.flush();
  resolve(await f.fixture.request(f.fixture.base + '/voice-repair-plan')); await h.flush();
  assert.equal(h.find('button', '检查 Voice 修复方案').props.disabled, false); assert.equal(h.nodes('form').length, 0); h.unmount();
});
test('presentation observation cannot submit old bytes after input switches during async digest', async () => {
  const f = setup('presentation'); let release, intercepted = false;
  const crypto = { subtle: { digest: (...args) => intercepted ? new Promise(resolve => { release = () => webcrypto.subtle.digest(...args).then(resolve); }) : webcrypto.subtle.digest(...args) } };
  const h = mount('presentation', f, { crypto }); await h.flush();
  await h.change('场景', 'scene-1'); await h.change('观察理由', 'fixture'); await h.change('证据引用', 'fixture:evidence');
  intercepted = true; await h.submit('登记此 Render'); assert.ok(release);
  h.setProps({ run: { ...f.fixture.load().run, preflight_fingerprint: 'changed-input' } }); await h.flush();
  await release(); await h.flush(); assert.equal(f.fixture.load().observations.length, 0);
  assert.equal(h.input('观察理由').props.value, ''); h.unmount();
});
test('observation storage failure preserves successful server observation and explains reload limitation', async () => {
  const f = setup('presentation'); const storage = { ...f.storage, setItem(key, value) { if (key.startsWith('content-os-presentation-observation:')) throw new Error('storage denied'); f.storage.setItem(key, value); } };
  const h = mount('presentation', f, { localStorage: storage }); await h.flush(); await observe(h);
  assert.equal(f.fixture.load().observations.length, 1); assert.match(h.text(), /观察已在后台保存/);
  assert.match(h.text(), /刷新后需重新登记观察/); await h.click('读取修复候选'); assert.match(h.text(), /render_revision/); h.unmount();
});
test('late authorization/stale input denial clears the executable plan without creating a replacement', async () => {
  for (const kind of ['voice', 'presentation']) {
    for (const cause of ['authorization', 'version']) {
      const f = setup(kind); const h = mount(kind, f); await h.flush();
      if (kind === 'voice') { await h.click('检查 Voice 修复方案'); await h.change('本次整段修复理由', 'fixture'); }
      else { await observe(h); await h.click('读取修复候选'); await h.change('本次修复理由', 'fixture'); }
      if (cause === 'authorization') f.fixture.setDenied(true); else f.fixture.changeInput();
      await h.submit(kind === 'voice' ? '按此方案创建整段' : '按此方案建立新 Render');
      assert.equal(f.fixture.load().records.length, 0); assert.match(h.text(), cause === 'authorization' ? /changed/ : /stale/);
      assert.ok(!h.nodes('button').some(node => String(node.props.children).includes(kind === 'voice' ? '按此方案创建整段' : '按此方案建立新 Render')));
      h.unmount();
    }
  }
});
test('editing an observation invalidates its old candidate; confirmation refusal performs no write', async () => {
  const f = setup('presentation'); const h = mount('presentation', f); await h.flush(); await observe(h); await h.click('读取修复候选');
  await h.change('所见问题', 'face_out_of_frame');
  assert.ok(!h.nodes('button').some(node => String(node.props.children).includes('按此方案建立新 Render')));
  h.setProps({ confirmAction: () => false }); await h.flush(); await h.submit('登记此 Render');
  assert.equal(f.fixture.load().observations.length, 1); assert.equal(f.fixture.load().records.length, 0); h.unmount();
});
test('replaying the exact Voice/presentation repair keeps successor progress and never restores old bindings', async () => {
  for (const kind of ['voice', 'presentation']) {
    const f = setup(kind); const h = mount(kind, f); let body;
    const request = (path, options) => { if (path.endsWith(`/${kind}-repair`) && options?.method === 'POST') body = JSON.parse(options.body); return f.fixture.request(path, options); };
    h.setProps({ request }); await h.flush();
    if (kind === 'voice') { await h.click('检查 Voice 修复方案'); await h.change('本次整段修复理由', 'fixture'); await h.submit('按此方案创建整段'); }
    else { await observe(h); await h.click('读取修复候选'); await h.change('本次修复理由', 'fixture'); await h.submit('按此方案建立新 Render'); }
    f.fixture.complete(); const before = f.fixture.load();
    await f.fixture.request(f.fixture.base + `/${kind}-repair`, { method: 'POST', body: JSON.stringify(body) });
    const next = setup(kind, f.storageMap).fixture.load(); assert.deepEqual(next.run, before.run); assert.deepEqual(next.records, before.records);
    assert.equal(next.records.length, 1); assert.equal(next.used, 1);
    h.unmount();
  }
});
