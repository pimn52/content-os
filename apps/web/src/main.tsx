import { StrictMode, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type UUID = string;
type RuntimeCapability = { key: string; status: string; provider: string | null; model: string | null; detail: string };
type RuntimeReadiness = { runtime_ready: boolean; capabilities: RuntimeCapability[] };
type BudgetResponse = { policy: { currency: string; max_amount: string | null; max_calls: number | null; allow_unknown_cost: boolean } | null; policy_source: string; snapshot: { currency: string; known_amount: string; unknown_cost_calls: number; calls: number; reserved_calls: number }; over_budget: boolean };
type CostLineItem = { scope: "scene" | "provider" | "render" | "audio"; scene_plan_id: UUID | null; label: string; cost: { category: string; amount: string | null; currency: string | null; provider?: string | null; note?: string | null } };
type CostEstimate = { currency: string; known_amount: string; unknown_cost_count: number; selected_scene_count: number; required_scene_count: number; line_items: CostLineItem[] };
type CostReductionSuggestion = { scene_plan_id: UUID; current: Candidate; suggested: Candidate; changed: boolean; reason: string };
type CostReductionResponse = { project_id: UUID; suggestions: CostReductionSuggestion[]; proposed_selections: Candidate[]; changed_scene_count: number };
type IPProfile = {
  id: UUID; creator_name: string; domains: string[]; audience: string | null; topics: string[];
  knowledge: string[]; opinions: string[]; vocabulary: string[]; style_notes: string[];
  avoided_expressions: string[]; boundaries: string[]; common_hooks: string[]; metadata: Record<string, unknown>;
};
type Project = { id: UUID; ip_profile_id: UUID; title: string; topic: string; format: string; resolution_width: number; resolution_height: number; fps: { numerator: number; denominator: number } };
type Asset = { id: UUID; source_file: string; source_kind: string; duration_ms: number; width: number; height: number; metadata: Record<string, unknown> };
type AssetStage = { status: "not_started" | "pending" | "running" | "completed" | "failed" | "cancelled"; job_id: UUID | null; error_code: string | null };
type AssetReadiness = { asset_id: UUID; stages: Record<string, AssetStage> };
type JobResponse = { id: UUID; type: string; status: AssetStage["status"]; attempt: number; error_code: string | null; error_message: string | null };
type AnalysisResultBundle = { id: UUID; input_hash: string; mode: "assisted_test" | "runtime"; source: string; model: string; tool: string; analyzed_at: string; results: unknown[] };
type AudioAsset = { id: UUID; source_file: string; duration_ms: number; sample_rate: number; channels: number; language: string | null; authorization_reference: string };
type ShootTask = { id: UUID; project_id: UUID; scene_plan_id: UUID; scene_id: string; what_to_shoot: string; framing: string; duration_ms: number; requires_speaking: boolean; status: "confirmed" | "fulfilled" | "dismissed"; asset_id: UUID | null; created_at: string; updated_at: string };
type ScenePlan = { id: UUID; project_id: UUID; scene_id: string; order: number; purpose: string; voice_text: string; duration_target_ms: number; preferred_sources: string[]; fallback_sources: string[]; evidence_refs: string[] };
type Candidate = { scene_plan_id: UUID; source_kind: string; asset_id: UUID | null; clip_id: UUID | null; match_score: number; why: string[]; recommended: boolean; requires_capture: boolean; estimated_cost: { category: string; amount: string | null; currency: string | null } };
type ShootListInstruction = { scene_plan_id: UUID; scene_id: string; what_to_shoot: string; framing: string; duration_ms: number; requires_speaking: boolean; speaking_note: string; fallback: string };
type Route = { scene_plan_id: UUID; candidates: Candidate[]; shoot_list?: ShootListInstruction[] };
type VideoSpec = { project_id: UUID; scenes: Array<{ scene_id: string; start_frame: number; duration_frames: number; visual: { source_kind: string; asset_id?: UUID; clip_id?: UUID; clip_start_ms?: number; clip_end_ms?: number; source_duration_ms?: number }; narration_asset_id?: UUID | null; narration_start_ms?: number | null; narration_end_ms?: number | null; caption: string | null; captions?: Array<{ start_ms: number; end_ms: number; text: string }> }> };
type Draft = { project_id: UUID; version: number; script_revision: number; script: string | null; topic: string | null; scenes: ScenePlan[]; routes: Route[]; confirmed: Candidate[]; video_spec: VideoSpec | null; ip_profile_version: number | null; evidence_refs: string[]; invalidation_reasons: string[] };
type ScenePlanResponse = { project_id: UUID; scenes: ScenePlan[]; script: string; generated_script: boolean; draft: Draft | null };
type Opportunity = { id: UUID; source_type: string; source_ref: string; title: string; observed_at: string; fit_reason: string; angle: string; uncertainty: string | null; evidence_refs: string[]; status: string };
type AccountConnection = { id: UUID; provider: string; account_external_id: string; display_name: string | null; read_only: boolean; connected_at: string; last_synced_at: string | null };
type HistoricalContent = { id: UUID; account_connection_id: UUID; external_id: string; title: string; published_at: string | null; description: string | null; transcript: string | null; metrics: Record<string, number> };
type Clip = { id: UUID; asset_id: UUID; start_ms: number; end_ms: number; visual_description: string | null; transcript: string | null };
type ConsentRecord = { subject_name: string; basis: "self" | "explicit_authorization"; confirmed: boolean; confirmed_at: string; authorization_reference?: string | null };
type VoiceProfile = { id: UUID; name: string; provider: string; provider_profile_id: string; reference_clip_ids: UUID[]; consent: ConsentRecord; language: string | null; created_at: string };
type TalkingProfile = { id: UUID; name: string; provider: string; provider_profile_id: string | null; reference_clip_ids: UUID[]; consent: ConsentRecord; created_at: string };
type Publication = { id: UUID; output_version: string; platform: string; published_at: string; content_url: string | null; metrics: Record<string, unknown>; metric_source: string | null };
type Feedback = { id: UUID; output_version: string; accepted: boolean; changed_fields: string[]; rejection_reason: string | null; notes: string | null; created_at: string };
type NextSuggestion = { reason: string; suggestion: string; evidence_refs: string[] };
type StoredRender = { render_id: UUID; spec_fingerprint: string };

const labels: Record<string, string> = { ready: "可用", provider_not_configured: "Provider 未配置", not_developed: "尚未开发", unavailable: "本机不可用" };
const sourceLabels: Record<string, string> = { manual: "人工", historical_content: "历史内容", account_signal: "账号信号" };

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers);
  if (!(options?.body instanceof FormData) && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const token = window.sessionStorage.getItem("content-os-access-token");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(path, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : `请求失败 (${response.status})`);
  return body as T;
}

