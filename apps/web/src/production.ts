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
  automatic?: boolean;
  start_job_id?: string | null;
  work_id?: string | null;
  delivery_intent?: "publish" | "draft";
  review_state?: "pending" | "approved" | "rejected";
  review_notes?: string | null;
  created_at?: string;
  voice_job_id: string | null; voice_audio_id: string | null; voice_qa_job_id: string | null;
  waiting_stop_reasons: string[];
  voice_qa_auto_stop_reasons: string[];
  talking_master_audio_id?: string | null;
  talking_dependencies: Array<{ scene_id: string; scene_plan_id: string; visual_dependency_fingerprint?: string }>;
  talking_source_bindings?: Array<Record<string, unknown> & { scene_plan_id: string; reference_clip_id: string }>;
  talking_qa_dependencies?: Array<{ scene_plan_id: string; generation_job_id: string; output_asset_id: string | null; qa_job_id: string | null; state: string }>;
  talking_preview_dependencies?: Array<{ scene_plan_id: string; series_id: string; job_id: string; output_asset_id: string | null; state: string; child_review_state: string; continuity_review_state: string }>;
};
export type ProductionRunReference = { id: string; project_id: string; draft_version: number; script_revision: number; stage: string; created_at: string };
export function currentRunReferences(references: ProductionRunReference[], projectId: string, draftVersion: number, scriptRevision: number) {
  return references.filter(value => value.project_id === projectId && value.draft_version === draftVersion
    && value.script_revision === scriptRevision && value.stage !== "cancelled");
}
export function productionKey(projectId: string) { return `content-os-production:${projectId}`; }
export function startBody(plan: ProductionPlan, talkingReviewPolicyVersion: 1 | 2 = plan.scenes?.some(scene => scene.production_need === "new_talking") ? 2 : 1) {
  return { idempotency_key: `web-production:${plan.project_id}:${plan.fingerprint}`, expected_fingerprint: plan.fingerprint, talking_review_policy_version: talkingReviewPolicyVersion };
}
export function presentationStyle(choice: "default" | "subject_first" | "editorial" | "comparison_stack") {
  return choice !== "default" ? { name: choice === "editorial" ? "content-os-editorial" : choice === "comparison_stack" ? "content-os-portrait-comparison" : "content-os-subject-first", background_color: "#f3efe5", foreground_color: "#172033", accent_color: "#ca4c2c" } : {};
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
  if (detail && typeof detail === "object" && "reasons" in detail && Array.isArray(detail.reasons) && detail.reasons.length) return detail.reasons.map(value => preflightReasonLabel(String(value))).join(" · ");
  if (detail && typeof detail === "object" && "code" in detail && typeof detail.code === "string") return detail.code;
  return `请求失败 (${status})`;
}
export function preflightReasonLabel(reason: string): string {
  const sourceReasons: Record<string,string> = {
    budget_call_limit: "当前项目调用额度已用完，尚未开始新制作。",
    video_production_already_running: "本项目已有视频在制作，请等待当前结果。",
    creator_talking_reference_not_configured: "请先在创作者资料中配置可用的人像参考。",
    talking_motion_capacity_insufficient: "本人动作素材不足以覆盖当前出镜安排，需要重新调度画面；已完成的合格旁白保留。",
    talking_motion_allocation_search_limit: "人物素材组合过多，未能完成本次自动调度；已完成的合格旁白保留。",
    talking_motion_occupancy_unresolved: "人物素材的使用记录尚未核实，暂未派发新的人物画面。",
    talking_qa_narration_requires_voice_repair: "问题来自旁白，需要先修复声音；不会重复生成人物画面。",
    automatic_draft_changed: "制作中的稿件发生变化，本次已停止，请用当前稿件重新开始。",
    planning_tail_budget: "剩余额度不足以完成规划、核验和后续制作，已保留当前成果与预算。",
    visual_revision_changed_content: "画面调整改变了旁白或证据要求，本次未采用，原方案已保留。",
    automatic_resume_source_not_latest_failed: "这次制作状态已变化，请刷新后继续当前任务。",
    automatic_resume_already_has_run: "本次已进入声音或成片阶段，请在对应制作记录中恢复。",
    scene_planner_response_invalid: "内容规划返回了不符合契约的方案，本次已停止，检查原因已保存。",
    "scene_planner_response_invalid:opening_context": "开场缺少主体，不能为缩短人物台词删掉主题。请保留完整开场并调整出镜位置。",
    voice_qa_failed: "新旁白未通过文案、静音或可播放性检查，证据已保留，尚未进入口型与渲染。",
    budget_chain_call_limit: "剩余额度不足以覆盖制作所需的最少调用，已在启动前停止。",
    visual_route_unresolved: "画面方案尚未解决，需要保留内容并调整选材或竖屏布局。",
    source_suitability_unknown: "选段还缺少画面事实核验，文件哈希或单帧描述不能代替。",
    source_subtitle_handling_unresolved: "原片字幕与新旁白的冲突尚未解决，需要分析确切选段或换素材。",
    source_subtitle_clearance_required: "已有字幕位置证据，但尚不能确认裁除后保留关键画面。",
    source_subtitles_overlap_required_picture: "原字幕与关键画面重叠，需要更换素材。",
    source_content_hash_mismatch: "实际素材与入库记录不一致，需要重新核实来源。",
    visual_source_playback_interval_invalid: "选定区间无法覆盖所需时长，需要重新选段，不能悄悄换画面。",
    material_analysis_not_configured: "素材视频核验尚未配置，已在生成语音与口型前停止。",
    material_video_analysis_timeout: "素材视频核验请求超时，本次已停止；原稿、素材和调用记录已保留，未自动重试。",
    material_video_analysis_failed: "素材视频核验服务未能返回有效结果，本次已停止；失败证据已保留，未自动重试。",
    material_candidates_unavailable: "没有找到能覆盖场景时长的候选素材，需要继续解决选材。",
    material_catalogue_changed: "准备好的素材已变化，需要重新确认当前素材内容。",
    material_scene_unresolved: "已核验候选，但尚无同时满足内容、字幕和呈现要求的选段；未启动语音与口型生成。",
    material_source_unavailable: "分镜所需的外部或 AI 画面尚未取得，当前无法继续；需求已保存，未启动语音与口型。",
    material_combination_unresolved: "单段素材可用，但尚无满足整片来源冲突要求的组合；未启动语音与口型。",
    source_scene_mismatch: "视频片段尚未认可用于当前文案与画面意图，需要重新匹配。",
    material_analysis_proxy_invalid: "核验用视频与原片区间不一致，需要重新提取。",
    material_analysis_scope_too_large: "当前素材核验范围过大，需要精简场景或候选。",
    material_analysis_batch_limit: "本次素材核验已达批次上限，仍有画面缺口；已完成的核验和原稿均已保留。",
    material_catalogue_batch_limit: "本次素材目录核验已达上限，已完成结果保留，未继续消耗额度。",
    material_analysis_proxy_too_large: "单段核验视频超过当前负载限制，需要调整取段或代理编码。",
    material_analysis_scope_changed: "原任务的核验素材或场景已变化，不能直接沿用旧记录。",
    material_analysis_tail_budget: "剩余额度需留给语音、口型和成片，本次未再发起素材核验。",
    graphic_narration_count_mismatch: "图解因素数量与本段旁白不一致，当前无法确定修正，已在生成语音前停止。",
    video_production_failed: "本次制作未完成，原稿、素材和失败诊断已保留。",
    automatic_text_card_requires_media: "自动制作不能以大字卡替代素材，需要改用合适画面。",
    diagram_repeats_narration: "图解重复了整段旁白，需要表达实际关系或改用素材。",
    capture_required: "仍需合适画面；这不代表必须补拍，也不能用大字卡填补缺口。",
    talking_reference_alignment_not_covered: "人物参考区间不足以覆盖模型所需帧，请改用更长区间或其他画面。",
    visual_source_repeated_in_video: "同一视频重复使用了同一来源切片，需要调整画面区间。",
    visual_source_reserved_by_other_video: "该来源区间已被其他视频占用，请使用未占用区间或其他合适画面。",
    visual_source_already_published: "该来源区间已用于发布视频，需要避开历史重复。",
    visual_source_lineage_unresolved: "画面来源区间无法核实，需要重新选择来源。",
    visual_source_history_unresolved: "历史画面使用记录无法核实，需要检查来源记录。",
    visual_source_lineage_changed: "原始素材已变化，需要重新核实来源。",
  };
  if (sourceReasons[reason]) return sourceReasons[reason];
  const [code,...detail] = reason.split(":");
  if (sourceReasons[code]) return sourceReasons[code] + (detail.length ? ` 场景：${detail.join(":")}` : "");
  if (reason === "talking_source_coverage_insufficient_replan") return "人物参考画面不足以连续覆盖方案。需保留文案并寻找其他合适画面，不能循环人物动作或用大字卡填补缺口。";
  if (reason === "talking_motion_restart_requires_replan") return "人物动作反复回到同一起点，需要更换参考区间或重新规划画面。";
  const prefix = "landscape_fallback_dominates_video:";
  if (reason.startsWith(prefix)) return `同一横屏次选镜头占成片过半：${reason.slice(prefix.length).split(",").join("、")}。需保留关键画面，先调整竖屏选材与布局；整片横版仅在明确选择时采用，不能用解释图文或大字卡代替素材。`;
  return reason;
}
export function canDispatchVoice(
  profile: { provider: string; consent: { confirmed: boolean } } | undefined,
  capability: { provider: string } | undefined,
): boolean {
  // Capability quality, license and budget remain backend admission decisions.
  return Boolean(profile?.consent.confirmed && capability && profile.provider === capability.provider);
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
  context: { status: string; speech_start_ms: number | null; speech_end_ms: number | null } | undefined,
): boolean {
  return Boolean(profile?.consent.confirmed && profile.reference_clip_ids.includes(clipId)
    && capability?.capability === "talking"
    && context?.status === "ready" && context.speech_start_ms != null && context.speech_end_ms != null
    && context.speech_end_ms > context.speech_start_ms);
}
export const productionLabels: Record<string, string> = {
  voice_qa_ready_for_draft: "旁白质检完成，正在继续制作",
  awaiting_approved_master: "等待已批准的主旁白", voice_pending: "旁白已排队", voice_running: "旁白生产中", voice_failed: "旁白生产失败",
  voice_completed_awaiting_qa: "旁白完成，等待技术 QA", voice_qa_pending: "旁白 QA 已排队", voice_qa_running: "旁白 QA 处理中", voice_qa_failed: "旁白 QA 失败",
  awaiting_u_voice_review: "等待旁白人审", u_voice_rejected: "旁白人审未通过", approved_master_ready_to_resume: "主旁白已批准，可续接计划",
  awaiting_talking_dependencies: "人物画面制作", render_pending: "渲染已排队", render_running: "渲染中", render_failed: "渲染失败",
  render_completed_awaiting_review: "渲染完成，尚未最终审查", cancelled: "已取消",
};
