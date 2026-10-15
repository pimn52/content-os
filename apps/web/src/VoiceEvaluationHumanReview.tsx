import { useEffect, useRef, useState } from "react";

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
const dimensions = [["likeness", "声线相似度"], ["naturalness", "自然度"], ["emphasis", "重音"], ["pace", "语速"], ["pauses", "停顿"], ["rhythm", "节奏"]] as const;
type Outcome = "" | "pass" | "needs_revision";
type Review = { approved: boolean; evidence_reference: string; findings: string[]; reviewed_at: string } & Record<typeof dimensions[number][0], Exclude<Outcome, "">>;
type Status = { project_id: string; audio_id: string; state: string; current_eligible: boolean; reasons: string[];
  expected_qa_fingerprint?: string; audio_sha256?: string; target_text?: string;
  record: { id: string; audio_id: string; project_id: string; audio_sha256: string; review: Review } | null };
const empty = () => Object.fromEntries(dimensions.map(([key]) => [key, ""])) as Record<typeof dimensions[number][0], Outcome>;

export function VoiceEvaluationHumanReview({ projectId, audioId, qaState, request }: {
  projectId: string; audioId: string; qaState: string | null; request: Request;
}) {
  const [status, setStatus] = useState<Status | null>(null), [outcomes, setOutcomes] = useState(empty);
  const [reference, setReference] = useState(""), [findings, setFindings] = useState(""), [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const epoch = useRef(0), locked = useRef(false), fingerprint = useRef("");
  const context = `${projectId}:${audioId}:${qaState}`;
  const current = useRef(context); current.current = context;
  const path = `/projects/${projectId}/voice-assets/${audioId}/evaluation-human-review`;
  function clearForm() { setOutcomes(empty()); setReference(""); setFindings(""); setConfirmed(false); }
  function receive(value: Status) {
    if (value.project_id !== projectId || value.audio_id !== audioId || (value.record &&
      (value.record.project_id !== projectId || value.record.audio_id !== audioId))) throw new Error("人审记录与当前音频不一致。");
    const identity = value.expected_qa_fingerprint ?? "";
    if (fingerprint.current !== identity) clearForm();
    fingerprint.current = identity; setStatus(value);
  }
  useEffect(() => {
    const token = ++epoch.current; locked.current = false; setBusy(false); setStatus(null); setError(""); fingerprint.current = ""; clearForm();
    request<Status>(`${path}?purpose=internal_evaluation`).then(value => {
      if (token === epoch.current) receive(value);
    }).catch(reason => { if (token === epoch.current) setError(String(reason.message ?? reason)); });
    return () => { ++epoch.current; };
  }, [projectId, audioId, qaState, request]);
  async function refresh() {
    const token = ++epoch.current;
    try { const value = await request<Status>(`${path}?purpose=internal_evaluation`); if (token === epoch.current) { receive(value); setError(""); } }
    catch (reason) { if (token === epoch.current) { setStatus(null); clearForm(); setError(String((reason as Error).message ?? reason)); } }
  }
  const ready = status?.current_eligible && !status.record && status.expected_qa_fingerprint &&
    dimensions.every(([key]) => outcomes[key] !== "") && reference.trim() && findings.trim() && confirmed;
  async function submit() {
    if (locked.current || !ready || !status) return;
    const token = ++epoch.current, expected = context;
    const active = () => token === epoch.current && expected === current.current;
    locked.current = true; setBusy(true); setError("");
    try {
      const record = await request<NonNullable<Status["record"]>>(path, { method: "POST", body: JSON.stringify({
        policy: "voice-evaluation-human-review-v1", purpose: "internal_evaluation", expected_qa_fingerprint: status.expected_qa_fingerprint,
        approved: dimensions.every(([key]) => outcomes[key] === "pass"), ...outcomes,
        evidence_reference: reference.trim(), findings: findings.split("\n").map(value => value.trim()).filter(Boolean),
      }) });
      if (!active()) return;
      if (record.project_id !== projectId || record.audio_id !== audioId) throw new Error("保存结果与当前音频不一致。");
      const value = await request<Status>(`${path}?purpose=internal_evaluation`);
      if (active()) receive(value);
    } catch (reason) { if (active()) { setConfirmed(false); setError(String((reason as Error).message ?? reason)); } }
    finally { if (token === epoch.current) { locked.current = false; setBusy(false); } }
  }
  return <section aria-label="草稿 U-Voice 人审">
    <h4>这条音频的 U-Voice 人审</h4>
    <p>人审记录：{!status ? "读取中" : status.record ? status.record.review.approved ? "六项通过" : "需要修改" : "尚未记录"}；当前记录权限：{!status ? "读取中" : status.current_eligible ? "可用" : "不可用"}。生产使用仍需单独准入。</p>
    <p>音频：{audioId} · SHA256：{status?.audio_sha256 ?? status?.record?.audio_sha256 ?? "读取中"}</p>
    {status?.target_text && <p>原文：{status.target_text}</p>}
    {status?.record && <><p>保存时间：{status.record.review.reviewed_at} · 证据：{status.record.review.evidence_reference}</p>
      <p>{dimensions.map(([key, label]) => `${label}：${status.record!.review[key] === "pass" ? "通过" : "需要修改"}`).join("；")}</p>
      <p>{status.record.review.findings.join("；")}</p><p>已有判断保留，不能覆盖；权限变化时仅作为历史反馈展示。</p></>}
    {status?.current_eligible && !status.record && <>
      {dimensions.map(([key, label]) => <label key={key}>{label}<select disabled={busy} value={outcomes[key]} onChange={event => {
        setOutcomes({ ...outcomes, [key]: event.target.value as Outcome }); setConfirmed(false);
      }}><option value="">尚未判断</option><option value="pass">通过</option><option value="needs_revision">需要修改</option></select></label>)}
      <label>人审证据引用<input disabled={busy} value={reference} onChange={event => { setReference(event.target.value); setConfirmed(false); }} /></label>
      <label>具体判断或硬伤（每行一项）<textarea disabled={busy} value={findings} onChange={event => { setFindings(event.target.value); setConfirmed(false); }} /></label>
      <label><input type="checkbox" disabled={busy} checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />确认上述判断针对当前音频和 SHA256，保存后不能覆盖。</label>
      <button className="button" disabled={busy || !ready} onClick={() => void submit()}>保存这条音频的人审</button>
    </>}
    {status?.reasons.length ? <p className="error">{status.reasons.join("、")}</p> : null}
    <button className="button" disabled={busy} onClick={() => void refresh()}>刷新人审记录</button>
    {error && <p className="error">{error}</p>}
  </section>;
}
