import { useEffect, useRef, useState } from "react";
import { recoveryIdempotencyKey } from "./production";

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type Status = { project_id: string; audio_id: string; state: string; reasons: string[];
  choices: Array<{ admission_id: string; label: string }>; target_text?: string;
  job_status?: string; call_status?: string; qa_state?: string; technical_ready: boolean };

export function VoiceEvaluationQA({ projectId, audioId, request, onResult }: { projectId: string; audioId: string; request: Request; onResult?: (audioId: string) => void }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [admission, setAdmission] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const epoch = useRef(0), locked = useRef(false);
  const context = `${projectId}:${audioId}`;
  const current = useRef(context); current.current = context;
  const notify = useRef(onResult); notify.current = onResult;
  const notified = useRef("");
  const path = `/projects/${projectId}/audio-assets/${audioId}/evaluation-qa`;
  function validate(value: Status) {
    if (value.project_id !== projectId || value.audio_id !== audioId) throw new Error("QA 状态与当前草稿不一致。");
    return value;
  }
  function receive(value: Status) {
    setStatus(value);
    if (value.call_status === "completed" && value.job_status === "completed") {
      const identity = JSON.stringify([context, value.qa_state, value.technical_ready]);
      if (notified.current !== identity) { notified.current = identity; notify.current?.(audioId); }
    }
  }
  useEffect(() => {
    const token = ++epoch.current;
    setStatus(null); setAdmission(""); setError(""); setBusy(false); locked.current = false; notified.current = "";
    request<Status>(path).then(value => {
      if (token === epoch.current) receive(validate(value));
    }).catch(reason => { if (token === epoch.current) setError(String(reason.message ?? reason)); });
    return () => { ++epoch.current; };
  }, [projectId, audioId, request]);
  async function refresh() {
    const token = ++epoch.current;
    try { const value = validate(await request<Status>(path)); if (token === epoch.current) { receive(value); setError(""); } }
    catch (reason) { if (token === epoch.current) setError(String((reason as Error).message ?? reason)); }
  }
  async function submit() {
    if (locked.current || status?.state !== "ready" || !status.target_text || !status.choices.some(c => c.admission_id === admission)) return;
    const token = ++epoch.current, expected = context;
    const active = () => token === epoch.current && expected === current.current;
    locked.current = true; setBusy(true); setError("");
    try {
      const body = { qa_policy: "voice-evaluation-qa-v1", purpose: "internal_evaluation", use_admission_id: admission,
        narration_audio_id: audioId, target_text: status.target_text };
      const key = await recoveryIdempotencyKey("voice-evaluation-qa", projectId, audioId, body.qa_policy, body);
      if (!active()) return;
      const job = await request<{ project_id: string }>(`/projects/${projectId}/voice-qa-jobs`,
        { method: "POST", body: JSON.stringify({ ...body, idempotency_key: key }) });
      if (!active()) return;
      if (job.project_id !== projectId) throw new Error("QA 创建结果与当前项目不一致。");
      const updated = validate(await request<Status>(path));
      if (active()) receive(updated);
    } catch (reason) { if (active()) setError(String((reason as Error).message ?? reason)); }
    finally { if (token === epoch.current) { locked.current = false; setBusy(false); } }
  }
  return <section aria-label="草稿独立 QA">
    <p>独立 QA：{status?.state ?? "读取中"}。仅检查这条语音的技术结果；U-Voice 和生产使用仍需各自准入。</p>
    {status?.state === "ready" && <>
      <label>独立 QA 准入<select disabled={busy} value={admission} onChange={e => setAdmission(e.target.value)}>
        <option value="">选择适用于当前草稿的准入</option>
        {status.choices.map(c => <option key={c.admission_id} value={c.admission_id}>{c.label}</option>)}
      </select></label>
      <p>只识别当前已生成音频一次；不会再次生成语音。本地耗时与成本未知，选择准入不会启动识别。</p>
      <button className="button" disabled={busy || !admission} onClick={() => void submit()}>提交一次独立 QA</button>
    </>}
    {status?.job_status && <p>QA 任务：{status.job_status}；调用：{status.call_status ?? "未开始"}；技术结果：{status.qa_state ?? "待评估"}。</p>}
    {status?.state === "reconciliation_required" && <p>已有调用需要核对或恢复任务状态；不会自动再次识别。</p>}
    {status?.technical_ready && <p>此音频的技术 QA 已通过；声线、自然度与发布质量请查看各自人审和准入结果。</p>}
    {status?.qa_state === "failed" && <p>技术 QA 未通过；保留原音频与失败证据供试听。</p>}
    {status?.reasons.length ? <p className="error">{status.reasons.join("、")}</p> : null}
    <button className="button" disabled={busy} onClick={() => void refresh()}>刷新独立 QA 状态</button>
    {error && <p className="error">{error}</p>}
  </section>;
}
