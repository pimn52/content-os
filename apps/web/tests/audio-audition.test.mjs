import test from 'node:test';
import assert from 'node:assert/strict';
import { componentHarness } from './hook-harness.mjs';

const file = decodeURI(new URL('../src/AudioAudition.tsx', import.meta.url).pathname).replace(/^\/([A-Z]:)/, '$1');
const access = (id, blocked = false) => ({ audio_id: id, state: blocked ? 'blocked' : 'review_available',
  reasons: blocked ? ['provider_use_admission_revoked'] : [],
  manifest: { lineage: 'known', scopes: [{ purpose: 'internal_evaluation' }] } });

test('evaluation audition requires explicit intent, checks again, preserves token and shows restrictions', async () => {
  const requests = [], media = [], revoked = [];
  const h = componentHarness(file, 'AudioAudition', { audioId: 'audio-1', projectId: 'project-1',
    request: async path => { requests.push(path); return access('audio-1'); } }, {
    fetch: async (path, options) => { media.push({ path, options }); return new Response('synthetic bytes'); },
    window: { sessionStorage: { getItem: () => 'fixture-access-token' } },
    URL: { createObjectURL: () => 'blob:fixture', revokeObjectURL: url => revoked.push(url) },
  });
  await h.flush();
  assert.equal(media.length, 0);
  assert.match(h.text(), /不代表质量通过/);
  await h.click('按内部评估用途检查并试听');
  assert.equal(requests.length, 2);
  assert.match(media[0].path, /project_id=project-1&purpose=internal_evaluation/);
  assert.equal(media[0].options.headers.get('Authorization'), 'Bearer fixture-access-token');
  assert.equal(h.nodes('audio')[0].props.src, 'blob:fixture');
  h.unmount(); assert.deepEqual(revoked, ['blob:fixture']);
});

test('revocation blocks the fresh bytes request while historical manifest remains visible', async () => {
  let media = 0;
  const h = componentHarness(file, 'AudioAudition', { audioId: 'audio-1', projectId: 'project-1',
    request: async () => access('audio-1', true) }, { fetch: async () => { media++; return new Response('bad'); } });
  await h.flush(); await h.click('按内部评估用途检查并试听');
  assert.equal(media, 0); assert.equal(h.nodes('audio').length, 0);
  assert.match(h.text(), /provider_use_admission_revoked/);
  assert.equal(h.nodes('details').length, 1);
  h.unmount();
});

test('project change discards in-flight media and releases prior preview', async () => {
  let deliver;
  const h = componentHarness(file, 'AudioAudition', { audioId: 'audio-1', projectId: 'project-1',
    request: async () => access('audio-1') }, {
    fetch: () => new Promise(resolve => { deliver = resolve; }),
  });
  await h.flush(); await h.click('按内部评估用途检查并试听');
  h.setProps({ projectId: 'project-2' }); await h.flush();
  deliver(new Response('stale bytes')); await h.flush();
  assert.equal(h.nodes('audio').length, 0);
  h.unmount();
});
