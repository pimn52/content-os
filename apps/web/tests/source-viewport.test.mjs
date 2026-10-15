import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { componentHarness } from './hook-harness.mjs';
const file = fileURLToPath(new URL('../src/SourceViewportPanel.tsx', import.meta.url));
const asset = { id: 'A', content_hash: 'a'.repeat(64), source_file: 'source.mp4', width: 1280, height: 720, duration_ms: 1000 };
const clip = { id: 'C', asset_id: 'A', start_ms: 0, end_ms: 1000 };
function setup(requestOverride, refreshOverride) {
  const calls = []; let refreshes = 0;
  const request = async (path, options) => { calls.push({ path, body: JSON.parse(options.body) });
    return requestOverride ? requestOverride(path, options) : { asset: { ...asset, id: 'new', width: 1080, height: 1440 }, clip: { ...clip, id: 'new-clip' } }; };
  const h = componentHarness(file, 'SourceViewportPanel', { assets: [asset], clips: [clip], request,
    onCreated: async () => { refreshes++; if (refreshOverride) await refreshOverride(); } });
  return { h, calls, refreshes: () => refreshes };
}
async function fill(h) {
  await h.flush(); await h.change('适配素材', 'A'); await h.change('来源区间', 'C');
  for (const [label, value] of [['裁剪左边距', '100'], ['裁剪上边距', '0'], ['裁剪宽度', '450'], ['裁剪高度', '600'], ['开始毫秒', '0'], ['结束毫秒', '1000'], ['原画面字幕', 'not_observed'], ['完整区间证据引用', 'human:full-interval']]) await h.change(label, value);
  await h.change('当前裁剪完整区间检查', 'reviewed');
}
test('explicit reviewed crop uses existing API and stable idempotency; refreshes library', async () => {
  const s = setup(); await fill(s.h);
  await s.h.submit('人物画面比例适配'); await s.h.submit('人物画面比例适配');
  assert.equal(s.calls.length, 2); assert.equal(s.calls[0].path, '/assets/vertical-derivations');
  assert.equal(s.calls[0].body.idempotency_key, s.calls[1].body.idempotency_key);
  assert.equal(s.calls[0].body.output_height, 1440);
  assert.deepEqual(s.calls[0].body.crop_window, { x: 100, y: 0, width: 450, height: 600 });
  assert.equal(s.calls[0].body.evidence.source_content_hash, asset.content_hash);
  assert.equal(s.calls[0].body.evidence.coverage, 'full_interval_continuous');
  assert.equal(s.refreshes(), 2); assert.match(s.h.text(), /成片构图和人物质量仍需实际检查/);
});
test('geometry, interval, unknown subtitle and missing review fail before API', async () => {
  for (const [label, value, message] of [['裁剪宽度', '451', /比例须/], ['结束毫秒', '1001', /区间须/], ['原画面字幕', '', /未知状态/], ['裁剪左边距', '1000', /超出源画面/]]) {
    const s = setup(); await fill(s.h); await s.h.change(label, value); await s.h.change('当前裁剪完整区间检查', 'reviewed');
    await s.h.submit('人物画面比例适配'); assert.equal(s.calls.length, 0); assert.match(s.h.text(), message);
  }
  const s = setup(); await fill(s.h); await s.h.change('源视口比例', '1680');
  assert.equal(s.h.input('当前裁剪完整区间检查').props.value, '');
  assert.equal(s.h.find('button', '创建已检查').props.disabled, true);
});
test('known subtitle overlap blocks; modified fields revoke previous review', async () => {
  const s = setup(); await fill(s.h); await s.h.change('原画面字幕', 'known_burned_in');
  for (const [label, value] of [['原字幕左边距', '0'], ['原字幕上边距', '500'], ['原字幕宽度', '1280'], ['原字幕高度', '100']]) await s.h.change(label, value);
  await s.h.change('当前裁剪完整区间检查', 'reviewed'); await s.h.submit('人物画面比例适配');
  assert.equal(s.calls.length, 0); assert.match(s.h.text(), /与已知原字幕重叠/);
  await s.h.change('裁剪左边距', '102'); assert.equal(s.h.input('当前裁剪完整区间检查').props.value, '');
});
test('late request cannot announce or refresh after source replacement; double submit is locked', async () => {
  let resolve; const pending = new Promise(r => { resolve = r; });
  const s = setup(() => pending); await fill(s.h);
  await s.h.submit('人物画面比例适配'); await s.h.submit('人物画面比例适配'); assert.equal(s.calls.length, 1);
  s.h.setProps({ assets: [{ ...asset, content_hash: 'b'.repeat(64) }] }); await s.h.flush();
  resolve({ asset: { width: 1080, height: 1440 }, clip }); await s.h.flush();
  assert.equal(s.refreshes(), 0); assert.doesNotMatch(s.h.text(), /已进入素材库/);
  assert.equal(s.h.input('完整区间证据引用').props.value, '');
});
test('API rejection retained; successful creation followed by refresh failure remains distinguishable', async () => {
  const fail = setup(async () => { throw new Error('TalkingRun source is not admitted'); }); await fill(fail.h);
  await fail.h.submit('人物画面比例适配'); assert.match(fail.h.text(), /not admitted/); assert.equal(fail.refreshes(), 0);
  const refreshFail = setup(null, async () => { throw new Error('offline'); }); await fill(refreshFail.h);
  await refreshFail.h.submit('人物画面比例适配'); assert.match(refreshFail.h.text(), /新素材已创建，素材库刷新失败/);
});
test('authenticated source preview verifies bytes, draws exact crop and releases object URL', async () => {
  const bytes = 'synthetic media bytes'; const previewAsset = { ...asset, content_hash: createHash('sha256').update(bytes).digest('hex') };
  let header; const revoked = [];
  const h = componentHarness(file, 'SourceViewportPanel', { assets: [previewAsset], clips: [clip], request: async () => {}, onCreated: async () => {} }, {
    window: { sessionStorage: { getItem: () => 'fixture-token' } },
    fetch: async (path, options) => { assert.equal(path, '/assets/A/media'); header = options.headers.get('Authorization'); return new Response(new Blob([bytes])); },
    URL: { createObjectURL: () => 'blob:verified-source', revokeObjectURL: url => revoked.push(url) },
  });
  await fill(h); await h.click('播放源视频'); assert.equal(header, 'Bearer fixture-token');
  assert.equal(h.nodes('video')[0].props.src, 'blob:verified-source');
  assert.equal(h.nodes('div').find(node => node.props.style?.maxWidth === 360).props.style.width, '100%');
  const overlay = h.nodes('div').find(node => node.props['aria-label'] === '当前固定裁剪范围');
  assert.equal(overlay.props.style.width, `${100 * 450 / 1280}%`);
  assert.match(h.text(), /单帧预览不能证明完整区间安全/);
  h.setProps({ assets: [] }); await h.flush(); assert.equal(h.nodes('video').length, 0); assert.deepEqual(revoked, ['blob:verified-source']);
});
test('changed preview bytes revoke review and never expose video', async () => {
  const h = componentHarness(file, 'SourceViewportPanel', { assets: [asset], clips: [clip], request: async () => {}, onCreated: async () => {} }, {
    fetch: async () => new Response(new Blob(['changed bytes'])),
  });
  await fill(h); await h.click('播放源视频'); assert.equal(h.nodes('video').length, 0);
  assert.equal(h.input('当前裁剪完整区间检查').props.value, ''); assert.match(h.text(), /源视频文件已变化/);
});
function feasibility(body, stop_reasons = []) { return { request: body, status: stop_reasons.length ? 'blocked' : 'needs_review', stop_reasons, source_hash_verified: true, crop_assessment: stop_reasons.length ? 'unusable' : 'unknown', retained_source_fraction: .293, upscale_factor: 2.4 }; }
test('read-only feasibility precedes review, discloses actual tradeoffs and never grants positive quality', async () => {
  const s = setup(async (_, options) => feasibility(JSON.parse(options.body))); await fill(s.h);
  await s.h.change('当前裁剪完整区间检查', ''); await s.h.click('检查裁剪可行性');
  assert.equal(s.calls[0].path, '/assets/A/viewport-feasibility'); assert.equal(s.calls[0].body.evidence, undefined);
  assert.match(s.h.text(), /29.3%/); assert.match(s.h.text(), /放大 2.40 倍/); assert.match(s.h.text(), /人物完整区间安全：未知/);
  assert.equal(s.h.input('当前裁剪完整区间检查').props.value, ''); assert.equal(s.refreshes(), 0);
  await s.h.change('裁剪左边距', '101'); assert.doesNotMatch(s.h.text(), /29.3%/);
});
test('known negative feasibility stops create; stale geometry result never returns', async () => {
  const negative = setup(async (_, options) => feasibility(JSON.parse(options.body), ['persisted_face_boundary_failure'])); await fill(negative.h);
  await negative.h.click('检查裁剪可行性'); assert.match(negative.h.text(), /已有脸部越界证据/);
  await negative.h.change('当前裁剪完整区间检查', 'reviewed'); assert.equal(negative.h.find('button', '创建已检查').props.disabled, true);
  let resolve; const pending = new Promise(r => { resolve = r; }); const stale = setup(() => pending); await fill(stale.h);
  await stale.h.click('检查裁剪可行性'); const body = stale.calls[0].body;
  await stale.h.change('裁剪左边距', '102'); resolve(feasibility(body)); await stale.h.flush();
  assert.doesNotMatch(stale.h.text(), /29.3%/); assert.equal(stale.h.find('button', '检查裁剪可行性').props.disabled, false);
});
