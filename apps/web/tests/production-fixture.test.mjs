import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { transformSync } from 'esbuild';

// Execute only the fixture's request/state functions. No DOM, browser or network.
const source = readFileSync(new URL('./production-fixture.tsx', import.meta.url), 'utf8')
  .split('function Fixture()')[0].replace(/^import .*;\r?\n/gm, '');
const code = transformSync(source + '\nglobalThis.fixture = { request, seedRepairFixture, completeReplacementGeneration, saved, repairRecordsKey, projectId, id, scenePlanId, sourceAssetId, replacementChildAssetId, talkingSeriesId, replacementSeriesId, replacementPreviewAssetId };', { loader: 'tsx' }).code;
function fixture(storage = new Map()) {
  const context = { URLSearchParams, location: { search: '?talking&policy=2&repair' },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key) },
    window: { location: { reload() {} }, dispatchEvent() {} }, Event: class {}, setTimeout };
  runInNewContext(code, context);
  const f = context.fixture;
  return { ...f, storage, run: () => JSON.parse(storage.get(f.saved)),
    base: `/projects/${f.projectId}/production-runs/${f.id}/talking-`,
    post: path => f.request(path, { method: 'POST', body: JSON.stringify({ idempotency_key: 'test-repair', reason: 'fixture explicit repair' }) }) };
}

test('replacement starts pending without output/preview and retains exact predecessor history', async () => {
  const f = fixture(); f.seedRepairFixture('rejected');
  await f.post(f.base + 'repair');
  assert.equal(f.run().talking_qa_dependencies[0].state, 'generation_pending');
  assert.equal(f.run().talking_qa_dependencies[0].output_asset_id, null);
  assert.equal(f.run().talking_preview_dependencies.length, 0);
  const [record] = await f.request(f.base + 'repairs');
  assert.equal(record.old_binding.talking_output_asset_id, f.sourceAssetId);
});

test('idempotent replay cannot erase successor progress', async () => {
  const f = fixture(); f.seedRepairFixture('rejected');
  await f.post(f.base + 'repair');
  const run = f.run(); run.talking_preview_dependencies = [{ series_id: f.replacementSeriesId, output_asset_id: f.replacementPreviewAssetId }];
  f.storage.set(f.saved, JSON.stringify(run));
  await f.post(f.base + 'repair');
  assert.equal(f.run().talking_preview_dependencies[0]?.series_id, f.replacementSeriesId);
  assert.equal((await f.request(f.base + 'repairs')).length, 1);
});

test('technical failure history does not invent a rejected predecessor preview', async () => {
  const f = fixture(); f.seedRepairFixture('technical_failure');
  const record = await f.post(f.base + 'repair');
  assert.equal(record.plan.failure_kind, 'technical');
  assert.equal(record.plan.findings.length, 0);
  assert.equal(record.predecessor_series_id, null);
  assert.equal(record.old_binding.talking_output_asset_id, null);
});

test('reseed isolates allowance and records; negative concern keeps original finding', async () => {
  const f = fixture(); f.seedRepairFixture('rejected'); await f.post(f.base + 'repair');
  f.seedRepairFixture('negative_concern');
  assert.equal((await f.request(f.base + 'repairs')).length, 0);
  const record = await f.post(f.base + 'repair');
  assert.equal(record.plan.findings[0].dimension, 'artifacts');
  assert.equal(record.plan.evidence[0].type, 'negative_concern_answer');
});

test('Worker completion, fresh QA and distinct unreviewed preview survive offline reload', async () => {
  const f = fixture(); f.seedRepairFixture('rejected'); await f.post(f.base + 'repair');
  await assert.rejects(f.post(f.base + 'qa-advance'), /talking_output_not_ready/);
  await assert.rejects(f.post(f.base + 'preview-prepare'), /talking_qa_not_verified/);
  f.completeReplacementGeneration();
  assert.equal(f.run().talking_qa_dependencies[0].state, 'output_awaiting_qa');
  assert.equal((await f.request(`/assets/${f.replacementChildAssetId}`)).id, f.replacementChildAssetId);
  await assert.rejects(f.post(f.base + 'preview-prepare'), /talking_qa_not_verified/);
  await f.post(f.base + 'qa-advance'); await f.post(f.base + 'preview-prepare');
  const next = fixture(f.storage);
  const preview = next.run().talking_preview_dependencies[0];
  assert.equal(preview.series_id, f.replacementSeriesId);
  assert.equal(preview.output_asset_id, f.replacementPreviewAssetId);
  assert.equal(preview.continuity_review_state, 'not_submitted');
  assert.equal(JSON.parse(f.storage.get(`fixture-aggregate-review:${f.talkingSeriesId}`)).approved, false);
  const series = await next.request(`/projects/${f.projectId}/talking-slice-series/${f.replacementSeriesId}`);
  assert.equal(series.required_dimensions.length, 6);
  assert.equal(series.continuity_review_state, 'not_submitted');
  assert.equal((await next.request(f.base + 'repairs'))[0].successor_series_id, f.replacementSeriesId);
  const exhausted = await next.request(f.base + `repair-plan?scene_plan_id=${f.scenePlanId}`);
  assert.equal(exhausted.action, 'stop');
  await assert.rejects(next.request(f.base + 'repair', { method: 'POST', body: JSON.stringify({ idempotency_key: 'another' }) }), /run_repair_allowance_exhausted/);
});
