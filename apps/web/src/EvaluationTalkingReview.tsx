import { useEffect, useRef, useState, type FormEvent } from "react";
type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
const dimensions = { visible_sync: "口型与声音同步", identity: "人物身份与外观", artifacts: "画面与声音伪影", source_performance: "来源表演与语义", continuity: "整段连续性", publishability: "人物片段可用性" };
type Dimension = keyof typeof dimensions;
type Concern = { id: string; finding: { dimension: Dimension; reason: string }; evidence_reference: string; answer: null | { approved: boolean; reason: string } };
type Review = { preview_asset_id: string | null; preview_sha256: string | null; planned_origin_sha256: string | null;
  review_policy_version: number; readiness: string; blockers: string[]; continuity_review_state: string;
  continuity_review_evidence_reference: string | null; review_concerns: Concern[] };

export function EvaluationTalkingReview({ projectId, operationId, revision, request, onChange }: {
  projectId: string; operationId: string; revision: string; request: Request; onChange: () => void;
}) {
  const [review, setReview] = useState<Review | null>(null), [error, setError] = useState("");
  const [busy, setBusy] = useState(false), [version, setVersion] = useState(0);
  const epoch = useRef(0), locked = useRef(false);
  const concernKeys = useRef(new Map<string, string>());
  const identity = `${projectId}:${operationId}:${revision}`, current = useRef(identity); current.current = identity;
  const base = `/projects/${projectId}/evaluation-talking/operations/${operationId}`;
  useEffect(() => {
    const token = ++epoch.current;
    setReview(null); setError(""); setBusy(true); locked.current = true;
    request<Review>(`${base}/review`).then(value => {
      if (token !== epoch.current) return;
      if (value.review_policy_version !== 2 || !value.preview_asset_id || !value.preview_sha256 || !value.planned_origin_sha256) throw new Error("当前精确预览评审材料不完整。");
      setReview(value);
    }).catch(reason => { if (token === epoch.current) setError(String(reason.message ?? reason)); })
      .finally(() => { if (token === epoch.current) { locked.current = false; setBusy(false); } });
    return () => { ++epoch.current; };
  }, [identity, version, request]);
  async function write(action: "review" | "admit" | "concerns" | "concern-answers", body?: unknown) {
    if (locked.current || !review) return;
    const token = ++epoch.current, expected = identity;
    const active = () => token === epoch.current && current.current === expected;
    locked.current = true; setBusy(true); setError("");
    try {
      await request(`${base}/${action}`, { method: "POST", ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      if (active()) { setReview(null); setVersion(v => v + 1); onChange(); }
    } catch (reason) { if (active()) { setError(String((reason as Error).message ?? reason)); setReview(null); } }
    finally { if (active()) { locked.current = false; setBusy(false); } }
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!review || locked.current) return;
    const data = new FormData(event.currentTarget), verdict = String(data.get("verdict") ?? "unknown");
    const evidence = String(data.get("evidence") ?? "").trim(), findings = String(data.get("findings") ?? "").trim();
    const values = Object.fromEntries(Object.keys(dimensions).map(key => [key,String(data.get(key) ?? "unknown")]));
    if (!evidence || data.get("inspected") !== "on" || !["pass","fail"].includes(verdict)) { setError("请明确评审结论、依据，并确认已完整检查此精确预览。"); return; }
    if (verdict === "pass" && Object.values(values).some(value => value !== "pass")) { setError("通过需要六项均明确通过；未知不能视为通过。"); return; }
    const failures = Object.entries(values).filter(([,value]) => value === "fail");
    if (verdict === "fail" && (!failures.length || !findings)) { setError("不通过需注明至少一项失败及具体硬伤。"); return; }
    void write("review", { approved: verdict === "pass", review_policy_version: 2, preview_asset_id: review.preview_asset_id,
      preview_sha256: review.preview_sha256, evidence_reference: evidence, dimensions: values,
      findings: findings ? [findings] : [], scoped_findings: failures.map(([dimension]) => ({dimension,reason:findings})) });
  }
  function answer(event: FormEvent<HTMLFormElement>, concern: Concern) {
    event.preventDefault(); if (!review || locked.current) return;
    const data = new FormData(event.currentTarget), verdict = String(data.get("verdict") ?? "unknown");
    const reason = String(data.get("reason") ?? "").trim(), evidence = String(data.get("evidence") ?? "").trim();
    if (!reason || !evidence || !["pass","fail"].includes(verdict)) { setError("请明确问题复核结论、依据和说明。"); return; }
    void write("concern-answers", { preview_asset_id: review.preview_asset_id, preview_sha256: review.preview_sha256,
      answers: [{ concern_id: concern.id, approved: verdict === "pass", reason, evidence_reference: evidence }] });
  }
  function registerConcern(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!review || locked.current) return;
    const data = new FormData(event.currentTarget), dimension = String(data.get("dimension") ?? "unknown");
    const reason = String(data.get("reason") ?? "").trim(), evidence = String(data.get("evidence") ?? "").trim();
    if (!Object.prototype.hasOwnProperty.call(dimensions, dimension) || !reason || !evidence) { setError("请选择问题维度，并填写具体硬伤和依据。"); return; }
    const body = { preview_asset_id: review.preview_asset_id, preview_sha256: review.preview_sha256,
      evidence_reference: evidence, finding: { dimension, reason } };
    const signature = JSON.stringify([identity, body]);
    let key = concernKeys.current.get(signature);
    if (!key) { key = crypto.randomUUID(); concernKeys.current.set(signature, key); }
    void write("concerns", { ...body, idempotency_key: key });
  }
  const eligible = !!review && review.readiness === "ready_for_human_continuity_review" && !review.blockers.length;
  return <div aria-label="精确人物预览六项评审">
    <h4>完整人物预览评审</h4><p>技术 QA 不代替人物自然度和连续性判断。只审核当前精确片段；最终整条视频仍需成片审核。</p>
    <button type="button" className="button" disabled={busy} onClick={() => setVersion(v => v + 1)}>重新核对评审材料</button>
    {busy && <p role="status">正在核对评审…</p>}{error && <p role="alert">{error}</p>}
    {review && <><p>已保存判断：{review.continuity_review_state === "approved" ? "通过" : review.continuity_review_state === "rejected" ? "不通过" : "尚未提交"} · {review.continuity_review_evidence_reference ?? "无已保存依据"}</p>
      <ul>{review.blockers.map(reason => <li key={reason}>{reason}</li>)}</ul>
      {review.continuity_review_state === "not_submitted" && <form key={`${identity}:${version}:${review.preview_sha256}:${review.planned_origin_sha256}`} aria-label="人物六项评审提交" onSubmit={submit}>
        {Object.entries(dimensions).map(([key,label]) => <label key={key}>{label}<select name={key} defaultValue="unknown" disabled={busy || !eligible}><option value="unknown">未判断</option><option value="pass">通过</option><option value="fail">不通过</option></select></label>)}
        <label>整体判断<select name="verdict" defaultValue="unknown" disabled={busy || !eligible}><option value="unknown">未判断</option><option value="pass">通过</option><option value="fail">不通过</option></select></label>
        <label>审核依据<input name="evidence" maxLength={2000} disabled={busy || !eligible} /></label>
        <label>硬伤与说明<textarea name="findings" maxLength={2000} disabled={busy || !eligible} /></label>
        <label style={{ display: "flex", alignItems: "center", gap: 8 }}><input type="checkbox" name="inspected" style={{ width: "auto" }} disabled={busy || !eligible} />已完整检查当前精确预览</label>
        <button type="submit" className="button" disabled={busy || !eligible}>保存人物六项判断</button>
      </form>}
      <form key={`concern:${identity}:${version}:${review.preview_sha256}`} aria-label="登记人物预览硬伤" onSubmit={registerConcern}>
        <label>问题维度<select name="dimension" defaultValue="unknown" disabled={busy}><option value="unknown">未选择</option>{Object.entries(dimensions).map(([key,label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <label>具体硬伤<textarea name="reason" maxLength={2000} disabled={busy} /></label>
        <label>问题依据<input name="evidence" maxLength={2000} disabled={busy} /></label>
        <button type="submit" className="button" disabled={busy}>登记预览问题</button>
      </form>
      {review.review_concerns.map(concern => <div key={concern.id}><p>{dimensions[concern.finding.dimension]}：{concern.finding.reason}</p>
        {concern.answer ? <p>复核：{concern.answer.approved ? "已澄清" : "未通过"} · {concern.answer.reason}</p> : <form aria-label={`复核问题 ${concern.id}`} onSubmit={event => answer(event,concern)}>
          <label>问题复核结论<select name="verdict" defaultValue="unknown" disabled={busy}><option value="unknown">未判断</option><option value="pass">已澄清</option><option value="fail">仍有硬伤</option></select></label>
          <label>复核依据<input name="evidence" maxLength={2000} disabled={busy} /></label><label>复核说明<input name="reason" maxLength={2000} disabled={busy} /></label>
          <button type="submit" className="button" disabled={busy}>保存问题复核</button>
        </form>}
      </div>)}
      <button type="button" className="button primary" disabled={busy || !eligible || review.continuity_review_state !== "approved"} onClick={() => void write("admit")}>将已审核人物片段准入为 TalkingRun</button>
    </>}
  </div>;
}
