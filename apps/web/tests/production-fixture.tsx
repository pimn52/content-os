import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ProductionPanel } from '../src/ProductionPanel';
import type { ProductionPlan, ProductionRun } from '../src/production';
import '../src/styles.css';
const id = '00000000-0000-0000-0000-000000000075';
const voiceJob = '00000000-0000-0000-0000-000000000076';
const audioId = '00000000-0000-0000-0000-000000000077';
const profileId = '00000000-0000-0000-0000-000000000078';
const capabilityId = '00000000-0000-0000-0000-000000000079';
const scenePlanId = '00000000-0000-0000-0000-000000000080';
const talkingProfileId = '00000000-0000-0000-0000-000000000081';
const talkingCapabilityId = '00000000-0000-0000-0000-000000000082';
const sourceAssetId = '00000000-0000-0000-0000-000000000083';
const sourceClipId = '00000000-0000-0000-0000-000000000084';
const sourceAssessmentId = '00000000-0000-0000-0000-000000000085';
const talkingJobId = '00000000-0000-0000-0000-000000000086';
const previewAssetId = '00000000-0000-0000-0000-000000000087';
const talkingSeriesId = '00000000-0000-0000-0000-000000000088';
const replacementJobId = '00000000-0000-0000-0000-000000000089';
const replacementChildAssetId = '00000000-0000-0000-0000-000000000090';
const replacementPreviewAssetId = '00000000-0000-0000-0000-000000000091';
const replacementSeriesId = '00000000-0000-0000-0000-000000000092';
const previewHash = 'e'.repeat(64);
const replacementPreviewHash = 'd'.repeat(64);
const sourceHash = 'b'.repeat(64);
const saved = 'content-os-fixture-run';
const talkingMode = new URLSearchParams(location.search).has('talking');
const projectId = talkingMode ? `fixture-v76a2-talking-v${new URLSearchParams(location.search).get('policy') === '2' ? 2 : 1}` : 'fixture-v75b1';
const repairMode = new URLSearchParams(location.search).has('repair');
const repairRecordsKey = `fixture-talking-repairs:${projectId}`;
const actionCountsKey = 'content-os-v76a2-review-actions';
const concernsKey = `content-os-v76a2-concerns:${projectId}`;
const fixturePolicy = new URLSearchParams(location.search).get('policy') === '2' ? 2 : 1;
let version = 1;
let stale = false;
let slow = false;
let mediaFailure = false;
let sourceApplyFailure = false;
let reviewSubmitted = false;
let fixtureChildReview: { approved: boolean; evidence_reference: string; findings: string[] } | null = null;
let fixtureConcerns: Array<{ id: string; finding: { dimension: string; reason: string; start_ms?: number; end_ms?: number }; evidence_reference: string; answer: null | { approved: boolean; evidence_reference: string; reason: string } }> = (() => { try { return JSON.parse(localStorage.getItem(concernsKey) ?? '[]'); } catch { return []; } })();
let childSubjectiveSubmissions = 0;
let aggregateSubjectiveSubmissions = 0;
function fixtureWav() {
  const samples = 2400;
  const bytes = new ArrayBuffer(44 + samples * 2);
  const view = new DataView(bytes);
  const fourcc = (offset: number, value: string) => [...value].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
  fourcc(0, 'RIFF'); view.setUint32(4, 36 + samples * 2, true); fourcc(8, 'WAVE'); fourcc(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 24000, true); view.setUint32(28, 48000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  fourcc(36, 'data'); view.setUint32(40, samples * 2, true);
  return bytes;
}
const plan = (): ProductionPlan => ({ project_id: projectId, draft_version: version, script_revision: 1,
  fingerprint: `fixture-${version}`, status: 'blocked', evidence_level: 'deterministic_metadata_only',
  cost_estimate: { known_amount: '0', currency: 'USD', unknown_cost_count: 2 },
  estimated_local_runtime_ms: null, estimated_user_active_minutes: null,
  stop_reasons: ['fixture_suitability_unknown'], authorization_needs: ['fixture_consent_required'],
  actions: [{ kind: 'voice', required: true, reason: 'missing_master', cost: { amount: null, currency: null } }],
  scenes: [{ scene_plan_id: 'scene-plan-1', scene_id: 'scene-1', production_need: 'new_talking', suitability: 'unknown', subtitle_treatment: 'suppress', burned_in_subtitles: 'unknown', reasons: ['fixture_subtitle_unknown'] }] });
const initial = (): ProductionRun => ({ id, project_id: projectId, status: talkingMode ? 'awaiting_talking_dependencies' : 'awaiting_approved_master', preflight_fingerprint: `fixture-${version}`, ...(talkingMode ? { talking_review_policy_version: fixturePolicy as 1 | 2 } : {}), render_job_id: null, voice_job_id: null, voice_audio_id: null, voice_qa_job_id: null, voice_qa_auto_stop_reasons: ['fixture_qa_stop'], waiting_stop_reasons: talkingMode ? ['fixture_source_waiting'] : ['approved_master_required'], talking_dependencies: [{ scene_plan_id: scenePlanId, scene_id: 'scene-1' }], ...(talkingMode ? { talking_master_audio_id: audioId, talking_source_bindings: [], talking_qa_dependencies: [], talking_preview_dependencies: [] } : {}) });
function seedRepairFixture(kind: 'rejected' | 'negative_concern' | 'technical_failure') {
  resetRepairFixture();
  const value = initial(); value.status = 'awaiting_talking_dependencies'; value.talking_master_audio_id = audioId;
  value.talking_source_bindings = [{ scene_plan_id: scenePlanId, talking_profile_id: talkingProfileId, reference_clip_id: sourceClipId, reference_asset_id: sourceAssetId, reference_asset_hash: sourceHash, reference_start_ms: 0, reference_end_ms: 12000, source_authorization_reference: 'fixture:rights', reference_assessment_reference: 'fixture:reference-assessment', performance_brief: {}, capability_profile_id: talkingCapabilityId, capability_evidence_reference: 'fixture:capability', license_evidence_reference: 'fixture:license', master_audio_id: audioId, master_start_ms: 250, master_end_ms: 2200, authorization_reference: 'fixture:authorization', suitability: 'review_claimed_suitable', suitability_assessment_id: sourceAssessmentId, talking_job_id: kind === 'technical_failure' ? talkingJobId : talkingJobId, talking_output_asset_id: kind === 'technical_failure' ? null : sourceAssetId, talking_qa_job_id: voiceJob, talking_series_id: kind === 'technical_failure' ? null : talkingSeriesId, talking_preview_job_id: kind === 'technical_failure' ? null : voiceJob, talking_run_id: null }];
  value.talking_qa_dependencies = [{ scene_plan_id: scenePlanId, generation_job_id: talkingJobId, output_asset_id: kind === 'technical_failure' ? null : sourceAssetId, qa_job_id: voiceJob, state: kind === 'technical_failure' ? 'generation_failed' : 'qa_verified_ready_for_preview' }];
  value.talking_preview_dependencies = kind === 'technical_failure' ? [] : [{ scene_plan_id: scenePlanId, series_id: talkingSeriesId, job_id: voiceJob, output_asset_id: previewAssetId, state: 'completed_review_only', child_review_state: 'pending', continuity_review_state: kind === 'negative_concern' ? 'approved' : 'rejected' }];
  localStorage.setItem(`content-os-production:${projectId}`, JSON.stringify({ project_id: projectId, id })); localStorage.setItem(saved, JSON.stringify(value));
  fixtureConcerns = kind === 'negative_concern' ? [{ id: 'fixture-negative-concern', finding: { dimension: 'artifacts', reason: 'fixture approved preview concern' }, evidence_reference: 'fixture:concern-evidence', answer: { approved: false, evidence_reference: 'fixture:answer-evidence', reason: 'fixture confirmed issue' } }] : [];
  localStorage.setItem(concernsKey, JSON.stringify(fixtureConcerns));
  if (kind !== 'technical_failure') localStorage.setItem(`fixture-aggregate-review:${talkingSeriesId}`, JSON.stringify({ approved: kind === 'negative_concern', findings: kind === 'rejected' ? ['fixture rejection stays immutable'] : [], evidence_reference: `fixture:${kind}`, dimensions: { visible_sync: kind === 'rejected' ? 'fail' : 'pass', identity: 'pass', artifacts: 'pass', source_performance: 'pass', continuity: 'pass', publishability: 'pass' } }));
  window.location.reload();
}
function resetRepairFixture() {
  for (const key of [repairRecordsKey, 'fixture-replacement-count', `fixture-aggregate-review:${talkingSeriesId}`, `fixture-aggregate-review:${replacementSeriesId}`, concernsKey]) localStorage.removeItem(key);
  fixtureConcerns = []; fixtureChildReview = null;
}
function completeReplacementGeneration() {
  const value: ProductionRun = JSON.parse(localStorage.getItem(saved) ?? JSON.stringify(initial()));
  const qa = value.talking_qa_dependencies?.[0];
  if (qa?.generation_job_id !== replacementJobId || qa.state !== 'generation_pending') return;
  qa.state = 'output_awaiting_qa'; qa.output_asset_id = replacementChildAssetId;
  const binding = value.talking_source_bindings?.[0];
  if (binding) binding.talking_output_asset_id = replacementChildAssetId;
  localStorage.setItem(saved, JSON.stringify(value));
  const records = JSON.parse(localStorage.getItem(repairRecordsKey) ?? '[]');
  localStorage.setItem(repairRecordsKey, JSON.stringify(records.map((item: { replacement_job_id: string }) => item.replacement_job_id === replacementJobId ? { ...item, replacement_job_status: 'completed' } : item)));
}
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  if (path.endsWith('/voice-repairs') || path.endsWith('/presentation-repairs')) return [] as T;
  const repairBase = `/projects/${projectId}/production-runs/${id}/talking-`;
  const runState = JSON.parse(localStorage.getItem(saved) ?? 'null');
  const activePreview = runState?.talking_preview_dependencies?.[0];
  const activeSeriesId = activePreview?.series_id ?? talkingSeriesId;
  const activePreviewAssetId = activePreview?.output_asset_id;
  if (path === `${repairBase}repairs`) return JSON.parse(localStorage.getItem(repairRecordsKey) ?? '[]') as T;
  if (path === `${repairBase}repair-plan?scene_plan_id=${scenePlanId}`) { const qa = runState?.talking_qa_dependencies?.[0]; const technical = qa?.state === 'generation_failed'; const negative = fixtureConcerns.find(item => item.answer?.approved === false); const used = JSON.parse(localStorage.getItem(repairRecordsKey) ?? '[]').length; return { project_id: projectId, run_id: id, scene_plan_id: scenePlanId, fingerprint: 'a'.repeat(64), failure_kind: technical ? 'technical' : 'quality', action: used ? 'stop' : 'regenerate_scene', stop_reasons: used ? ['run_repair_allowance_exhausted'] : [], evidence: [{ type: technical ? 'generation_job' : negative ? 'negative_concern_answer' : 'fixture_rejected_preview', ...(negative ? { concern: negative } : {}) }], findings: technical ? [] : negative ? [negative.finding] : [{ dimension: 'visible_sync', reason: 'fixture:局部可见同步偏差', start_ms: 420, end_ms: 780 }], master_audio_id: audioId, master_start_ms: 250, master_end_ms: 2200, remaining_run_repairs: used ? 0 : 1, max_provider_calls: 1, external_charge_ceiling: '0', currency: 'USD', local_compute_cost: null, user_active_minutes: null, review_scope: 'fresh_child_qa_and_policy_required_whole_result_review' } as T; }
  if (path === `${repairBase}repair` && options?.method === 'POST') {
    const old = JSON.parse(localStorage.getItem(saved) ?? JSON.stringify(initial()));
    const payload = JSON.parse(String(options.body));
    const records = JSON.parse(localStorage.getItem(repairRecordsKey) ?? '[]');
    const replay = records.find((item: { idempotency_key?: string }) => item.idempotency_key === payload.idempotency_key);
    if (replay) return replay as T;
    const repairPlan = await request<{ action: string }>(`${repairBase}repair-plan?scene_plan_id=${scenePlanId}`);
    if (repairPlan.action !== 'regenerate_scene') throw new Error('run_repair_allowance_exhausted');
    const record = { id: 'fixture-repair-1', project_id: projectId, run_id: id, scene_plan_id: scenePlanId, idempotency_key: payload.idempotency_key, replacement_job_id: replacementJobId, predecessor_series_id: old.talking_source_bindings[0].talking_series_id, successor_series_id: null, reused_master_audio_id: audioId, reason: payload.reason, plan: repairPlan, old_binding: { ...old.talking_source_bindings[0] }, replacement_job_status: 'queued', provider_calls: [], predecessor_provider_calls: [{ actual_cost: null }], created_at: new Date().toISOString() };
    records.push(record); localStorage.setItem('fixture-replacement-count', '1');
    localStorage.setItem(repairRecordsKey, JSON.stringify(records));
    fixtureConcerns = []; localStorage.setItem(concernsKey, '[]');
    old.talking_qa_dependencies = [{ scene_plan_id: scenePlanId, generation_job_id: record.replacement_job_id, output_asset_id: null, qa_job_id: null, state: 'generation_pending' }];
    old.talking_preview_dependencies = [];
    old.talking_source_bindings[0].talking_job_id = record.replacement_job_id; old.talking_source_bindings[0].talking_output_asset_id = null; old.talking_source_bindings[0].talking_qa_job_id = null; old.talking_source_bindings[0].talking_series_id = null; old.talking_source_bindings[0].talking_preview_job_id = null; old.talking_source_bindings[0].talking_run_id = null;
    localStorage.setItem(saved, JSON.stringify(old)); return record as T;
  }
  if (path === '/talking-profiles') return [{ id: talkingProfileId, name: 'Fixture Talking', provider: 'fixture-talking', provider_profile_id: 'talking-fixture', reference_clip_ids: [sourceClipId], consent: { subject_name: 'Fixture', confirmed: true, authorization_reference: 'fixture:talking-consent' } }] as T;
  if (path === '/assets') { const value = JSON.parse(localStorage.getItem(saved) ?? 'null'); const qa = value?.talking_qa_dependencies?.[0]; const preview = value?.talking_preview_dependencies?.[0]; return [{ id: sourceAssetId, content_hash: sourceHash, width: 720, height: 1280, authorization_reference: 'fixture:rights', metadata: { ...(fixtureChildReview ? { talking_generation: { human_review: fixtureChildReview } } : {}) } }, ...(qa?.output_asset_id === replacementChildAssetId ? [{ id: replacementChildAssetId, content_hash: 'c'.repeat(64), width: 720, height: 1280, authorization_reference: 'fixture:authorization', metadata: { talking_generation: { project_id: projectId, job_id: replacementJobId } } }] : []), ...(preview?.output_asset_id === previewAssetId ? [{ id: previewAssetId, content_hash: previewHash, width: 720, height: 1280, authorization_reference: 'fixture:authorization', metadata: { talking_run_preview: { technical_qa_state: 'verified', origin_sha256: 'f'.repeat(64) } } }] : []), ...(preview?.output_asset_id === replacementPreviewAssetId ? [{ id: replacementPreviewAssetId, content_hash: replacementPreviewHash, width: 720, height: 1280, authorization_reference: 'fixture:authorization', metadata: { talking_run_preview: { technical_qa_state: 'verified', origin_sha256: 'a'.repeat(64) } } }] : [])] as T; }
  if (path === `/assets/${previewAssetId}`) return { id: previewAssetId, content_hash: previewHash, width: 720, height: 1280, authorization_reference: 'fixture:authorization', metadata: { talking_run_preview: { technical_qa_state: 'verified', origin_sha256: 'f'.repeat(64) } } } as T;
  if (path === `/assets/${replacementPreviewAssetId}`) return { id: replacementPreviewAssetId, content_hash: replacementPreviewHash, width: 720, height: 1280, authorization_reference: 'fixture:authorization', metadata: { talking_run_preview: { technical_qa_state: 'verified', origin_sha256: 'a'.repeat(64) } } } as T;
  if (path === `/assets/${replacementChildAssetId}`) return { id: replacementChildAssetId, content_hash: 'c'.repeat(64), width: 720, height: 1280, authorization_reference: 'fixture:authorization', metadata: { talking_generation: { project_id: projectId, job_id: replacementJobId } } } as T;
  if (path === `/assets/${sourceAssetId}/clips`) return [{ id: sourceClipId, asset_id: sourceAssetId, start_ms: 0, end_ms: 12000, orientation: 'portrait', talking_candidate: true, face_visibility: 0.9, mouth_visibility: 0.8, talking_reference_assessment: { burned_in_subtitles: false, evidence_reference: 'fixture:reference-assessment' } }] as T;
  if (path === '/execution-settings/capabilities') return [
    { id: capabilityId, scope_key: 'voice:fixture', capability: 'voice', mode: 'local', provider: 'fixture-provider', model: 'fixture-model', runtime: 'fixture-runtime', machine_id: 'fixture-machine', readiness: 'configured', quality_status: 'unknown', commercial_status: 'unknown', evidence_reference: null, license_evidence_reference: null, provenance_source: 'fixture:manual', feature_support: {} },
    { id: talkingCapabilityId, scope_key: 'talking:fixture', capability: 'talking', mode: 'local', provider: 'fixture-talking', model: 'fixture-model', runtime: 'fixture-runtime', machine_id: 'fixture-machine', readiness: 'configured', quality_status: 'unknown', commercial_status: 'unknown', evidence_reference: null, license_evidence_reference: null, provenance_source: 'fixture:manual', feature_support: {} },
  ] as T;
  if (path.includes('/talking-source-context?')) return { project_id: projectId, run_id: id, scene_plan_id: scenePlanId, scene_id: 'scene-1', scene_voice_text: 'Fixture scene speech.', preflight_fingerprint: `fixture-${version}`, visual_dependency_fingerprint: 'c'.repeat(64), master_audio_id: audioId, master_content_hash: 'a'.repeat(64), status: 'ready', reason: null, speech_start_ms: 250, speech_end_ms: 2200 } as T;
  if (path === `/clips/${sourceClipId}/talking-source-reviews`) return [{ assessment_id: sourceAssessmentId, assessment: { source_content_hash: sourceHash, start_ms: 0, end_ms: 12000, presentation: 'source_native_portrait', coverage: 'full_interval_continuous', face_head_clearance: 'pass', motion_continuity: 'pass', subtitle_clearance: 'pass', effective_quality: 'pass', reviewer_reference: 'fixture:reviewer', evidence_reference: 'talking-source-reviews/fixture.json', evidence_class: 'assisted_test' }, decision: 'review_claimed_suitable', admission: { id: 'fixture-admission', active: true }, state: 'current', evidence_sha256: 'd'.repeat(64), reasons: [] }] as T;
  if (path === '/voice-profiles') return [{ id: profileId, name: 'Fixture voice', provider: 'fixture-provider', provider_profile_id: 'profile-fixture', consent: { subject_name: 'Fixture', confirmed: true, authorization_reference: 'fixture:consent' } }] as T;
  if (path === '/execution-settings/capabilities') return [{ id: capabilityId, scope_key: 'voice:fixture', capability: 'voice', mode: 'local', provider: 'fixture-provider', model: 'fixture-model', runtime: 'fixture-runtime', machine_id: 'fixture-machine', readiness: 'configured', quality_status: 'unknown', commercial_status: 'unknown', evidence_reference: null, license_evidence_reference: null, provenance_source: 'fixture:manual', feature_support: {} }] as T;
  if (path === `/audio-assets/${audioId}`) {
    const review = localStorage.getItem('fixture-review'); reviewSubmitted = Boolean(review);
    return { id: audioId, content_hash: 'a'.repeat(64), duration_ms: 3000, authorization_reference: 'fixture:consent', metadata: { voice_generation: { project_id: projectId, job_id: voiceJob, qa_state: 'verified', human_review: review ? JSON.parse(review) : undefined } } } as T;
  }
  if (path.endsWith(`/voice-assets/${audioId}/human-review`)) {
    localStorage.setItem('fixture-review', String(options?.body));
    reviewSubmitted = true;
    const value: ProductionRun = JSON.parse(localStorage.getItem(saved) ?? JSON.stringify(initial()));
    value.status = JSON.parse(String(options?.body)).approved ? 'awaiting_talking_dependencies' : 'u_voice_rejected'; localStorage.setItem(saved, JSON.stringify(value));
    return {} as T;
  }
  if (path.endsWith('production-preflight')) {
    const value = plan(); if (slow) await new Promise(resolve => setTimeout(resolve, 1200)); return value as T;
  }
  if (path.endsWith('production-runs')) {
    if (stale || JSON.parse(String(options?.body)).expected_fingerprint !== plan().fingerprint) throw new Error('stale_production_preflight');
    const value = initial(); localStorage.setItem(saved, JSON.stringify(value)); return value as T;
  }
  const value: ProductionRun = JSON.parse(localStorage.getItem(saved) ?? JSON.stringify(initial()));
  value.voice_qa_auto_stop_reasons = ['fixture_qa_stop'];
  const replacementQa = value.talking_qa_dependencies?.[0];
  if (replacementQa?.generation_job_id === replacementJobId) {
    if (path.endsWith('/talking-qa-advance') && !replacementQa.output_asset_id) throw new Error('talking_output_not_ready');
    if (path.endsWith('/talking-preview-prepare') && replacementQa.state !== 'qa_verified_ready_for_preview') throw new Error('talking_qa_not_verified');
  }
  if (path.endsWith('/voice-dispatch')) {
    if (JSON.parse(String(options?.body)).capability_profile_id === capabilityId) throw new Error('exact_voice_capability_not_verified · voice_provider_license_scope_unverified');
    value.status = 'voice_running'; value.voice_job_id = voiceJob; value.waiting_stop_reasons = [];
  }
  if (path.endsWith('/voice-qa-advance')) { value.status = 'awaiting_u_voice_review'; value.voice_audio_id = audioId; value.voice_qa_job_id = voiceJob; value.voice_qa_auto_stop_reasons = []; value.waiting_stop_reasons = []; }
  if (path.endsWith('/talking-source-bind')) {
    value.talking_source_bindings = [{ scene_plan_id: scenePlanId, talking_profile_id: talkingProfileId, reference_clip_id: sourceClipId, reference_asset_id: sourceAssetId, reference_asset_hash: sourceHash, reference_start_ms: 0, reference_end_ms: 12000, source_authorization_reference: 'fixture:rights', reference_assessment_reference: 'fixture:reference-assessment', performance_brief: { require_no_burned_subtitles: true }, capability_profile_id: talkingCapabilityId, capability_evidence_reference: 'fixture:capability', license_evidence_reference: 'fixture:license', master_audio_id: audioId, master_start_ms: 250, master_end_ms: 2200, authorization_reference: 'fixture:authorization', suitability: 'full_interval_unknown', suitability_assessment_id: null, talking_job_id: null, talking_output_asset_id: null, talking_qa_job_id: null, talking_series_id: null, talking_preview_job_id: null, talking_run_id: null }];
  }
  if (path.endsWith('/talking-suitability-apply')) { const binding = value.talking_source_bindings?.[0]; if (binding) { binding.suitability = 'review_claimed_suitable'; binding.suitability_assessment_id = sourceAssessmentId; } }
  if (path.endsWith('/talking-suitability-apply') && sourceApplyFailure) throw new Error('fixture_source_changed_or_unavailable');
  if (path.endsWith('/talking-dispatch')) { const binding = value.talking_source_bindings?.[0]; if (binding) { binding.talking_job_id = talkingJobId; binding.talking_qa_job_id = voiceJob; binding.talking_output_asset_id = sourceAssetId; } value.talking_qa_dependencies = [{ scene_plan_id: scenePlanId, generation_job_id: talkingJobId, output_asset_id: sourceAssetId, qa_job_id: voiceJob, state: fixturePolicy === 2 ? 'qa_verified_ready_for_preview' : 'qa_verified_awaiting_u_talking' }]; }
  if (path.endsWith(`/talking-assets/${sourceAssetId}/human-review`)) { childSubjectiveSubmissions++; localStorage.setItem(actionCountsKey, JSON.stringify({ child: childSubjectiveSubmissions, aggregate: aggregateSubjectiveSubmissions })); window.dispatchEvent(new Event('fixture-review-count')); fixtureChildReview = JSON.parse(String(options?.body)); return { id: sourceAssetId, content_hash: sourceHash, width: 720, height: 1280, authorization_reference: 'fixture:rights', metadata: { talking_generation: { human_review: fixtureChildReview } } } as T; }
  if (path.endsWith('/talking-qa-advance') && value.talking_qa_dependencies?.[0]?.generation_job_id === replacementJobId) { value.talking_qa_dependencies[0].state = 'qa_verified_ready_for_preview'; value.talking_qa_dependencies[0].qa_job_id = '00000000-0000-0000-0000-000000000093'; const binding = value.talking_source_bindings?.[0]; if (binding) { binding.talking_output_asset_id = replacementChildAssetId; binding.talking_qa_job_id = value.talking_qa_dependencies[0].qa_job_id; } const records = JSON.parse(localStorage.getItem(repairRecordsKey) ?? '[]'); localStorage.setItem(repairRecordsKey, JSON.stringify(records.map((item: { replacement_job_id: string }) => item.replacement_job_id === replacementJobId ? { ...item, replacement_job_status: 'completed' } : item))); }
  if (path.endsWith('/talking-preview-prepare')) { const replacement = value.talking_qa_dependencies?.[0]?.generation_job_id === replacementJobId; value.talking_preview_dependencies = [{ scene_plan_id: scenePlanId, series_id: replacement ? replacementSeriesId : talkingSeriesId, job_id: replacement ? replacementJobId : voiceJob, output_asset_id: replacement ? replacementPreviewAssetId : previewAssetId, state: 'completed_review_only', child_review_state: fixtureChildReview ? 'approved' : 'pending', continuity_review_state: 'not_submitted' }]; const binding = value.talking_source_bindings?.[0]; if (binding) { binding.talking_series_id = replacement ? replacementSeriesId : talkingSeriesId; binding.talking_preview_job_id = replacement ? replacementJobId : voiceJob; } if (replacement) { const records = JSON.parse(localStorage.getItem(repairRecordsKey) ?? '[]'); localStorage.setItem(repairRecordsKey, JSON.stringify(records.map((item: { replacement_job_id: string }) => item.replacement_job_id === replacementJobId ? { ...item, successor_series_id: replacementSeriesId } : item))); } }
  if (path === `/projects/${projectId}/talking-slice-series/${activeSeriesId}/review-concerns` && options?.method === 'POST') { const payload = JSON.parse(String(options.body)); const concernId = `fixture-concern-${fixtureConcerns.length + 1}`; fixtureConcerns = [...fixtureConcerns, { id: concernId, finding: payload.finding, evidence_reference: payload.evidence_reference, answer: null }]; localStorage.setItem(concernsKey, JSON.stringify(fixtureConcerns)); return { id: concernId } as T; }
  if (path === `/projects/${projectId}/talking-slice-series/${activeSeriesId}/review-concern-answers` && options?.method === 'POST') { const payload = JSON.parse(String(options.body)); fixtureConcerns = fixtureConcerns.map(concern => { const answer = payload.answers.find((item: { concern_id: string }) => item.concern_id === concern.id); return answer ? { ...concern, answer: { approved: answer.approved, evidence_reference: answer.evidence_reference, reason: answer.reason } } : concern; }); localStorage.setItem(concernsKey, JSON.stringify(fixtureConcerns)); }
  if (path === `/projects/${projectId}/talking-slice-series/${activeSeriesId}/continuity-review` && options?.method === 'POST') { aggregateSubjectiveSubmissions++; localStorage.setItem(actionCountsKey, JSON.stringify({ child: childSubjectiveSubmissions, aggregate: aggregateSubjectiveSubmissions })); window.dispatchEvent(new Event('fixture-review-count')); const payload = JSON.parse(String(options.body)); fixtureConcerns = fixtureConcerns.map(concern => { const answer = payload.concern_answers?.find((item: { concern_id: string }) => item.concern_id === concern.id); return answer ? { ...concern, answer: { approved: answer.approved, evidence_reference: answer.evidence_reference, reason: answer.reason } } : concern; }); localStorage.setItem(concernsKey, JSON.stringify(fixtureConcerns)); const preview = value.talking_preview_dependencies?.[0]; if (preview) preview.continuity_review_state = payload.approved ? 'approved' : 'rejected'; localStorage.setItem(saved, JSON.stringify(value)); const review = { id: `fixture-review-${activeSeriesId}`, series_id: activeSeriesId, approved: payload.approved, evidence_reference: payload.evidence_reference, findings: payload.findings, child_job_ids: [value.talking_qa_dependencies?.[0]?.generation_job_id], preview_asset_id: activePreviewAssetId, preview_sha256: activePreviewAssetId === replacementPreviewAssetId ? replacementPreviewHash : previewHash, preview_qa_sha256: 'f'.repeat(64), reviewed_at: new Date().toISOString(), review_policy_version: fixturePolicy, dimensions: payload.dimensions ?? null, scoped_findings: payload.scoped_findings ?? [] }; localStorage.setItem(`fixture-aggregate-review:${activeSeriesId}`, JSON.stringify(review)); return review as T; }
  if (path.endsWith('/talking-run-admit') && value.talking_preview_dependencies?.[0]) value.talking_preview_dependencies[0].state = 'admitted';
  if (path === `/projects/${projectId}/talking-slice-series/${activeSeriesId}`) { const review = JSON.parse(localStorage.getItem(`fixture-aggregate-review:${activeSeriesId}`) ?? 'null'); return { id: activeSeriesId, readiness: 'ready_for_human_continuity_review', ready_for_human_continuity_review: !review, children: [{ job_id: value.talking_qa_dependencies?.[0]?.generation_job_id ?? talkingJobId, job_status: 'completed', output_asset_id: activeSeriesId === replacementSeriesId ? replacementChildAssetId : sourceAssetId, automated_qa_state: 'verified', human_review_state: fixtureChildReview?.approved ? 'approved' : 'pending', blockers: [] }], blockers: [], continuity_review_state: review ? review.approved ? 'approved' : 'rejected' : 'not_submitted', continuity_review_evidence_reference: review?.evidence_reference ?? null, continuity_review_findings: review?.findings ?? [], planned_origin_sha256: activeSeriesId === replacementSeriesId ? 'a'.repeat(64) : 'f'.repeat(64), preview_asset_id: activePreviewAssetId, preview_sha256: activePreviewAssetId === replacementPreviewAssetId ? replacementPreviewHash : previewHash, review_policy_version: fixturePolicy, required_dimensions: fixturePolicy === 2 ? ['visible_sync', 'identity', 'artifacts', 'source_performance', 'continuity', 'publishability'] : [], review_concerns: fixtureConcerns } as T; }
  if (path.endsWith('/voice-dispatch') || path.endsWith('/voice-qa-advance') || path.endsWith('/talking-source-bind') || path.endsWith('/talking-suitability-apply') || path.endsWith('/talking-dispatch') || path.endsWith('/talking-qa-advance') || path.endsWith('/talking-preview-prepare') || path.endsWith('/talking-run-admit')) localStorage.setItem(saved, JSON.stringify(value));
  if (path.endsWith('/resume')) throw new Error('approved_master_required · human_review_required');
  if (path.endsWith('/cancel')) { value.status = 'cancelled'; localStorage.setItem(saved, JSON.stringify(value)); }
  return value as T;
}
function Fixture() {
  const [revision, setRevision] = useState(1);
  const [actionCounts, setActionCounts] = useState<{ child: number; aggregate: number }>({ child: 0, aggregate: 0 });
  useEffect(() => {
    const updateCounts = () => { try { setActionCounts(JSON.parse(localStorage.getItem(actionCountsKey) ?? '{"child":0,"aggregate":0}')); } catch { setActionCounts({ child: 0, aggregate: 0 }); } };
    updateCounts(); window.addEventListener('fixture-review-count', updateCounts);
    const realFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      if (String(input).endsWith(`/audio-assets/${audioId}/media`)) return mediaFailure ? Promise.resolve(new Response('', { status: 403 })) : Promise.resolve(new Response(new Blob([fixtureWav()], { type: 'audio/wav' }), { status: 200 }));
      return realFetch(input, init);
    };
    return () => { window.fetch = realFetch; window.removeEventListener('fixture-review-count', updateCounts); };
  }, []);
  return <main><h1>{talkingMode ? `${repairMode ? 'V76b2r' : 'V76a2'} policy v${fixturePolicy} Talking isolated fixture — no backend/provider calls` : 'V75b1 isolated fixture — no backend/provider calls'}</h1>
    {talkingMode && <p role="status" aria-label="fixture subjective actions">必需主观提交动作计数（fixture 操作数，非耗时）：子片 {actionCounts.child} + 完整 Run {actionCounts.aggregate} = {actionCounts.child + actionCounts.aggregate}。预期 v1=2、v2=1。</p>}
    <label><input type="checkbox" onChange={event => { stale = event.target.checked; }}/>模拟过期拒绝</label>
    <label><input type="checkbox" onChange={event => { slow = event.target.checked; }}/>延迟预检</label>
    <label><input type="checkbox" onChange={event => { mediaFailure = event.target.checked; }}/>模拟试听权限拒绝</label>
    {talkingMode && <label><input type="checkbox" onChange={event => { sourceApplyFailure = event.target.checked; }}/>模拟来源审核后台拒绝</label>}
    <button onClick={() => { resetRepairFixture(); for (const key of ['content-os-production:fixture-v75b1', `content-os-production:${projectId}`, saved, 'fixture-review', 'fixture-aggregate-review', concernsKey]) localStorage.removeItem(key); childSubjectiveSubmissions = 0; aggregateSubjectiveSubmissions = 0; fixtureChildReview = null; fixtureConcerns = []; localStorage.setItem(actionCountsKey, '{"child":0,"aggregate":0}'); window.location.reload(); }}>重置隔离执行 fixture</button>
    <button onClick={() => { const value = initial(); value.status = 'voice_completed_awaiting_qa'; value.voice_job_id = voiceJob; value.voice_audio_id = audioId; localStorage.setItem('content-os-production:fixture-v75b1', JSON.stringify({ project_id: projectId, id })); localStorage.setItem(saved, JSON.stringify(value)); window.location.reload(); }}>模拟 Worker Voice 完成</button>
    {talkingMode && <button onClick={() => { const value = initial(); localStorage.setItem(`content-os-production:${projectId}`, JSON.stringify({ project_id: projectId, id })); localStorage.setItem(saved, JSON.stringify(value)); window.location.reload(); }}>启动隔离 Talking UI 场景</button>}
    {repairMode && <><button onClick={() => seedRepairFixture('rejected')}>Seed completed-but-rejected repair fixture</button><button onClick={() => seedRepairFixture('negative_concern')}>Seed approved preview with negative concern</button><button onClick={() => seedRepairFixture('technical_failure')}>Seed technical generation failure</button><button onClick={() => { completeReplacementGeneration(); window.location.reload(); }}>模拟 Worker replacement generation 完成（不含 QA）</button></>}
    <button onClick={() => { version += 1; setRevision(version); }}>修改草稿版本</button>
    <ProductionPanel projectId={projectId} draftVersion={revision} scriptRevision={1} hasScenes request={request} confirmAction={message => message.includes('AudioAsset') && message.includes('不能覆盖') || message.includes('Talking 子片 Asset') || message.includes('完整 TalkingRun 预览') || message.includes('确认仅重生成场景') || message.includes('的局部问题？') || message.includes('个局部问题回答？')}/>
  </main>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><Fixture/></React.StrictMode>);
