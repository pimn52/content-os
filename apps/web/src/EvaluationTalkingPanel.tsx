import { useEffect, useRef, useState } from "react";
import { EvaluationTalkingPreview } from "./EvaluationTalkingPreview";
import { EvaluationTalkingReview } from "./EvaluationTalkingReview";
type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type Candidate = { capability_profile_id: string; provider: string; model: string; readiness: string; quality_status: string;
  verified_duration_ms: number | null; calibration_grant_available: boolean };
type Selection = { master_run_id: string; scene_plan_id: string; talking_profile_id: string; reference_clip_id: string;
  source_admission_id: string; capability_profile_id: string; execution_admission_id: string; authorization_reference: string;
  brief: Record<string, unknown>; policy: "evaluation-talking-run-v1" | "evaluation-talking-calibration-v1"; purpose: "internal_evaluation"; use_admission_id: string | null;
  trial_duration_limit_ms?: number; trial_processing_limit_ms?: number };
type Receipt = { id: string; project_id: string; revoked_at: string | null; created_at: string;
  review: { selection: Selection; intended_activity: string; proof: { profile?: { name: string };
    scene?: { scene_id?: string; voice_text?: string }; source_asset?: { width: number; height: number; content_hash: string };
    master_start_ms?: number; master_end_ms?: number } } };
type Operation = { id: string; project_id: string; master_run_id: string; scene_plan_id: string; state: string; created_at: string;
  current_eligible: boolean; reasons: string[]; stages: Record<string, string>; use_blockers: string[]; talking_run_id: string | null; review_policy_version: 2;
  policy?: string; quality_observation?: { scope: "exact_sample_only"; observed_pass_duration_ms: number } | null };
type Preflight = { policy: string; purpose: string; eligible: boolean; reasons: string[]; fingerprint?: string; request_sha256?: string;
  budget_state?: string; estimated_cost?: { amount: string | null; currency: string | null } };
const labels: Record<string, string> = { generation: "人物生成", qa: "技术 QA", preview: "精确旁白预览", pending: "已排队", running: "处理中", completed: "已完成", failed: "失败", cancelled: "已取消", not_created: "尚未创建",
  in_progress: "处理中／等待执行", blocked: "当前受阻", awaiting_review: "等待精确预览人审", admitted: "TalkingRun 已准入" };
const budgetReasons: Record<string,string> = { budget_unknown_cost: "人物生成成本未知，预算未允许未知成本。", budget_existing_unknown_cost: "已有调用成本尚未核对，当前预算阻止继续。",
  budget_call_limit: "人物生成将超过当前调用预算。", budget_amount_limit: "人物生成将超过当前金额预算。", budget_currency_mismatch: "估价币种与预算不一致。" };
const calibrationReasons: Record<string,string> = { talking_calibration_configuration_missing: "人物候选尚缺有效的本地配置证据。", talking_calibration_trial_binding_changed: "当前输入、配置或试验边界与采用的授权不一致，需要重新核对。", talking_calibration_processing_bound_exceeded: "完整片段及对齐所需区间超过本次试验处理上限。", talking_calibration_grant_spent: "本次试验授权已绑定原操作，请读取原记录核对结果。", talking_calibration_claim_changed: "一次试验的持久化授权记录不一致，当前操作已阻止。", talking_calibration_configuration_changed: "已保存样本对应的配置或权限已变化，当前复用受阻。", talking_execution_policy_mismatch: "当前执行端策略与这次人物操作不一致。" };
const candidateLabels: Record<string,string> = { configured: "已配置", available: "可用", verified: "已验证", implemented: "已实现", unknown: "未知", observed: "已有观察" };

