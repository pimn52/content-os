import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { recoveryIdempotencyKey, recoveryIdentity, type ProductionRun } from "./production";

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type RenderJob = { id: string; type: string; status: string; render_id: string | null; preview_url: string | null; error_code: string | null };
type Draft = { scenes: Array<{ id: string; scene_id: string; caption_emphasis: string[] }> };
type Finding = "duplicate_text" | "known_subtitle_conflict" | "text_overlay" | "face_out_of_frame" | "source_change" | "unknown_subtitles";
type Observation = { id: string; render_job_id: string; request: { idempotency_key: string; render_sha256: string; scene_id: string; finding: Finding; reason: string; evidence_reference: string } };
type SceneChange = { scene_id: string; before: Record<string, unknown>; after: Record<string, unknown> };
type PresentationPlan = {
  project_id: string; run_id: string; observation: Observation; fingerprint: string; action: "render_revision" | "replan" | "stop"; stop_reasons: string[]; changes: SceneChange[];
  remaining_run_repairs: number; max_local_render_calls: number; voice_calls: number; talking_calls: number;
  external_charge_ceiling: string; local_compute_cost: null; user_active_minutes: null; review_scope: string;
  candidate_spec: { master_narration: { audio_asset_id: string } | null; edit_plan: { scenes: Array<{ scene_id: string; selected_asset_id: string | null }> } | null } | null;
};
type RepairRecord = {
  id: string; reason: string; replacement_job_id: string; status: string; final_review_state: string;
  technical_qa: null | { state: string; sha256?: string; width?: number; height?: number; duration_ms?: number; scope?: string };
  plan: { observation: Observation; action: string; predecessor_job: { id: string; payload: { render_id?: string; video_spec?: { master_narration?: { audio_asset_id: string } | null } } };
    changes: SceneChange[]; candidate_spec?: PresentationPlan["candidate_spec"]; remaining_run_repairs: number };
  provider_calls?: Array<{ status: string; actual_cost: { amount: string | null } | null }>;
};
const findings: Array<[Finding, string]> = [
  ["duplicate_text", "新标题重复了旁白全文"],
  ["known_subtitle_conflict", "Timed 字幕与已确认的原字幕冲突"],
  ["text_overlay", "文字与人物画面重叠"],
  ["face_out_of_frame", "人脸超出画面（需要重规划）"],
  ["source_change", "需要更换来源或时间区间（需要重规划）"],
  ["unknown_subtitles", "原字幕情况未知（需要重规划）"],
];
const labels: Record<string, string> = { timed_captions: "按时间显示字幕", none: "不叠加新字幕", headline: "标题", contrast: "对照", key_point: "要点", portrait_panel: "竖屏面板", full_canvas: "铺满画面" };
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
  const [repairReason, setRepairReason] = useState(""); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [notice, setNotice] = useState("");
  const epoch = useRef(0); const locked = useRef(false);
  const identity = recoveryIdentity(projectId, run.id, run.render_job_id, null, JSON.stringify([
    run.status, run.preflight_fingerprint, run.voice_audio_id, run.talking_master_audio_id,
    run.talking_preview_dependencies, run.talking_source_bindings,
  ]));
  const identityRef = useRef(identity); identityRef.current = identity;
  const base = `/projects/${projectId}/production-runs/${run.id}`;
  const canInspect = run.status === "render_completed_awaiting_review" && job?.status === "completed" && !!renderHash && !!renderUrl;
  const scenes = draft?.scenes ?? [];
  const retainedMaster = plan?.candidate_spec?.master_narration?.audio_asset_id ?? run.voice_audio_id ?? run.talking_master_audio_id ?? null;
  const currentScene = scenes.find(item => item.scene_id === sceneId);
  const sceneChoices = useMemo(() => scenes.map(item => ({ id: item.scene_id, label: item.scene_id })), [draft]);

  useEffect(() => {
    const token = ++epoch.current; const expected = identity;
    const active = () => token === epoch.current && identityRef.current === expected;
    locked.current = false; setBusy(false); setError(""); setNotice(""); setJob(null); setDraft(null); setRenderUrl(""); setRenderHash("");
    setObservation(null); setPlan(null); setRecords([]);
    setSceneId(""); setFinding("duplicate_text"); setReason(""); setEvidence(""); setRepairReason("");
    const urls: string[] = [];
    const headers = new Headers(); const bearer = window.sessionStorage.getItem("content-os-access-token");
    if (bearer) headers.set("Authorization", `Bearer ${bearer}`);
    Promise.all([
      request<Draft>(`/projects/${projectId}/draft`),
      request<RepairRecord[]>(base + "/presentation-repairs"),
      run.render_job_id ? request<RenderJob>(`/jobs/${run.render_job_id}`) : Promise.resolve(null),
    ]).then(async ([currentDraft, history, currentJob]) => {
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
      const key = observationStorageKey(projectId, run.id, currentJob.id, hash);
      try {
        const saved = JSON.parse(localStorage.getItem(key) ?? "null") as Observation | null;
        if (saved?.request?.render_sha256 === hash && saved.render_job_id === currentJob.id) {
          setObservation(saved); setSceneId(saved.request.scene_id); setFinding(saved.request.finding); setReason(saved.request.reason); setEvidence(saved.request.evidence_reference);
        }
      } catch { /* A malformed local observation is discarded; the server remains authoritative. */ }
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
      if (!confirmAction(`记录在 Render Job ${job.id} / SHA-256 ${renderHash} / 场景 ${sceneId} 的人工观察？这是观察记录，不等同于技术 QA 或完整成片审核。`)) return;
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
    if (!observation || !plan || plan.action !== "render_revision" || plan.stop_reasons.length || plan.remaining_run_repairs <= 0 || !retainedMaster || !repairReason.trim() || locked.current) return;
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
  return <section className="draft-editor" aria-label="此执行的成片呈现修复">
    <div className="row-between"><div><span className="kicker">PRESENTATION / RENDER REVISION</span><h3>成片呈现与新版本</h3></div><span className="badge">{run.status}</span></div>
    {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="ok">{notice}</p>}
    <p className="muted">仅检查完整 Render 文件中的具体场景。记录为人工观察，按当前方案修复后会建立新的 Render；原主旁白、Talking 来源、原版本及审批记录分别保留。</p>
    {job && <p>当前 Render Job {job.id} · {job.status} · {job.error_code ?? "无技术错误码"} · {renderHash ? `SHA-256 ${renderHash}` : "媒体 hash 未读取"}</p>}
    {renderUrl && <video className="render-preview" controls preload="metadata" src={renderUrl} />}
    {canInspect && <form className="subform" onSubmit={event => void recordObservation(event)}>
      <h4>登记此 Render 的一项场景观察</h4>
      <label>场景<select value={sceneId} disabled={busy} onChange={event => { clearObservationDraft(); setSceneId(event.target.value); }} required><option value="">请选择</option>{sceneChoices.map(item => <option value={item.id} key={item.id}>{item.label}</option>)}</select></label>
      <label>所见问题<select value={finding} disabled={busy} onChange={event => { clearObservationDraft(); setFinding(event.target.value as Finding); }}>{findings.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>观察理由<input value={reason} disabled={busy} onChange={event => { clearObservationDraft(); setReason(event.target.value); }} required maxLength={2000} /></label>
      <label>证据引用（如 Render 时间点/人工检查记录）<input value={evidence} disabled={busy} onChange={event => { clearObservationDraft(); setEvidence(event.target.value); }} required maxLength={2000} /></label>
      <button className="button" disabled={busy || !currentScene || !reason.trim() || !evidence.trim()}>确认记录人工观察</button>
    </form>}
    {observation && <article className="scene"><h4>人工观察引用（读取方案时由后台复核）</h4><p>Render {observation.render_job_id} / SHA-256 {observation.request.render_sha256} · 场景 {observation.request.scene_id} · {findings.find(([key]) => key === observation.request.finding)?.[1]}</p>
      <p>{observation.request.reason} · 证据：{observation.request.evidence_reference}</p>
      {!plan && <button type="button" className="button" disabled={busy} onClick={() => void inspectPlan()}>读取修复候选方案</button>}
    </article>}
    {plan && <article className="scene" aria-label="呈现修复候选">
      <h4>后端候选 · {plan.action} · Run 剩余额度 {plan.remaining_run_repairs}</h4>
      <p>渲染上限 {plan.max_local_render_calls} 次 · Voice {plan.voice_calls} 次 · Talking {plan.talking_calls} 次 · 外部收费上限 {plan.external_charge_ceiling} · 本机成本 / 主动时间未知</p>
      {(plan.changes ?? []).map(change => <section key={change.scene_id} className="subform"><strong>场景 {change.scene_id}</strong><p>改前：{describe(change.before)}</p><p>改后：{describe(change.after)}</p><p>旁白文案与原音频、来源素材、区间和裁切不由此动作修改。</p></section>)}
      <p>保留主旁白：{retainedMaster ?? "身份未知；后端候选/Run 也未给出，不可确认"}</p>
      <p>保留 Talking/人物来源：{(run.talking_preview_dependencies ?? []).map(item => `${item.scene_plan_id}: series ${item.series_id} / output ${item.output_asset_id ?? "无输出"}`).join(" · ") || "当前 Run 无 Talking 预览绑定"}</p>
      <p>画面选择：{plan.candidate_spec?.edit_plan?.scenes?.map(scene => `${scene.scene_id}: ${scene.selected_asset_id ?? "同一版式/无外部素材"}`).join(" · ") || "以原 VideoSpec 为准"}</p>
      {plan.stop_reasons.length > 0 && <ul>{plan.stop_reasons.map(item => <li className="error" key={item}>{item}</li>)}</ul>}
      {plan.action === "render_revision" && <form className="subform" onSubmit={event => void submitRepair(event)}>
        <label>本次修复理由<input value={repairReason} disabled={busy} onChange={event => setRepairReason(event.target.value)} required maxLength={2000} /></label>
        <button className="button primary" disabled={busy || !repairReason.trim() || !retainedMaster || plan.stop_reasons.length > 0 || plan.remaining_run_repairs <= 0}>按此方案建立新 Render（后台复核）</button>
      </form>}
      {plan.action === "replan" && <p className="muted"><a href="#project-draft-editor">编辑当前稿件/场景方案</a>，保存后<a href="#production-preflight">重新预检整条方案</a>。来源、时间段和裁切需要在规划阶段处理。</p>}
      {plan.action === "stop" && <p className="error">当前不满足修复条件；按上方原因诊断，不重复渲染。</p>}
    </article>}
    {records.map(record => <article className="scene" key={record.id}><h4>呈现修复历史 · 新 Job {record.status}</h4>
      <p>理由：{record.reason} · 前序 Render Job：{record.plan.predecessor_job.id} · 后继 Render Job：{record.replacement_job_id}</p>
      <p>前序主旁白：{record.plan.predecessor_job.payload.video_spec?.master_narration?.audio_asset_id ?? "未知"} · 当前完整审查：{record.final_review_state}</p>
      {record.plan.changes.map(change => <p key={change.scene_id}>场景 {change.scene_id}：{describe(change.before)} → {describe(change.after)}</p>)}
      {record.technical_qa ? <p>技术 QA：{record.technical_qa.state} · 新文件 hash {record.technical_qa.sha256} · {record.technical_qa.width}×{record.technical_qa.height} · {record.technical_qa.duration_ms} ms · {record.technical_qa.scope}</p> : <p>新输出技术检查尚无可用结果。</p>}
      {(record.provider_calls ?? []).map((call, index) => <small key={index}>本地 Render 调用 · {call.status} · 实际外部费用 {call.actual_cost?.amount ?? "未知"}</small>)}
    </article>)}
  </section>;
}
