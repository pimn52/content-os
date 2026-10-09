// Isolated API-shape fixture. No backend/provider requests or creator judgments.
type Storage = Pick<globalThis.Storage, "getItem" | "setItem" | "removeItem">;
export const recoveryIds = {
  run: "00000000-0000-0000-0000-000000000601",
  oldJob: "00000000-0000-0000-0000-000000000602",
  newJob: "00000000-0000-0000-0000-000000000603",
  oldAudio: "00000000-0000-0000-0000-000000000604",
  newAudio: "00000000-0000-0000-0000-000000000605",
  qa: "00000000-0000-0000-0000-000000000606",
  oldRender: "00000000-0000-0000-0000-000000000607",
  newRender: "00000000-0000-0000-0000-000000000608",
  observation: "00000000-0000-0000-0000-000000000609",
};
export const renderHashes = {
  before: "23537e4b9c084cf20e88b42a401b052a4f3a630aa8917faa416f50d3059497a7",
  after: "bcd62ae14fa563cf434e3fbfaebab64f38d8941cc341c14cab7739ff8ba3c2d9",
};
export const manualComparison = {
  historical_action_count: null,
  historical_evidence: "旧实施路径的主动分钟与同任务操作总数未测量；无完整普通恢复入口，需实施侧查找、重建和手工绑定。",
  automatic_binding_actions: 0,
  unchanged_subjective_voice_reviews: 1,
  final_product_review_required: true,
};
export function createRecoveryFixture(storage: Storage, kind: "voice" | "presentation") {
  const projectId = kind === "voice" ? "00000000-0000-0000-0000-000000000610" : "00000000-0000-0000-0000-000000000611";
  const key = `fixture-recovery:${projectId}`;
  const base = `/projects/${projectId}/production-runs/${recoveryIds.run}`;
  const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
  const fresh = () => ({
    version: 1, denied: false, used: 0, operations: [] as string[], observations: [] as any[], records: [] as any[], review: null as any,
    run: { id: recoveryIds.run, project_id: projectId, status: kind === "voice" ? "voice_failed" : "render_completed_awaiting_review",
      preflight_fingerprint: "fixture-recovery-v1", voice_job_id: kind === "voice" ? recoveryIds.oldJob : null,
      voice_audio_id: kind === "voice" ? null : recoveryIds.oldAudio, voice_qa_job_id: null as string | null,
      voice_qa_auto_stop_reasons: [], waiting_stop_reasons: [], talking_dependencies: [],
      talking_source_bindings: [], talking_preview_dependencies: [],
      render_job_id: kind === "presentation" ? recoveryIds.oldJob : null },
  });
  const load = () => JSON.parse(storage.getItem(key) ?? JSON.stringify(fresh())) as ReturnType<typeof fresh>;
  const save = (state: ReturnType<typeof fresh>) => storage.setItem(key, JSON.stringify(state));
  const fingerprint = (state: ReturnType<typeof fresh>) => (kind === "voice" ? "a" : "b").repeat(60) + state.version.toString(16).padStart(4, "0");
  const count = (state: ReturnType<typeof fresh>, action: string) => state.operations.push(action);
  function seed() {
    save(fresh());
    storage.setItem(`content-os-production:${projectId}`, JSON.stringify({ project_id: projectId, id: recoveryIds.run }));
  }
  function changeInput() { const state = load(); state.version++; state.run.preflight_fingerprint = `fixture-recovery-v${state.version}`; save(state); }
  function setDenied(value: boolean) { const state = load(); state.denied = value; save(state); }
  function complete() {
    const state = load();
    if (state.run.voice_job_id !== recoveryIds.newJob && state.run.render_job_id !== recoveryIds.newJob) return;
    if (kind === "voice") state.run.status = "voice_completed_awaiting_qa";
    else state.run.status = "render_completed_awaiting_review";
    const record = state.records[0];
    if (record) {
      if (kind === "voice") { record.replacement_job_status = "completed"; record.successor_audio = [{ id: recoveryIds.newAudio, content_hash: "b".repeat(64), duration_ms: 1000 }]; }
      else { record.status = "completed"; record.technical_qa = { state: "verified", sha256: renderHashes.after, width: 108, height: 192, duration_ms: 1000, scope: "fixture_technical_only_final_u_product_required" }; }
    }
    save(state);
  }
  const scene = { scene_id: "scene-1", graphic_text: "Fixture complete narration.", caption: "Fixture complete narration.",
    captions: [{ text: "Fixture complete narration." }], subtitle_treatment: "timed_captions", portrait_presentation: "full_canvas" };
  const candidate = { master_narration: { audio_asset_id: recoveryIds.oldAudio }, edit_plan: { scenes: [{ scene_id: "scene-1", selected_asset_id: null }] } };
  async function request<T>(path: string, options?: RequestInit): Promise<T> {
    const state = load(); const body = options?.body ? JSON.parse(String(options.body)) : {};
    const write = options?.method === "POST";
    if (path === "/voice-profiles" || path === "/execution-settings/capabilities") return [] as T;
    if (path === base) return clone(state.run) as T;
    if (path === `/projects/${projectId}/draft`) return { version: state.version, scenes: [{ id: "scene-plan-1", scene_id: "scene-1", caption_emphasis: ["Fixture short point"] }] } as T;
    if (path === base + "/voice-repairs") return clone(kind === "voice" ? state.records : []) as T;
    if (path === base + "/presentation-repairs") return clone(kind === "presentation" ? state.records : []) as T;
    if (path === base + "/voice-repair-plan") {
      count(state, "read_voice_plan"); save(state);
      const reasons = state.used ? ["run_repair_allowance_exhausted"] : state.denied ? ["voice_repair_consent_changed"] : [];
      return { project_id: projectId, run_id: recoveryIds.run, fingerprint: fingerprint(state),
        action: reasons.length ? "stop" : state.review ? "replan" : "regenerate_take", failure_kind: state.review ? "subjective" : "technical",
        stop_reasons: state.review ? [...reasons, "voice_repair_human_judgment_requires_replan"] : reasons,
        remaining_run_repairs: 1 - state.used, max_tts_calls: 1, max_qa_asr_calls: 1, external_charge_ceiling: "0",
        local_compute_cost: null, user_active_minutes: null, execution_granularity: "whole_take",
        review_scope: "fresh_full_copy_qa_and_exact_u_voice", worker_readiness: "not_live_observed", evidence: { level: "fixture" } } as T;
    }
    if (path === base + "/voice-repair" && write) {
      const replay = state.records.find(record => record.idempotency_key === body.idempotency_key);
      if (replay) return clone(replay) as T;
      if (state.used) throw new Error("run_repair_allowance_exhausted");
      if (state.denied) throw new Error("voice_repair_consent_changed");
      if (body.expected_fingerprint !== fingerprint(state)) throw new Error("stale_voice_repair_plan");
      if (body.confirmed_action !== "regenerate_take" || !body.reason?.trim()) throw new Error("voice_repair_explicit_action_required");
      const record = { id: "fixture-voice-repair", reason: body.reason, idempotency_key: body.idempotency_key,
        old_job_ids: [recoveryIds.oldJob, null], old_audio_id: null, replacement_job_id: recoveryIds.newJob,
        replacement_job_status: "queued", successor_audio: [], successor_qa_job_id: null, provider_calls: [] };
      state.records.push(record); state.used++; count(state, "apply_voice_repair");
      state.run.voice_job_id = recoveryIds.newJob; state.run.voice_audio_id = null; state.run.voice_qa_job_id = null; state.run.status = "voice_pending";
      save(state); return clone(record) as T;
    }
    if (path === base + "/voice-qa-advance" && write) {
      if (state.run.status !== "voice_completed_awaiting_qa") throw new Error("voice_output_not_ready");
      state.run.voice_audio_id = recoveryIds.newAudio; state.run.voice_qa_job_id = recoveryIds.qa; state.run.status = "awaiting_u_voice_review";
      state.records[0].successor_qa_job_id = recoveryIds.qa; count(state, "advance_voice_qa"); save(state); return clone(state.run) as T;
    }
    if (path === `/audio-assets/${recoveryIds.newAudio}`) return { id: recoveryIds.newAudio, content_hash: "b".repeat(64), duration_ms: 1000,
      authorization_reference: "fixture:consent", metadata: { voice_generation: { project_id: projectId, job_id: recoveryIds.newJob,
        qa_state: state.run.voice_qa_job_id ? "verified" : "pending", ...(state.review ? { human_review: state.review } : {}) } } } as T;
    if (path === `/projects/${projectId}/voice-assets/${recoveryIds.newAudio}/human-review` && write) {
      if (state.run.status !== "awaiting_u_voice_review" || state.review) throw new Error("fixture_review_not_ready_or_immutable");
      state.review = body; state.run.status = body.approved ? "approved_master_ready_to_resume" : "u_voice_rejected";
      count(state, "submit_new_u_voice"); save(state); return body as T;
    }
    if (path.startsWith("/jobs/")) {
      const jobId = path.slice(6); const replacement = jobId === recoveryIds.newJob;
      if (jobId !== recoveryIds.oldJob && !replacement) throw new Error("fixture_job_not_found");
      return { id: jobId, type: "render", status: replacement ? state.records[0]?.status ?? "pending" : "completed",
        render_id: replacement ? recoveryIds.newRender : recoveryIds.oldRender, error_code: null } as T;
    }
    if (path === base + "/presentation-observations" && write) {
      if (state.denied) throw new Error("presentation_authorization_changed");
      const expectedHash = state.run.render_job_id === recoveryIds.oldJob ? renderHashes.before : renderHashes.after;
      if (body.render_sha256 !== expectedHash || body.scene_id !== "scene-1") throw new Error("presentation_observation_stale");
      const replay = state.observations.find(item => item.request.idempotency_key === body.idempotency_key);
      if (replay) return clone(replay) as T;
      const observation = { id: `00000000-0000-0000-0000-${String(609 + state.observations.length).padStart(12, "0")}`, render_job_id: state.run.render_job_id, evidence_class: "human_observation", request: body };
      state.observations.push(observation); count(state, "record_presentation_observation"); save(state); return clone(observation) as T;
    }
    if (path.startsWith(base + "/presentation-repair-plan?")) {
      const obs = state.observations.find(item => item.id === new URLSearchParams(path.split("?")[1]).get("observation_id"));
      if (!obs || obs.render_job_id !== state.run.render_job_id) throw new Error("presentation_observation_stale");
      const supported = obs.request.finding === "duplicate_text";
      const reasons = state.used ? ["run_repair_allowance_exhausted"] : state.denied ? ["presentation_authorization_changed"] : supported ? [] : ["presentation_source_interval_or_layout_replan_required"];
      const after = { ...scene, graphic_text: "Fixture short point" };
      count(state, "read_presentation_plan"); save(state);
      return { project_id: projectId, run_id: recoveryIds.run, fingerprint: fingerprint(state), observation: obs,
        action: state.used || state.denied ? "stop" : supported ? "render_revision" : "replan", stop_reasons: reasons,
        changes: supported ? [{ scene_id: "scene-1", before: scene, after }] : [],
        predecessor_job: { id: recoveryIds.oldJob, payload: { render_id: recoveryIds.oldRender, video_spec: candidate } },
        candidate_spec: supported ? candidate : null, remaining_run_repairs: 1 - state.used,
        max_local_render_calls: 1, voice_calls: 0, talking_calls: 0, external_charge_ceiling: "0", local_compute_cost: null,
        user_active_minutes: null, review_scope: "new_output_technical_qa_and_final_u_product" } as T;
    }
    if (path === base + "/presentation-repair" && write) {
      const replay = state.records.find(record => record.idempotency_key === body.idempotency_key);
      if (replay) return clone(replay) as T;
      if (state.used) throw new Error("run_repair_allowance_exhausted");
      if (state.denied) throw new Error("presentation_authorization_changed");
      if (body.expected_fingerprint !== fingerprint(state)) throw new Error("stale_presentation_repair_plan");
      if (body.confirmed_action !== "render_revision" || !body.reason?.trim()) throw new Error("presentation_explicit_action_required");
      const plan: any = await request(base + `/presentation-repair-plan?observation_id=${body.observation_id}`);
      if (plan.action !== "render_revision") throw new Error("presentation_source_interval_or_layout_replan_required");
      // This internal guard is not another UI action.
      state.operations = load().operations.filter((_, index, values) => index !== values.length - 1);
      const record = { id: "fixture-presentation-repair", idempotency_key: body.idempotency_key, reason: body.reason, plan,
        replacement_job_id: recoveryIds.newJob, status: "pending", technical_qa: null, final_review_state: "not_submitted", provider_calls: [] };
      state.records.push(record); state.used++; count(state, "apply_presentation_repair");
      state.run.render_job_id = recoveryIds.newJob; state.run.status = "render_pending"; save(state); return clone(record) as T;
    }
    if (path === base + "/cancel" && write) { state.run.status = "cancelled"; save(state); return clone(state.run) as T; }
    if (path === base + "/resume" && write) throw new Error("fixture_fresh_human_review_required");
    throw new Error(`isolated_fixture_unhandled_request:${path}`);
  }
  return { projectId, base, request, seed, load, complete, changeInput, setDenied, key };
}
