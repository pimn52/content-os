import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { canBindTalkingSource, canReuseTalkingSource, talkingRepairKey, talkingReviewPolicyVersion, talkingSeriesReviewKey, type ProductionRun } from "./production";

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type Profile = { id: string; name: string; provider: string; reference_clip_ids: string[]; consent: { confirmed: boolean; authorization_reference: string | null } };
type Capability = { id: string; capability: string; provider: string; model: string; runtime: string; machine_id: string; readiness: string; quality_status: string; commercial_status: string; provenance_source: string; evidence_reference: string | null; license_evidence_reference: string | null };
type Asset = { id: string; content_hash: string; width: number; height: number; authorization_reference: string; metadata: Record<string, unknown> };
type Clip = { id: string; asset_id: string; start_ms: number; end_ms: number; orientation: string | null; talking_candidate: boolean; face_visibility: number | null; mouth_visibility: number | null; talking_reference_assessment: { burned_in_subtitles: boolean | null; evidence_reference: string } | null };
type ReviewChoice = { assessment_id: string; assessment: { source_content_hash: string; start_ms: number; end_ms: number; presentation: string; coverage: string; face_head_clearance: string; motion_continuity: string; subtitle_clearance: string; effective_quality: string; reviewer_reference: string; evidence_reference: string; evidence_class: string } | null; decision: string | null; admission: { id: string; active: boolean } | null; state: string; evidence_sha256: string | null; reasons: string[] };
type SourceContext = { scene_plan_id: string; scene_id: string; scene_voice_text: string; master_audio_id: string; master_content_hash: string; status: "ready" | "unresolved"; reason: string | null; speech_start_ms: number | null; speech_end_ms: number | null };
type Binding = { scene_plan_id: string; talking_profile_id: string; reference_clip_id: string; reference_asset_id: string; reference_asset_hash: string; reference_start_ms: number; reference_end_ms: number; source_authorization_reference: string; reference_assessment_reference: string; performance_brief: Record<string, unknown>; capability_profile_id: string; capability_evidence_reference: string; license_evidence_reference: string; master_audio_id: string; master_start_ms: number; master_end_ms: number; authorization_reference: string; suitability: string; suitability_assessment_id: string | null; talking_job_id: string | null; talking_output_asset_id: string | null; talking_qa_job_id: string | null; talking_series_id: string | null; talking_preview_job_id: string | null; talking_run_id: string | null };
type Qa = { scene_plan_id: string; generation_job_id: string; output_asset_id: string | null; qa_job_id: string | null; state: string };
type Preview = { scene_plan_id: string; series_id: string; job_id: string; output_asset_id: string | null; state: string; child_review_state: string; continuity_review_state: string };
type Dimension = "visible_sync" | "identity" | "artifacts" | "source_performance" | "continuity" | "publishability";
type Concern = { id: string; finding: { dimension: Dimension; reason: string; start_ms?: number | null; end_ms?: number | null }; evidence_reference: string; answer: null | { approved: boolean; evidence_reference: string; reason: string } };
type SeriesReview = { readiness?: string; ready_for_human_continuity_review?: boolean; children?: Array<{ job_id: string; job_status: string; output_asset_id: string | null; automated_qa_state: string; human_review_state: string; blockers: string[] }>; blockers?: string[]; planned_origin_sha256?: string | null; preview_asset_id?: string | null; preview_sha256?: string | null; review_policy_version?: 1 | 2; required_dimensions?: Dimension[]; review_concerns?: Concern[]; continuity_review_state?: string; continuity_review_evidence_reference?: string | null; continuity_review_findings?: string[]; dimensions?: Partial<Record<Dimension, string>> | null; scoped_findings?: Array<{ dimension: Dimension; reason: string; start_ms?: number | null; end_ms?: number | null }>; [key: string]: unknown };
type RepairPlan = { fingerprint: string; failure_kind: string; action: "regenerate_scene" | "replan" | "stop"; stop_reasons: string[]; findings: Array<{ dimension: string; reason: string; start_ms?: number | null; end_ms?: number | null }>; master_audio_id: string; master_start_ms: number; master_end_ms: number; remaining_run_repairs: number; max_provider_calls: number; external_charge_ceiling: string; currency: string; local_compute_cost: null; user_active_minutes: null; review_scope: string };
type RepairCallCost = { amount: string | null; currency: string | null } | null;
type RepairRecord = { id: string; scene_plan_id: string; reason: string; replacement_job_id: string; predecessor_series_id: string | null; successor_series_id: string | null; reused_master_audio_id: string; created_at: string; replacement_job_status?: string; plan?: Pick<RepairPlan, "failure_kind" | "findings">; old_binding?: Pick<Binding, "talking_output_asset_id">; provider_calls?: Array<{ actual_cost: RepairCallCost }>; predecessor_provider_calls?: Array<{ actual_cost: RepairCallCost }> };
const dimensions: Array<[Dimension, string]> = [["visible_sync", "口型/可见同步"], ["identity", "身份/相似度"], ["artifacts", "脸部/口部伪影"], ["source_performance", "原素材表现保留"], ["continuity", "全片连续性"], ["publishability", "可发布性"]];
async function stableConcernKey(value: unknown) {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(value)));
  return `web-v2-concern:${Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("")}`;
}
type Scene = { scene_plan_id: string; scene_id: string };
const labels: Record<string, string> = { current: "当前有效，可复用", missing_admission: "有审核记录但未授予复用", revoked: "复用已撤销", source_stale: "原素材已变化", evidence_unavailable: "证据不可用", evidence_changed: "证据内容已变化", corrupt_record: "记录损坏" };
const statusText = (value: string) => value.replace(/_/g, " ");
const lines = (value: string) => value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);