function split(value: string): string[] { return value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean); }
function join(value: string[] | null | undefined): string { return (value ?? []).join(", "); }
function prettyStatus(status: string): string { return labels[status] ?? status; }
function costLabel(cost: { amount: string | null; currency: string | null }): string { return cost.amount == null ? "未知" : `${cost.amount} ${cost.currency ?? ""}`.trim(); }
function dateValue(value: FormDataEntryValue | null): string | null { const text = String(value ?? "").trim(); return text ? new Date(text).toISOString() : null; }
function jsonObject(value: string): Record<string, unknown> { const text = value.trim(); if (!text) return {}; const parsed: unknown = JSON.parse(text); if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("指标必须是 JSON 对象"); return parsed as Record<string, unknown>; }
function renderStorageKey(projectId: UUID): string { return `content-os-render:${projectId}`; }
function renderFingerprint(spec: VideoSpec | null): string | null { return spec ? JSON.stringify(spec) : null; }
function isNarratedSpec(spec: VideoSpec | null): spec is VideoSpec {
  return Boolean(spec && spec.scenes.length > 0 && spec.scenes.every((scene) => Boolean(scene.narration_asset_id)));
}
function isSourceLedSpec(spec: VideoSpec | null): spec is VideoSpec {
  return Boolean(spec && spec.scenes.length > 0 && spec.scenes.every((scene) => !scene.narration_asset_id && (scene.captions?.length ?? 0) > 0));
}
function isRenderableSpec(spec: VideoSpec | null): spec is VideoSpec {
  return isNarratedSpec(spec) || isSourceLedSpec(spec);
}
function sourceEndWarning(spec: VideoSpec | null): string | null {
  if (!isSourceLedSpec(spec)) return null;
  const visual = spec.scenes[spec.scenes.length - 1]?.visual;
  if (visual?.clip_end_ms == null || visual.source_duration_ms == null) return null;
  return visual.clip_end_ms >= visual.source_duration_ms - 1_500
    ? "末场已接近原文件末尾，没有后续真实语音可补完；若听感不完整，请绑定新旁白或重新选择结尾镜头。"
    : null;
}
function downloadBlob(filename: string, content: BlobPart, type: string): void {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function escapeXml(value: string): string {
  return value.replace(/[&<>\"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '\"': "&quot;", "'": "&apos;" })[character] ?? character);
}

function App() {
  const [readiness, setReadiness] = useState<RuntimeReadiness | null>(null);
  const [budget, setBudget] = useState<BudgetResponse | null>(null);
  const [profile, setProfile] = useState<IPProfile | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [assetReadiness, setAssetReadiness] = useState<Record<UUID, AssetReadiness>>({});
  const [audioAssets, setAudioAssets] = useState<AudioAsset[]>([]);
  const [shootTasks, setShootTasks] = useState<ShootTask[]>([]);
  const [clips, setClips] = useState<Clip[]>([]);
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [accounts, setAccounts] = useState<AccountConnection[]>([]);
  const [historicalContent, setHistoricalContent] = useState<HistoricalContent[]>([]);
  const [voiceProfiles, setVoiceProfiles] = useState<VoiceProfile[]>([]);
  const [talkingProfiles, setTalkingProfiles] = useState<TalkingProfile[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<UUID | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [routes, setRoutes] = useState<Route[]>([]);
  const [costEstimate, setCostEstimate] = useState<CostEstimate | null>(null);
  const [selections, setSelections] = useState<Record<UUID, Candidate>>({});
  const [narrationAssetIds, setNarrationAssetIds] = useState<Record<UUID, UUID>>({});
  const [videoSpec, setVideoSpec] = useState<VideoSpec | null>(null);
  const [renderUrl, setRenderUrl] = useState<string | null>(null);
  const [renderId, setRenderId] = useState<UUID | null>(null);
  const [publications, setPublications] = useState<Publication[]>([]);
  const [feedback, setFeedback] = useState<Feedback[]>([]);
  const [suggestions, setSuggestions] = useState<NextSuggestion[]>([]);
  const [message, setMessage] = useState<{ text: string; error?: boolean }>({ text: "正在加载本地工作区…" });
  const [busy, setBusy] = useState(false);
  const [tokenInput, setTokenInput] = useState("");
  const [budgetAmount, setBudgetAmount] = useState("");
  const [budgetCalls, setBudgetCalls] = useState("");
  const [allowUnknownCost, setAllowUnknownCost] = useState(false);
  const latestProjectId = useRef<UUID | null>(null);

  const selectedProject = useMemo(() => projects.find((project) => project.id === selectedProjectId) ?? null, [projects, selectedProjectId]);
  const shootList = useMemo(() => routes.flatMap((route) => route.shoot_list ?? []), [routes]);

  function narrationForDraft(value: Draft): Record<UUID, UUID> {
    const sceneIds = new Map(value.scenes.map((scene) => [scene.scene_id, scene.id]));
    return Object.fromEntries((value.video_spec?.scenes ?? []).flatMap((scene) => {
      const scenePlanId = sceneIds.get(scene.scene_id);
      return scenePlanId && scene.narration_asset_id ? [[scenePlanId, scene.narration_asset_id] as [UUID, UUID]] : [];
    }));
  }

  async function loadAll() {
    try {
      const [currentBudget, runtime, currentProfile, currentProjects, currentAssets, currentAudios, currentOpportunities, currentAccounts, currentHistorical, currentVoice, currentTalking] = await Promise.all([
        api<BudgetResponse>("/budget"),
        api<RuntimeReadiness>("/runtime/readiness"), api<IPProfile>("/ip-profile").catch(() => null),
        api<Project[]>("/projects"), api<Asset[]>("/assets"), api<AudioAsset[]>("/audio-assets"), api<Opportunity[]>("/opportunities"),
        api<AccountConnection[]>("/account-connections"), api<HistoricalContent[]>("/historical-content"),
        api<VoiceProfile[]>("/voice-profiles"), api<TalkingProfile[]>("/talking-profiles"),
      ]);
      const currentShootTasks = (await Promise.all(currentProjects.map((project) => api<ShootTask[]>(`/projects/${project.id}/shoot-tasks`)))).flat();
      const currentClips = (await Promise.all(currentAssets.map((asset) => api<Clip[]>(`/assets/${asset.id}/clips`)))).flat();
      const currentReadiness = await Promise.all(currentAssets.map((asset) => api<AssetReadiness>(`/assets/${asset.id}/readiness`)));
      setReadiness(runtime); setBudget(currentBudget); setBudgetAmount(currentBudget.policy?.max_amount ?? ""); setBudgetCalls(currentBudget.policy?.max_calls == null ? "" : String(currentBudget.policy.max_calls)); setAllowUnknownCost(currentBudget.policy?.allow_unknown_cost ?? false);
      setProfile(currentProfile); setProjects(currentProjects); setAssets(currentAssets); setAssetReadiness(Object.fromEntries(currentReadiness.map((value) => [value.asset_id, value]))); setAudioAssets(currentAudios); setShootTasks(currentShootTasks); setClips(currentClips); setOpportunities(currentOpportunities);
      setAccounts(currentAccounts); setHistoricalContent(currentHistorical); setVoiceProfiles(currentVoice); setTalkingProfiles(currentTalking);
      const saved = localStorage.getItem("content-os-active-project");
      const next = saved && currentProjects.some((project) => project.id === saved) ? saved : currentProjects[0]?.id ?? null;
      setSelectedProjectId(next);
      setMessage({ text: "本地工作区已就绪" });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "工作区加载失败", error: true }); }
  }

  async function refreshProject(projectId: UUID) {
    const [value, currentPublications, currentFeedback, currentSuggestions, currentCostEstimate] = await Promise.all([
      api<Draft>(`/projects/${projectId}/draft`),
      api<Publication[]>(`/projects/${projectId}/publications`),
      api<Feedback[]>(`/projects/${projectId}/feedback`),
      api<NextSuggestion[]>(`/projects/${projectId}/next-suggestions`),
      api<CostEstimate>(`/projects/${projectId}/cost-estimate`),
    ]);
    const usableSpec = isRenderableSpec(value.video_spec) ? value.video_spec : null;
    setDraft(value); setRoutes(value.routes); setSelections(Object.fromEntries(value.confirmed.map((candidate) => [candidate.scene_plan_id, candidate]))); setNarrationAssetIds(narrationForDraft(value)); setVideoSpec(usableSpec);
    if (!usableSpec || value.invalidation_reasons.length > 0) invalidateProjectRender(projectId);
    setCostEstimate(currentCostEstimate);
    setPublications(currentPublications); setFeedback(currentFeedback); setSuggestions(currentSuggestions);
    void restoreProjectRender(projectId, usableSpec);
    if (value.video_spec && !usableSpec) setMessage({ text: "该草稿只有没有真实时间轴的历史 source-only 输出；旧渲染保留，但正式流程需要绑定旁白或先导入真实 ASR/SRT/VTT", error: true });
  }

  function invalidateProjectRender(projectId = selectedProject?.id) {
    if (projectId) localStorage.removeItem(renderStorageKey(projectId));
    setRenderUrl(null); setRenderId(null);
  }

  async function restoreProjectRender(projectId: UUID, spec: VideoSpec | null) {
    const rawStoredRender = localStorage.getItem(renderStorageKey(projectId));
    if (!rawStoredRender || !spec) return;
    let storedRender: StoredRender;
    try {
      const parsed: unknown = JSON.parse(rawStoredRender);
      if (!parsed || typeof parsed !== "object" || !("render_id" in parsed) || !("spec_fingerprint" in parsed) || typeof parsed.render_id !== "string" || typeof parsed.spec_fingerprint !== "string") throw new Error("invalid stored render");
      storedRender = parsed as StoredRender;
    } catch {
      localStorage.removeItem(renderStorageKey(projectId));
      return;
    }
    if (storedRender.spec_fingerprint !== renderFingerprint(spec)) {
      localStorage.removeItem(renderStorageKey(projectId));
      return;
    }
    const savedRenderId = storedRender.render_id;
    const previewUrl = `/projects/${projectId}/renders/${savedRenderId}`;
    let restoredUrl = previewUrl;
    let blobUrl: string | null = null;
    try {
      const token = window.sessionStorage.getItem("content-os-access-token");
      if (token) {
        const media = await fetch(previewUrl, { headers: { Authorization: `Bearer ${token}` } });
        if (!media.ok) throw new Error(`预览加载失败 (${media.status})`);
        blobUrl = URL.createObjectURL(await media.blob());
        restoredUrl = blobUrl;
      } else {
        const response = await fetch(previewUrl, { method: "HEAD" });
        if (!response.ok) throw new Error(`本地渲染产物不存在 (${response.status})`);
      }
      if (latestProjectId.current !== projectId) {
        if (blobUrl) URL.revokeObjectURL(blobUrl);
        return;
      }
      setRenderId(savedRenderId); setRenderUrl(restoredUrl);
      setMessage({ text: "已恢复上次本地渲染，可继续预览或下载" });
    } catch {
      if (blobUrl) URL.revokeObjectURL(blobUrl);
      localStorage.removeItem(renderStorageKey(projectId));
    }
  }

  useEffect(() => { void loadAll(); }, []);

  useEffect(() => {
    const active = assets.filter((asset) => ["preprocessed", "transcript", "visual", "index"].some((name) => ["pending", "running"].includes(assetReadiness[asset.id]?.stages[name]?.status ?? "not_started")));
    if (!active.length) return;
    const timer = window.setInterval(() => { void Promise.all(active.map((asset) => refreshAssetReadiness(asset.id))); }, 2_000);
    return () => window.clearInterval(timer);
  }, [assets, assetReadiness]);

  useEffect(() => {
    latestProjectId.current = selectedProjectId;
    setRenderUrl(null); setRenderId(null);
    if (!selectedProjectId) { setDraft(null); setRoutes([]); setSelections({}); setCostEstimate(null); setNarrationAssetIds({}); setVideoSpec(null); setRenderUrl(null); setRenderId(null); setPublications([]); setFeedback([]); setSuggestions([]); return; }
    localStorage.setItem("content-os-active-project", selectedProjectId);
    void refreshProject(selectedProjectId).catch((error) => setMessage({ text: error instanceof Error ? error.message : "项目记录加载失败", error: true }));
  }, [selectedProjectId]);

  useEffect(() => () => {
    if (renderUrl?.startsWith("blob:")) URL.revokeObjectURL(renderUrl);
  }, [renderUrl]);

  function saveAccessToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = tokenInput.trim();
    if (value) window.sessionStorage.setItem("content-os-access-token", value);
    else window.sessionStorage.removeItem("content-os-access-token");
    setTokenInput("");
    setMessage({ text: value ? "私网访问凭据已写入当前标签页，正在重新加载" : "已清除当前标签页的私网访问凭据" });
    void loadAll();
  }

  async function saveProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!profile) return; setBusy(true);
    const form = new FormData(event.currentTarget);
    try {
      const value = await api<IPProfile>("/ip-profile", { method: "PUT", body: JSON.stringify({ creator_name: String(form.get("creator_name") ?? ""), audience: String(form.get("audience") ?? "") || null, domains: split(String(form.get("domains") ?? "")), topics: split(String(form.get("topics") ?? "")), knowledge: split(String(form.get("knowledge") ?? "")), opinions: split(String(form.get("opinions") ?? "")), style_notes: split(String(form.get("style_notes") ?? "")), boundaries: split(String(form.get("boundaries") ?? "")), vocabulary: profile.vocabulary, avoided_expressions: profile.avoided_expressions, common_hooks: profile.common_hooks, metadata: profile.metadata }) });
      setProfile(value);
      if (selectedProjectId) await refreshProject(selectedProjectId);
      setMessage({ text: selectedProjectId ? "IP 资料已保存并生成新版本；当前项目的后续成果已按新 IP 重新检查" : "IP 资料已保存并生成新版本" });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "保存失败", error: true }); } finally { setBusy(false); }
  }

  async function saveBudget(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true);
    try {
      const value = await api<BudgetResponse>("/budget", { method: "PUT", body: JSON.stringify({ currency: "USD", max_amount: budgetAmount.trim() || null, max_calls: budgetCalls.trim() ? Number(budgetCalls) : null, allow_unknown_cost: allowUnknownCost }) });
      setBudget(value); setMessage({ text: "运行预算策略已保存；未知价格只有显式允许后才会执行" });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "预算策略保存失败", error: true }); } finally { setBusy(false); }
  }

  async function importMedia(event: FormEvent<HTMLFormElement>, inbox = false) {
    event.preventDefault(); setBusy(true); const form = new FormData(event.currentTarget); const source_path = String(form.get("source_path") ?? ""); const authorization_reference = String(form.get("authorization_reference") ?? "");
    try {
      const result = await api<{ imported?: number; existing?: number; discovered?: number; analysis_jobs?: JobResponse[]; errors?: unknown[] } | Asset[]>(inbox ? "/inbox/scan" : "/imports", { method: "POST", body: JSON.stringify({ source_path, authorization_reference, recursive: true, ...(inbox ? { enqueue_analysis: form.get("enqueue_analysis") === "on" } : {}) }) });
      await loadAll();
      if (inbox) {
        const scan = result as { discovered: number; imported: number; analysis_jobs?: JobResponse[] };
        const queued = scan.analysis_jobs?.length ?? 0;
        setMessage({ text: `Inbox 扫描完成：发现 ${scan.discovered} 个，新导入 ${scan.imported} 个${queued ? `，已自动排入 ${queued} 个本地媒体分析任务` : ""}` });
      } else setMessage({ text: `导入完成：${(result as Asset[]).length} 个素材` });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "导入失败", error: true }); } finally { setBusy(false); }
  }

  async function importAudio(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true);
    const form = new FormData(event.currentTarget);
    try {
      const result = await api<AudioAsset[]>("/audio-imports", {
        method: "POST",
        body: JSON.stringify({
          source_path: String(form.get("source_path") ?? ""),
          authorization_reference: String(form.get("authorization_reference") ?? ""),
          language: String(form.get("language") ?? "") || null,
          recursive: true,
        }),
      });
      await loadAll(); event.currentTarget.reset();
      setMessage({ text: `旁白导入完成：${result.length} 个文件；每个场景请绑定一条对应录音` });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "旁白导入失败", error: true }); }
    finally { setBusy(false); }
  }

  async function importImage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true);
    const form = new FormData(event.currentTarget);
    try {
      const result = await api<unknown[]>("/image-imports", {
        method: "POST",
        body: JSON.stringify({
          source_path: String(form.get("source_path") ?? ""),
          authorization_reference: String(form.get("authorization_reference") ?? ""),
          source_kind: String(form.get("source_kind") ?? "screenshot"),
          recursive: true,
        }),
      });
      await loadAll(); event.currentTarget.reset();
      setMessage({ text: `静态视觉导入完成：${result.length} 个文件；可作为截图/图表候选供审核` });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "静态视觉导入失败", error: true }); }
    finally { setBusy(false); }
  }

  async function importTranscript(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true);
    const form = new FormData(event.currentTarget);
    const assetId = String(form.get("asset_id") ?? "");
    try {
      const result = await api<{ segment_count: number; updated_clip_count: number }>(`/assets/${assetId}/transcript-imports`, {
        method: "POST",
        body: JSON.stringify({
          source_path: String(form.get("source_path") ?? ""),
          source_reference: String(form.get("source_reference") ?? ""),
        }),
      });
      await loadAll(); event.currentTarget.reset();
      setMessage({ text: `时间轴字幕导入完成：${result.segment_count} 段，更新 ${result.updated_clip_count} 个 Clip；后续按真实句界匹配` });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "字幕导入失败", error: true }); }
    finally { setBusy(false); }
  }

  async function importAudioTranscript(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true);
    const form = new FormData(event.currentTarget);
    const audioId = String(form.get("audio_id") ?? "");
    try {
      const result = await api<{ segment_count: number }>(`/audio-assets/${audioId}/transcript-imports`, {
        method: "POST",
        body: JSON.stringify({
          source_path: String(form.get("source_path") ?? ""),
          source_reference: String(form.get("source_reference") ?? ""),
        }),
      });
      await loadAll(); event.currentTarget.reset();
      setMessage({ text: `旁白时间轴导入完成：${result.segment_count} 段；组装时将按真实音频句段显示字幕` });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "旁白字幕导入失败", error: true }); }
    finally { setBusy(false); }
  }

  async function uploadMedia(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true);
    const form = new FormData(event.currentTarget);
    const file = form.get("file");
    if (!(file instanceof File) || !file.name) { setMessage({ text: "请选择一个视频文件", error: true }); setBusy(false); return; }
    const body = new FormData();
    body.append("file", file);
    body.append("authorization_reference", String(form.get("authorization_reference") ?? ""));
    const shootTaskId = String(form.get("shoot_task_id") ?? "");
    if (shootTaskId) body.append("shoot_task_id", shootTaskId);
    try { await api<Asset>("/uploads", { method: "POST", body }); await loadAll(); event.currentTarget.reset(); setMessage({ text: `浏览器上传完成：${file.name}` }); } catch (error) { setMessage({ text: error instanceof Error ? error.message : "上传失败", error: true }); } finally { setBusy(false); }
  }

  async function uploadAudio(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true);
    const form = new FormData(event.currentTarget);
    const file = form.get("file");
    if (!(file instanceof File) || !file.name) { setMessage({ text: "请选择一个音频文件", error: true }); setBusy(false); return; }
    const body = new FormData();
    body.append("file", file);
    body.append("authorization_reference", String(form.get("authorization_reference") ?? ""));
    body.append("language", String(form.get("language") ?? ""));
    try { await api<AudioAsset>("/audio-uploads", { method: "POST", body }); await loadAll(); event.currentTarget.reset(); setMessage({ text: `本人录音上传完成：${file.name}` }); } catch (error) { setMessage({ text: error instanceof Error ? error.message : "录音上传失败", error: true }); } finally { setBusy(false); }
  }

  async function confirmShootTask(item: ShootListInstruction) {
    if (!selectedProject) return;
    setBusy(true);
    try {
      const task = await api<ShootTask>(`/projects/${selectedProject.id}/shoot-tasks`, { method: "POST", body: JSON.stringify(item) });
      setShootTasks((current) => [...current.filter((value) => value.id !== task.id && value.scene_plan_id !== task.scene_plan_id), task]);
      setMessage({ text: `已确认场景 ${task.scene_id} 补拍；上传视频时可绑定该任务` });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "补拍任务确认失败", error: true }); } finally { setBusy(false); }
  }

  async function dismissShootTask(item: ShootListInstruction) {
    const task = shootTasks.find((value) => value.scene_plan_id === item.scene_plan_id);
    if (!task) return;
    setBusy(true);
    try { const updated = await api<ShootTask>(`/shoot-tasks/${task.id}`, { method: "PATCH", body: JSON.stringify({ status: "dismissed" }) }); setShootTasks((current) => current.map((value) => value.id === updated.id ? updated : value)); setMessage({ text: `已拒绝场景 ${item.scene_id} 补拍，可使用本地替代或保留缺口` }); } catch (error) { setMessage({ text: error instanceof Error ? error.message : "补拍任务更新失败", error: true }); } finally { setBusy(false); }
  }

  async function saveAssetClassification(assetId: UUID, event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true);
    const form = new FormData(event.currentTarget);
    try {
      const value = await api<Asset>(`/assets/${assetId}/usage`, {
        method: "PATCH",
        body: JSON.stringify({ usage: String(form.get("usage")), identity: String(form.get("identity")) }),
      });
      setAssets((current) => current.map((asset) => asset.id === value.id ? value : asset));
      setMessage({ text: "素材用途与身份已保存；只有明确标记为生产且身份符合的素材才会进入对外输出候选" });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "素材分组保存失败", error: true }); }
    finally { setBusy(false); }
  }

  async function refreshAssetReadiness(assetId: UUID) {
    const value = await api<AssetReadiness>(`/assets/${assetId}/readiness`);
    setAssetReadiness((current) => ({ ...current, [assetId]: value }));
    return value;
  }

  async function enqueueTranscription(asset: Asset) {
    const current = assetReadiness[asset.id]?.stages.transcript;
    const retry = current?.status === "failed" || current?.status === "cancelled" || current?.status === "completed";
    const idempotencyKey = `web-transcribe:${asset.id}:${retry ? Date.now() : "current"}`;
    setBusy(true);
    try {
      const job = await api<JobResponse>(`/assets/${asset.id}/jobs/transcribe_audio`, { method: "POST", body: JSON.stringify({ idempotency_key: idempotencyKey }) });
      await refreshAssetReadiness(asset.id);
      setMessage({ text: job.status === "completed" ? "该素材已有完成的 ASR 转写" : "ASR 已入队；请保持本地 Worker 运行，完成后会更新阶段状态" });
    } catch (error) {
      setMessage({ text: error instanceof Error ? error.message : "ASR 任务提交失败", error: true });
    } finally { setBusy(false); }
  }

  function transcriptStage(assetId: UUID): AssetStage {
    return assetReadiness[assetId]?.stages.transcript ?? { status: "not_started", job_id: null, error_code: null };
  }

  function transcriptStageLabel(stage: AssetStage): string {
    const labelsByStatus: Record<AssetStage["status"], string> = { not_started: "未开始", pending: "排队中", running: "转写中", completed: "已完成", failed: "失败", cancelled: "已取消" };
    return `ASR：${labelsByStatus[stage.status]}${stage.error_code ? `（${stage.error_code}）` : ""}`;
  }

  function analysisStage(assetId: UUID): AssetStage {
    return assetReadiness[assetId]?.stages.preprocessed ?? { status: "not_started", job_id: null, error_code: null };
  }

  function analysisStageLabel(stage: AssetStage): string {
    const labelsByStatus: Record<AssetStage["status"], string> = { not_started: "未开始", pending: "排队中", running: "处理中", completed: "已完成", failed: "失败", cancelled: "已取消" };
    return `媒体分析：${labelsByStatus[stage.status]}${stage.error_code ? `（${stage.error_code}）` : ""}`;
  }

  function analysisProvenance(asset: Asset): string | null {
    const value = asset.metadata.r1_analysis;
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const metadata = value as Record<string, unknown>;
    const source = typeof metadata.source === "string" ? metadata.source : null;
    const model = typeof metadata.model === "string" ? metadata.model : null;
    return source && model ? `分析：${source} / ${model}` : "分析结果已绑定";
  }

  async function importAnalysisBundle(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const file = (form.get("bundle") as File | null);
    if (!file || !file.size) { setMessage({ text: "请选择 AnalysisResultBundle JSON 文件", error: true }); return; }
    setBusy(true);
    try {
      const parsed: unknown = JSON.parse(await file.text());
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("分析包必须是 JSON 对象");
      const bundle = await api<AnalysisResultBundle>("/analysis-results", { method: "POST", body: JSON.stringify(parsed) });
      await loadAll();
      event.currentTarget.reset();
      setMessage({ text: `真实 ${bundle.mode} 分析包已导入：${bundle.source} / ${bundle.model}，${bundle.results.length} 个结果已绑定本地素材` });
    } catch (error) {
      setMessage({ text: error instanceof Error ? error.message : "分析包导入失败", error: true });
    } finally { setBusy(false); }
  }

  async function createOpportunity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); const form = new FormData(event.currentTarget);
    try { await api<Opportunity>("/opportunities", { method: "POST", body: JSON.stringify({ source_type: String(form.get("source_type")), source_ref: String(form.get("source_ref")), title: String(form.get("title")), observed_at: new Date(String(form.get("observed_at"))).toISOString(), fit_reason: String(form.get("fit_reason")), angle: String(form.get("angle")), uncertainty: String(form.get("uncertainty") ?? "") || null, evidence_refs: split(String(form.get("evidence_refs") ?? "")) }) }); await loadAll(); event.currentTarget.reset(); setMessage({ text: "有来源选题已保存" }); } catch (error) { setMessage({ text: error instanceof Error ? error.message : "选题保存失败", error: true }); } finally { setBusy(false); }
  }

  async function createAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); const form = new FormData(event.currentTarget);
    try { await api<AccountConnection>("/account-connections", { method: "POST", body: JSON.stringify({ provider: String(form.get("provider")), account_external_id: String(form.get("account_external_id")), display_name: String(form.get("display_name") ?? "") || null, connected_at: dateValue(form.get("connected_at")) }) }); await loadAll(); event.currentTarget.reset(); setMessage({ text: "只读账号记录已保存" }); } catch (error) { setMessage({ text: error instanceof Error ? error.message : "账号保存失败", error: true }); } finally { setBusy(false); }
  }

  async function createHistorical(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); const form = new FormData(event.currentTarget);
    try { await api<HistoricalContent>("/historical-content", { method: "POST", body: JSON.stringify({ account_connection_id: String(form.get("account_connection_id")), external_id: String(form.get("external_id")), title: String(form.get("title")), published_at: dateValue(form.get("published_at")), description: String(form.get("description") ?? "") || null, transcript: String(form.get("transcript") ?? "") || null, metrics: jsonObject(String(form.get("metrics") ?? "")) }) }); await loadAll(); event.currentTarget.reset(); setMessage({ text: "历史内容记录已保存" }); } catch (error) { setMessage({ text: error instanceof Error ? error.message : "历史内容保存失败", error: true }); } finally { setBusy(false); }
  }

  function consentFrom(form: FormData): ConsentRecord { const basis = String(form.get("basis")) as ConsentRecord["basis"]; return { subject_name: String(form.get("subject_name")), basis, confirmed: true, confirmed_at: new Date().toISOString(), authorization_reference: String(form.get("authorization_reference") ?? "") || null }; }

  async function createVoice(event: FormEvent<HTMLFormElement>, talking = false) {
    event.preventDefault(); setBusy(true); const form = new FormData(event.currentTarget); const body = { name: String(form.get("name")), provider: String(form.get("provider")), provider_profile_id: String(form.get("provider_profile_id") ?? "") || null, reference_clip_ids: split(String(form.get("reference_clip_ids"))), consent: consentFrom(form), created_at: new Date().toISOString() };
    try { await api<VoiceProfile | TalkingProfile>(talking ? "/talking-profiles" : "/voice-profiles", { method: "POST", body: JSON.stringify(body) }); await loadAll(); event.currentTarget.reset(); setMessage({ text: talking ? "Talking Profile 注册记录已保存" : "Voice Profile 注册记录已保存" }); } catch (error) { setMessage({ text: error instanceof Error ? error.message : "Profile 保存失败", error: true }); } finally { setBusy(false); }
  }

  async function recordPublication(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!selectedProject) return; setBusy(true); const form = new FormData(event.currentTarget);
    try { await api<Publication>(`/projects/${selectedProject.id}/publications`, { method: "POST", body: JSON.stringify({ output_version: String(form.get("output_version")), platform: String(form.get("platform")), published_at: dateValue(form.get("published_at")), content_url: String(form.get("content_url") ?? "") || null, content_external_id: String(form.get("content_external_id") ?? "") || null, metrics: jsonObject(String(form.get("metrics") ?? "")), metric_source: String(form.get("metric_source") ?? "") || null, observation_window_days: String(form.get("observation_window_days") ?? "") ? Number(form.get("observation_window_days")) : null }) }); await refreshProject(selectedProject.id); event.currentTarget.reset(); setMessage({ text: "人工发布记录已保存" }); } catch (error) { setMessage({ text: error instanceof Error ? error.message : "发布记录保存失败", error: true }); } finally { setBusy(false); }
  }

  async function recordFeedback(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!selectedProject) return; setBusy(true); const form = new FormData(event.currentTarget);
    try { await api<Feedback>(`/projects/${selectedProject.id}/feedback`, { method: "POST", body: JSON.stringify({ output_version: String(form.get("output_version")), accepted: String(form.get("accepted")) === "true", changed_fields: split(String(form.get("changed_fields") ?? "")), rejection_reason: String(form.get("rejection_reason") ?? "") || null, notes: String(form.get("notes") ?? "") || null }) }); await refreshProject(selectedProject.id); event.currentTarget.reset(); setMessage({ text: "反馈已保存，下一轮建议已刷新" }); } catch (error) { setMessage({ text: error instanceof Error ? error.message : "反馈保存失败", error: true }); } finally { setBusy(false); }
  }

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); const form = new FormData(event.currentTarget);
    try { const project = await api<Project>("/projects", { method: "POST", body: JSON.stringify({ title: String(form.get("title")), topic: String(form.get("topic")), creator_name: profile?.creator_name ?? "本地创作者" }) }); await loadAll(); setSelectedProjectId(project.id); setMessage({ text: "项目已创建，可以开始生成草稿" }); } catch (error) { setMessage({ text: error instanceof Error ? error.message : "项目创建失败", error: true }); } finally { setBusy(false); }
  }

  async function planProject() {
    if (!selectedProject) return; setBusy(true);
    try {
      const script = draft?.script ?? null;
      const topic = draft?.topic ?? selectedProject.topic;
      const result = await api<ScenePlanResponse>(`/projects/${selectedProject.id}/scene-plan`, { method: "POST", body: JSON.stringify({ script: script || undefined, topic, persist: true }) });
      if (!result.draft) throw new Error("文案与 ScenePlan 没有保存为项目草稿");
      setRoutes([]); setSelections({}); setCostEstimate(null); setNarrationAssetIds({}); setVideoSpec(null); invalidateProjectRender(selectedProject.id);
      setDraft(result.draft); setRoutes(result.draft.routes); setSelections({}); setNarrationAssetIds({}); setVideoSpec(null);
      setMessage({ text: `已生成并保存新文案与 ${result.scenes.length} 个场景；下一步查找素材` });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "ScenePlan 失败", error: true }); } finally { setBusy(false); }
  }

  async function routeProject() {
    if (!selectedProject || !draft?.scenes.length) return; setBusy(true);
    try {
      const scenes = draft.scenes;
      const result = await api<Route[]>(`/projects/${selectedProject.id}/asset-routes`, { method: "POST", body: JSON.stringify({ scenes, max_candidates: 3 }) });
      setRoutes(result); setSelections({}); setCostEstimate(null); setVideoSpec(null); invalidateProjectRender(selectedProject.id);
      await persistDraft(scenes, [], null, result, draft.script, draft.topic);
      setCostEstimate(await api<CostEstimate>(`/projects/${selectedProject.id}/cost-estimate`));
      setMessage({ text: "候选已生成并保存，请按场景选择或保留推荐" });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "候选检索失败", error: true }); } finally { setBusy(false); }
  }

  async function reduceCostProject() {
    if (!selectedProject || !draft?.scenes.length || !routes.length) return;
    setBusy(true);
    try {
      const result = await api<CostReductionResponse>(`/projects/${selectedProject.id}/cost-reduction`);
      if (result.changed_scene_count === 0) {
        setMessage({ text: "没有满足匹配度阈值且价格已知更低的候选；未知价格未被视为更便宜" });
        return;
      }
      const suggested = new Map(result.proposed_selections.map((candidate) => [candidate.scene_plan_id, candidate]));
      const nextSelections = draft.scenes.map((scene) => suggested.get(scene.id) ?? selections[scene.id]).filter((candidate): candidate is Candidate => Boolean(candidate));
      await persistDraft(draft.scenes, nextSelections, null, routes, draft.script, draft.topic);
      setCostEstimate(await api<CostEstimate>(`/projects/${selectedProject.id}/cost-estimate`));
      setMessage({ text: `已应用 ${result.changed_scene_count} 个场景的低成本候选；请继续审核匹配度` });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "低成本方案生成失败", error: true }); }
    finally { setBusy(false); }
  }

  async function assembleProject() {
    if (!selectedProject || !draft?.scenes.length) return;
    const values = Object.values(selections);
    if (values.length !== draft.scenes.length) { setMessage({ text: "请为每个场景选择一个候选", error: true }); return; }
    const missingNarration = draft.scenes.find((scene) => !narrationAssetIds[scene.id]);
    if (missingNarration) {
      setMessage({ text: `场景“${missingNarration.purpose}”有新稿件但没有旁白；先导入并绑定已授权音频，避免新字幕覆盖旧原声`, error: true });
      return;
    }
    setBusy(true);
    try {
      const spec = await api<VideoSpec>(`/projects/${selectedProject.id}/video-spec`, { method: "POST", body: JSON.stringify({ scenes: draft.scenes, selections: values, explicit_scene_ids: values.filter((candidate) => !candidate.recommended).map((candidate) => candidate.scene_plan_id), narration_asset_ids: narrationAssetIds, narration_required: true }) });
      setVideoSpec(spec); invalidateProjectRender(selectedProject.id); setMessage({ text: "VideoSpec 已组装，可以预览或导出" }); await persistDraft(draft.scenes, values, spec, routes, draft.script, draft.topic);
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "VideoSpec 组装失败", error: true }); } finally { setBusy(false); }
  }

  async function assembleSourceLedProject() {
    if (!selectedProject || !draft?.scenes.length) return;
    const values = Object.values(selections);
    if (values.length !== draft.scenes.length) { setMessage({ text: "请为每个场景选择一个候选", error: true }); return; }
    setBusy(true);
    try {
      const spec = await api<VideoSpec>(`/projects/${selectedProject.id}/video-spec`, { method: "POST", body: JSON.stringify({ scenes: draft.scenes, selections: values, explicit_scene_ids: values.filter((candidate) => !candidate.recommended).map((candidate) => candidate.scene_plan_id), narration_asset_ids: {}, narration_required: false }) });
      if (!isSourceLedSpec(spec)) {
        throw new Error("source-led 组装需要每个场景都有真实 ASR/SRT/VTT 时间轴；当前未猜测语义剪辑");
      }
      setVideoSpec(spec); invalidateProjectRender(selectedProject.id); setMessage({ text: "Source-led VideoSpec 已组装：保留原声，按真实时间轴剪辑并显示字幕" }); await persistDraft(draft.scenes, values, spec, routes, draft.script, draft.topic);
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "Source-led 组装失败", error: true }); } finally { setBusy(false); }
  }

  async function persistDraft(scenes = draft?.scenes ?? [], confirmed = Object.values(selections), spec = videoSpec, nextRoutes = routes, script = draft?.script ?? null, topic = draft?.topic ?? selectedProject?.topic ?? null) {
    if (!selectedProject) return;
    const value = await api<Draft>(`/projects/${selectedProject.id}/draft`, { method: "PUT", body: JSON.stringify({ script, topic, scenes, routes: nextRoutes, confirmed, video_spec: spec }) });
    setDraft(value); setRoutes(value.routes); setSelections(Object.fromEntries(value.confirmed.map((candidate) => [candidate.scene_plan_id, candidate]))); setNarrationAssetIds(narrationForDraft(value)); setVideoSpec(value.video_spec);
  }

  async function saveDraftText() {
    if (!selectedProject || !draft) return; setBusy(true);
    try { invalidateProjectRender(selectedProject.id); setVideoSpec(null); await persistDraft(draft.scenes, [], null, [], draft.script ?? null, draft.topic ?? selectedProject.topic); setMessage({ text: "脚本/场景文案已保存，旧候选与渲染已失效，请重新生成 ScenePlan 并选片" }); } catch (error) { setMessage({ text: error instanceof Error ? error.message : "稿件保存失败", error: true }); } finally { setBusy(false); }
  }

  function invalidateDraftPlan() {
    setRoutes([]); setSelections({}); setCostEstimate(null); setNarrationAssetIds({}); setVideoSpec(null); invalidateProjectRender();
  }

  function updateDraftScript(script: string) {
    setDraft((current) => current ? { ...current, script: script || null } : current);
    invalidateDraftPlan();
  }

  function updateSceneVoiceText(sceneId: UUID, voiceText: string) {
    setDraft((current) => current ? { ...current, scenes: current.scenes.map((scene) => scene.id === sceneId ? { ...scene, voice_text: voiceText } : scene) } : current);
    invalidateDraftPlan();
  }

  async function renderProject() {
    if (!selectedProject || !videoSpec) return;
    if (!isRenderableSpec(videoSpec)) { setMessage({ text: "当前 VideoSpec 没有逐场景新旁白或真实时间轴字幕，不能渲染；请绑定授权音频或先完成 ASR/SRT/VTT", error: true }); return; }
    setBusy(true);
    try {
      const result = await api<{ render_id: UUID; preview_url: string; download_url?: string }>(`/projects/${selectedProject.id}/render`, { method: "POST", body: JSON.stringify({ video_spec: videoSpec }) });
      setRenderId(result.render_id);
      const token = window.sessionStorage.getItem("content-os-access-token");
      if (token) {
        const media = await fetch(result.preview_url, { headers: { Authorization: `Bearer ${token}` } });
        if (!media.ok) throw new Error(`预览加载失败 (${media.status})`);
        setRenderUrl(URL.createObjectURL(await media.blob()));
      } else setRenderUrl(result.download_url ?? result.preview_url);
      localStorage.setItem(renderStorageKey(selectedProject.id), JSON.stringify({ render_id: result.render_id, spec_fingerprint: renderFingerprint(videoSpec)! } satisfies StoredRender));
      setMessage({ text: "本地渲染完成" });
    } catch (error) { setMessage({ text: error instanceof Error ? error.message : "渲染失败", error: true }); } finally { setBusy(false); }
  }

  function exportDraftText() {
    if (!selectedProject || !draft) return;
    const scenes = draft.scenes.map((scene, index) => `${index + 1}. ${scene.purpose}\n${scene.voice_text}`).join("\n\n");
    const text = [`${selectedProject.title}`, `主题：${selectedProject.topic}`, "", "脚本 / 旁白稿", draft.script ?? "（未填写独立脚本；以下为 ScenePlan 文案）", "", "ScenePlan", scenes].join("\n");
    downloadBlob(`${selectedProject.title || "content-os"}-文案.txt`, text, "text/plain;charset=utf-8");
    setMessage({ text: "文案已导出为 UTF-8 文本，可继续人工修改" });
  }

  function exportDraftCover() {
    if (!selectedProject || !draft) return;
    const title = escapeXml(selectedProject.title || "Content OS");
    const topic = escapeXml(selectedProject.topic || "本地创作者内容");
    const hook = escapeXml(draft.scenes[0]?.voice_text || "可审核的短视频草稿");
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1920" viewBox="0 0 1080 1920"><defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#10243d"/><stop offset="1" stop-color="#071019"/></linearGradient></defs><rect width="1080" height="1920" fill="url(#bg)"/><rect x="72" y="88" width="936" height="8" rx="4" fill="#65d3b4"/><text x="72" y="210" fill="#65d3b4" font-family="Arial, Microsoft YaHei, sans-serif" font-size="34" font-weight="700">CONTENT OS · DRAFT COVER</text><text x="72" y="470" fill="#f5f8fc" font-family="Arial, Microsoft YaHei, sans-serif" font-size="78" font-weight="700">${title}</text><text x="72" y="590" fill="#b8c8dc" font-family="Arial, Microsoft YaHei, sans-serif" font-size="40">${topic}</text><foreignObject x="72" y="790" width="936" height="520"><div xmlns="http://www.w3.org/1999/xhtml" style="color:#edf4ff;font:48px/1.45 Arial,'Microsoft YaHei',sans-serif;">${hook}</div></foreignObject><text x="72" y="1780" fill="#8e9db4" font-family="Arial, Microsoft YaHei, sans-serif" font-size="28">基础可编辑模板 · 需人工审核后发布</text></svg>`;
    downloadBlob(`${selectedProject.title || "content-os"}-封面.svg`, svg, "image/svg+xml;charset=utf-8");
    setMessage({ text: "基础封面模板已导出为 SVG；可在发布前继续编辑" });
  }

  async function recordRenderedUsage() {
    if (!selectedProject || !videoSpec || !renderId) return;
    const seen = new Set<string>();
    const events = videoSpec.scenes.flatMap((scene) => {
      const visual = scene.visual;
      if (!visual.asset_id) return [];
      const mediaKind = ["screenshot", "chart"].includes(visual.source_kind) ? "image" : "video";
      const key = `${mediaKind}:${visual.asset_id}:${visual.clip_id ?? "-"}`;
      if (seen.has(key)) return [];
      seen.add(key);
      return [{ media_id: visual.asset_id, media_kind: mediaKind, ...(mediaKind === "video" && visual.clip_id ? { clip_id: visual.clip_id } : {}) }];
    });
    const audioEvents = videoSpec.scenes.flatMap((scene) => scene.narration_asset_id ? [{ media_id: scene.narration_asset_id, media_kind: "audio" as const }] : []);
    for (const event of audioEvents) {
      const key = `audio:${event.media_id}`;
      if (!seen.has(key)) { seen.add(key); events.push(event); }
    }
    if (!events.length) { setMessage({ text: "该输出没有可记录的本地媒体使用；排版缺口保持不计入素材使用" }); return; }
    setBusy(true);
    try { await api(`/projects/${selectedProject.id}/usage-events`, { method: "POST", body: JSON.stringify({ output_version: `render-${renderId}`, events }) }); setMessage({ text: `已确认导出并记录 ${events.length} 项素材使用（重复点击幂等）` }); } catch (error) { setMessage({ text: error instanceof Error ? error.message : "素材使用记录失败", error: true }); } finally { setBusy(false); }
  }

  async function selectCandidate(candidate: Candidate) {
    const nextSelections = { ...selections, [candidate.scene_plan_id]: candidate };
    setSelections(nextSelections); setCostEstimate(null); setVideoSpec(null); invalidateProjectRender();
    if (!selectedProject || !draft) return;
    try { await persistDraft(draft.scenes, Object.values(nextSelections), null, routes, draft.script, draft.topic); setCostEstimate(await api<CostEstimate>(`/projects/${selectedProject.id}/cost-estimate`)); } catch (error) { setMessage({ text: error instanceof Error ? error.message : "候选保存失败", error: true }); }
  }

  return <div className="app-shell">
    <header className="topbar"><div><span className="eyebrow">LOCAL-FIRST / BYOK</span><h1>Content OS</h1><p>理解创作者、复用真实素材、形成可审核的短视频草稿。</p></div><div className="topbar-actions"><form className="access-form" onSubmit={saveAccessToken}><input type="password" value={tokenInput} onChange={(event) => setTokenInput(event.target.value)} placeholder="私网 token（可选）" aria-label="私网 token" /><button className="button" type="submit">连接</button></form><div className="status-pill"><span className={readiness?.runtime_ready ? "dot ready" : "dot"}></span>{readiness?.runtime_ready ? "Runtime 已就绪" : "Runtime 仍需配置"}</div></div></header>
    <main>
      <section className="notice"><strong>{message.error ? "需要处理" : "工作区状态"}</strong><span className={message.error ? "error" : ""}>{message.text}</span></section>
      <div className="columns">
        <section className="card"><div className="section-heading"><div><span className="kicker">01 / IDENTITY</span><h2>我的 IP</h2></div><span className="badge">版本化</span></div>
          {profile ? <form onSubmit={saveProfile}><Field label="创作者" name="creator_name" defaultValue={profile.creator_name} /><Field label="受众" name="audience" defaultValue={profile.audience ?? ""} /><Field label="专业领域" name="domains" defaultValue={join(profile.domains)} /><Field label="知识" name="knowledge" as="textarea" defaultValue={join(profile.knowledge)} /><Field label="观点" name="opinions" as="textarea" defaultValue={join(profile.opinions)} /><Field label="表达风格" name="style_notes" as="textarea" defaultValue={join(profile.style_notes)} /><Field label="边界" name="boundaries" as="textarea" defaultValue={join(profile.boundaries)} /><button disabled={busy} className="button primary">保存资料版本</button></form> : <p className="muted">尚未初始化 IP 资料。</p>}
        </section>
        <section className="card"><div className="section-heading"><div><span className="kicker">02 / CAPTURE</span><h2>素材进入</h2></div><span className="badge">本地</span></div>
          <form onSubmit={(event) => void importMedia(event)}><h3>导入视频/目录</h3><Field label="本地路径" name="source_path" placeholder="C:\\Videos" /><Field label="授权记录引用" name="authorization_reference" placeholder="creator-owned-2026" /><button disabled={busy} className="button primary">导入素材</button></form>
          <form onSubmit={(event) => void uploadMedia(event)} className="subform"><h3>浏览器 / 手机上传</h3><label>视频文件<input name="file" type="file" accept="video/*" required /></label><Field label="授权记录引用" name="authorization_reference" placeholder="creator-owned-2026" /><label>绑定补拍任务（可选）<select name="shoot_task_id" defaultValue=""><option value="">不绑定任务</option>{shootTasks.filter((task) => task.project_id === selectedProjectId && task.status === "confirmed").map((task) => <option key={task.id} value={task.id}>场景 {task.scene_id} · 至少 {Math.ceil(task.duration_ms / 1000)} 秒</option>)}</select></label><button disabled={busy} className="button primary">上传并导入</button><small className="muted">文件会进入本机数据根并自动 hash 去重，不上传到第三方；可绑定已确认的补拍任务。</small></form>
           <form onSubmit={(event) => void importAudio(event)} className="subform"><h3>导入旁白 / 本人录音</h3><Field label="本地音频路径" name="source_path" placeholder="C:\\Audio\\scene-01.wav" /><Field label="授权记录引用" name="authorization_reference" placeholder="creator-voice-2026" /><Field label="语言（可选）" name="language" placeholder="zh-CN" /><button disabled={busy} className="button primary">导入旁白</button><small className="muted">系统按 ffprobe 的真实时长组装；新稿件不能复用旧原声，每个场景绑定对应的已授权录音。</small></form>
           <form onSubmit={(event) => void uploadAudio(event)} className="subform"><h3>浏览器 / 手机上传本人录音</h3><label>音频文件<input name="file" type="file" accept="audio/*" required /></label><Field label="授权记录引用" name="authorization_reference" placeholder="creator-voice-2026" /><Field label="语言（可选）" name="language" placeholder="zh-CN" /><button disabled={busy} className="button primary">上传并导入录音</button><small className="muted">文件留在本机数据根并按 hash 去重；仅用于你明确授权的新旁白，不复用原视频声轨。</small></form>
           <form onSubmit={(event) => void importImage(event)} className="subform"><h3>导入图片 / 截图 / 图表</h3><Field label="本地图片路径" name="source_path" placeholder="C:\\Images\\chart.png" /><label>视觉用途<select name="source_kind" defaultValue="screenshot"><option value="screenshot">截图 / 静态画面</option><option value="chart">图表</option></select></label><Field label="授权记录引用" name="authorization_reference" placeholder="creator-owned-2026" /><button disabled={busy} className="button primary">导入静态视觉</button><small className="muted">支持 PNG/JPEG/WebP；图片只作为显式 B-roll/fallback，仍需审核用途与授权，不生成外部素材。</small></form>
           <form onSubmit={(event) => void importAnalysisBundle(event)} className="subform"><h3>导入真实 assisted-test 分析包</h3><label>AnalysisResultBundle JSON<input name="bundle" type="file" accept=".json,application/json" required /></label><button disabled={busy} className="button">导入分析包</button><small className="muted">选择模型实际分析产生的 `analysis-bundle.json`；系统按 input hash 和 asset ID 校验并回放到本地 Clip，不把夹具或文件名当作语义结果。</small></form>
           <form onSubmit={(event) => void importTranscript(event)} className="subform"><h3>导入真实时间轴字幕</h3><label>目标素材<select name="asset_id" required disabled={!assets.length}><option value="">选择已有素材</option>{assets.map((asset) => <option key={asset.id} value={asset.id}>{asset.source_file.split(/[\\/]/).pop()} · {Math.round(asset.duration_ms / 1000)}s</option>)}</select></label><Field label="本地 SRT / VTT 路径" name="source_path" placeholder="C:\\Captions\\take-01.srt" /><Field label="字幕来源引用" name="source_reference" placeholder="creator-captions-2026-09-09" /><button disabled={busy || !assets.length} className="button">导入时间轴</button><small className="muted">只接受你提供的 UTF-8 SRT/VTT；保留原始时间段与来源引用，不把固定字幕冒充 ASR。</small></form>
          <form onSubmit={(event) => void importAudioTranscript(event)} className="subform"><h3>为旁白绑定真实字幕</h3><label>目标旁白<select name="audio_id" required disabled={!audioAssets.length}><option value="">选择已导入录音</option>{audioAssets.map((audio) => <option key={audio.id} value={audio.id}>{audio.source_file.split(/[\\/]/).pop()} · {Math.round(audio.duration_ms / 1000)}s</option>)}</select></label><Field label="本地 SRT / VTT 路径" name="source_path" placeholder="C:\\Captions\\narration.srt" /><Field label="字幕来源引用" name="source_reference" placeholder="creator-narration-captions-2026-09-09" /><button disabled={busy || !audioAssets.length} className="button">绑定旁白字幕</button><small className="muted">仅绑定你提供的真实时间轴；没有时间轴时不会伪造口型或语义同步。</small></form>
           <form onSubmit={(event) => void importMedia(event, true)} className="subform"><h3>Inbox 按需扫描</h3><Field label="Inbox 路径" name="source_path" placeholder="C:\\Videos\\Inbox" /><Field label="授权记录引用" name="authorization_reference" placeholder="creator-owned-2026" /><label className="checkbox-line"><input name="enqueue_analysis" type="checkbox" defaultChecked /> 新导入素材自动排入本地媒体分析</label><small className="muted">只自动执行本地预处理/连续 Clip 提取；ASR、视觉分析和索引仍按独立阶段显示，不会因入队被标为完成。</small><button disabled={busy} className="button">扫描 Inbox</button></form>
          <div className="asset-list">{assets.slice(0, 8).map((asset) => { const analysis = analysisStage(asset.id); const stage = transcriptStage(asset.id); const active = stage.status === "pending" || stage.status === "running"; const provenance = analysisProvenance(asset); return <div className="asset-row" key={asset.id}><div><strong>{asset.source_file.split(/[\\/]/).pop()}</strong><small>{asset.width}×{asset.height} · {Math.round(asset.duration_ms / 1000)}s · {asset.source_kind}</small><small className={analysis.status === "failed" ? "error" : analysis.status === "completed" ? "ok" : "muted"}>{analysisStageLabel(analysis)}</small><small className={stage.status === "failed" ? "error" : stage.status === "completed" ? "ok" : "muted"}>{transcriptStageLabel(stage)}</small>{provenance && <small className="muted">{provenance}</small>}</div><form className="asset-classification" onSubmit={(event) => void saveAssetClassification(asset.id, event)}><label>用途<select name="usage" defaultValue={String(asset.metadata.r1_usage ?? "unknown")}><option value="unknown">暂不使用</option><option value="reference">仅参考</option><option value="production">可用于生产</option></select></label><label>身份<select name="identity" defaultValue={String(asset.metadata.r1_identity ?? "unknown")}><option value="unknown">未知</option><option value="creator">本人形象</option><option value="other">其他人物</option><option value="none">无人/辅助画面</option></select></label><button disabled={busy} className="button">保存分组</button><button type="button" disabled={busy || active} className="button" onClick={() => void enqueueTranscription(asset)}>{active ? "ASR 处理中" : stage.status === "completed" ? "重新转写" : stage.status === "failed" || stage.status === "cancelled" ? "重试 ASR" : "提交 ASR"}</button></form></div>; })}{assets.length === 0 && <p className="muted">还没有本地素材。</p>}</div>
          <div className="asset-list"><h3>已导入配音 / 本人录音</h3>{audioAssets.map((audio) => <div className="asset-row" key={audio.id}><div><strong>{audio.source_file.split(/[\\/]/).pop()}</strong><small>{Math.round(audio.duration_ms / 1000)}s · {audio.sample_rate}Hz · {audio.channels}ch · {audio.language ?? "语言未标注"}</small></div><span className="badge">已授权引用</span></div>)}{audioAssets.length === 0 && <p className="muted">尚无本地旁白；可在旧版 workspace 导入后回到这里绑定。</p>}</div>
        </section>
      </div>
      <section className="card"><div className="section-heading"><div><span className="kicker">03 / SIGNAL</span><h2>有来源选题</h2></div><span className="badge">不抓全网</span></div><div className="columns compact"><form onSubmit={createOpportunity}><Field label="来源引用" name="source_ref" placeholder="note:2026-09-09" /><Field label="标题" name="title" placeholder="要表达什么" /><label>来源类型<select name="source_type" defaultValue="manual"><option value="manual">人工</option><option value="historical_content">历史内容</option><option value="account_signal">账号信号</option></select></label><label>观察时间<input name="observed_at" type="datetime-local" defaultValue={new Date().toISOString().slice(0, 16)} /></label><Field label="为何适合当前 IP" name="fit_reason" as="textarea" /><Field label="内容角度" name="angle" as="textarea" /><Field label="不确定性" name="uncertainty" as="textarea" /><Field label="证据引用" name="evidence_refs" placeholder="source:..." /><button disabled={busy} className="button primary">保存选题</button></form><div className="opportunity-list">{opportunities.map((value) => <article key={value.id}><div className="row-between"><strong>{value.title}</strong><span className="badge">{sourceLabels[value.source_type] ?? value.source_type}</span></div><p>{value.angle}</p><small>{value.source_ref} · {value.status} · {value.evidence_refs.join(", ") || "无额外证据引用"}</small></article>)}{opportunities.length === 0 && <p className="muted">还没有选题来源。</p>}</div></div></section>
      <section className="card"><div className="section-heading"><div><span className="kicker">04 / DRAFT</span><h2>项目与草稿</h2></div><span className="badge">可恢复</span></div><div className="project-toolbar"><form onSubmit={createProject} className="project-form"><Field label="项目标题" name="title" placeholder="本次内容的名称" /><Field label="主题" name="topic" placeholder="可以只填主题，不必先写完整稿件" /><button disabled={busy} className="button primary">创建项目</button></form><label className="project-select">当前项目<select value={selectedProjectId ?? ""} onChange={(event) => setSelectedProjectId(event.target.value || null)}><option value="">选择项目</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.title} · {project.topic}</option>)}</select></label></div>
        {selectedProject && <div className="production"><div className="row-between"><div><h3>{selectedProject.title}</h3><p className="muted">{selectedProject.topic} · 草稿 v{draft?.version ?? 0} · 文案 r{draft?.script_revision ?? 0}</p></div><div className="actions"><button disabled={busy} className="button" onClick={() => void planProject()}>生成文案 + ScenePlan</button><button disabled={busy || !draft?.scenes.length} className="button" onClick={() => void routeProject()}>查找候选</button><button disabled={busy || !routes.length} className="button" onClick={() => void assembleSourceLedProject()}>按原声 + ASR 组装</button><button disabled={busy || !routes.length} className="button" onClick={() => void assembleProject()}>新旁白组装</button><button disabled={busy || !videoSpec} className="button primary" onClick={() => void renderProject()}>本地渲染</button></div></div>
        {routes.length > 0 && <div className="cost-actions"><button disabled={busy} className="button" onClick={() => void reduceCostProject()}>按低成本替代方案</button><small className="muted">只比较当前候选；未知价格不会被当作便宜，应用后仍需审核匹配度。</small></div>}
        {draft && <div className="draft-editor"><label>脚本 / 旁白稿（可选）<textarea value={draft.script ?? ""} onChange={(event) => updateDraftScript(event.target.value)} placeholder="可以先写要表达的观点；留空时由模型生成可编辑文案。" /></label><button disabled={busy} className="button" onClick={() => void saveDraftText()}>保存稿件</button><small className="muted">服务端会记录稿件与 IP 证据版本；编辑脚本、场景文案或 IP 后，旧计划、候选和渲染都会失效，不能被旧标签页重新提交。</small>{draft.invalidation_reasons.length > 0 && <small className="error">已失效：{draft.invalidation_reasons.join("、")}；请重新生成后续结果。</small>}</div>}
          {draft?.scenes.length ? <div className="scene-list">{draft.scenes.map((scene) => <article className="scene" key={scene.id}><div className="row-between"><div><span className="scene-number">{String(scene.order + 1).padStart(2, "0")}</span><strong>{scene.purpose}</strong><label className="scene-script">场景文案<textarea value={scene.voice_text} onChange={(event) => updateSceneVoiceText(scene.id, event.target.value)} rows={3} /></label></div><span className="badge">{scene.duration_target_ms}ms</span></div><label className="narration-select">新旁白 / 本人录音（可选；新文案路径必需）<select value={narrationAssetIds[scene.id] ?? ""} onChange={(event) => { const next = { ...narrationAssetIds }; if (event.target.value) next[scene.id] = event.target.value; else delete next[scene.id]; setNarrationAssetIds(next); setVideoSpec(null); setRenderUrl(null); setRenderId(null); }}><option value="">先选择已授权音频</option>{audioAssets.map((audio) => <option key={audio.id} value={audio.id}>{audio.source_file.split(/[\\/]/).pop()} · {Math.round(audio.duration_ms / 1000)}s</option>)}</select><small className="muted">Source-led 模式可保留原片原声并使用真实 ASR/SRT/VTT 时间轴；若要渲染新文案，则必须绑定逐场景授权录音。对口型/声音克隆不在此路径。</small></label><div className="candidates">{(routes.find((route) => route.scene_plan_id === scene.id)?.candidates ?? []).map((candidate, index) => <button key={`${candidate.scene_plan_id}-${candidate.asset_id ?? candidate.source_kind}-${index}`} className={`candidate ${selections[scene.id] === candidate ? "selected" : ""}`} onClick={() => selectCandidate(candidate)}><span><b>{index + 1}. {candidate.source_kind}</b><small>{candidate.why.join(" · ")}</small></span><span className="candidate-score">{Math.round(candidate.match_score * 100)}%</span></button>)}{!routes.find((route) => route.scene_plan_id === scene.id) && <p className="muted">点击“查找候选”开始。</p>}</div></article>)}</div> : <div className="empty-state"><strong>从主题开始</strong><p>输入主题后生成 ScenePlan；系统会读取当前 IP、选题证据和素材摘要。</p></div>}
          {routes.length > 0 && <section className="shoot-list" aria-label="Shoot List"><div className="row-between"><div><span className="kicker">OPTIONAL / CAPTURE GAP</span><h3>补拍清单</h3></div><span className="badge">可选，不阻断制作</span></div>{shootList.length > 0 ? shootList.map((item) => { const task = shootTasks.find((value) => value.scene_plan_id === item.scene_plan_id); return <article className="shoot-item" key={`${item.scene_plan_id}-${item.scene_id}`}><div className="row-between"><strong>场景 {item.scene_id}</strong><span className="badge">至少 {Math.ceil(item.duration_ms / 1000)} 秒</span></div><p><b>拍什么：</b>{item.what_to_shoot}</p><p><b>机位：</b>{item.framing}</p><p><b>是否需说话：</b>{item.requires_speaking ? "是" : "否"}</p><small>{item.speaking_note}</small><small>{item.fallback}</small><div className="shoot-actions">{task?.status === "fulfilled" ? <span className="ok">已上传并绑定补拍视频</span> : task?.status === "confirmed" ? <><span className="muted">已确认，等待上传</span><button disabled={busy} className="button" onClick={() => void dismissShootTask(item)}>不补拍，使用替代</button></> : task?.status === "dismissed" ? <button disabled={busy} className="button" onClick={() => void confirmShootTask(item)}>恢复补拍</button> : <button disabled={busy} className="button primary" onClick={() => void confirmShootTask(item)}>确认补拍</button>}</div></article>; }) : <p className="muted">当前候选没有需要补拍的场景；出现素材缺口时，这里会给出可执行建议。</p>}</section>}
          {videoSpec && <div className="result-panel"><strong>{isNarratedSpec(videoSpec) ? "新旁白 VideoSpec" : "Source-led VideoSpec"}</strong><span>{videoSpec.scenes.length} 个场景 · 可本地渲染</span>{sourceEndWarning(videoSpec) && <small className="error">{sourceEndWarning(videoSpec)}</small>}</div>}{renderUrl && <><video className="render-preview" controls src={renderUrl} /><div className="export-actions"><a className="button primary" href={renderUrl} download={`${selectedProject.title || "content-os"}.mp4`}>下载 MP4</a><button disabled={busy || !draft} className="button" onClick={exportDraftCover}>下载基础封面</button><button disabled={busy || !draft} className="button" onClick={exportDraftText}>下载文案</button><button disabled={busy} className="button" onClick={() => void recordRenderedUsage()}>确认导出并记录素材使用</button></div><small className="muted">MP4、封面和文案均先下载到本机；只有点击确认后才记录素材 production usage。预览、失败和重试不计入使用。</small></>}
          {costEstimate && (routes.length > 0 || costEstimate.required_scene_count > 0) && <section className="cost-panel" aria-label="项目成本估价"><div className="row-between"><div><span className="kicker">BEFORE EXECUTION</span><h3>项目执行前成本估价</h3></div><span className="badge">不自动扣费</span></div><div className="cost-total"><strong>{costEstimate.known_amount} {costEstimate.currency}</strong><span>已知金额 · 已选 {costEstimate.selected_scene_count}/{costEstimate.required_scene_count} 个场景</span></div>{costEstimate.unknown_cost_count > 0 && <p className="error">另有 {costEstimate.unknown_cost_count} 项价格未知；系统不会按 0 计入，需明确价格或显式允许未知成本后才会执行 Provider 调用。</p>}{costEstimate.line_items.length > 0 && <div className="cost-lines">{costEstimate.line_items.map((item, index) => <div className="cost-line" key={`${item.label}-${index}`}><span>{item.label}</span><span>{costLabel(item.cost)}</span></div>)}</div>}{costEstimate.selected_scene_count < costEstimate.required_scene_count && <small className="muted">完成所有场景选片后才会加入本地渲染项；当前摘要仅覆盖已确认的场景。</small>}</section>}
        </div>}
      </section>
      <section className="card"><div className="section-heading"><div><span className="kicker">05 / READINESS</span><h2>运行能力与预算</h2></div><span className="badge">不探测外部服务</span></div><div className="columns compact"><div className="capability-grid">{(readiness?.capabilities ?? []).map((capability) => <div className="capability" key={capability.key}><div className="row-between"><strong>{capability.key}</strong><span className={capability.status === "ready" ? "ok" : capability.status === "not_developed" ? "muted" : "error"}>{prettyStatus(capability.status)}</span></div><small>{capability.detail}{capability.model ? ` · ${capability.model}` : ""}</small></div>)}</div><form onSubmit={(event) => void saveBudget(event)}><h3>一次设置运行预算</h3><label>金额上限（USD，可留空）<input value={budgetAmount} onChange={(event) => setBudgetAmount(event.target.value)} inputMode="decimal" placeholder="例如 5.00" /></label><label>调用次数上限（可留空）<input value={budgetCalls} onChange={(event) => setBudgetCalls(event.target.value)} inputMode="numeric" placeholder="例如 20" /></label><label className="checkbox-line"><input type="checkbox" checked={allowUnknownCost} onChange={(event) => setAllowUnknownCost(event.target.checked)} /> 允许 Provider 未返回账单金额</label><button disabled={busy} className="button primary">保存预算策略</button><small className="muted">当前：{budget?.snapshot.calls ?? 0} 次调用 · 已知金额 {budget?.snapshot.known_amount ?? "0"} {budget?.snapshot.currency ?? "USD"} · 未知金额 {budget?.snapshot.unknown_cost_calls ?? 0} 次。Provider 调用仍会记录成功/失败和未知成本。</small></form></div></section>
      <section className="card"><div className="section-heading"><div><span className="kicker">06 / INTELLIGENCE</span><h2>账号、历史与声音</h2></div><span className="badge">只读 / consent-gated</span></div>
        <div className="columns compact">
          <div><form onSubmit={(event) => void createAccount(event)}><h3>只读账号记录</h3><Field label="Provider" name="provider" placeholder="youtube" /><Field label="账号外部 ID" name="account_external_id" placeholder="channel-id" /><Field label="显示名称" name="display_name" placeholder="我的频道" /><label>记录时间<input name="connected_at" type="datetime-local" defaultValue={new Date().toISOString().slice(0, 16)} required /></label><button disabled={busy} className="button primary">保存账号</button></form><div className="asset-list">{accounts.map((account) => <div className="asset-row" key={account.id}><div><strong>{account.display_name || account.account_external_id}</strong><small>{account.provider} · {account.read_only ? "只读" : "请检查权限"}</small></div><span className="badge">历史 {historicalContent.filter((item) => item.account_connection_id === account.id).length}</span></div>)}{accounts.length === 0 && <p className="muted">尚无账号连接；不会在此保存令牌。</p>}</div></div>
          <div><form onSubmit={(event) => void createHistorical(event)}><h3>历史内容导入记录</h3><label>所属账号<select name="account_connection_id" required disabled={!accounts.length}><option value="">选择只读账号</option>{accounts.map((account) => <option key={account.id} value={account.id}>{account.display_name || account.account_external_id}</option>)}</select></label><Field label="外部内容 ID" name="external_id" placeholder="video-id" /><Field label="标题" name="title" placeholder="历史内容标题" /><Field label="字幕 / 转录" name="transcript" as="textarea" /><Field label="指标 JSON（可选）" name="metrics" placeholder='{"views": 100}' /><button disabled={busy || !accounts.length} className="button primary">保存历史内容</button></form><div className="asset-list">{historicalContent.slice(0, 6).map((item) => <div className="asset-row" key={item.id}><div><strong>{item.title}</strong><small>{item.external_id} · {Object.keys(item.metrics).join(", ") || "无指标"}</small></div></div>)}{historicalContent.length === 0 && <p className="muted">导入字幕和元数据后才能形成可追溯证据。</p>}</div></div>
        </div>
        <div className="columns compact">
          <form onSubmit={(event) => void createVoice(event)}><h3>Voice Profile 登记</h3><Field label="名称" name="name" placeholder="本人声音候选" /><Field label="Provider" name="provider" placeholder="provider-name" /><Field label="Provider Profile ID" name="provider_profile_id" placeholder="profile-id" /><Field label="参考 Clip IDs" name="reference_clip_ids" placeholder="uuid, uuid" /><Field label="同意主体" name="subject_name" placeholder="本人" /><label>同意依据<select name="basis" defaultValue="self"><option value="self">本人</option><option value="explicit_authorization">明确授权</option></select></label><Field label="授权记录引用（如适用）" name="authorization_reference" /><label><input name="confirmed" type="checkbox" defaultChecked required /> 我确认这些参考 Clip 可用于此 Profile 登记</label><button disabled={busy} className="button">登记 Voice Profile</button></form>
          <form onSubmit={(event) => void createVoice(event, true)}><h3>Talking Profile 登记</h3><Field label="名称" name="name" placeholder="本人 Talking 候选" /><Field label="Provider" name="provider" placeholder="provider-name" /><Field label="Provider Profile ID（可选）" name="provider_profile_id" placeholder="profile-id" /><Field label="参考 Clip IDs" name="reference_clip_ids" placeholder="uuid, uuid" /><Field label="同意主体" name="subject_name" placeholder="本人" /><label>同意依据<select name="basis" defaultValue="self"><option value="self">本人</option><option value="explicit_authorization">明确授权</option></select></label><Field label="授权记录引用（如适用）" name="authorization_reference" /><label><input name="confirmed" type="checkbox" defaultChecked required /> 我确认这些参考 Clip 可用于此 Profile 登记</label><button disabled={busy} className="button">登记 Talking Profile</button></form>
        </div>
        <p className="muted">参考 Clip 必须是已导入并可定位的真实 Clip；当前只登记授权与候选元数据，不提供生成、试听或质量通过结论。{clips.length ? ` 当前可引用 Clip：${clips.length} 个。` : " 需要先完成素材分析才会出现可引用 Clip。"}</p>
        <div className="columns compact"><div className="asset-list">{voiceProfiles.map((item) => <div className="asset-row" key={item.id}><div><strong>{item.name}</strong><small>Voice · {item.provider} · refs {item.reference_clip_ids.length}</small></div><span className="badge">已同意</span></div>)}{voiceProfiles.length === 0 && <p className="muted">尚无 Voice Profile 登记。</p>}</div><div className="asset-list">{talkingProfiles.map((item) => <div className="asset-row" key={item.id}><div><strong>{item.name}</strong><small>Talking · {item.provider} · refs {item.reference_clip_ids.length}</small></div><span className="badge">已同意</span></div>)}{talkingProfiles.length === 0 && <p className="muted">尚无 Talking Profile 登记。</p>}</div></div>
      </section>
      <section className="card"><div className="section-heading"><div><span className="kicker">07 / LOOP</span><h2>发布反馈与下一轮</h2></div><span className="badge">人工发布</span></div>
        {selectedProject ? <><div className="columns compact"><form onSubmit={(event) => void recordPublication(event)}><h3>记录发布</h3><Field label="输出版本" name="output_version" defaultValue={`draft-${draft?.version ?? 0}`} /><Field label="平台" name="platform" placeholder="YouTube" /><label>发布时间<input name="published_at" type="datetime-local" defaultValue={new Date().toISOString().slice(0, 16)} required /></label><Field label="内容 URL（可选）" name="content_url" /><Field label="平台内容 ID（可选）" name="content_external_id" /><Field label="指标 JSON（可选）" name="metrics" placeholder='{"views": 100}' /><Field label="指标来源（有指标时必填）" name="metric_source" placeholder="platform-export-2026-09-09" /><Field label="观察窗口天数" name="observation_window_days" placeholder="7" /><button disabled={busy} className="button primary">保存发布记录</button></form><form onSubmit={(event) => void recordFeedback(event)}><h3>记录审核反馈</h3><Field label="输出版本" name="output_version" defaultValue={`draft-${draft?.version ?? 0}`} /><label>结果<select name="accepted" defaultValue="false"><option value="false">拒绝 / 需要修改</option><option value="true">接受</option></select></label><Field label="修改字段" name="changed_fields" placeholder="hook,caption,visual" /><Field label="拒绝原因" name="rejection_reason" as="textarea" /><Field label="备注" name="notes" as="textarea" /><button disabled={busy} className="button primary">保存反馈</button></form></div><div className="columns compact"><div className="asset-list"><h3>最近发布</h3>{publications.map((item) => <div className="asset-row" key={item.id}><div><strong>{item.platform} · {item.output_version}</strong><small>{item.content_url || "未填写 URL"} · {Object.keys(item.metrics).join(", ") || "无指标"}</small></div></div>)}{publications.length === 0 && <p className="muted">还没有人工发布记录。</p>}</div><div className="asset-list"><h3>下一轮建议</h3>{suggestions.map((item, index) => <div className="asset-row" key={`${item.reason}-${index}`}><div><strong>{item.suggestion}</strong><small>{item.reason} · {item.evidence_refs.join(", ")}</small></div></div>)}{suggestions.length === 0 && <p className="muted">提交反馈后生成带证据引用的规则建议。</p>}</div></div><div className="asset-list"><h3>最近反馈</h3>{feedback.slice(-4).reverse().map((item) => <div className="asset-row" key={item.id}><div><strong>{item.output_version} · {item.accepted ? "已接受" : "需修改"}</strong><small>{item.changed_fields.join(", ") || "未指定修改字段"} · {item.rejection_reason || item.notes || "无备注"}</small></div></div>)}</div></> : <p className="muted">先创建或选择项目，才能记录发布和反馈。</p>}
      </section>
    </main>
    <footer>Content OS R1 · 本地数据优先 · 用户审核后导出 · 未配置的能力不会被假数据掩盖</footer>
  </div>;
}

function Field({ label, name, defaultValue, placeholder, as = "input" }: { label: string; name: string; defaultValue?: string; placeholder?: string; as?: "input" | "textarea" }) { return <label>{label}{as === "textarea" ? <textarea name={name} defaultValue={defaultValue} placeholder={placeholder} /> : <input name={name} defaultValue={defaultValue} placeholder={placeholder} />}</label>; }

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