export function EvaluationTalkingPanel({ projectId, draftVersion, scriptRevision, draftDirty, styleTokens, request }: {
  projectId: string; draftVersion: number; scriptRevision: number; draftDirty: boolean; styleTokens: Record<string, string | undefined>; request: Request;
}) {
  const [receipts, setReceipts] = useState<Receipt[]>([]), [operations, setOperations] = useState<Operation[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [receiptId, setReceiptId] = useState(""), [operation, setOperation] = useState<Operation | null>(null);
  const [plan, setPlan] = useState<Preflight | null>(null), [error, setError] = useState("");
  const [busy, setBusy] = useState(false), [revision, setRevision] = useState(0), [readRevision, setReadRevision] = useState(0);
  const epoch = useRef(0), locked = useRef(false);
  const identity = `${projectId}:${draftVersion}:${scriptRevision}:${draftDirty}:${JSON.stringify(styleTokens)}`;
  const current = useRef(identity); current.current = identity;
  const base = `/projects/${projectId}/evaluation-talking`;
  const selected = receipts.find(r => r.id === receiptId && !r.revoked_at);
  useEffect(() => {
    const token = ++epoch.current;
    setReceipts([]); setOperations([]); setCandidates([]); setReceiptId(""); setOperation(null); setPlan(null); setError("");
    locked.current = true; setBusy(true);
    Promise.all([request<Receipt[]>(`${base}/admissions`), request<Operation[]>(`${base}/operations`), request<Candidate[]>(`${base}/candidates`)]).then(([grants,saved,available]) => {
      if (token !== epoch.current) return;
      if (grants.some(r => r.project_id !== projectId) || saved.some(r => r.project_id !== projectId)) throw new Error("后台返回了其他项目的记录，已拒绝显示。");
      setReceipts(grants); setOperations(saved); setCandidates(available ?? []);
    }).catch(reason => { if (token === epoch.current) setError(String(reason.message ?? reason)); })
      .finally(() => { if (token === epoch.current) { locked.current = false; setBusy(false); } });
    return () => { ++epoch.current; };
  }, [identity, revision, request]);
  function choose(value: string) { ++epoch.current; locked.current = false; setBusy(false); setReceiptId(value); setPlan(null); setOperation(null); setError(""); }
  async function act(action: "preflight" | "create" | "reload" | "qa" | "preview" | "cancel", saved = operation) {
    if (locked.current || draftDirty && (action === "preflight" || action === "create")) return;
    if ((action === "preflight" || action === "create") && !selected) return;
    if (action === "create" && (!plan?.eligible || !plan.fingerprint)) return;
    if (!["preflight","create"].includes(action) && !saved) return;
    const token = ++epoch.current, expected = identity;
    const active = () => token === epoch.current && expected === current.current;
    locked.current = true; setBusy(true); setError("");
    // The list retains history; a failed current read must not leave the old
    // operation's controls or decoded preview as current authority.
    if (action !== "preflight") setOperation(null);
    try {
      if (action === "preflight") {
        const value = await request<Preflight>(`${base}/preflight`, { method: "POST", body: JSON.stringify({ ...selected!.review.selection, use_admission_id: selected!.id }) });
        if (value.policy !== selected!.review.selection.policy || !["evaluation-talking-run-v1","evaluation-talking-calibration-v1"].includes(value.policy) || value.purpose !== "internal_evaluation") throw new Error("预检用途与当前选择不一致。");
        if (active()) setPlan(value);
      } else {
        let id = saved?.id;
        if (action === "create") {
          const value = await request<{ id: string; project_id: string }>(`${base}/operations`, { method: "POST", body: JSON.stringify({ ...selected!.review.selection, use_admission_id: selected!.id,
            expected_fingerprint: plan!.fingerprint, idempotency_key: `web-evaluation-talking:${projectId}:${plan!.fingerprint}` }) });
          if (value.project_id !== projectId) throw new Error("创建结果与当前项目不一致。");
          id = value.id;
        } else if (action !== "reload") await request(`${base}/operations/${id}/${action}`, { method: "POST" });
        if (!active()) return;
        const value = await request<Operation>(`${base}/operations/${id}`);
        if (value.project_id !== projectId || value.id !== id || value.review_policy_version !== 2) throw new Error("执行记录与当前范围不一致。");
        if (active()) { setOperation(value); setOperations(old => [value,...old.filter(r => r.id !== value.id)]); setPlan(null); setReadRevision(v => v + 1); }
      }
    } catch (reason) { if (active()) { setError(String((reason as Error).message ?? reason)); setPlan(null); } }
    finally { if (active()) { locked.current = false; setBusy(false); } }
  }
  const canAdvance = operation?.current_eligible && !["failed","cancelled","blocked"].includes(operation.state);
  return <section aria-label="内部评估人物制作" className="scene">
    <h3>为已保存方案制作人物画面</h3>
    <p>选择已采用的人物素材、主旁白和配置用途组合。预检会重新核对当前方案、授权和能力；保存只建立一次人物制作操作。</p>
    <button type="button" className="button" disabled={busy} onClick={() => setRevision(v => v + 1)}>刷新人物用途与已保存操作</button>
    {busy && <p role="status">正在核对记录…</p>}{error && <p role="alert">{error}</p>}
    {candidates.filter(c => c.quality_status !== "verified" || c.verified_duration_ms == null).map(c => <p key={c.capability_profile_id}>{c.provider} / {c.model}：配置状态 {candidateLabels[c.readiness] ?? c.readiness}，质量 {candidateLabels[c.quality_status] ?? c.quality_status}。需要一次有界配置验证；{c.calibration_grant_available ? "已有采用的校准用途组合，可选择后预检。" : "尚无采用的校准用途组合，不能开始生成。"} 一般时长能力未验证。</p>)}
    <label>已采用的人物制作用途<select value={receiptId} disabled={busy || draftDirty} onChange={e => choose(e.target.value)}>
      <option value="">选择精确用途组合</option>{receipts.map(r => <option key={r.id} value={r.id} disabled={!!r.revoked_at}>{r.review.intended_activity} · {r.created_at}{r.revoked_at ? " · 已撤销" : ""}</option>)}
    </select></label>
    {!busy && !receipts.some(r => !r.revoked_at) && <p>本项目尚无已采用的人物制作用途组合。需由准入操作员核对精确输入、输出用途及执行许可；旁白人审或原生成许可不能代替它。</p>}
    {selected?.review.selection.policy === "evaluation-talking-calibration-v1" && <p>已采用一次有界配置验证授权。试验旁白上限 {selected.review.selection.trial_duration_limit_ms} ms，处理区间上限 {selected.review.selection.trial_processing_limit_ms} ms，最多一次生成。候选的一般质量与时长能力仍未验证；同一输出须通过技术 QA 和六项精确预览人审后才能准入。</p>}
    {selected && <details><summary>已绑定的素材与用途证据</summary><p>{selected.review.intended_activity}</p>
      <p>人物：{selected.review.proof.profile?.name ?? "名称未记录"} · 场景：{selected.review.proof.scene?.scene_id ?? "已绑定场景"}</p>
      <p>{selected.review.proof.scene?.voice_text ?? ""} · 旁白区间：{selected.review.proof.master_start_ms ?? "未知"}–{selected.review.proof.master_end_ms ?? "未知"} ms</p>
      <p>来源画面：{selected.review.proof.source_asset?.width ?? "未知"} × {selected.review.proof.source_asset?.height ?? "未知"}</p>
    </details>}
    {draftDirty && <p>请先保存文案和方案，再检查人物制作。</p>}
    <button type="button" className="button" disabled={busy || draftDirty || !selected} onClick={() => void act("preflight")}>检查人物制作准入</button>
    {plan && <div><p>{plan.eligible ? "此用途预检通过。执行仍须由已配置的专用 Worker 核对。" : "当前不能创建人物制作。"}</p><ul>{plan.reasons.map(r => <li key={r}>{calibrationReasons[r] ?? budgetReasons[r] ?? r}</li>)}</ul>
      {plan.estimated_cost && <p>生成估价：{plan.estimated_cost.amount == null ? "未知" : `${plan.estimated_cost.amount} ${plan.estimated_cost.currency ?? ""}`}。预检未占用额度，执行时会重新检查并记账。</p>}
      {plan.budget_state === "existing_operation_no_new_reservation" && <p>已存在相同操作，将读取原操作，不重新占用生成额度。</p>}
      <button type="button" className="button primary" disabled={busy || draftDirty || !plan.eligible || !plan.fingerprint} onClick={() => void act("create")}>保存人物制作操作</button></div>}
    {!!operations.length && <label>已保存的人物制作<select value={operation?.id ?? ""} disabled={busy} onChange={e => { const saved = operations.find(r => r.id === e.target.value); if (saved) void act("reload",saved); }}>
      <option value="">选择并读取当前状态</option>{operations.map(r => <option key={r.id} value={r.id}>{r.created_at} · {labels[r.state] ?? r.state}</option>)}
    </select></label>}
    {operation && <article><h4>{labels[operation.state] ?? operation.state}</h4>
      {operation.policy === "evaluation-talking-calibration-v1" && <p>{operation.quality_observation ? `精确样本已通过，观察时长 ${operation.quality_observation.observed_pass_duration_ms} ms；同一 TalkingRun 可在既有用途范围内复用。` : "校准结果尚未形成已准入的精确质量观察。"} 一般质量与时长范围仍未验证，最终成片就绪状态需另行核对。</p>}
      <p>{Object.entries(operation.stages).map(([role,state]) => `${labels[role] ?? role}：${labels[state] ?? state}`).join(" · ")}</p>
      <ul>{[...operation.reasons,...operation.use_blockers].map(r => <li key={r}>{calibrationReasons[r] ?? r}</li>)}</ul>
      <p>失败不会自动重试；刷新只核对已有记录。人物预览通过也不等于最终成片可发布。</p>
      <button type="button" className="button" disabled={busy} onClick={() => void act("reload")}>重新核对人物操作</button>
      <button type="button" className="button" disabled={busy || !canAdvance || operation.stages.generation !== "completed" || operation.stages.qa !== "not_created"} onClick={() => void act("qa")}>提交人物技术 QA</button>
      <button type="button" className="button" disabled={busy || !canAdvance || operation.stages.qa !== "completed" || operation.stages.preview !== "not_created"} onClick={() => void act("preview")}>提交精确旁白预览</button>
      <button type="button" className="button" disabled={busy || operation.state === "cancelled"} onClick={() => void act("cancel")}>取消人物操作</button>
      {operation.stages.preview === "completed" && <EvaluationTalkingPreview key={`${operation.id}:${readRevision}`} projectId={projectId} operationId={operation.id} revision={`${identity}:${readRevision}`} request={request} />}
      {operation.stages.preview === "completed" && <EvaluationTalkingReview key={`review:${operation.id}:${readRevision}`} projectId={projectId} operationId={operation.id} revision={`${identity}:${readRevision}`} request={request} onChange={() => void act("reload")} />}
    </article>}
  </section>;
}
