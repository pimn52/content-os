export type ProductionPlan = {
  project_id: string; draft_version: number; script_revision: number; fingerprint: string;
  status: string; evidence_level: string;
  cost_estimate: { known_amount: string; currency: string; unknown_cost_count: number };
  estimated_local_runtime_ms: number | null; estimated_user_active_minutes: number | null;
  stop_reasons: string[]; authorization_needs: string[];
  scenes: Array<{ scene_plan_id: string; scene_id: string; production_need: string; suitability: string; subtitle_treatment: string; burned_in_subtitles: string; reasons: string[] }>;
  actions: Array<{ kind: string; required: boolean; reason: string; cost: { amount: string | null; currency: string | null } }>;
};
export type ProductionRun = {
  id: string; project_id: string; status: string; preflight_fingerprint: string; render_job_id: string | null;
  talking_review_policy_version?: 1 | 2;
  voice_job_id: string | null; voice_audio_id: string | null; voice_qa_job_id: string | null;
  waiting_stop_reasons: string[];
  voice_qa_auto_stop_reasons: string[];
  talking_master_audio_id?: string | null;
  talking_dependencies: Array<{ scene_id: string; scene_plan_id: string; visual_dependency_fingerprint?: string }>;
  talking_source_bindings?: Array<Record<string, unknown> & { scene_plan_id: string; reference_clip_id: string }>;
  talking_qa_dependencies?: Array<{ scene_plan_id: string; generation_job_id: string; output_asset_id: string | null; qa_job_id: string | null; state: string }>;
  talking_preview_dependencies?: Array<{ scene_plan_id: string; series_id: string; job_id: string; output_asset_id: string | null; state: string; child_review_state: string; continuity_review_state: string }>;
};
export function productionKey(projectId: string) { return `content-os-production:${projectId}`; }
export function startBody(plan: ProductionPlan, talkingReviewPolicyVersion: 1 | 2 = 2) {
  return { idempotency_key: `web-production:${plan.project_id}:${plan.fingerprint}`, expected_fingerprint: plan.fingerprint, talking_review_policy_version: talkingReviewPolicyVersion };
}
export function talkingRepairKey(projectId: string, runId: string, scenePlanId: string, fingerprint: string) {
  return `web-talking-repair:${projectId}:${runId}:${scenePlanId}:${fingerprint}`;
}
export function recoveryIdentity(projectId: string, runId: string, candidateId: string | null, candidateHash: string | null, fingerprint = "") {
  return `recovery:${projectId}:${runId}:${candidateId ?? "none"}:${candidateHash ?? "unknown"}:${fingerprint}`;
}
export async function recoveryIdempotencyKey(prefix: string, projectId: string, runId: string, fingerprint: string, action: unknown) {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(action)));
  const digest = Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
  return `web-${prefix}:${projectId}:${runId}:${fingerprint}:${digest}`;
}
export function talkingSeriesReviewKey(scenePlanId: string, seriesId: string, assetId: string | null | undefined, contentHash: string | undefined, policyVersion: 1 | 2, qaIdentity = "") {
  return `${scenePlanId}:${seriesId}:${assetId ?? "no-asset"}:${contentHash ?? "no-hash"}:v${policyVersion}:qa=${qaIdentity || "unknown"}`;
}
export function talkingReviewPolicyVersion(run: Pick<ProductionRun, "talking_review_policy_version">): 1 | 2 {
  return run.talking_review_policy_version ?? 1;
}
export function parseBookmark(raw: string | null, projectId: string): string | null {
  try {
    const value = JSON.parse(raw ?? "null");
    return value?.project_id === projectId && typeof value.id === "string"
      && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value.id) ? value.id : null;
  } catch { return null; }
}
export function requestError(body: { detail?: unknown }, status: number): string {
  const detail = body.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "reasons" in detail && Array.isArray(detail.reasons) && detail.reasons.length) return detail.reasons.map(String).join(" · ");
  if (detail && typeof detail === "object" && "code" in detail && typeof detail.code === "string") return detail.code;
  return `请求失败 (${status})`;
}
export function canDispatchVoice(
  profile: { provider: string; consent: { confirmed: boolean } } | undefined,
  capability: { provider: string } | undefined,
  authorizationReference: string,
): boolean {
  // Capability quality, license and budget remain backend admission decisions.
  return Boolean(profile?.consent.confirmed && capability && profile.provider === capability.provider && authorizationReference.trim());
}
export function voiceCandidateMatches(
  metadata: { voice_generation?: Record<string, unknown> }, projectId: string, voiceJobId: string,
): boolean {
  const generation = metadata.voice_generation;
  return Boolean(generation && generation.project_id === projectId && generation.job_id === voiceJobId);
}
export function canReuseTalkingSource(choice: { state: string; decision: string | null; admission: { id: string; active: boolean } | null } | undefined): boolean {
  return Boolean(choice?.state === "current" && choice.decision === "review_claimed_suitable" && choice.admission?.active && choice.admission.id);
}
export function canBindTalkingSource(
  profile: { consent: { confirmed: boolean }; reference_clip_ids: string[] } | undefined,
  clipId: string,
  capability: { capability: string } | undefined,
  authorizationReference: string,
  context: { status: string; speech_start_ms: number | null; speech_end_ms: number | null } | undefined,
): boolean {
  return Boolean(profile?.consent.confirmed && profile.reference_clip_ids.includes(clipId)
    && capability?.capability === "talking" && authorizationReference.trim()
    && context?.status === "ready" && context.speech_start_ms != null && context.speech_end_ms != null
    && context.speech_end_ms > context.speech_start_ms);
}
export const productionLabels: Record<string, string> = {
  awaiting_approved_master: "等待已批准的主旁白", voice_pending: "旁白已排队", voice_running: "旁白生产中", voice_failed: "旁白生产失败",
  voice_completed_awaiting_qa: "旁白完成，等待技术 QA", voice_qa_pending: "旁白 QA 已排队", voice_qa_running: "旁白 QA 处理中", voice_qa_failed: "旁白 QA 失败",
  awaiting_u_voice_review: "等待旁白人审", u_voice_rejected: "旁白人审未通过", approved_master_ready_to_resume: "主旁白已批准，可续接计划",
  awaiting_talking_dependencies: "等待人物画面来源与审核", render_pending: "渲染已排队", render_running: "渲染中", render_failed: "渲染失败",
  render_completed_awaiting_review: "渲染完成，尚未最终审查", cancelled: "已取消",
};
