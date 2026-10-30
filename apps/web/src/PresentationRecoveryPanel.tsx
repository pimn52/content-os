import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { recoveryIdempotencyKey, recoveryIdentity, type ProductionRun } from "./production";

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type RenderJob = { id: string; type: string; status: string; render_id: string | null; preview_url: string | null; error_code: string | null };
type Draft = { scenes: Array<{ id: string; scene_id: string; caption_emphasis: string[] }> };
type Finding = "duplicate_text" | "known_subtitle_conflict" | "text_overlay" | "face_out_of_frame" | "source_change" | "unknown_subtitles" | "voice_naturalness" | "talking_naturalness" | "visible_sync" | "semantic_mismatch" | "narrative_rhythm";
type MachineDiagnosis = { source: string; method: string; provider?: string | null; model?: string | null; call_id?: string | null; state: string; limitations?: string[] };
type MaterialRecheck = {state:"pass"|"failed"|"unknown";assessment_state:string;responsible_stage:string;limitations:string[];
  required_capability:string[];evidence_current:boolean|null;scope?:{scene_id?:string;scene_ids?:string[]}|null};
type Observation = { id: string; render_job_id: string; source?: "human_observation" | "deterministic_spec_qa"; request: { idempotency_key: string; render_sha256: string; scene_id: string; finding: Finding; reason: string; evidence_reference: string } };
type SceneChange = { scene_id: string; before: Record<string, unknown>; after: Record<string, unknown> };
type PresentationPlan = {
  project_id: string; run_id: string; observation: Observation; fingerprint: string; action: "render_revision" | "scoped_fix_required" | "replan" | "stop"; stop_reasons: string[]; changes: SceneChange[];
  responsible_stage?: "material" | "timing" | "voice" | "talking" | "assembly" | "unknown"; assessment_state?: "known" | "unknown";
  scope?: { scene_id: string; start_ms: number | null; end_ms: number | null };
  preserves?: { master_audio_id: string | null; master_audio_sha256?: string | null; talking_job_ids: string[]; talking_output_asset_ids: string[]; scene_plan_ids: string[] };
  required_capability?: string[];
  material_replacements?: Array<{scene_id:string}>;
  semantic_recheck_calls?: number;
  diagnoses?: Array<{observation_id:string;finding:Finding;scope:{scene_id:string;start_ms:number;end_ms:number};assessment_state:string;
    responsible_stage?:string;machine_diagnosis?:MachineDiagnosis;evidence_current?:boolean}>;
  remaining_run_repairs: number; max_local_render_calls: number; voice_calls: number; talking_calls: number;
  external_charge_ceiling: string; local_compute_cost: null; user_active_minutes: null; review_scope: string;
  candidate_spec: { master_narration: { audio_asset_id: string } | null; edit_plan: { scenes: Array<{ scene_id: string; selected_asset_id: string | null }> } | null } | null;
};
type RepairRecord = {
  id: string; reason: string; replacement_job_id: string; status: string; final_review_state: string;
  technical_qa: null | { state: string; sha256?: string; width?: number; height?: number; duration_ms?: number; scope?: string };
  semantic_recheck?: MaterialRecheck | null;
  plan: { observation: Observation; action: string; predecessor_job: { id: string; payload: { render_id?: string; video_spec?: { master_narration?: { audio_asset_id: string } | null } } };
    changes: SceneChange[]; candidate_spec?: PresentationPlan["candidate_spec"]; remaining_run_repairs: number;
    semantic_recheck_calls?:number;material_replacements?:Array<{scene_id:string}> };
  provider_calls?: Array<{ status: string; actual_cost: { amount: string | null } | null }>;
};
const findings: Array<[Finding, string]> = [
  ["duplicate_text", "新标题重复了旁白全文"],
  ["known_subtitle_conflict", "Timed 字幕与已确认的原字幕冲突"],
  ["text_overlay", "文字与人物画面重叠"],
  ["face_out_of_frame", "人脸超出画面"],
  ["source_change", "需要更换来源或时间区间"],
  ["unknown_subtitles", "原字幕情况未知"],
  ["voice_naturalness", "声音不自然"],
  ["talking_naturalness", "人物表演不自然"],
  ["visible_sync", "人物口型与声音不同步"],
  ["semantic_mismatch", "画面与讲述含义不匹配"],
  ["narrative_rhythm", "讲述节奏不合适"],
];
const labels: Record<string, string> = { timed_captions: "按时间显示字幕", none: "不叠加新字幕", headline: "标题", contrast: "对照", key_point: "要点", portrait_panel: "竖屏面板", full_canvas: "铺满画面" };
const stages: Record<string, string> = { material: "素材与用途", timing: "配时与节奏", voice: "声音", talking: "人物画面", assembly: "字幕与装配", unknown: "责任待定位" };
const capabilityLabels: Record<string, string> = {
  joint_scoped_repair_plan: "把同一成片的多个问题合并为局部修正方案",
  exact_render_text_duplication_diagnosis: "核对确切成片中的文字重复",
  full_interval_source_subtitle_analysis: "核验本次来源完整区间的原字幕",
  exact_scene_text_face_layout_diagnosis: "定位本场景文字与人物画面的遮挡",
  source_vs_render_framing_diagnosis: "区分来源画面与成片裁切造成的人脸出框",
  scene_source_interval_selection_and_qa: "选择并核验本场景的新来源或区间",
  scoped_material_replacement: "仅替换受影响场景的素材",
  exact_audio_naturalness_diagnosis: "诊断确切音频的自然度",
  consumed_voice_dependency_repair: "声音修改后更新受影响的人物画面与成片",
  exact_talking_naturalness_diagnosis: "诊断确切人物输出的自然度",
  render_stage_talking_repair: "成片阶段的人物段修复与复检",
  render_vs_talking_sync_diagnosis: "对照确切成片、人物段与旁白，定位生成或装配造成的音画不同步",
  exact_scene_narration_visual_semantics_diagnosis: "诊断本场景讲述与画面含义的匹配",
  configured_video_comparison_provider: "配置可对照视频的画面核验能力",
  current_render_semantic_diagnosis: "核验当前成片与原素材的画面语义",
  semantic_layout_correction: "修正受影响场景的语义呈现",
  exact_scene_delivery_vs_edit_timing_diagnosis: "区分声音讲述与剪辑配时造成的节奏问题",
  scoped_timing_reassembly: "仅重排受影响的场景配时",
  exact_scene_defect_diagnosis: "定位本场景缺陷的具体原因",
  scene_semantic_short_emphasis_selection: "为本场景选定合适的语义短强调",
};
function describe(scene: Record<string, unknown> | undefined) {
  if (!scene) return "无对应场景";
  return [
    `图形文字：${typeof scene.graphic_text === "string" ? scene.graphic_text : "无"}`,
    `字幕：${labels[String(scene.subtitle_treatment)] ?? String(scene.subtitle_treatment ?? "未知")}`,
    `画面：${labels[String(scene.portrait_presentation)] ?? String(scene.portrait_presentation ?? "未知")}`,
    `旁白字幕语义保留：${scene.caption || (Array.isArray(scene.captions) && scene.captions.length) ? "是" : "无"}`,
  ].join(" · ");
}
function errorText(reason: unknown) { return reason instanceof Error ? reason.message : "请求失败；刷新执行状态后检查服务端原因。"; }
function recheckText(value: MaterialRecheck) {
  if (value.evidence_current === false) return "替换画面的复验结果已失效；保留成果，需重新定位。";
  if (value.state === "pass") return "替换场景的机器画面复验通过，仍待整片人审。";
  return value.state === "failed" ? "替换后的画面复验未通过；保留现有成果，需修正受影响场景。"
    : "替换后的画面尚未确认符合讲述含义；保留现有成果，需继续定位。";
}
function observationStorageKey(projectId: string, runId: string, jobId: string, hash: string) {
  return `content-os-presentation-observation:${projectId}:${runId}:${jobId}:${hash}`;
}

