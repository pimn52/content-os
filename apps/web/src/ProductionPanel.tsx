import { useEffect, useRef, useState } from "react";
import { parseBookmark, presentationStyle, productionKey, productionLabels, startBody, type ProductionPlan, type ProductionRun } from "./production";
import { ProductionVoicePanel } from "./ProductionVoicePanel";
import { ProductionTalkingPanel } from "./ProductionTalkingPanel";
import { PresentationRecoveryPanel } from "./PresentationRecoveryPanel";
import { EvaluationMasterPanel } from "./EvaluationMasterPanel";
import { EvaluationTalkingPanel } from "./EvaluationTalkingPanel";
import { EvaluationCompositionPanel } from "./EvaluationCompositionPanel";
import { commercialChoice, ExecutionPurposeControl, type ExecutionChoice } from "./ExecutionPurposeControl";
type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
export function ProductionPanel({ projectId, draftVersion, scriptRevision, hasScenes, draftDirty = false, request, confirmAction }: {
  projectId: string; draftVersion: number; scriptRevision: number; hasScenes: boolean; draftDirty?: boolean; request: Request; confirmAction?: (message: string) => boolean;
}) {
  const [plan, setPlan] = useState<ProductionPlan | null>(null);
  const [run, setRun] = useState<ProductionRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [presentation, setPresentation] = useState<"default" | "subject_first">("default");
  const [execution, setExecution] = useState<ExecutionChoice>(commercialChoice);
  const executionRef = useRef(execution); executionRef.current = execution;
  const presentationRef = useRef(presentation); presentationRef.current = presentation;
  const epoch = useRef(0);
  const locked = useRef(false);
  const current = useRef({ projectId, draftVersion, scriptRevision });
  current.current = { projectId, draftVersion, scriptRevision };
  useEffect(() => {
    const token = ++epoch.current;
    const scope = { projectId, draftVersion, scriptRevision };
    const active = () => token === epoch.current && JSON.stringify(current.current) === JSON.stringify(scope);
    setPlan(null); setRun(null); setError(""); setBusy(false); locked.current = false; setPresentation("default"); setExecution(commercialChoice());
    let id: string | null = null;
    try { id = parseBookmark(localStorage.getItem(productionKey(projectId)), projectId); }
    catch { setError("无法读取本浏览器执行记录；不会推断后台任务不存在。"); }
    if (id) {
      setBusy(true); locked.current = true;
      request<ProductionRun>(`/projects/${projectId}/production-runs/${id}`).then(value => {
        if (active()) {
          if (value.project_id !== projectId) throw new Error("后台返回了其他项目的执行记录，已拒绝显示。");
          if(value.evaluation_composition){
            const purpose: ExecutionChoice={purpose:"internal_evaluation",use_admission_ids:[]};
            executionRef.current=purpose;setExecution(purpose);
          }
          setRun(value);
        }
      }).catch(reason => { if (active()) setError(String(reason.message ?? reason)); })
        .finally(() => { if (active()) { setBusy(false); locked.current = false; } });
    }
    return () => { ++epoch.current; };
  }, [projectId, draftVersion, scriptRevision, draftDirty, request]);
  async function perform(action: "preflight" | "start" | "refresh" | "resume" | "cancel") {
    if (locked.current) return;
    if (action === "start" && execution.purpose === "internal_evaluation" && (!plan?.retained_media || plan.status !== "ready_for_authorized_dispatch")) return;
    if (draftDirty && ["preflight", "start", "resume"].includes(action)) return;
    const token = epoch.current;
    const scope = { projectId, draftVersion, scriptRevision };
    const inspectedPresentation = presentation;
    const inspectedExecution = execution;
    const active = () => token === epoch.current && JSON.stringify(current.current) === JSON.stringify(scope) && presentationRef.current === inspectedPresentation && JSON.stringify(executionRef.current) === JSON.stringify(inspectedExecution);
    locked.current = true; setBusy(true); setError("");
    try {
      if (action === "preflight") {
        const value = await request<ProductionPlan>(`/projects/${projectId}/production-preflight`, { method: "POST", body: JSON.stringify({ style_tokens: presentationStyle(inspectedPresentation), ...inspectedExecution }) });
        if (active() && value.project_id === projectId && value.draft_version === draftVersion && value.script_revision === scriptRevision) setPlan(value);
        else if (active()) setError("草稿版本已变化，请刷新项目后重新预检。");
      } else {
        if (action === "start" && !plan || action !== "start" && !run) return;
        const path = action === "start" ? `/projects/${projectId}/production-runs`
          : `/projects/${projectId}/production-runs/${run!.id}${action === "refresh" ? "" : `/${action}`}`;
        const value = await request<ProductionRun>(path, action === "refresh" ? undefined : {
          method: "POST", ...(action === "start" ? { body: JSON.stringify({ ...startBody(plan!, 2), style_tokens: presentationStyle(inspectedPresentation), ...inspectedExecution }) } : {}),
        });
        if (value.project_id !== projectId) throw new Error("后台返回了其他项目的执行记录，已拒绝显示。");
        // Persist a completed action's identity even if the draft changed during its request.
        try { localStorage.setItem(productionKey(projectId), JSON.stringify({ project_id: projectId, id: value.id })); }
        catch { if (active()) setError("后台动作已完成，但浏览器无法保存恢复引用；请勿因存储失败重复启动。"); }
        if (active()) setRun(value);
      }
    } catch (reason) {
      if (active()) { setError(reason instanceof Error ? reason.message : "动作失败；请刷新状态后决定是否重试。"); if (action === "preflight") setPlan(null); }
    } finally { if (active()) { setBusy(false); locked.current = false; } }
  }
  return <section className="cost-panel" aria-label="整条方案与执行">
    <div id="production-preflight" className="row-between"><div><span className="kicker">PLAN → PRODUCTION</span><h3>整条方案与执行</h3></div><button type="button" className="button" disabled={busy || !hasScenes || draftDirty} onClick={() => void perform("preflight")}>预检整条方案</button></div>
    {draftDirty && <p role="status">草稿有未保存的内容或画面计划。请先保存，旧预检不能用于新执行。</p>}
    <p className="muted">只读预检不派发模型。保存/启动由后端判断，可能仅进入等待，不代表批准成本、素材或审核。Voice 与 Talking 各自在此 Run 下有明确的配置、执行与审核步骤；每次写操作仍由后台复核。</p>
    <ExecutionPurposeControl lane="existing-master" key={projectId} projectId={projectId} value={execution} request={request} disabled={busy || !!run && run.status !== "cancelled"} onChange={value => {
      executionRef.current = value; setExecution(value); ++epoch.current; setPlan(null); setError(""); setBusy(false); locked.current = false;
    }} />
    <label>新执行的成片人物呈现<select value={presentation} disabled={busy || !!run && run.status !== "cancelled"} onChange={event => { ++epoch.current; locked.current = false; setBusy(false); setPresentation(event.target.value as "default" | "subject_first"); setPlan(null); setError(""); }}><option value="default">现有默认布局</option><option value="subject_first">人物优先 · 大画面、紧凑可选文字</option></select></label>
    <small>构图合适的全屏竖屏画面仍是首选。人物优先适用于已准入 Talking 的完整画面 contain，方形或宽视口是素材受限时的折中；横向素材仍可能留白，先在素材比例适配中评估可用裁剪。它不自动裁切、不新增人物审核，也不能保证成片达标。选择变化后须重新预检；已建立执行保留原样式。</small>
    {execution.purpose === "internal_evaluation" && plan && !plan.retained_media && <EvaluationMasterPanel projectId={projectId} draftVersion={draftVersion} scriptRevision={scriptRevision} draftDirty={draftDirty} styleTokens={presentationStyle(presentation)} request={request} />}
    {execution.purpose === "internal_evaluation" && plan && !plan.retained_media && <EvaluationTalkingPanel projectId={projectId} draftVersion={draftVersion} scriptRevision={scriptRevision} draftDirty={draftDirty} styleTokens={presentationStyle(presentation)} request={request} />}
    {execution.purpose === "internal_evaluation" && <EvaluationCompositionPanel projectId={projectId} draftVersion={draftVersion} scriptRevision={scriptRevision} draftDirty={draftDirty} request={request} onAdopt={() => {++epoch.current;setPlan(null);locked.current=false;setBusy(false);}} enableGenerationRecovery={!!plan && !plan.retained_media} />}
    {error && <p role="alert" className="error">{error}</p>}
    {plan && <>
      <div className="cost-total"><strong>{plan.cost_estimate.known_amount} {plan.cost_estimate.currency}</strong><span>整条方案已知金额；另有 {plan.cost_estimate.unknown_cost_count} 项未知</span></div>
      <p>本地耗时：{plan.estimated_local_runtime_ms == null ? "未知" : `${plan.estimated_local_runtime_ms} ms`} · 用户主动时间：{plan.estimated_user_active_minutes == null ? "未知" : `${plan.estimated_user_active_minutes} 分钟`}</p>
      <p className="muted">预检状态：{plan.status} · 证据等级：{plan.evidence_level}。元数据/fixture 或已审核资产不等于新主题成片验收。</p>
      <ul>{plan.stop_reasons.map(reason => <li key={reason} className="error">{reason}</li>)}</ul><p>授权检查：{plan.authorization_needs.join(" · ") || "无新增项"}</p>
      <div className="cost-lines">{plan.actions.map((action, index) => <div key={index} className="cost-line"><span>{action.required ? "必要" : "条件/预留"} · {action.kind} · {action.reason}</span><span>{action.cost.amount == null ? "成本未知" : `${action.cost.amount} ${action.cost.currency ?? ""}`}</span></div>)}</div>
      <div className="scene-list">{plan.scenes.map(scene => <article className="scene" key={scene.scene_plan_id}><strong>{scene.scene_id}</strong><p>生产需求：{scene.production_need} · 适用性：{scene.suitability}</p><p>字幕：{scene.subtitle_treatment} · 原字幕：{scene.burned_in_subtitles}</p><small>{scene.reasons.join(" · ")}</small></article>)}</div>
      <button type="button" className="button primary" disabled={busy || draftDirty || execution.purpose === "internal_evaluation" && (!plan.retained_media || plan.status !== "ready_for_authorized_dispatch") || !!run && run.status !== "cancelled"} onClick={() => void perform("start")}>保存 / 启动此方案（后端校验）</button>
    </>}
    {run && <article className="scene" aria-label="当前执行"><h3>{productionLabels[run.status] ?? run.status}</h3><p>人物画面需求：{run.talking_dependencies.map(item => item.scene_id).join("、") || "无"}</p>
      <ul>{[...new Set([...run.waiting_stop_reasons, ...run.voice_qa_auto_stop_reasons])].map(reason => <li key={reason} className="error">{reason}</li>)}</ul><div className="actions">
        <button type="button" className="button" disabled={busy} onClick={() => void perform("refresh")}>刷新执行状态</button>
        <button type="button" className="button" disabled={busy || draftDirty || run.status === "cancelled"} onClick={() => void perform("resume")}>{run.evaluation_composition ? "核对已有成片（不重新渲染）" : "续接已批准依赖（后端校验）"}</button>
        <button type="button" className="button" disabled={busy || run.status === "cancelled"} onClick={() => void perform("cancel")}>取消执行（后端校验）</button></div>
      <details><summary>诊断引用</summary><p>执行：{run.id}</p><p>状态：{run.status}</p></details></article>}
    {run?.evaluation_composition && <article aria-label="本地非商业成片">
      <p>已审核媒体复用；本地非商业用途。渲染完成仍需成片审核。</p>
      {run.evaluation_composition.reasons.map(reason => <p key={reason} className="error">{reason}</p>)}
      {run.evaluation_composition.state === "awaiting_u_product" && run.evaluation_composition.current_eligible &&
        <video controls preload="metadata" src={`/projects/${projectId}/renders/${run.evaluation_composition.render_id}?purpose=internal_evaluation`} style={{maxWidth:360,width:"100%"}} />}
    </article>}
    {run && !run.evaluation_composition && run.status !== "cancelled" && <ProductionVoicePanel key={`${projectId}:${draftVersion}:${scriptRevision}:${run.id}`} projectId={projectId} draftVersion={draftVersion} scriptRevision={scriptRevision} run={run} request={request} confirmAction={confirmAction} onRunChange={setRun} />}
    {run && run.talking_dependencies?.length > 0 && <ProductionTalkingPanel key={`${projectId}:${draftVersion}:${scriptRevision}:${run.id}:${run.preflight_fingerprint}`} projectId={projectId} run={run} request={request} confirmAction={confirmAction} onRunChange={setRun} />}
    {run && !run.evaluation_composition && run.render_job_id && <PresentationRecoveryPanel key={`${projectId}:${draftVersion}:${scriptRevision}:${run.id}:${run.render_job_id}:${run.status}`} projectId={projectId} run={run} request={request} confirmAction={confirmAction} onRunChange={setRun} />}
    <small className="muted">只恢复本浏览器保存的执行引用；换设备或清除存储后尚无后台执行列表。失败后不自动重试、不换供应商、不跳过人审；渲染完成仍待最终审查。</small>
  </section>;
}
