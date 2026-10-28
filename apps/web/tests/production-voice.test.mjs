import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { componentHarness } from './hook-harness.mjs';

const file = fileURLToPath(new URL('../src/ProductionVoicePanel.tsx', import.meta.url));
const voiceRun = (changes = {}) => ({
  id: 'run-1', project_id: 'project-1', status: 'awaiting_u_voice_review', preflight_fingerprint: 'fingerprint-1',
  render_job_id: null, voice_job_id: 'voice-job-1', voice_audio_id: 'audio-1', voice_qa_job_id: 'qa-1',
  waiting_stop_reasons: [], voice_qa_auto_stop_reasons: [], talking_dependencies: [], ...changes,
});
const audioAsset = (generation = {}) => ({
  id: 'audio-1', content_hash: 'a'.repeat(64), duration_ms: 5_000, authorization_reference: 'imported-source',
  metadata: { voice_generation: { project_id: 'project-1', job_id: 'voice-job-1', qa_state: 'verified', ...generation } },
});

function fixture({ audio = audioAsset(), run = voiceRun() } = {}) {
  const calls = [];
  let currentRun = run;
  const request = async (path, options) => {
    calls.push({ path, options });
    if (path.endsWith('/voice-repairs')) return [];
    if (path.startsWith('/voice-profiles?')) return [];
    if (path === '/execution-settings/capabilities') return [];
    if (path === '/audio-assets/audio-1') return audio;
    if (path.endsWith('/human-review') && options?.method === 'POST') return audio;
    if (path === '/projects/project-1/production-runs/run-1') return currentRun;
    throw new Error(`unexpected fixture request: ${path}`);
  };
  const runChanges = [];
  const create = () => componentHarness(file, 'ProductionVoicePanel', {
    projectId: 'project-1', draftVersion: 4, scriptRevision: 2, run: currentRun, request,
    confirmAction: () => true, onRunChange: updated => { runChanges.push(updated); currentRun = updated; },
  }, { fetch: async () => ({ ok: true, blob: async () => new Blob(['audio bytes']) }) });
  return { calls, create, runChanges };
}

test('ordinary ProductionRun human review stays bound to the exact QA-verified AudioAsset', async () => {
  const f = fixture();
  const h = f.create();
  await h.flush();
  assert.match(h.text(), /QA 状态：verified/);
  assert.equal(h.nodes('audio').length, 1);
  const fields = {
    likeness: 'pass', naturalness: 'pass', emphasis: 'pass', pace: 'pass', pauses: 'pass', rhythm: 'pass',
    evidence_reference: 'owner-review:production-run-1', findings: '口齿清晰，节奏自然。',
  };
  await h.submit('针对上述音频提交六维 U-Voice 判断', fields);
  const post = f.calls.find(call => call.path.endsWith('/voice-assets/audio-1/human-review'));
  assert.ok(post);
  const body = JSON.parse(post.options.body);
  assert.equal(body.approved, true);
  assert.equal(body.evidence_reference, fields.evidence_reference);
  assert.equal(body.rhythm, 'pass');
  assert.equal(f.runChanges.length, 1);
  h.unmount();
});

test('mismatched Voice Job audio cannot be auditioned or human reviewed', async () => {
  const f = fixture({ audio: audioAsset({ job_id: 'another-job' }) });
  const h = f.create();
  await h.flush();
  assert.match(h.text(), /Voice Job\/项目来源不一致/);
  assert.equal(h.nodes('audio').length, 0);
  assert.equal(h.nodes('form').length, 0);
  assert.equal(f.calls.some(call => call.options?.method === 'POST'), false);
  h.unmount();
});