export function PresentationRecoveryPanel({ projectId, run, request, confirmAction = message => window.confirm(message), onRunChange }: {
  projectId: string; run: ProductionRun; request: Request; confirmAction?: (message: string) => boolean; onRunChange: (run: ProductionRun) => void;
}) {
  const [job, setJob] = useState<RenderJob | null>(null); const [draft, setDraft] = useState<Draft | null>(null);
  const [renderUrl, setRenderUrl] = useState(""); const [renderHash, setRenderHash] = useState("");
  const [observation, setObservation] = useState<Observation | null>(null); const [plan, setPlan] = useState<PresentationPlan | null>(null);
  const [records, setRecords] = useState<RepairRecord[]>([]); const [finding, setFinding] = useState<Finding>("duplicate_text");
  const [sceneId, setSceneId] = useState(""); const [reason, setReason] = useState(""); const [evidence, setEvidence] = useState("");
  const [reviewReason, setReviewReason] = useState("");
  const [choice,setChoice]=useState("candidate");
  const [choiceImpact,setChoiceImpact]=useState<{delivery_intent:string;retained_source_ms:number;discard_render_ids:string[]}|null>(null);
  const [repairReason, setRepairReason] = useState(""); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [notice, setNotice] = useState("");
  const epoch = useRef(0); const locked = useRef(false);
  const identity = recoveryIdentity(projectId, run.id, run.render_job_id, null, JSON.stringify([
    run.status, run.preflight_fingerprint, run.voice_audio_id, run.talking_master_audio_id,
    run.talking_preview_dependencies, run.talking_source_bindings,
  ]));
  const identityRef = useRef(identity); identityRef.current = identity;
  const base = `/projects/${projectId}/production-runs/${run.id}`;
  const currentRepair = records.find(record => record.replacement_job_id === run.render_job_id);
  const materialRecheckPending = job?.status === "completed" && !!currentRepair
    && !!currentRepair.plan.semantic_recheck_calls && !currentRepair.semantic_recheck;
  const canInspect = run.status === "render_completed_awaiting_review" && job?.status === "completed" && !!renderHash && !!renderUrl && !materialRecheckPending;
  const scenes = draft?.scenes ?? [];
  const retainedMaster = plan?.preserves?.master_audio_id ?? plan?.candidate_spec?.master_narration?.audio_asset_id ?? run.voice_audio_id ?? run.talking_master_audio_id ?? null;
  const semanticDiagnoses = plan?.diagnoses?.filter(item => item.finding === "semantic_mismatch" && item.machine_diagnosis) ?? [];
  const currentScene = scenes.find(item => item.scene_id === sceneId);
  const sceneChoices = useMemo(() => scenes.map(item => ({ id: item.scene_id, label: item.scene_id })), [draft]);

  useEffect(() => {
    if (!materialRecheckPending) return;
    const token = epoch.current; const expected = identity;
    const timer = setInterval(() => {
      void request<RepairRecord[]>(base + "/presentation-repairs").then(history => {
        if (token === epoch.current && identityRef.current === expected) setRecords(history);
      }).catch(() => {});
    }, 2000);
    return () => clearInterval(timer);
  }, [materialRecheckPending, identity, base, request]);

  useEffect(() => {
    const token = ++epoch.current; const expected = identity;
    const active = () => token === epoch.current && identityRef.current === expected;
    locked.current = false; setBusy(false); setError(""); setNotice(""); setJob(null); setDraft(null); setRenderUrl(""); setRenderHash("");
    setObservation(null); setPlan(null); setRecords([]);setChoice("candidate");setChoiceImpact(null);
    setSceneId(""); setFinding("duplicate_text"); setReason(""); setEvidence(""); setRepairReason("");
    const urls: string[] = [];
    const headers = new Headers(); const bearer = window.sessionStorage.getItem("content-os-access-token");
    if (bearer) headers.set("Authorization", `Bearer ${bearer}`);
    Promise.all([
      request<Draft>(`/projects/${projectId}/draft`),
      request<RepairRecord[]>(base + "/presentation-repairs"),
      run.render_job_id ? request<RenderJob>(`/jobs/${run.render_job_id}`) : Promise.resolve(null),
      request<Observation[]>(`/projects/${projectId}/planning-observations`),
    ]).then(async ([currentDraft, history, currentJob, observations]) => {
      if (!active()) return;
      setDraft(currentDraft); setRecords(history);
      if (currentJob) setJob(currentJob);
      if (!currentJob || currentJob.type !== "render" || currentJob.status !== "completed" || !currentJob.render_id
        || run.status !== "render_completed_awaiting_review") return;
      const response = await fetch(`/projects/${projectId}/renders/${currentJob.render_id}`, { headers });
      if (!response.ok) throw new Error(`Run 当前 Render 读取失败 (${response.status})`);
      const blob = await response.blob(); const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
      const hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
      const url = URL.createObjectURL(blob); urls.push(url);
      if (!active()) { URL.revokeObjectURL(url); return; }
      setRenderUrl(url); setRenderHash(hash);
      const currentChoice=await request<{disposition:string}>(`/projects/${projectId}/renders/${currentJob.render_id}/choice`);
      if(!active())return;
      setChoice(currentChoice.disposition);
      const impact=await request<{delivery_intent:string;retained_source_ms:number;discard_render_ids:string[]}>(`/projects/${projectId}/renders/${currentJob.render_id}/choice-impact`);
      if(!active())return;
      setChoiceImpact(impact);
      const saved = observations.filter(item => item.render_job_id === currentJob.id && item.request.render_sha256 === hash).slice(-1)[0];
      if (saved) {
        setObservation(saved); setSceneId(saved.request.scene_id); setFinding(saved.request.finding); setReason(saved.request.reason); setEvidence(saved.request.evidence_reference);
      }
    }).catch(cause => { if (active()) setError(errorText(cause)); });
    return () => { ++epoch.current; for (const url of urls) URL.revokeObjectURL(url); };
  }, [projectId, run.id, run.render_job_id, run.status, request, identity]);

  async function recordObservation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canInspect || !job || !currentScene || !reason.trim() || !evidence.trim() || locked.current) return;
    const token = epoch.current; const expected = identity;
    const active = () => token === epoch.current && identityRef.current === expected;
    locked.current = true; setBusy(true); setError(""); setNotice(""); setPlan(null);
    try {
      const value = { idempotency_key: await recoveryIdempotencyKey("presentation-observation", projectId, run.id, renderHash,
        { job_id: job.id, scene_id: sceneId, finding, reason: reason.trim(), evidence_reference: evidence.trim() }),
        render_sha256: renderHash, scene_id: sceneId, finding, reason: reason.trim(), evidence_reference: evidence.trim() };
      if (!active()) return;
      const saved = await request<Observation>(base + "/presentation-observations", { method: "POST", body: JSON.stringify(value) });
      if (!active() || saved.request.render_sha256 !== renderHash || saved.render_job_id !== job.id) return;
      setObservation(saved);
      try { localStorage.setItem(observationStorageKey(projectId, run.id, job.id, renderHash), JSON.stringify(saved)); }
      catch { setNotice("观察已在后台保存且当前页面可继续；浏览器无法缓存引用，刷新后需重新登记观察。请勿因缓存失败重复提交修复。"); return; }
      setNotice("已保存绑定当前 Render 文件的人工观察。继续读取后台候选方案，确认具体画面变化后再提交。");
    } catch (cause) { if (token === epoch.current && identityRef.current === expected) setError(errorText(cause)); }
    finally { if (token === epoch.current && identityRef.current === expected) { locked.current = false; setBusy(false); } }
  }

  async function inspectPlan() {
    if (!observation || locked.current) return;
    const token = epoch.current; const expected = identity; locked.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const value = await request<PresentationPlan>(base + `/presentation-repair-plan?observation_id=${encodeURIComponent(observation.id)}`);
      if (token !== epoch.current || identityRef.current !== expected) return;
      if (value.project_id !== projectId || value.run_id !== run.id) throw new Error("后台呈现方案与当前项目/执行不一致，已拒绝使用。");
      if (value.observation.id !== observation.id || value.observation.render_job_id !== job?.id || value.observation.request.render_sha256 !== renderHash) throw new Error("后台观察与当前 Render 文件不一致，请重新读取当前输出。");
      if (value.fingerprint) { setObservation(value.observation); setPlan(value); }
    } catch (cause) { if (token === epoch.current && identityRef.current === expected) setError(errorText(cause)); }
    finally { if (token === epoch.current && identityRef.current === expected) { locked.current = false; setBusy(false); } }
  }

  async function submitRepair(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!observation || !plan || plan.action !== "render_revision" || plan.assessment_state === "unknown" || plan.stop_reasons.length || plan.remaining_run_repairs <= 0 || !retainedMaster || !repairReason.trim() || locked.current) return;
    const talkingDependencies = (run.talking_preview_dependencies ?? []).map(item => `${item.scene_plan_id}:${item.series_id}:${item.output_asset_id ?? "no-output"}`).join("、") || "无当前 Run 级 Talking 预览绑定";
    if (!confirmAction(`将基于已显示的改前/改后方案创建新 Render。保留主旁白 ${retainedMaster ?? "身份未知"} 与人物来源绑定 ${talkingDependencies}；不派发 Voice/Talking；新输出技术检查后仍待完整 U-Product 审核。确认执行吗？`)) return;
    const token = epoch.current; const expected = identity; locked.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const body = { observation_id: observation.id, expected_fingerprint: plan.fingerprint,
        idempotency_key: await recoveryIdempotencyKey("presentation-repair", projectId, run.id, plan.fingerprint,
          { observation_id: observation.id, reason: repairReason.trim() }),
        confirmed_action: "render_revision", reason: repairReason.trim() };
      if (token !== epoch.current || identityRef.current !== expected) return;
      await request<RepairRecord>(base + "/presentation-repair", { method: "POST", body: JSON.stringify(body) });
      const [updated, history] = await Promise.all([request<ProductionRun>(base), request<RepairRecord[]>(base + "/presentation-repairs")]);
      if (token !== epoch.current || identityRef.current !== expected || updated.id !== run.id || updated.project_id !== projectId) return;
      setRecords(history); setPlan(null); setObservation(null); setRepairReason("");
      onRunChange(updated); setNotice("新 Render 版本已建立；历史保留旧版本，新输出完成后还需技术检查和成片人审。");
    } catch (cause) { if (token === epoch.current && identityRef.current === expected) { setPlan(null); setError(errorText(cause)); } }
    finally { if (token === epoch.current && identityRef.current === expected) { locked.current = false; setBusy(false); } }
  }

  function clearObservationDraft() { setObservation(null); setPlan(null); setRepairReason(""); }
  async function choose(disposition:"selected"|"discarded") {
    if(!canInspect||!job?.render_id||locked.current)return;
    const token=epoch.current;locked.current=true;setBusy(true);setError("");
    try {
      const value=await request<{disposition:string}>(`/projects/${projectId}/renders/${job.render_id}/choice`,{method:"PUT",body:JSON.stringify({disposition})});
      if(token===epoch.current){setChoice(value.disposition);setNotice(disposition==="selected"?"已选用此版本；同作品弃用候选解除占用，文件和发布史保留。":"已不予使用；解除未发布占用，保留已发布历史。");}
    }catch(cause){if(token===epoch.current)setError(errorText(cause));}
    finally{if(token===epoch.current){locked.current=false;setBusy(false);}}
  }
  async function reviewVideo(accepted: boolean) {
    if (!canInspect || !job?.render_id || locked.current || (!accepted && !reviewReason.trim())) return;
    const token = epoch.current;
    locked.current = true; setBusy(true); setError("");
    try {
      await request(`/projects/${projectId}/feedback`, { method:"POST", body:JSON.stringify({
        output_version:`render:${job.render_id}`, accepted, changed_fields:[],
        rejection_reason:accepted ? null : reviewReason.trim(), notes:reviewReason.trim() || null,
      }) });
      const current = await request<ProductionRun>(base);
      if (token === epoch.current) onRunChange(current);
    } catch (cause) { if (token === epoch.current) setError(errorText(cause)); }
    finally { if (token === epoch.current) { locked.current=false; setBusy(false); } }
  }
  return <section className="draft-editor" aria-label="此执行的成片呈现修复">
    <div className="row-between"><div><span className="kicker">PRESENTATION / RENDER REVISION</span><h3>成片呈现与新版本</h3></div><span className="badge">{run.status}</span></div>
    {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="ok">{notice}</p>}
    <p className="muted">检查完整成片中的具体场景，可登记人工观察或查看自动方案检查。按支持的方案修复后建立新版本，原主旁白、人物结果、原版本及审核记录分别保留。</p>
    {materialRecheckPending&&<p role="status">正在复核替换后的实际画面。旁白、人物结果和其它场景继续保留；复验后再交整片审核。</p>}
    {job && <p>当前 Render Job {job.id} · {job.status} · {job.error_code ?? "无技术错误码"} · {renderHash ? `SHA-256 ${renderHash}` : "媒体 hash 未读取"}</p>}
    {canInspect && <section aria-label="完整成片审核" className="subform">
      <p>{choice==="selected"?"当前选用版":choice==="discarded"?"不予使用，未发布占用已释放":run.delivery_intent==="draft"?"仅试做与比较：保留使用记录，不挤占正式作品":"作品候选：已保留占用，同作品版本可继续比较"}</p>
      {choiceImpact&&<p>选用此版将{choiceImpact.delivery_intent==="draft"?"保留试做用料记录":"锁定本版用料"}（{(choiceImpact.retained_source_ms/1000).toFixed(1)} 秒），弃用同作品其他 {choiceImpact.discard_render_ids?.length??0} 个候选并解除其未发布占用。已发布历史保留；同时导出多版时仍需检查实际复用冲突。</p>}
      <button type="button" disabled={busy||choice==="selected"||run.review_state==="rejected"} onClick={()=>void choose("selected")}>选用此版</button>
      <button type="button" disabled={busy||choice==="discarded"} onClick={()=>void choose("discarded")}>不予使用</button>
      <p>成片版本：render:{job?.render_id}。{run.review_state === "approved" ? "用户已接受" : run.review_state === "rejected" ? "用户已拒绝，需要修改" : "待用户判断画面、声音与内容质量"}</p>
      {(!run.review_state || run.review_state === "pending") && <>
        <label>成片反馈<input value={reviewReason} onChange={event => setReviewReason(event.target.value)} maxLength={2000} disabled={busy} /></label>
        <button type="button" disabled={busy || !reviewReason.trim()} onClick={() => void reviewVideo(false)}>需修改</button>
        <button type="button" disabled={busy} onClick={() => void reviewVideo(true)}>接受此版成片</button>
      </>}
    </section>}
    {renderUrl && <><video className="render-preview" controls preload="metadata" src={renderUrl} /><a href={`/projects/${projectId}/renders/${job?.render_id}/credits`} target="_blank" rel="noreferrer">下载素材来源与署名</a></>}
    {canInspect && <form className="subform" onSubmit={event => void recordObservation(event)}>
      <h4>登记此 Render 的一项场景观察</h4>
      <label>场景<select value={sceneId} disabled={busy} onChange={event => { clearObservationDraft(); setSceneId(event.target.value); }} required><option value="">请选择</option>{sceneChoices.map(item => <option value={item.id} key={item.id}>{item.label}</option>)}</select></label>
      <label>所见问题<select value={finding} disabled={busy} onChange={event => { clearObservationDraft(); setFinding(event.target.value as Finding); }}>{findings.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>观察理由<input value={reason} disabled={busy} onChange={event => { clearObservationDraft(); setReason(event.target.value); }} required maxLength={2000} /></label>
      <label>证据引用（如 Render 时间点/人工检查记录）<input value={evidence} disabled={busy} onChange={event => { clearObservationDraft(); setEvidence(event.target.value); }} required maxLength={2000} /></label>
      <button className="button" disabled={busy || !currentScene || !reason.trim() || !evidence.trim()}>确认记录人工观察</button>
    </form>}
    {observation && <article className="scene"><h4>{observation.source === "deterministic_spec_qa" ? "自动方案检查" : "人工观察"}引用（读取方案时由后台复核）</h4><p>Render {observation.render_job_id} / SHA-256 {observation.request.render_sha256} · 场景 {observation.request.scene_id} · {findings.find(([key]) => key === observation.request.finding)?.[1]}</p>
      <p>{observation.request.reason} · 证据：{observation.request.evidence_reference}</p>
      {!plan && <button type="button" className="button" disabled={busy} onClick={() => void inspectPlan()}>读取修复候选方案</button>}
    </article>}
    {plan && <article className="scene" aria-label="呈现修复候选">
      <h4>后端候选 · {plan.action} · Run 剩余额度 {plan.remaining_run_repairs}</h4>
      <p>责任工序：{stages[plan.responsible_stage ?? "unknown"]} · 诊断：{plan.assessment_state === "known" ? "已有本项诊断证据" : plan.assessment_state === "unknown" ? "待补证据，尚未定位可执行修复" : "由后台候选复核"}</p>
      <p>问题范围：场景 {plan.scope?.scene_id ?? observation?.request.scene_id ?? "未知"}{plan.scope?.start_ms != null && plan.scope?.end_ms != null ? ` · ${plan.scope.start_ms}–${plan.scope.end_ms} ms` : " · 时间区间待定位"}</p>
      {plan.diagnoses&&plan.diagnoses.length>1&&<><p>{plan.action==="render_revision"?`本次一并处理 ${plan.diagnoses.length} 项问题，只建立一个修订版本。`:`本版保留 ${plan.diagnoses.length} 项问题，尚未建立修订版本。`}</p><ul>{plan.diagnoses.map(item=><li key={item.observation_id}>{item.scope.scene_id} · {findings.find(([key])=>key===item.finding)?.[1]??"质量问题"} · {item.assessment_state==="known"?"已有局部诊断":"待补诊断"}</li>)}</ul></>}
      {!!semanticDiagnoses.length&&<section aria-label="机器画面语义对照"><h4>机器画面语义对照</h4>
        <p className="muted">机器仅对照静音画面与旁白文字，不评价听感自然度、口型同步，也不代表最终人审通过。</p>
        <ul>{semanticDiagnoses.map(item=><li key={item.observation_id}>场景 {item.scope.scene_id} · {item.evidence_current===false?"原诊断证据已失效，需重新定位":item.machine_diagnosis?.state==="known"?`已定位本项责任：${stages[item.responsible_stage??plan.responsible_stage??"unknown"]??"责任待定位"}，${plan.material_replacements?.length?"本方案仅替换问题场景画面":"局部修正仍待核验"}`:"尚未确定本项责任，保留现有成果"}</li>)}</ul>
      </section>}
      {!!plan.required_capability?.length && <div><p>待补能力或证据：</p><ul>{plan.required_capability.map(capability => <li key={capability}>{capabilityLabels[capability] ?? capability}</li>)}</ul></div>}
      <p>渲染上限 {plan.max_local_render_calls} 次 · Voice {plan.voice_calls} 次 · Talking {plan.talking_calls} 次{plan.semantic_recheck_calls?` · 画面复验 ${plan.semantic_recheck_calls} 次`:""} · {plan.external_charge_ceiling==="unknown"?"外部费用待核实，受现有预算约束":`外部收费上限 ${plan.external_charge_ceiling}`} · 本机成本 / 主动时间未知</p>
      {!!plan.material_replacements?.length&&<p>仅替换已核验的场景素材，保留原时轴、字幕文字、旁白及其它人物画面；{plan.semantic_recheck_calls?`新输出再进行 ${plan.semantic_recheck_calls} 次实际画面复验，之后仍待整片人审。`:"新成片仍需完整质量复核。"}</p>}
      {(plan.changes ?? []).map(change => <section key={change.scene_id} className="subform"><strong>场景 {change.scene_id}</strong><p>改前：{describe(change.before)}</p><p>改后：{describe(change.after)}</p><p>{plan.material_replacements?.some(item=>item.scene_id===change.scene_id)?"更换本场景来源，并重新核对其字幕与呈现证据；旁白和其它场景保留。":"旁白文案与原音频、来源素材、区间和裁切不由此动作修改。"}</p></section>)}
      <p>保留主旁白：{retainedMaster ?? "身份未知；后端候选/Run 也未给出，不可确认"}</p>
      {plan.preserves?.master_audio_sha256 && <p>主旁白 SHA-256：{plan.preserves.master_audio_sha256}</p>}
      {plan.preserves && <><p>保留人物 Job：{plan.preserves.talking_job_ids.join(" · ") || "当前执行无已绑定人物 Job"}</p><p>保留人物输出：{plan.preserves.talking_output_asset_ids.join(" · ") || "当前执行无已绑定人物输出"}</p><p>保留场景方案：{plan.preserves.scene_plan_ids.join(" · ") || "身份未知"}</p></>}
      <p>保留 Talking/人物来源：{(run.talking_preview_dependencies ?? []).map(item => `${item.scene_plan_id}: series ${item.series_id} / output ${item.output_asset_id ?? "无输出"}`).join(" · ") || "当前 Run 无 Talking 预览绑定"}</p>
      <p>画面选择：{plan.candidate_spec?.edit_plan?.scenes?.map(scene => `${scene.scene_id}: ${scene.selected_asset_id ?? "同一版式/无外部素材"}`).join(" · ") || "以原 VideoSpec 为准"}</p>
      {plan.stop_reasons.length > 0 && <ul>{plan.stop_reasons.map(item => <li className="error" key={item}>{item}</li>)}</ul>}
      {plan.action === "render_revision" && plan.assessment_state !== "unknown" && <form className="subform" onSubmit={event => void submitRepair(event)}>
        <label>本次修复理由<input value={repairReason} disabled={busy} onChange={event => setRepairReason(event.target.value)} required maxLength={2000} /></label>
        <button className="button primary" disabled={busy || !repairReason.trim() || !retainedMaster || plan.stop_reasons.length > 0 || plan.remaining_run_repairs <= 0}>按此方案建立新 Render（后台复核）</button>
      </form>}
      {(plan.action === "scoped_fix_required" || plan.action === "replan" || plan.assessment_state === "unknown") && <p className="muted">当前缺陷尚无可执行的局部修复。保留现有成果，按责任工序补齐上述诊断或能力，再确定需修改的依赖与复检范围。</p>}
      {plan.action === "stop" && <p className="error">当前不满足修复条件；按上方原因诊断，不重复渲染。</p>}
    </article>}
    {records.map(record => <article className="scene" key={record.id}><h4>呈现修复历史 · 新 Job {record.status}</h4>
      <p>理由：{record.reason} · 前序 Render Job：{record.plan.predecessor_job.id} · 后继 Render Job：{record.replacement_job_id}</p>
      <p>前序主旁白：{record.plan.predecessor_job.payload.video_spec?.master_narration?.audio_asset_id ?? "未知"} · 当前完整审查：{record.final_review_state}</p>
      {record.plan.changes.map(change => <p key={change.scene_id}>场景 {change.scene_id}：{describe(change.before)} → {describe(change.after)}</p>)}
      {record.technical_qa ? <p>技术 QA：{record.technical_qa.state} · 新文件 hash {record.technical_qa.sha256} · {record.technical_qa.width}×{record.technical_qa.height} · {record.technical_qa.duration_ms} ms · {record.technical_qa.scope}</p> : <p>新输出技术检查尚无可用结果。</p>}
      {record.semantic_recheck?<><p>{recheckText(record.semantic_recheck)}{record.semantic_recheck.scope?.scene_id&&` 复验场景：${record.semantic_recheck.scope.scene_id}。`}</p><p className="muted">机器只复核替换场景的画面与讲述含义，不评价听感自然度、口型同步，也不代表整片通过。</p></>:!!record.plan.semantic_recheck_calls&&<p>{record.status==="completed"?"正在复核替换后的实际画面。":"新输出完成后将自动复核替换场景的实际画面。"}</p>}
      {(record.provider_calls ?? []).map((call, index) => <small key={index}>本地 Render 调用 · {call.status} · 实际外部费用 {call.actual_cost?.amount ?? "未知"}</small>)}
    </article>)}
  </section>;
}
