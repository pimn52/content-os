import { useEffect, useRef, useState } from "react";
import { currentRunReferences, parseBookmark, preflightReasonLabel, presentationStyle, productionKey, productionLabels, startBody, type ProductionPlan, type ProductionRun, type ProductionRunReference } from "./production";
import { ProductionVoicePanel } from "./ProductionVoicePanel";
import { ProductionTalkingPanel } from "./ProductionTalkingPanel";
import { PresentationRecoveryPanel } from "./PresentationRecoveryPanel";
import { AutomaticProductionPanel } from "./AutomaticProductionPanel";
type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
const sourceLabels:Record<string,string>={known_deterministic:"可生成的图解",analyzed_source_interval:"当前视频区间已做机器核验",
  reviewed_source_interval:"已有区间复核",preserved_full_frame:"文件完整，画面仍待核验",reviewed_native_planned_run:"已有可复用人物画面",
  qa_verified_planned_output:"本次人物画面通过技术检查",unknown:"待核验"};
const needLabels:Record<string,string>={none:"可复用",new_talking:"需生成人物画面",capture:"仍需合适素材"};
const burnedLabels:Record<string,string>={present:"片内存在",absent:"区间未见",unknown:"待核验"};
export function ProductionPanel({ projectId, draftVersion, scriptRevision, hasScenes, draftDirty = false, request, confirmAction,topic }: {
  projectId: string; draftVersion: number; scriptRevision: number; hasScenes: boolean; draftDirty?: boolean; request: Request; confirmAction?: (message: string) => boolean;
  topic?:string;
}) {
  const [plan, setPlan] = useState<ProductionPlan | null>(null);
  const [run, setRun] = useState<ProductionRun | null>(null);
  const [references, setReferences] = useState<ProductionRunReference[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [presentation, setPresentation] = useState<"default" | "subject_first" | "editorial" | "comparison_stack">("default");
  const presentationRef = useRef(presentation); presentationRef.current = presentation;
  const epoch = useRef(0);
  const locked = useRef(false);
  const current = useRef({ projectId, draftVersion, scriptRevision });
  current.current = { projectId, draftVersion, scriptRevision };
  useEffect(() => {
    const token = ++epoch.current;
    const scope = { projectId, draftVersion, scriptRevision };
    const active = () => token === epoch.current && JSON.stringify(current.current) === JSON.stringify(scope);
    setPlan(null); setRun(null); setReferences([]); setError(""); setBusy(false); locked.current = false; setPresentation("default");
    let id: string | null = null;
    try { id = parseBookmark(localStorage.getItem(productionKey(projectId)), projectId); }
    catch { setError("无法读取本浏览器执行记录；不会推断后台任务不存在。"); }
    setBusy(true); locked.current = true;
    request<ProductionRunReference[]>(`/projects/${projectId}/production-runs?draft_version=${draftVersion}&script_revision=${scriptRevision}`).then(async values => {
      if (!Array.isArray(values) || values.some(value => value.project_id !== projectId)) throw new Error("后台执行列表与项目不匹配。");
      const available = currentRunReferences(values, projectId, draftVersion, scriptRevision);
      if (active()) setReferences(available);
      const selected = available.find(value => value.id === id) ?? available[0];
      if (!selected) return;
      const value = await request<ProductionRun>(`/projects/${projectId}/production-runs/${selected.id}`);
      if (value.id !== selected.id || value.project_id !== projectId) throw new Error("后台执行记录与项目不匹配。");
      if (active()) setRun(value);
    }).catch(reason => { if (active()) setError(String(reason.message ?? reason)); })
      .finally(() => { if (active()) { setBusy(false); locked.current = false; } });
    return () => { ++epoch.current; };
  }, [projectId, draftVersion, scriptRevision, draftDirty, request]);
  async function selectRun(id: string) {
    if (locked.current) return;
    if (!id) { setRun(null); return; }
    const token = ++epoch.current;
    locked.current = true; setBusy(true); setError("");
    try {
      const value = await request<ProductionRun>(`/projects/${projectId}/production-runs/${id}`);
      if (value.id !== id || value.project_id !== projectId || !references.some(reference => reference.id === id)) throw new Error("执行记录已变化，请刷新项目。");
      if (token === epoch.current) {
        setRun(value);
        try { localStorage.setItem(productionKey(projectId), JSON.stringify({project_id: projectId, id})); }
        catch { setError("执行记录已打开，但本浏览器无法保存恢复引用。"); }
      }
    } catch (reason) { if (token === epoch.current) setError(String((reason as Error).message ?? reason)); }
    finally { if (token === epoch.current) { locked.current = false; setBusy(false); } }
  }
  async function perform(action: "preflight" | "start" | "refresh" | "resume" | "cancel") {
    if (locked.current) return;
    if (draftDirty && ["preflight", "start", "resume"].includes(action)) return;
    const token = epoch.current;
    const scope = { projectId, draftVersion, scriptRevision };
    const inspectedPresentation = presentation;
    const active = () => token === epoch.current && JSON.stringify(current.current) === JSON.stringify(scope) && presentationRef.current === inspectedPresentation;
    locked.current = true; setBusy(true); setError("");
    try {
      if (action === "preflight") {
        const value = await request<ProductionPlan>(`/projects/${projectId}/production-preflight`, { method: "POST", body: JSON.stringify({ style_tokens: presentationStyle(inspectedPresentation) }) });
        if (active() && value.project_id === projectId && value.draft_version === draftVersion && value.script_revision === scriptRevision) setPlan(value);
        else if (active()) setError("草稿版本已变化，请刷新项目后重新预检。");
      } else {
        if (action === "start" && !plan || action !== "start" && !run) return;
        const path = action === "start" ? `/projects/${projectId}/production-runs`
          : `/projects/${projectId}/production-runs/${run!.id}${action === "refresh" ? "" : `/${action}`}`;
        const value = await request<ProductionRun>(path, action === "refresh" ? undefined : {
          method: "POST", ...(action === "start" ? {
            body: JSON.stringify({ ...startBody(plan!), style_tokens: presentationStyle(inspectedPresentation) }) } : {}),
        });
        if (value.project_id !== projectId) throw new Error("后台返回了其他项目的执行记录，已拒绝显示。");
        // Persist a completed action's identity even if the draft changed during its request.
        try { localStorage.setItem(productionKey(projectId), JSON.stringify({ project_id: projectId, id: value.id })); }
        catch { if (active()) setError("后台动作已完成，但浏览器无法保存恢复引用；请勿因存储失败重复启动。"); }
        if (active()) {
          setRun(value);
          if (action === "start") {
            setReferences(previous => [{id: value.id, project_id: projectId, draft_version: draftVersion,
              script_revision: scriptRevision, stage: value.status, created_at: value.created_at ?? new Date().toISOString()},
              ...previous.filter(reference => reference.id !== value.id)]);
          }
          if (action === "cancel") setReferences(previous => previous.filter(reference => reference.id !== value.id));
        }
      }
    } catch (reason) {
      if (active()) { setError(reason instanceof Error ? reason.message : "动作失败；请刷新状态后决定是否重试。"); if (action === "preflight") setPlan(null); }
    } finally { if (active()) { setBusy(false); locked.current = false; } }
  }
  return <section className="cost-panel" aria-label="整条方案与执行">
    {topic!==undefined&&<AutomaticProductionPanel projectId={projectId} topic={topic} request={request}/>}
    <details open={topic===undefined}><summary>手动编辑与执行记录</summary>
    <div id="production-preflight" className="row-between"><div><span className="kicker">PLAN → PRODUCTION</span><h3>整条方案与执行</h3></div><button type="button" className="button" disabled={busy || !hasScenes || draftDirty} onClick={() => void perform("preflight")}>预检整条方案</button></div>
    {references.length > 0 && <label>当前草稿的已有执行<select value={run?.id ?? ""} disabled={busy} onChange={event => void selectRun(event.target.value)}>
      <option value="">选择执行记录</option>{references.map(value => <option key={value.id} value={value.id}>{new Date(value.created_at).toLocaleString()} · {value.stage}</option>)}
    </select></label>}
    {draftDirty && <p role="status">草稿有未保存的内容或画面计划。请先保存，旧预检不能用于新执行。</p>}
    <p className="muted">开始前检查素材、旁白、人物参考与预算，沿用已登记的授权。自动制作完成后查看整片，有问题再修改。</p>
    <label>新执行的成片画面呈现<select value={presentation} disabled={busy || !!run && run.status !== "cancelled"} onChange={event => { ++epoch.current; locked.current = false; setBusy(false); setPresentation(event.target.value as "default" | "subject_first" | "editorial" | "comparison_stack"); setPlan(null); setError(""); }}><option value="default">现有默认布局</option><option value="subject_first">人物优先 · 大画面、紧凑可选文字</option><option value="comparison_stack">左右对比横屏素材 → 上下排布</option><option value="editorial">语义标题排版 · 需已保存的场景图文计划</option></select></label>
    <small>“左右对比 → 上下排布”只适用于 16:9 横屏、左右各一幅且底部烧录字幕的对比素材；它保留两侧标签、裁掉底部字幕条，并使用旁白时间轴字幕。其他授权横向 B-roll 沿用现有人物优先整帧面板。下沿画面可能被裁切，需检查短片实际效果；全屏竖屏素材仍是首选。选择变化后须重新预检；已建立执行保留原样式。</small>
    {error && <p role="alert" className="error">{error}</p>}
    {plan && <>
      <div className="cost-total"><strong>{plan.cost_estimate.known_amount} {plan.cost_estimate.currency}</strong><span>整条方案已知金额；另有 {plan.cost_estimate.unknown_cost_count} 项未知</span></div>
      <p>本地耗时：{plan.estimated_local_runtime_ms == null ? "未知" : `${plan.estimated_local_runtime_ms} ms`} · 用户主动时间：{plan.estimated_user_active_minutes == null ? "未知" : `${plan.estimated_user_active_minutes} 分钟`}</p>
      <p className="muted">检查状态：{plan.status === "ready_for_authorized_dispatch" ? "可开始" : "尚有未解决项"}。素材检查结果与整片效果分别记录。</p>
      <ul>{plan.stop_reasons.map(reason => <li key={reason} className="error">{preflightReasonLabel(reason)}</li>)}</ul><p>授权检查：{plan.authorization_needs.join(" · ") || "无新增项"}</p>
      <div className="cost-lines">{plan.actions.map((action, index) => <div key={index} className="cost-line"><span>{action.required ? "必要" : "条件/预留"} · {action.kind} · {action.reason}</span><span>{action.cost.amount == null ? "成本未知" : `${action.cost.amount} ${action.cost.currency ?? ""}`}</span></div>)}</div>
      <div className="scene-list">{plan.scenes.map(scene => <article className="scene" key={scene.scene_plan_id}><strong>{scene.scene_id}</strong><p>画面：{needLabels[scene.production_need] ?? "待检查"} · {sourceLabels[scene.suitability] ?? "待核验"}</p><p>新字幕：{scene.subtitle_treatment === "timed_captions" ? "配合本次旁白" : "不叠加"} · 原字幕：{burnedLabels[scene.burned_in_subtitles] ?? "待核验"}</p><small>{scene.reasons.join(" · ")}</small></article>)}</div>
      <button type="button" className="button primary" disabled={busy || draftDirty || !!run && run.status !== "cancelled"} onClick={() => void perform("start")}>保存 / 启动此方案（后端校验）</button>
    </>}
    {run && <article className="scene" aria-label="当前执行"><h3>{run.review_state === "rejected" ? "成片已拒绝，需要修改" : run.review_state === "approved" ? "成片已接受" : productionLabels[run.status] ?? run.status}</h3><p>人物画面需求：{run.talking_dependencies.map(item => item.scene_id).join("、") || "无"}</p>
      {run.review_state && run.review_state !== "pending" && <p className={run.review_state === "rejected" ? "error" : "ok"}>成片审核：{run.review_state === "rejected" ? "用户已拒绝，需要修改" : "用户已接受"}。{run.review_notes}</p>}
      <ul>{[...new Set([...run.waiting_stop_reasons, ...run.voice_qa_auto_stop_reasons])].map(reason => <li key={reason} className="error">{preflightReasonLabel(reason)}</li>)}</ul><div className="actions">
        <button type="button" className="button" disabled={busy} onClick={() => void perform("refresh")}>刷新执行状态</button>
        <button type="button" className="button" disabled={busy || draftDirty || run.status === "cancelled"} onClick={() => void perform("resume")}>续接当前执行</button>
        <button type="button" className="button" disabled={busy || run.status === "cancelled"} onClick={() => void perform("cancel")}>取消执行（后端校验）</button></div>
      <details><summary>诊断引用</summary><p>执行：{run.id}</p><p>状态：{run.status}</p></details></article>}
    {run && run.status !== "cancelled" && <ProductionVoicePanel key={`${projectId}:${draftVersion}:${scriptRevision}:${run.id}`} projectId={projectId} draftVersion={draftVersion} scriptRevision={scriptRevision} run={run} request={request} confirmAction={confirmAction} onRunChange={setRun} />}
    {run && run.talking_dependencies?.length > 0 && <ProductionTalkingPanel key={`${projectId}:${draftVersion}:${scriptRevision}:${run.id}:${run.preflight_fingerprint}`} projectId={projectId} run={run} request={request} confirmAction={confirmAction} onRunChange={setRun} />}
    {run && run.render_job_id && <PresentationRecoveryPanel key={`${projectId}:${draftVersion}:${scriptRevision}:${run.id}:${run.render_job_id}:${run.status}`} projectId={projectId} run={run} request={request} confirmAction={confirmAction} onRunChange={setRun} />}
    <small className="muted">执行记录从后台恢复。失败不自动重试；渲染完成后审核整片。</small>
    </details>
  </section>;
}
