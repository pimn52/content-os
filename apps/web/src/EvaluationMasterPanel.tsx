import { useEffect, useRef, useState } from "react";
import { AudioAudition } from "./AudioAudition";
import type { ProductionRun } from "./production";
type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type Candidate = { audio_id: string; text: string; duration_ms: number; reviewed_at: string | null; human_approved: boolean; current_review_eligible: boolean; reasons: string[] };
type Receipt = { id: string; project_id: string; revoked_at: string | null; created_at: string; review: { selection: { audio_id: string }; intended_activity: string } };
type Preflight = { project_id: string; audio_id?: string; master_stage_eligible: boolean; whole_run_ready: false; reasons: string[]; downstream_reasons: string[]; fingerprint?: string };
function reasonText(reason: string) {
  const labels: Record<string, string> = {
    evaluation_master_use_admission_required: "缺少该音频用于本方案主旁白的用途准入。",
    evaluation_master_use_admission_revoked: "主旁白用途准入已撤销，当前不可使用。",
    evaluation_master_copy_mismatch: "音频与当前保存的完整文案不一致，请选择匹配音频或重新规划。",
    evaluation_master_human_approval_required: "完整音频的六项人审尚未通过。",
    evaluation_master_use_binding_changed: "用途凭据与当前音频、文案或方案不一致。",
    evaluation_master_proof_changed: "原音频或审核证据发生变化，需要重新核对。",
    evaluation_master_authority_changed: "检查期间素材或权限发生变化，请刷新后重新检查。",
    evaluation_master_downstream_operation_not_supported: "此执行当前只支持主旁白引用。",
    evaluation_master_run_cancelled: "此主旁白执行已取消。",
    evaluation_talking_execution_not_integrated: "人物画面制作尚未接入此评估流程。",
    evaluation_render_execution_not_integrated: "成片渲染尚未接入此评估流程。",
    provider_license_scope_unverified: "后续生成用途许可尚未验证。",
    voice_capability_not_verified: "新语音生成能力尚未验证；已有音频只在自身范围内有效。",
    talking_capability_not_verified: "人物画面生成能力尚未验证。",
    unknown_cost_requires_budget_authorization: "后续制作仍有未批准的未知成本。",
    budget_call_limit: "后续调用已到预算上限。",
  };
  if (reason.startsWith("capture_required:")) return "仍需补充所要求的拍摄素材。";
  if (reason.startsWith("talking_source_not_admitted:")) return "所需人物画面尚未准入。";
  return labels[reason] ?? reason;
}
export function EvaluationMasterPanel({ projectId, draftVersion, scriptRevision, draftDirty, styleTokens, request }: {
  projectId: string; draftVersion: number; scriptRevision: number; draftDirty: boolean; styleTokens: Record<string, string | undefined>; request: Request;
}) {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [runs, setRuns] = useState<ProductionRun[]>([]);
  const [audio, setAudio] = useState("");
  const [receipt, setReceipt] = useState("");
  const [plan, setPlan] = useState<Preflight | null>(null);
  const [run, setRun] = useState<ProductionRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const epoch = useRef(0), locked = useRef(false);
  const identity = `${projectId}:${draftVersion}:${scriptRevision}:${draftDirty}:${JSON.stringify(styleTokens)}`;
  const current = useRef(identity); current.current = identity;
  const base = `/projects/${projectId}/evaluation-master`;
  useEffect(() => {
    const token = ++epoch.current;
    setPlan(null); setAudio(""); setReceipt(""); setRun(null); setCandidates([]); setReceipts([]); setRuns([]);
    setError(""); setBusy(true); locked.current = true;
    Promise.all([request<Candidate[]>(`${base}/candidates`), request<Receipt[]>(`${base}/admissions`), request<ProductionRun[]>(`${base}/runs`)]).then(([assets, grants, saved]) => {
      if (token !== epoch.current) return;
      if (grants.some(g => g.project_id !== projectId) || saved.some(r => r.project_id !== projectId)) throw new Error("项目记录不匹配，已拒绝显示。");
      setCandidates(assets); setReceipts(grants); setRuns(saved);
    }).catch(reason => { if (token === epoch.current) setError(String(reason.message ?? reason)); })
      .finally(() => { if (token === epoch.current) { setBusy(false); locked.current = false; } });
    return () => { ++epoch.current; };
  }, [identity, revision, request]);
  const selection = () => ({ policy: "evaluation-run-voice-master-v1", purpose: "internal_evaluation", audio_id: audio,
    admission_id: receipt || null, style_tokens: styleTokens, talking_review_policy_version: 2 });
  function resetChoice() { ++epoch.current; locked.current = false; setBusy(false); setPlan(null); setError(""); }
  async function act(action: "preflight" | "create" | "reload" | "cancel", selected?: ProductionRun) {
    if (locked.current || (action === "preflight" || action === "create") && draftDirty) return;
    const token = ++epoch.current, expected = identity; locked.current = true; setBusy(true); setError("");
    const active = () => token === epoch.current && current.current === expected;
    try {
      if (action === "preflight") {
        const value = await request<Preflight>(`${base}/preflight`, { method: "POST", body: JSON.stringify(selection()) });
        if (value.project_id !== projectId || value.audio_id && value.audio_id !== audio) throw new Error("预检素材不匹配。");
        if (active()) setPlan(value);
      } else {
        const saved = selected ?? run;
        const path = action === "create" ? `${base}/runs` : `/projects/${projectId}/production-runs/${saved!.id}${action === "cancel" ? "/cancel" : ""}`;
        const value = await request<ProductionRun>(path, action === "reload" ? undefined : { method: "POST",
          ...(action === "create" ? { body: JSON.stringify({ ...selection(), expected_fingerprint: plan!.fingerprint,
            idempotency_key: `web-evaluation-master:${projectId}:${plan!.fingerprint}` }) } : {}) });
        if (value.project_id !== projectId || !value.evaluation_master) throw new Error("执行记录不匹配。");
        if (active()) { setRun(value); setRuns(old => [value, ...old.filter(r => r.id !== value.id)]); setPlan(null); }
      }
    } catch (reason) { if (active()) { setError(String((reason as Error).message ?? reason)); setPlan(null); } }
    finally { if (active()) { setBusy(false); locked.current = false; } }
  }
  const info = run?.evaluation_master;
  return <section aria-label="内部评估主旁白" className="scene">
    <h3>把已审核旁白用于此方案</h3>
    <p>选择与已保存完整文案一致的音频，原样建立主旁白引用。此步骤不生成音频；人物画面和成片仍需后续准入。</p>
    <button type="button" className="button" disabled={busy} onClick={() => setRevision(r => r + 1)}>刷新已保存旁白与执行</button>
    {busy && <p role="status">正在核对当前记录…</p>}
    {error && <p role="alert">{error}</p>}
    {!busy && !candidates.length && <p>本项目暂无已记录人审的旁白。请先在 Voice 草稿中完成技术 QA 与人审。</p>}
    <label>完整旁白<select value={audio} disabled={busy || draftDirty} onChange={e => { resetChoice(); setAudio(e.target.value); setReceipt(""); }}>
      <option value="">选择已保存音频</option>{candidates.map(c => <option key={c.audio_id} value={c.audio_id} disabled={!c.human_approved || !c.current_review_eligible}>
        {c.text} · {(c.duration_ms / 1000).toFixed(2)} 秒 · {c.reviewed_at ?? "审核时间未知"}{!c.human_approved ? " · 人审未通过" : !c.current_review_eligible ? " · 当前不可用" : ""}
      </option>)}
    </select></label>
    <label>主旁白用途准入<select value={receipt} disabled={busy || draftDirty || !audio} onChange={e => { resetChoice(); setReceipt(e.target.value); }}>
      <option value="">选择已采用的本用途凭据</option>{receipts.filter(r => r.review.selection.audio_id === audio).map(r => <option key={r.id} value={r.id} disabled={!!r.revoked_at}>
        {r.review.intended_activity} · {r.created_at}{r.revoked_at ? " · 已撤销" : ""}
      </option>)}
    </select></label>
    {audio && !receipts.some(r => r.review.selection.audio_id === audio && !r.revoked_at) && <p>缺少该音频用于本方案主旁白的已采用用途凭据。已有生成许可或人审通过不授予此用途；需由配置的准入操作员核对并采用。</p>}
    <button type="button" className="button" disabled={busy || draftDirty || !audio} onClick={() => void act("preflight")}>检查主旁白匹配与用途</button>
    {plan && <div><p>{plan.master_stage_eligible ? "主旁白阶段可建立；整条制作尚未就绪。" : "当前不能建立主旁白引用。"}</p>
      <ul>{plan.reasons.map(r => <li key={r}>{reasonText(r)}</li>)}</ul>
      <details><summary>后续制作尚缺条件</summary><ul>{plan.downstream_reasons.map(r => <li key={r}>{reasonText(r)}</li>)}</ul></details>
      <button type="button" className="button primary" disabled={busy || draftDirty || !plan.master_stage_eligible || !plan.fingerprint} onClick={() => void act("create")}>保存方案主旁白引用</button>
    </div>}
    {runs.length > 0 && <label>已保存的主旁白执行<select value={run?.id ?? ""} disabled={busy} onChange={e => { const selected = runs.find(r => r.id === e.target.value); if (selected) void act("reload", selected); }}>
      <option value="">选择并读取当前状态</option>{runs.map(r => <option value={r.id} key={r.id}>{r.created_at ?? "已保存"} · {r.status === "cancelled" ? "已取消" : "主旁白阶段"}</option>)}
    </select></label>}
    {run && info && <article><h4>{run.status === "cancelled" ? "执行已取消" : info.current_eligible ? "原音频已绑定为主旁白" : "历史绑定保留，当前不可继续使用"}</h4>
      <p>整条制作尚未就绪；没有启动人物画面制作或渲染。</p><ul>{run.waiting_stop_reasons.map(r => <li key={r}>{reasonText(r)}</li>)}</ul>
      {info.current_eligible && info.audio_id && <><p>{info.copy} · {Number(info.duration_ms) / 1000} 秒</p>
        <AudioAudition key={`${run.id}:${info.audio_id}`} audioId={info.audio_id} projectId={projectId} request={request} accessBase={`${base}/runs/${run.id}/audio`} /></>}
      <button type="button" className="button" disabled={busy} onClick={() => void act("reload")}>重新核对执行</button>
      <button type="button" className="button" disabled={busy || run.status === "cancelled"} onClick={() => void act("cancel")}>取消此主旁白执行</button>
    </article>}
  </section>;
}