export function ProductionTalkingPanel({ projectId, run, request, confirmAction = message => window.confirm(message), onRunChange }: {
  projectId: string; run: ProductionRun; request: Request; confirmAction?: (message: string) => boolean; onRunChange: (run: ProductionRun) => void;
}) {
  const [profiles, setProfiles] = useState<Profile[]>([]); const [caps, setCaps] = useState<Capability[]>([]); const [assets, setAssets] = useState<Asset[]>([]);
  const [clips, setClips] = useState<Clip[]>([]); const [reviews, setReviews] = useState<Record<string, ReviewChoice[]>>({}); const [contexts, setContexts] = useState<Record<string, SourceContext>>({});
  const [profileId, setProfileId] = useState(""); const [clipId, setClipId] = useState(""); const [capId, setCapId] = useState(""); const [authorization, setAuthorization] = useState(""); const [reviewKey, setReviewKey] = useState("");
  const [operation, setOperation] = useState(""); const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [seriesReview, setSeriesReview] = useState<Record<string, SeriesReview>>({});
  const [mediaUrls, setMediaUrls] = useState<Record<string, string>>({}); const scopeRef = useRef(""); const epoch = useRef(0); const busy = useRef(false);
  const [repairPlans, setRepairPlans] = useState<Record<string, RepairPlan>>({}); const [repairReasons, setRepairReasons] = useState<Record<string, string>>({}); const [repairRecords, setRepairRecords] = useState<RepairRecord[]>([]);
  const scope = `${projectId}:${run.id}:${run.preflight_fingerprint}:${run.talking_master_audio_id ?? "none"}:${JSON.stringify([
    ...(run.talking_qa_dependencies ?? []).map(item => [item.scene_plan_id, item.generation_job_id, item.output_asset_id, item.qa_job_id, item.state]),
    ...(run.talking_preview_dependencies ?? []).map(item => [item.scene_plan_id, item.series_id, item.job_id, item.output_asset_id, item.state]),
  ])}`; scopeRef.current = scope;
  const selectedProfile = profiles.find(item => item.id === profileId); const permittedClips = selectedProfile ? clips.filter(item => selectedProfile.reference_clip_ids.includes(item.id)) : [];
  const selectedClip = permittedClips.find(item => item.id === clipId); const selectedAsset = assets.find(item => item.id === selectedClip?.asset_id); const selectedCap = caps.find(item => item.id === capId);
  const reviewChoices = clipId ? reviews[clipId] ?? [] : [];
  const repairScope = `${scope}:${JSON.stringify((run.talking_preview_dependencies ?? []).map(dependency => {
    const qa = (run.talking_qa_dependencies ?? []).find(item => item.scene_plan_id === dependency.scene_plan_id);
    const qaIdentity = qa ? `${qa.generation_job_id}:${qa.qa_job_id ?? "no-qa"}:${qa.state}:${qa.output_asset_id ?? "no-output"}` : "no-qa-dependency";
    const key = talkingSeriesReviewKey(dependency.scene_plan_id, dependency.series_id, dependency.output_asset_id, assets.find(item => item.id === dependency.output_asset_id)?.content_hash, talkingReviewPolicyVersion(run), qaIdentity);
    const review = seriesReview[key];
    return [key, review?.continuity_review_state, ...(review?.review_concerns ?? []).map(item => `${item.id}:${item.answer?.approved ?? "pending"}`)];
  }))}`;
  const repairScopeRef = useRef(repairScope); repairScopeRef.current = repairScope;
  const lock = () => { if (busy.current) return false; busy.current = true; setOperation("action"); setError(""); setNotice(""); return true; };
  const unlock = () => { busy.current = false; setOperation(""); };
  async function refreshRun(message?: string) {
    const value = await request<ProductionRun>(`/projects/${projectId}/production-runs/${run.id}`);
    if (value.id !== run.id || value.project_id !== projectId) throw new Error("刷新返回了其他 Run 或项目。");
    if (scopeRef.current === scope) { onRunChange(value); if (message) setNotice(message); }
  }
  useEffect(() => {
    const token = ++epoch.current; setProfiles([]); setCaps([]); setAssets([]); setClips([]); setReviews({}); setContexts({}); setSeriesReview({}); setReviewKey(""); setMediaUrls({}); setError(""); setNotice("");
    Promise.all([request<Profile[]>("/talking-profiles"), request<Capability[]>("/execution-settings/capabilities"), request<Asset[]>("/assets")]).then(async ([p, c, a]) => {
      if (token !== epoch.current) return;
      setProfiles(p); setCaps(c.filter(x => x.capability === "talking")); setAssets(a);
      const ids = [...new Set(p.flatMap(item => item.reference_clip_ids))];
      const sets = await Promise.all(a.map(async asset => request<Clip[]>(`/assets/${asset.id}/clips`).catch(() => [])));
      if (token === epoch.current) setClips(sets.flat().filter(clip => ids.includes(clip.id)));
    }).catch(reason => { if (token === epoch.current) setError(reason instanceof Error ? reason.message : "Talking 资料加载失败"); });
    return () => { ++epoch.current; setReviewKey(""); setMediaUrls(current => { Object.values(current).forEach(URL.revokeObjectURL); return {}; }); };
  }, [projectId, request, run.id]);
  useEffect(() => { setReviewKey(""); setReviews({}); setContexts({}); setSeriesReview({}); setRepairPlans({}); setRepairReasons({}); setRepairRecords([]); busy.current = false; setOperation(""); }, [scope]);
  useEffect(() => { setRepairPlans({}); setRepairReasons({}); }, [repairScope]);
  useEffect(() => { let active = true; request<RepairRecord[]>(`/projects/${projectId}/production-runs/${run.id}/talking-repairs`).then(items => { if (active && scopeRef.current === scope) setRepairRecords(items); }).catch(reason => { if (active && scopeRef.current === scope) setError(reason instanceof Error ? reason.message : "修复记录读取失败"); }); return () => { active = false; }; }, [projectId, run.id, request, scope]);
  useEffect(() => {
    const token = epoch.current;
    const bindings = run.talking_source_bindings ?? [];
    for (const binding of bindings) {
      if (reviews[binding.reference_clip_id]) continue;
      request<ReviewChoice[]>(`/clips/${binding.reference_clip_id}/talking-source-reviews`).then(value => {
        if (token === epoch.current && scopeRef.current === scope) setReviews(previous => ({ ...previous, [binding.reference_clip_id]: value }));
      }).catch(reason => { if (token === epoch.current && scopeRef.current === scope) setError(reason instanceof Error ? reason.message : "已绑定来源的审核状态读取失败"); });
    }
  }, [run.talking_source_bindings, reviews, request, scope]);
  useEffect(() => {
    const token = epoch.current;
    for (const dependency of run.talking_preview_dependencies ?? []) {
      const qa = (run.talking_qa_dependencies ?? []).find(item => item.scene_plan_id === dependency.scene_plan_id);
      const qaIdentity = qa ? `${qa.generation_job_id}:${qa.qa_job_id ?? "no-qa"}:${qa.state}:${qa.output_asset_id ?? "no-output"}` : "no-qa-dependency";
      const cacheKey = talkingSeriesReviewKey(dependency.scene_plan_id, dependency.series_id, dependency.output_asset_id, assets.find(item => item.id === dependency.output_asset_id)?.content_hash, talkingReviewPolicyVersion(run), qaIdentity);
      if (seriesReview[cacheKey]) continue;
      request<SeriesReview>(`/projects/${projectId}/talking-slice-series/${dependency.series_id}`).then(value => {
        if (token === epoch.current && scopeRef.current === scope) setSeriesReview(previous => ({ ...previous, [cacheKey]: value }));
      }).catch(reason => { if (token === epoch.current && scopeRef.current === scope) setError(reason instanceof Error ? reason.message : "Run 预览审核依赖读取失败"); });
    }
  }, [run.talking_preview_dependencies, run.talking_review_policy_version, seriesReview, assets, projectId, request, scope]);
  useEffect(() => {
    let cancelled = false; const created: string[] = [];
    async function load() {
      const exactAssetIds = [...new Set([...(run.talking_qa_dependencies ?? []).map(item => item.output_asset_id), ...(run.talking_preview_dependencies ?? []).map(item => item.output_asset_id)].filter((item): item is string => !!item))];
      for (const assetId of exactAssetIds) {
        if (assetId) {
          try { const headers = new Headers(); const token = window.sessionStorage.getItem("content-os-access-token"); if (token) headers.set("Authorization", `Bearer ${token}`);
            const response = await fetch(`/assets/${assetId}/media`, { headers }); if (!response.ok) continue;
            const url = URL.createObjectURL(await response.blob()); created.push(url); if (!cancelled) setMediaUrls(previous => ({ ...previous, [assetId]: url })); else URL.revokeObjectURL(url);
          } catch { /* Media failure is rendered as unavailable; metadata and review remain separate. */ }
        }
      }
    }
    void load(); return () => { cancelled = true; created.forEach(URL.revokeObjectURL); };
  }, [projectId, run.id, run.talking_qa_dependencies, run.talking_preview_dependencies]);
  useEffect(() => {
    let cancelled = false;
    const ids = [...new Set([...(run.talking_qa_dependencies ?? []).map(item => item.output_asset_id), ...(run.talking_preview_dependencies ?? []).map(item => item.output_asset_id)].filter((id): id is string => !!id))];
    void Promise.all(ids.map(async id => { if (assets.some(item => item.id === id)) return null; try { return await request<Asset>(`/assets/${id}`); } catch { return null; } }))
      .then(values => { if (!cancelled) setAssets(previous => { const additions = values.filter((item): item is Asset => !!item && !previous.some(old => old.id === item.id)); return additions.length ? [...previous, ...additions] : previous; }); });
    return () => { cancelled = true; };
  }, [run.talking_qa_dependencies, run.talking_preview_dependencies, assets, request]);
  async function loadClipEvidence(id: string) {
    setClipId(id); setReviewKey(""); setError(""); setReviews(previous => ({ ...previous, [id]: [] }));
    try { const value = await request<ReviewChoice[]>(`/clips/${id}/talking-source-reviews`); if (scopeRef.current === scope) setReviews(previous => ({ ...previous, [id]: value })); }
    catch (reason) { if (scopeRef.current === scope) setError(reason instanceof Error ? reason.message : "来源审核读取失败"); }
  }
  async function loadContext(scene: Scene) {
    try { const value = await request<SourceContext>(`/projects/${projectId}/production-runs/${run.id}/talking-source-context?scene_plan_id=${encodeURIComponent(scene.scene_plan_id)}`); if (scopeRef.current === scope) setContexts(previous => ({ ...previous, [scene.scene_plan_id]: value })); }
    catch (reason) { if (scopeRef.current === scope) setError(reason instanceof Error ? reason.message : "旁白区间读取失败"); }
  }
  async function act(path: string, body?: unknown, message?: string) {
    if (!lock()) return; const expected = scope;
    try { const value = await request<ProductionRun>(`/projects/${projectId}/production-runs/${run.id}/${path}`, { method: "POST", ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      if (value.project_id !== projectId || value.id !== run.id) throw new Error("动作返回的 Run 与当前范围不一致。"); if (scopeRef.current === expected) { onRunChange(value); if (message) setNotice(message); }
    } catch (reason) { if (scopeRef.current === expected) setError(reason instanceof Error ? reason.message : "后台拒绝此操作；刷新状态后再决定下一步。"); } finally { unlock(); }
  }
  async function bind(scene: Scene) {
    const context = contexts[scene.scene_plan_id]; if (!canBindTalkingSource(selectedProfile, clipId, selectedCap, authorization, context) || !selectedClip) return;
    await act("talking-source-bind", { scene_plan_id: scene.scene_plan_id, talking_profile_id: profileId, reference_clip_id: clipId, capability_profile_id: capId, master_start_ms: context.speech_start_ms, master_end_ms: context.speech_end_ms, authorization_reference: authorization.trim(), brief: { require_no_burned_subtitles: true } }, "来源与精确旁白区间已绑定；是否适用仍由现有后台门禁判定。");
  }
  async function submitSourceReview(event: FormEvent<HTMLFormElement>, scene: Scene) {
    event.preventDefault(); const form = event.currentTarget; const source = selectedClip; if (!source || !selectedAsset || !reviewKey.trim()) return;
    if (!confirmAction(`确认你已完整检查原生素材 Clip ${source.id} 的全区间 ${source.start_ms}–${source.end_ms} ms？这将保存不可覆盖的来源审核记录。`)) return;
    if (!lock()) return; const expected = scope; const data = new FormData(form); const outcome = (key: string) => String(data.get(key) ?? "unknown");
    try {
      const key = `web-talking-review:${projectId}:${run.id}:${scene.scene_plan_id}:${source.id}:${selectedAsset.content_hash}`;
      await request(`/clips/${source.id}/talking-source-reviews`, { method: "POST", headers: { "X-Content-OS-Talking-Review-Key": reviewKey.trim() }, body: JSON.stringify({ idempotency_key: key, expected_source_content_hash: selectedAsset.content_hash, expected_start_ms: source.start_ms, expected_end_ms: source.end_ms, presentation: selectedAsset.width < selectedAsset.height ? "source_native_portrait" : "unspecified", coverage: "full_interval_continuous", face_head_clearance: outcome("face"), motion_continuity: outcome("motion"), subtitle_clearance: outcome("subtitles"), effective_quality: outcome("quality"), findings: lines(String(data.get("findings") ?? "")), confirmed_inspection: true, adopt_for_reuse: data.get("adopt") === "on" }) });
      if (scopeRef.current === expected) { await loadClipEvidence(source.id); setNotice("来源判断已提交；只有明确完整且正向并选择复用的记录会生成复用凭据。"); }
    } catch (reason) { if (scopeRef.current === expected) setError(reason instanceof Error ? reason.message : "来源审核提交失败"); } finally { setReviewKey(""); unlock(); }
  }
  async function applySource(scene: Scene, review: ReviewChoice) { if (!review.admission?.id) return; await act("talking-suitability-apply", { scene_plan_id: scene.scene_plan_id, assessment_id: review.assessment_id }, "来源审核已提交给 Run；不会自动派发。"); }
  async function loadSeries(scene: Scene, preview: Preview) {
    const qa = (run.talking_qa_dependencies ?? []).find(item => item.scene_plan_id === scene.scene_plan_id);
    const qaIdentity = qa ? `${qa.generation_job_id}:${qa.qa_job_id ?? "no-qa"}:${qa.state}:${qa.output_asset_id ?? "no-output"}` : "no-qa-dependency";
    const cacheKey = talkingSeriesReviewKey(scene.scene_plan_id, preview.series_id, preview.output_asset_id, assets.find(item => item.id === preview.output_asset_id)?.content_hash, talkingReviewPolicyVersion(run), qaIdentity);
    try { const value = await request<SeriesReview>(`/projects/${projectId}/talking-slice-series/${preview.series_id}`); if (scopeRef.current === scope) setSeriesReview(previous => ({ ...previous, [cacheKey]: value })); }
    catch (reason) { if (scopeRef.current === scope) setError(reason instanceof Error ? reason.message : "Talking 子任务审核材料读取失败"); }
  }
  async function loadRepairPlan(scene: Scene) {
    const expected = repairScope;
    try { const plan = await request<RepairPlan>(`/projects/${projectId}/production-runs/${run.id}/talking-repair-plan?scene_plan_id=${encodeURIComponent(scene.scene_plan_id)}`); if (repairScopeRef.current === expected) setRepairPlans(previous => ({ ...previous, [scene.scene_plan_id]: plan })); }
    catch (reason) { if (repairScopeRef.current === expected) setError(reason instanceof Error ? reason.message : "修复计划读取失败；没有提交修复"); }
  }
  async function submitRepair(event: FormEvent<HTMLFormElement>, scene: Scene, plan: RepairPlan) {
    event.preventDefault(); const reason = repairReasons[scene.scene_plan_id]?.trim();
    if (!reason || plan.action !== "regenerate_scene" || !confirmAction(`确认仅重生成场景 ${scene.scene_id}？该动作最多一次 provider call；外部收费上限 ${plan.external_charge_ceiling} ${plan.currency}；新产物必须通过全新 QA 与完整结果审核。`)) return;
    if (!lock()) return; const expected = repairScope;
    try { await request<RepairRecord>(`/projects/${projectId}/production-runs/${run.id}/talking-repair`, { method: "POST", body: JSON.stringify({ scene_plan_id: scene.scene_plan_id, expected_fingerprint: plan.fingerprint, idempotency_key: talkingRepairKey(projectId, run.id, scene.scene_plan_id, plan.fingerprint), confirmed_action: "regenerate_scene", reason }) });
      if (repairScopeRef.current === expected) { setRepairPlans(previous => { const next = { ...previous }; delete next[scene.scene_plan_id]; return next; }); setRepairReasons(previous => ({ ...previous, [scene.scene_plan_id]: "" })); const records = await request<RepairRecord[]>(`/projects/${projectId}/production-runs/${run.id}/talking-repairs`); if (repairScopeRef.current !== expected) return; setRepairRecords(records); await refreshRun("修复 Job 已创建；前序证据保留，新产物等待新的 QA 和完整审核。"); }
    } catch (reason) { if (repairScopeRef.current === expected) setError(reason instanceof Error ? reason.message : "后台拒绝修复；未自动重试，请刷新计划后再决定。"); } finally { unlock(); }
  }
  async function submitChildReview(event: FormEvent<HTMLFormElement>, assetId: string) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); const approved = data.get("approved") === "yes"; const findings = lines(String(data.get("findings") ?? "")); const evidence = String(data.get("evidence") ?? "").trim();
    if (!evidence || !findings.length || !confirmAction(`确认提交此 Talking 子片 Asset ${assetId} 的独立 U-Talking 判断？此判断不可覆盖。`)) return;
    if (!lock()) return; try { const updatedAsset = await request<Asset>(`/projects/${projectId}/talking-assets/${assetId}/human-review`, { method: "POST", body: JSON.stringify({ approved, evidence_reference: evidence, findings }) }); setAssets(previous => previous.map(item => item.id === updatedAsset.id ? updatedAsset : item)); await refreshRun("此子片 U-Talking 判断已保存；不会自动续跑。"); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "子片审核提交失败"); } finally { unlock(); }
  }
  async function submitContinuity(event: FormEvent<HTMLFormElement>, scene: Scene, preview: Preview, asset: Asset) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); const approved = data.get("approved") === "yes"; const evidence = String(data.get("evidence") ?? "").trim(); const findings = lines(String(data.get("findings") ?? ""));
    const qa = (run.talking_qa_dependencies ?? []).find(item => item.scene_plan_id === scene.scene_plan_id);
    const qaIdentity = qa ? `${qa.generation_job_id}:${qa.qa_job_id ?? "no-qa"}:${qa.state}:${qa.output_asset_id ?? "no-output"}` : "no-qa-dependency";
    const currentKey = talkingSeriesReviewKey(scene.scene_plan_id, preview.series_id, preview.output_asset_id, assets.find(item => item.id === preview.output_asset_id)?.content_hash, talkingReviewPolicyVersion(run), qaIdentity);
    const currentSeries = seriesReview[currentKey];
    const policy = talkingReviewPolicyVersion(run); const values = Object.fromEntries(dimensions.map(([key]) => [key, String(data.get(key) ?? "unknown")])) as Record<Dimension, string>;
    const v2Answers = policy === 2 ? (currentSeries?.review_concerns ?? []).filter(item => !item.answer).map(item => ({ concern_id: item.id, approved: data.get(`concern:${item.id}`) === "yes", evidence_reference: String(data.get(`concern-evidence:${item.id}`) ?? "").trim(), reason: String(data.get(`concern-reason:${item.id}`) ?? "").trim() })) : [];
    const dimensionValues = Object.values(values);
    const scopedFindings = policy === 2 && !approved ? [{ dimension: String(data.get("failure_dimension") ?? "continuity"), reason: String(data.get("failure_reason") ?? "").trim() }] : [];
    if (!evidence || (policy === 1 && !findings.length) || (policy === 2 && approved && dimensionValues.some(value => value !== "pass")) || (policy === 2 && !approved && !scopedFindings[0].reason) || v2Answers.some(item => !item.evidence_reference || !item.reason) || !confirmAction(`确认审核完整 TalkingRun 预览 ${asset.id}（SHA-256 ${asset.content_hash}），政策 v${policy}？该决定绑定精确预览且不可覆盖。`)) return;
    const body = { approved, evidence_reference: evidence, findings, preview_asset_id: asset.id, preview_sha256: asset.content_hash,
      ...(policy === 2 ? { review_policy_version: 2, dimensions: values, scoped_findings: scopedFindings, concern_answers: v2Answers } : {}) };
    if (!lock()) return; const expected = scope; try { await request(`/projects/${projectId}/talking-slice-series/${preview.series_id}/continuity-review`, { method: "POST", body: JSON.stringify(body) }); if (scopeRef.current !== expected) return; await loadSeries(scene, preview); if (scopeRef.current !== expected) return; await refreshRun("完整 TalkingRun 判断已保存；仍需明确运行准入，后台会检查未回答问题与拒绝记录。"); }
    catch (reason) { if (scopeRef.current === expected) setError(reason instanceof Error ? reason.message : "Run 连续性审核提交失败"); } finally { unlock(); }
  }
  async function registerConcern(event: FormEvent<HTMLFormElement>, scene: Scene, preview: Preview, asset: Asset) {
    event.preventDefault(); const data = new FormData(event.currentTarget); const reason = String(data.get("concern_reason") ?? "").trim(); const evidence = String(data.get("concern_evidence") ?? "").trim();
    if (!reason || !evidence || !confirmAction(`将此观察登记为绑定 Preview ${asset.id}/${asset.content_hash} 的局部问题？`)) return;
    if (!lock()) return; const expected = scope; try { const finding = { dimension: String(data.get("concern_dimension")), reason, ...(String(data.get("start_ms") ?? "") ? { start_ms: Number(data.get("start_ms")), end_ms: Number(data.get("end_ms")) } : {}) }; const previewIdentity = { preview_asset_id: asset.id, preview_sha256: asset.content_hash, finding, evidence_reference: evidence }; const idempotencyKey = await stableConcernKey({ projectId, seriesId: preview.series_id, ...previewIdentity }); if (scopeRef.current !== expected) return; await request(`/projects/${projectId}/talking-slice-series/${preview.series_id}/review-concerns`, { method: "POST", body: JSON.stringify({ idempotency_key: idempotencyKey, ...previewIdentity }) }); if (scopeRef.current !== expected) return; await loadSeries(scene, preview); if (scopeRef.current !== expected) return; setNotice("局部问题已追加；完整判断仍待提交/需重新检查。"); }
    catch (reason) { if (scopeRef.current === expected) setError(reason instanceof Error ? reason.message : "局部问题登记失败"); } finally { unlock(); }
  }
  async function answerConcerns(event: FormEvent<HTMLFormElement>, scene: Scene, preview: Preview, asset: Asset) {
    event.preventDefault(); const qa = (run.talking_qa_dependencies ?? []).find(item => item.scene_plan_id === scene.scene_plan_id); const qaIdentity = qa ? `${qa.generation_job_id}:${qa.qa_job_id ?? "no-qa"}:${qa.state}:${qa.output_asset_id ?? "no-output"}` : "no-qa-dependency"; const cacheKey = talkingSeriesReviewKey(scene.scene_plan_id, preview.series_id, preview.output_asset_id, asset.content_hash, talkingReviewPolicyVersion(run), qaIdentity); const data = new FormData(event.currentTarget); const pending = (seriesReview[cacheKey]?.review_concerns ?? []).filter(item => !item.answer);
    const answers = pending.map(item => ({ concern_id: item.id, approved: data.get(`late:${item.id}`) === "yes", evidence_reference: String(data.get(`late-evidence:${item.id}`) ?? "").trim(), reason: String(data.get(`late-reason:${item.id}`) ?? "").trim() }));
    if (!answers.length || answers.some(item => !item.evidence_reference || !item.reason) || !confirmAction(`确认追加 ${answers.length} 个局部问题回答？原完整 Run 判断不会被覆盖。`)) return;
    if (!lock()) return; const expected = scope; try { await request(`/projects/${projectId}/talking-slice-series/${preview.series_id}/review-concern-answers`, { method: "POST", body: JSON.stringify({ preview_asset_id: asset.id, preview_sha256: asset.content_hash, answers }) }); if (scopeRef.current !== expected) return; await loadSeries(scene, preview); if (scopeRef.current !== expected) return; await refreshRun("局部问题回答已追加；后台仍需复核完整审核与准入状态。"); }
    catch (reason) { if (scopeRef.current === expected) setError(reason instanceof Error ? reason.message : "局部问题回答失败"); } finally { unlock(); }
  }
  const scenes = run.talking_dependencies ?? [];
  const sceneFor = (id: string): Scene => ({ scene_plan_id: id, scene_id: id });
  const sceneIndex = useMemo(() => new Map(scenes.map((item, index) => [item.scene_plan_id, index + 1])), [scenes]);
  return <section className="draft-editor" aria-label="此执行的人物画面 TalkingRun">
    <div className="row-between"><div><span className="kicker">TALKING / SOURCE → REVIEWED RUN</span><h3>此执行的人物画面</h3></div><span className="badge">{scenes.length} 个 Talking 场景</span></div>
    <p className="muted">这里只使用 Talking Profile 已登记的参考 Clip。来源适用性、同意、能力、许可、成本及执行门禁均由后台复核；技术 QA 不替代本人相似度、自然度或可发布性判断。</p>
    {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="ok">{notice}</p>}
    {scenes.map((dependency, index) => {
      const scene = sceneFor(dependency.scene_plan_id); const binding = (run.talking_source_bindings ?? []).find(item => item.scene_plan_id === dependency.scene_plan_id) as Binding | undefined;
      const qa = (run.talking_qa_dependencies ?? []).find(item => item.scene_plan_id === dependency.scene_plan_id) as Qa | undefined;
      const preview = (run.talking_preview_dependencies ?? []).find(item => item.scene_plan_id === dependency.scene_plan_id) as Preview | undefined;
      const context = contexts[dependency.scene_plan_id]; const review = binding ? reviews[binding.reference_clip_id] ?? [] : reviewChoices;
      const knownReview = review.find(item => item.assessment_id === binding?.suitability_assessment_id)
        ?? review.find(item => item.state === "current" && item.decision === "review_claimed_suitable" && item.admission?.active)
        ?? review.find(item => item.assessment?.source_content_hash === binding?.reference_asset_hash);
      const existing = knownReview ?? review.find(item => item.state === "current");
      const boundProfile = profiles.find(item => item.id === binding?.talking_profile_id);
      const boundCapability = caps.find(item => item.id === binding?.capability_profile_id);
      const child = qa?.output_asset_id ? assets.find(item => item.id === qa.output_asset_id) : undefined;
      const priorChildReview = child?.metadata.talking_generation && typeof child.metadata.talking_generation === "object" ? (child.metadata.talking_generation as Record<string, unknown>).human_review as { approved?: boolean; evidence_reference?: string; findings?: string[] } | undefined : undefined;
      const previewAsset = preview?.output_asset_id ? assets.find(item => item.id === preview.output_asset_id) : undefined;
      const qaIdentity = qa ? `${qa.generation_job_id}:${qa.qa_job_id ?? "no-qa"}:${qa.state}:${qa.output_asset_id ?? "no-output"}` : "no-qa-dependency";
      const reviewCacheKey = preview ? talkingSeriesReviewKey(dependency.scene_plan_id, preview.series_id, preview.output_asset_id, assets.find(item => item.id === preview.output_asset_id)?.content_hash, talkingReviewPolicyVersion(run), qaIdentity) : "";
      const series = reviewCacheKey ? seriesReview[reviewCacheKey] : undefined;
      const concernScope = (series?.review_concerns ?? []).map(item => `${item.id}:${item.answer?.approved ?? "pending"}`).join(",");
      const repairItems = repairRecords.filter(item => item.scene_plan_id === scene.scene_plan_id);
      const hasNegativeConcern = series?.continuity_review_state === "approved" && (series.review_concerns ?? []).some(item => item.answer?.approved === false);
      const canRequestRepairPlan = preview?.continuity_review_state === "rejected" || qa?.state === "generation_failed" || hasNegativeConcern;
      return <article className="scene" key={`${dependency.scene_plan_id}:${talkingReviewPolicyVersion(run)}:${qa?.generation_job_id ?? "no-job"}:${qa?.output_asset_id ?? "no-qa-asset"}:${preview?.series_id ?? "no-series"}:${previewAsset?.id ?? "no-preview"}:${previewAsset?.content_hash ?? "no-hash"}:${concernScope}`} aria-label={`Talking 场景 ${index + 1}`}>
        <h4>场景 {index + 1} · {dependency.scene_id}</h4>
        <button type="button" className="button" disabled={!!operation} onClick={() => void loadContext(scene)}>读取此场景的主旁白区间</button>
        {context && <p>Master {context.master_audio_id} · SHA-256 {context.master_content_hash} · {context.status === "ready" ? `${context.speech_start_ms}–${context.speech_end_ms} ms` : `无法确定区间：${context.reason}`}<br />场景旁白：{context.scene_voice_text}</p>}
        {!binding && <>
          <label>Talking Profile<select value={profileId} disabled={!!operation} onChange={event => { setProfileId(event.target.value); setClipId(""); setReviewKey(""); }}><option value="">请选择</option>{profiles.map(item => <option key={item.id} value={item.id}>{item.name} · {item.provider} · 本人同意：{item.consent.confirmed ? "已记录" : "未确认"} · 已授权来源 {item.reference_clip_ids.length} 项</option>)}</select></label>
          <label>Profile 授权来源 Clip<select value={clipId} disabled={!!operation || !selectedProfile} onChange={event => void loadClipEvidence(event.target.value)}><option value="">请选择</option>{permittedClips.map(item => { const asset = assets.find(a => a.id === item.asset_id); return <option key={item.id} value={item.id}>Clip {item.id} · {asset ? `${asset.width}×${asset.height}` : "素材信息未知"} · {item.start_ms}–{item.end_ms} ms · 字幕 {item.talking_reference_assessment?.burned_in_subtitles == null ? "未知" : item.talking_reference_assessment.burned_in_subtitles ? "有" : "未观察到"}</option>; })}</select></label>
          {selectedClip && selectedAsset && <p>素材 {selectedAsset.id} · {selectedAsset.width}×{selectedAsset.height} · hash {selectedAsset.content_hash} · 权利引用 {selectedAsset.authorization_reference} · 人脸可见 {selectedClip.face_visibility ?? "未知"} · 嘴部可见 {selectedClip.mouth_visibility ?? "未知"}。这些元数据不代表全区间质量审核。</p>}
          <label>Talking 能力档案<select value={capId} disabled={!!operation} onChange={event => setCapId(event.target.value)}><option value="">请选择</option>{caps.map(item => <option key={item.id} value={item.id}>{item.provider}/{item.model} · {item.runtime}/{item.machine_id} · readiness={item.readiness} · quality={item.quality_status} · license={item.commercial_status}</option>)}</select></label>
          {selectedCap && <p className="muted">能力证据 {selectedCap.evidence_reference ?? "未知"} · 许可证据 {selectedCap.license_evidence_reference ?? "未知"} · 来源 {selectedCap.provenance_source}</p>}
          <label>此场景 Talking 授权引用<input value={authorization} disabled={!!operation} onChange={event => setAuthorization(event.target.value)} /></label>
          <button className="button primary" disabled={!!operation || !canBindTalkingSource(selectedProfile, clipId, selectedCap, authorization, context) || !selectedClip} onClick={() => void bind(scene)}>绑定所选素材及服务端区间（后台复核）</button>
        </>}
        {binding && <div><p>已绑定 Profile {binding.talking_profile_id}（同意：{boundProfile?.consent.confirmed ? "已记录" : "未知/未确认"}）· Clip {binding.reference_clip_id} · 源 hash {binding.reference_asset_hash} · speech {binding.master_start_ms}–{binding.master_end_ms} ms · Source/rights {binding.source_authorization_reference} · capability {boundCapability ? `${boundCapability.provider}/${boundCapability.model} · quality=${boundCapability.quality_status} · license=${boundCapability.commercial_status}` : binding.capability_profile_id} · 许可引用 {binding.license_evidence_reference || "未知"} · {binding.suitability}</p>
          {existing && <p>绑定来源审核：{labels[existing.state] ?? existing.state} · 类型 {existing.assessment?.evidence_class ?? "未知"} · decision {existing.decision ?? "未知"} · receipt {existing.admission?.id ?? "无"} · 源区间 {existing.assessment?.start_ms ?? "未知"}–{existing.assessment?.end_ms ?? "未知"} ms · hash {existing.assessment?.source_content_hash ?? "未知"} · 覆盖 {existing.assessment?.coverage ?? "未知"} · 结论 {existing.assessment?.face_head_clearance ?? "?"}/{existing.assessment?.motion_continuity ?? "?"}/{existing.assessment?.subtitle_clearance ?? "?"}/{existing.assessment?.effective_quality ?? "?"} · evidence {existing.evidence_sha256 ?? "不可用"} · {existing.reasons.join(" · ")}</p>}
          {!binding.suitability_assessment_id && <>
            <button type="button" className="button" disabled={!!operation} onClick={() => void loadClipEvidence(binding.reference_clip_id)}>读取既有完整区间来源判断</button>
            {review.map(choice => <article key={choice.assessment_id} className="subform"><strong>{labels[choice.state] ?? statusText(choice.state)} · {choice.decision ?? "未知"}</strong><p>Assessment {choice.assessment_id} · {choice.assessment?.coverage ?? "覆盖未知"} · 头像/头部 {choice.assessment?.face_head_clearance ?? "未知"} · 连续性 {choice.assessment?.motion_continuity ?? "未知"} · 字幕 {choice.assessment?.subtitle_clearance ?? "未知"} · 成片质量 {choice.assessment?.effective_quality ?? "未知"}</p><p>审核 {choice.assessment?.reviewer_reference ?? "未知"} · evidence {choice.evidence_sha256 ?? "不可用"} · {choice.reasons.join(" · ")}</p>{canReuseTalkingSource(choice) && <button className="button" disabled={!!operation} onClick={() => void applySource(scene, choice)}>将既有有效判断应用于此场景</button>}</article>)}
            {!knownReview && selectedClip?.id === binding.reference_clip_id && <form className="subform" onSubmit={event => void submitSourceReview(event, scene)}><h5>缺少可复用记录：执行一次全区间来源人审</h5><p>这是对原生素材全区间的主观核验，不是自动检测，也不批准横转竖或局部二次裁切。审查中不得把测试/fixture 证据作为真实判断。</p>
              <label>本机配置的 reviewer key（只在内存使用）<input type="password" autoComplete="off" value={reviewKey} onChange={event => setReviewKey(event.target.value)} /></label>
              {[ ["face", "头部/人脸全区间保持在画面内"], ["motion", "动作与连续性可接受"], ["subtitles", "字幕对该用途无冲突"], ["quality", "此来源用于此场景的整体质量"] ].map(([name, label]) => <label key={name}>{label}<select name={name} defaultValue="unknown" required><option value="unknown">未知</option><option value="pass">通过</option><option value="fail">不通过</option></select></label>)}
              <label><input type="checkbox" name="adopt" />若完整正向，保存为以后可复用的审核依据</label><label>实际观察发现（每行一条）<textarea name="findings" required rows={3} /></label>
              <button className="button" disabled={!!operation || !reviewKey.trim() || !selectedAsset}>提交全区间审核</button>
            </form>}
            {knownReview && !existing?.admission?.active && <p className="error">此 Clip 已有不可覆盖的记录，但当前无有效复用授权（{labels[knownReview.state] ?? knownReview.state} / {knownReview.decision ?? "判断未知"}）。不要重复审同一证据；需选另一授权素材或先解决记录失效原因。</p>}
          </>}
          {binding.suitability_assessment_id && <div><p>来源审核 {binding.suitability_assessment_id} 已绑定。</p><button className="button primary" disabled={!!operation || !existing?.admission?.id || existing.state !== "current"} onClick={() => existing?.admission?.id && void act("talking-dispatch", { scene_plan_id: scene.scene_plan_id, source_admission_id: existing.admission.id }, "已提交派发请求；请检查后台状态与预算/许可门禁。")}>按已审核来源派发 Talking（后台校验）</button>{!existing?.admission?.id && <p className="error">当前绑定尚无有效来源复用凭据，不能派发。</p>}</div>}
        </div>}
        {qa && <div className="subform"><h5>Talking 子任务与技术 QA</h5><p>生成 Job {qa.generation_job_id} · QA Job {qa.qa_job_id ?? "尚无"} · 状态 {statusText(qa.state)} · 输出 {qa.output_asset_id ?? "尚无"}</p><p>QA 只证明自动技术检查，不证明人脸身份/自然度/唇形质量。</p>
          {(qa.state === "output_awaiting_qa" || qa.state === "qa_failed") && <button className="button" disabled={!!operation} onClick={() => void act("talking-qa-advance", { scene_plan_id: scene.scene_plan_id })}>推进独立 Talking 技术 QA（后台校验）</button>}
          {child && <><p>子片 Asset {child.id} · hash {child.content_hash}</p>{mediaUrls[child.id] ? <video controls preload="none" src={mediaUrls[child.id]} /> : <p className="muted">子片预览不可用或仍在加载。</p>}{run.talking_review_policy_version !== 2 && priorChildReview ? <p>已存在不可覆盖的 U-Talking：{priorChildReview.approved ? "通过" : "未通过"} · {priorChildReview.evidence_reference} · {priorChildReview.findings?.join("；")}</p> : run.talking_review_policy_version === 2 ? <p className={priorChildReview?.approved === false ? "error" : "muted"}>V2 子片状态：{priorChildReview?.approved === false ? "已有不可覆盖的历史驳回，仍阻断后续预览" : priorChildReview ? "存在历史正向子片判断；不作为 Run 审批依据" : "主观判断待完整 Run 预览；技术 QA 独立保留"}</p> : qa.state === "qa_verified_awaiting_u_talking" && <form className="subform" onSubmit={event => void submitChildReview(event, child.id)}><h5>针对精确子片提交 U-Talking 判断</h5><label>判断<select name="approved" defaultValue="" required><option value="" disabled>请选择</option><option value="yes">通过</option><option value="no">未通过</option></select></label><label>审核证据引用<input name="evidence" required /></label><label>具体发现（每行一条）<textarea name="findings" required rows={3} /></label><button className="button">保存不可覆盖的子片判断</button></form>}</>}
          {(run.talking_review_policy_version === 2 ? qa.state === "qa_verified_ready_for_preview" && priorChildReview?.approved !== false : qa.state === "qa_verified_awaiting_u_talking" && priorChildReview?.approved) && !preview && <button className="button" disabled={!!operation} onClick={() => void act("talking-preview-prepare", { scene_plan_id: scene.scene_plan_id }, "已请求准备完整 Run 的同 Master 预览；等待后台状态后刷新。")}>准备精确 TalkingRun 预览</button>}
        </div>}
        {(canRequestRepairPlan || repairItems.length > 0) && <RepairControls scene={scene} plan={repairPlans[scene.scene_plan_id]} reason={repairReasons[scene.scene_plan_id] ?? ""} records={repairItems} canRequestPlan={canRequestRepairPlan} busy={!!operation} onLoad={() => void loadRepairPlan(scene)} onReason={value => setRepairReasons(previous => ({ ...previous, [scene.scene_plan_id]: value }))} onSubmit={event => { const plan = repairPlans[scene.scene_plan_id]; if (plan) void submitRepair(event, scene, plan); }} />}
        {preview && <div className="subform"><h5>完整 TalkingRun 预览与准入</h5><p>Series {preview.series_id} · Preview Job {preview.job_id} · {statusText(preview.state)} · 子片审核 {preview.child_review_state} · Run 连续性 {preview.continuity_review_state}</p>
          {previewAsset && <><p>完整预览 Asset {previewAsset.id} · SHA-256 {previewAsset.content_hash} · 技术 QA {String(((previewAsset.metadata.talking_run_preview as Record<string, unknown> | undefined)?.technical_qa_state) ?? "详见审核报告")}</p>{mediaUrls[previewAsset.id] ? <video controls preload="none" src={mediaUrls[previewAsset.id]} /> : <p role="alert">认证预览媒体不可用或仍在加载。</p>}
            <button className="button" disabled={!!operation} onClick={() => void loadSeries(scene, preview)}>读取预览审核依赖</button>
            <p>完整审核包状态：{series?.readiness ?? "正在读取"} · planned origin {String(series?.planned_origin_sha256 ?? "未知")} · report preview hash {String(series?.preview_sha256 ?? previewAsset.content_hash)} · {series?.blockers?.join(" · ")}</p>
            {series?.review_policy_version === 2 && <><p>审核政策 v2 · 必需维度：{(series.required_dimensions ?? dimensions.map(([key]) => key)).join("、")}</p>{(series.review_concerns ?? []).map(item => <p key={item.id}>局部问题 {item.finding.dimension}：{item.finding.reason} · {item.answer ? `已回答：${item.answer.approved ? "接受/不阻断" : "否决"}（${item.answer.reason}）` : "待回答，当前阻断准入"}</p>)}{series.continuity_review_state !== "rejected" && <form className="subform" onSubmit={event => void registerConcern(event, scene, preview, previewAsset)}><h5>追加此精确预览的局部观察</h5><label>维度<select name="concern_dimension">{dimensions.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label>问题描述<input name="concern_reason" required /></label><label>证据引用<input name="concern_evidence" required /></label><label>局部起始毫秒（可选）<input type="number" min="0" name="start_ms" /></label><label>局部结束毫秒（两者需同时填写）<input type="number" min="1" name="end_ms" /></label><button className="button" disabled={!!operation}>追加不可删除的问题记录</button></form>}{series.continuity_review_state === "approved" && (series.review_concerns ?? []).some(item => !item.answer) && <form className="subform" onSubmit={event => void answerConcerns(event, scene, preview, previewAsset)}><h5>回答批准后新增的问题（追加式，不改写完整判断）</h5>{(series.review_concerns ?? []).filter(item => !item.answer).map(item => <fieldset key={item.id}><legend>{item.finding.dimension}：{item.finding.reason}</legend><label>判断<select name={`late:${item.id}`} defaultValue="" required><option value="" disabled>请选择</option><option value="yes">检查通过、不阻断</option><option value="no">确认问题、阻断</option></select></label><label>证据引用<input name={`late-evidence:${item.id}`} required /></label><label>理由<input name={`late-reason:${item.id}`} required /></label></fieldset>)}<button className="button">追加问题回答</button></form>}</>}
            {(series?.ready_for_human_continuity_review ?? preview.state === "completed_review_only") && !["approved", "rejected"].includes(series?.continuity_review_state ?? preview.continuity_review_state) && <form className="subform" onSubmit={event => void submitContinuity(event, scene, preview, previewAsset)}><h5>针对此精确完整预览提交 Run 连续性判断（政策 v{run.talking_review_policy_version ?? 1}）</h5><label>判断<select name="approved" defaultValue="" required><option value="" disabled>请选择</option><option value="yes">通过</option><option value="no">未通过</option></select></label>{run.talking_review_policy_version === 2 && <>{dimensions.map(([key, label]) => <label key={key}>{label}<select name={key} defaultValue="unknown" required><option value="unknown">未知</option><option value="pass">通过</option><option value="fail">不通过</option></select></label>)}<label>驳回维度<select name="failure_dimension">{dimensions.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label>驳回原因<input name="failure_reason" /></label>{(series?.review_concerns ?? []).filter(item => !item.answer).map(item => <fieldset key={item.id}><legend>回答问题：{item.finding.reason}</legend><label>判断<select name={`concern:${item.id}`} defaultValue="" required><option value="" disabled>请选择</option><option value="yes">已检查，不构成阻断</option><option value="no">确认问题，否决</option></select></label><label>证据引用<input name={`concern-evidence:${item.id}`} required /></label><label>回答理由<input name={`concern-reason:${item.id}`} required /></label></fieldset>)}</>}<label>审核证据引用<input name="evidence" required /></label><label>具体发现（每行一条）<textarea name="findings" rows={3} required={run.talking_review_policy_version !== 2 || undefined} /></label><button className="button">提交完整 Run 判断（绑定此 hash）</button></form>}{series?.continuity_review_state === "rejected" && <p role="alert" className="error">已有不可覆盖的完整 Run 驳回；此记录不可被新表单覆盖。可读取独立的有界修复计划，若其允许才可显式重生成新产物。</p>}{series?.continuity_review_state === "approved" && <p>已批准完整 Run 审核：政策 v{series.review_policy_version ?? 1} · {String(series.continuity_review_evidence_reference ?? "证据引用已保存")} · {Array.isArray(series.continuity_review_findings) ? series.continuity_review_findings.map(String).join("；") : ""}</p>}</>}
          {preview.continuity_review_state === "approved" && preview.state !== "admitted" && <button className="button primary" disabled={!!operation} onClick={() => void act("talking-run-admit", { scene_plan_id: scene.scene_plan_id }, "已提交完整 TalkingRun 准入；后台会再次校验并消费相同预览字节。")}>准入此精确 TalkingRun（后台校验）</button>}
          {preview.state === "admitted" && <p role="status" className="ok">此 TalkingRun 已通过明确准入 · TalkingRun ID {binding?.talking_run_id ?? "后台状态已准入"}。此状态不代表最终成片通过。</p>}
        </div>}
        {!binding && <p className="muted">下一操作不会自动选择来源。当前执行状态：{run.status} · 场景序号 {sceneIndex.get(dependency.scene_plan_id)}</p>}
      </article>;
    })}
    <small className="muted">切换项目、草稿、Master 或 Run 会清除本地审核密钥与异步显示。表单提交均为不可覆盖的人审，不会自动续跑、重试或渲染。</small>
  </section>;
}

function RepairControls({ scene, plan, reason, records, canRequestPlan, busy, onLoad, onReason, onSubmit }: {
  scene: Scene; plan?: RepairPlan; reason: string; records: RepairRecord[]; canRequestPlan: boolean; busy: boolean;
  onLoad: () => void; onReason: (value: string) => void; onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  const actualCost = (calls: Array<{ actual_cost: RepairCallCost }> | undefined) => !calls?.length ? "无已记录 provider call（不是费用为零的测量）" : calls.map(item => item.actual_cost?.amount == null ? "实际费用未知" : `${item.actual_cost.amount} ${item.actual_cost.currency ?? "币种未知"}`).join("、");
  return <section className="subform" aria-label={`Talking 修复场景 ${scene.scene_id}`}>
    <h5>有界场景修复</h5>
    {records.map(record => <div key={record.id} role="status" aria-label={`修复记录 ${record.id}`}><p>修复记录 {record.id} · replacement Job {record.replacement_job_id}（{record.replacement_job_status ?? "状态未知"}）· 前序 Asset {record.old_binding?.talking_output_asset_id ?? "未知"} · 原 Series {record.predecessor_series_id ?? "无"} · successor Series {record.successor_series_id ?? "尚未创建"} · 理由：{record.reason}</p>{record.plan && <><p>前序证据类别：{record.plan.failure_kind === "quality" ? "质量审核证据" : record.plan.failure_kind === "technical" ? "技术执行证据" : record.plan.failure_kind}</p>{record.plan.findings.map((finding, index) => <p key={`${finding.dimension}:${index}`}>前序发现 {finding.dimension}：{finding.reason}{finding.start_ms != null || finding.end_ms != null ? ` · ${finding.start_ms ?? "?"}–${finding.end_ms ?? "?"} ms` : ""}</p>)}</>}<p>复用 Master {record.reused_master_audio_id} · provider call：{actualCost(record.provider_calls)} · 原产物 provider call：{actualCost(record.predecessor_provider_calls)}</p><p>修复会保留前序记录；replacement 需要新的技术 QA 和完整 TalkingRun 审核。</p></div>)}
    {!plan && canRequestPlan && <button type="button" className="button" disabled={busy} onClick={onLoad}>读取当前证据的修复计划</button>}
    {!plan && !canRequestPlan && records.length > 0 && <p>当前 replacement 正在处理中；新的修复计划需等当前证据状态更新后读取。</p>}
    {plan && <><p>决策：{plan.action} · 证据类型：{plan.failure_kind} · 剩余 Run 修复次数：{plan.remaining_run_repairs}</p>
      {plan.findings.map((finding, index) => <p key={`${finding.dimension}:${index}`}>发现 {finding.dimension}：{finding.reason}{finding.start_ms != null || finding.end_ms != null ? ` · ${finding.start_ms ?? "?"}–${finding.end_ms ?? "?"} ms` : ""}</p>)}
      {plan.stop_reasons.map(item => <p key={item} role="alert">停止原因：{item}</p>)}
      <p>执行粒度：整场景（非局部区间替换）· Master {plan.master_audio_id}（{plan.master_start_ms}–{plan.master_end_ms} ms）复用 · 最多 {plan.max_provider_calls} 次 provider call · 外部收费上限 {plan.external_charge_ceiling} {plan.currency} · 本地计算成本未知 · 用户操作时间未知。</p>
      <p>新产物审核范围：{plan.review_scope === "fresh_child_qa_and_policy_required_whole_result_review" ? "新的子片技术 QA + 按当前审核政策完整审查新 TalkingRun" : plan.review_scope}</p>
      {plan.action === "regenerate_scene" && <form onSubmit={onSubmit}><label>本次修复理由（必填）<textarea value={reason} onChange={event => onReason(event.target.value)} required rows={2} /></label><button className="button primary" disabled={busy || !reason.trim()}>明确确认：只重生成此场景</button></form>}
      {plan.action !== "regenerate_scene" && <p>此计划不允许重生成；需按上述停止原因重规划或停止。</p>}
    </>}
  </section>;
}
