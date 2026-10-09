import { readFileSync } from 'node:fs';
import { transformSync } from 'esbuild';
import test from 'node:test';
import assert from 'node:assert/strict';
const source = readFileSync(new URL('../src/production.ts', import.meta.url), 'utf8');
const { code } = transformSync(source, { loader: 'ts', format: 'esm' });
const { startBody, talkingRepairKey, talkingSeriesReviewKey, talkingReviewPolicyVersion, recoveryIdentity, recoveryIdempotencyKey, parseBookmark, productionKey, requestError, productionLabels, canDispatchVoice, voiceCandidateMatches, canReuseTalkingSource, canBindTalkingSource } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
test('recovery requests are scoped to the current candidate and explicit action', async () => {
  const identity = recoveryIdentity('p1', 'r1', 'job-1', 'a'.repeat(64), 'failed');
  assert.equal(identity, recoveryIdentity('p1', 'r1', 'job-1', 'a'.repeat(64), 'failed'));
  assert.notEqual(identity, recoveryIdentity('p1', 'r2', 'job-1', 'a'.repeat(64), 'failed'));
  assert.notEqual(identity, recoveryIdentity('p1', 'r1', 'job-2', 'a'.repeat(64), 'failed'));
  const key = await recoveryIdempotencyKey('presentation-repair', 'p1', 'r1', 'fingerprint-1', { observation_id: 'obs-1', reason: 'fix' });
  assert.match(key, /^web-presentation-repair:p1:r1:fingerprint-1:[a-f0-9]{64}$/);
  assert.notEqual(key, await recoveryIdempotencyKey('presentation-repair', 'p1', 'r1', 'fingerprint-1', { observation_id: 'obs-2', reason: 'fix' }));
  assert.notEqual(key, await recoveryIdempotencyKey('voice-repair', 'p1', 'r1', 'fingerprint-1', { observation_id: 'obs-1', reason: 'fix' }));
});
test('repair idempotency key binds the exact project, Run, scene and evidence fingerprint', () => {
  const key = talkingRepairKey('p1', 'r1', 's1', 'a'.repeat(64));
  assert.equal(key, talkingRepairKey('p1', 'r1', 's1', 'a'.repeat(64)));
  assert.notEqual(key, talkingRepairKey('p2', 'r1', 's1', 'a'.repeat(64)));
  assert.notEqual(key, talkingRepairKey('p1', 'r1', 's2', 'a'.repeat(64)));
  assert.notEqual(key, talkingRepairKey('p1', 'r1', 's1', 'b'.repeat(64)));
});
test('Talking series review cache identity changes with successor media, hash, QA or policy', () => {
  const predecessor = talkingSeriesReviewKey('scene-1', 'series-old', 'asset-old', 'a'.repeat(64), 2, 'job-old:qa-old:verified');
  for (const next of [
    talkingSeriesReviewKey('scene-1', 'series-new', 'asset-new', 'b'.repeat(64), 2, 'job-new:qa-new:verified'),
    talkingSeriesReviewKey('scene-1', 'series-old', 'asset-old', 'a'.repeat(64), 2, 'job-old:qa-next:verified'),
    talkingSeriesReviewKey('scene-1', 'series-old', 'asset-old', 'a'.repeat(64), 1, 'job-old:qa-old:verified'),
  ]) assert.notEqual(next, predecessor);
});
test('new Runs explicitly select v2 while callers can retain v1; identity remains deterministic and scoped', () => {
  const plan = { project_id: 'p1', fingerprint: 'abc' };
  assert.deepEqual(startBody(plan), { idempotency_key: 'web-production:p1:abc', expected_fingerprint: 'abc', talking_review_policy_version: 2 });
  assert.equal(startBody(plan, 1).talking_review_policy_version, 1);
  assert.deepEqual(startBody(plan), startBody({ ...plan }));
  assert.notEqual(startBody(plan).idempotency_key, startBody({ ...plan, fingerprint: 'xyz' }).idempotency_key);
  assert.notEqual(startBody(plan).idempotency_key, startBody({ ...plan, project_id: 'p2' }).idempotency_key);
});
test('persisted Run policy controls the UI; missing historical field remains v1', () => {
  assert.equal(talkingReviewPolicyVersion({ talking_review_policy_version: 2 }), 2);
  assert.equal(talkingReviewPolicyVersion({ talking_review_policy_version: 1 }), 1);
  assert.equal(talkingReviewPolicyVersion({}), 1);
});
test('bookmark cannot cross project or inject paths; invalid storage is safe', () => {
  const id = '00000000-0000-0000-0000-000000000001';
  assert.equal(parseBookmark(JSON.stringify({ project_id: 'p1', id }), 'p1'), id);
  assert.equal(parseBookmark(JSON.stringify({ project_id: 'p1', id }), 'p2'), null);
  for (const raw of [null, 'bad', '{}', '{"project_id":"p1","id":"../other"}']) assert.equal(parseBookmark(raw, 'p1'), null);
  assert.notEqual(productionKey('p1'), productionKey('p2'));
});
test('backend denial reasons and stale errors remain visible', () => {
  assert.equal(requestError({ detail: { code: 'blocked', reasons: ['consent_missing', 'cost_unknown'] } }, 409), 'consent_missing · cost_unknown');
  assert.equal(requestError({ detail: 'stale_production_preflight' }, 409), 'stale_production_preflight');
  assert.equal(requestError({}, 500), '请求失败 (500)');
  assert.equal(requestError({ detail: { code: 'blocked', reasons: [] } }, 409), 'blocked');
});
test('technical completion is never presented as final human approval', () => {
  assert.equal(productionLabels.render_completed_awaiting_review, '渲染完成，尚未最终审查');
  assert.equal(productionLabels.awaiting_u_voice_review, '等待旁白人审');
});
test('Voice dispatch requires explicit matching consent/profile/provider and authorization; backend owns capability gates', () => {
  const profile = { provider: 'fixture', consent: { confirmed: true } };
  const unknownCapability = { provider: 'fixture', readiness: 'configured', quality_status: 'unknown', commercial_status: 'unknown' };
  assert.equal(canDispatchVoice(profile, unknownCapability, 'explicit-ref'), true);
  assert.equal(canDispatchVoice({ ...profile, consent: { confirmed: false } }, unknownCapability, 'explicit-ref'), false);
  assert.equal(canDispatchVoice(profile, { provider: 'other' }, 'explicit-ref'), false);
  assert.equal(canDispatchVoice(profile, unknownCapability, '  '), false);
});
test('candidate audio must carry the current project and exact Voice Job origin', () => {
  const metadata = { voice_generation: { project_id: 'p1', job_id: 'j1' } };
  assert.equal(voiceCandidateMatches(metadata, 'p1', 'j1'), true);
  assert.equal(voiceCandidateMatches(metadata, 'p2', 'j1'), false);
  assert.equal(voiceCandidateMatches(metadata, 'p1', 'j2'), false);
  assert.equal(voiceCandidateMatches({}, 'p1', 'j1'), false);
});
test('Talking source reuse fails closed unless exact current positive receipt is active', () => {
  const valid = { state: 'current', decision: 'review_claimed_suitable', admission: { id: 'receipt', active: true } };
  assert.equal(canReuseTalkingSource(valid), true);
  for (const invalid of [
    { ...valid, state: 'source_stale' }, { ...valid, state: 'revoked' },
    { ...valid, decision: 'unknown' }, { ...valid, admission: { id: 'receipt', active: false } },
    { ...valid, admission: null }, undefined,
  ]) assert.equal(canReuseTalkingSource(invalid), false);
});
test('Talking bind requires selected profile-owned Clip, consent, explicit capability/auth and service-resolved speech span', () => {
  const profile = { consent: { confirmed: true }, reference_clip_ids: ['clip-1'] };
  const cap = { capability: 'talking' };
  const context = { status: 'ready', speech_start_ms: 25, speech_end_ms: 750 };
  assert.equal(canBindTalkingSource(profile, 'clip-1', cap, 'authorization', context), true);
  assert.equal(canBindTalkingSource(profile, 'clip-2', cap, 'authorization', context), false);
  assert.equal(canBindTalkingSource({ ...profile, consent: { confirmed: false } }, 'clip-1', cap, 'authorization', context), false);
  assert.equal(canBindTalkingSource(profile, 'clip-1', { capability: 'voice' }, 'authorization', context), false);
  assert.equal(canBindTalkingSource(profile, 'clip-1', cap, ' ', context), false);
  assert.equal(canBindTalkingSource(profile, 'clip-1', cap, 'authorization', { ...context, status: 'unresolved' }), false);
  assert.equal(canBindTalkingSource(profile, 'clip-1', cap, 'authorization', { ...context, speech_end_ms: 25 }), false);
});
