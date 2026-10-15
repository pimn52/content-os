import { useEffect, useState } from "react";
export type ExecutionChoice = { purpose: "commercial_production" | "internal_evaluation"; use_admission_ids: string[] };
type Admission = { id: string; project_id: string; revoked_at: string | null; review: {
  admission_policy?: string; intended_activity: string; license_version: string; permitted_operations: string[];
  identity: { capability: string; provider: string; model: string };
} };
type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
export const commercialChoice = (): ExecutionChoice => ({ purpose: "commercial_production", use_admission_ids: [] });
export function ExecutionPurposeControl({ projectId, value, onChange, disabled, request, lane }: {
  projectId: string; value: ExecutionChoice; onChange: (value: ExecutionChoice) => void; disabled: boolean; request: Request; lane?: "voice-draft-v1" | "existing-master";
}) {
  const [records, setRecords] = useState<Admission[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    setRecords([]); setError(""); setLoading(value.purpose === "internal_evaluation" && lane !== "existing-master");
    if (value.purpose === "internal_evaluation" && lane !== "existing-master") {
      request<Admission[]>(`/projects/${projectId}/provider-use-admissions`).then(rows => {
        if (!Array.isArray(rows) || rows.some(row => row.project_id !== projectId)) throw new Error("准入记录项目不匹配，已拒绝显示。");
        if (active) setRecords(rows);
      }).catch(reason => { if (active) setError(reason instanceof Error ? reason.message : "准入记录读取失败"); })
        .finally(() => { if (active) setLoading(false); });
    }
    return () => { active = false; };
  }, [projectId, value.purpose, revision, request, lane]);
  return <fieldset disabled={disabled}>
    <label>新执行用途<select value={value.purpose} onChange={event => onChange({ purpose: event.target.value as ExecutionChoice["purpose"], use_admission_ids: [] })}>
      <option value="commercial_production">商业制作（仍需后端许可校验）</option>
      <option value="internal_evaluation">内部评估（不得转为商用）</option>
    </select></label>
    {value.purpose === "internal_evaluation" && lane !== "existing-master" && <>
      <p>选择本项目已有的用途准入记录；选择本身不授予权限。后台逐次复核许可、配置、撤销及运行时。{lane === "voice-draft-v1" ? "此入口只支持评估 Voice 草稿；Worker 配置或当前证据缺失时会阻止执行。" : "当前评估 Worker 通路尚未开放。"}</p>
      <button type="button" className="button" disabled={loading} onClick={() => { onChange({ ...value, use_admission_ids: [] }); setRevision(revision + 1); }}>刷新用途准入记录</button>
      {loading && <p role="status">正在读取用途准入记录…</p>}
      {error && <p role="alert" className="error">{error}</p>}
      {!loading && !error && records.length === 0 && <p>本项目没有用途准入记录。诊断成功和素材人审不能替代生成准入。</p>}
      {records.map(record => <label key={record.id}><input type="checkbox" checked={value.use_admission_ids.includes(record.id)}
        disabled={loading || !!record.revoked_at || !record.review.permitted_operations.includes("generation") || !value.use_admission_ids.includes(record.id) && value.use_admission_ids.length >= 20}
        onChange={event => onChange({ ...value, use_admission_ids: event.target.checked ? [...value.use_admission_ids, record.id].sort() : value.use_admission_ids.filter(id => id !== record.id) })} />
        {record.review.identity.capability} · {record.review.identity.provider} · {record.review.identity.model} · {record.review.intended_activity} · {record.review.license_version}
        {record.review.admission_policy === "voice-calibration-v1" ? " · 首次验证，仅一次，质量未知" : ""}
        {record.revoked_at ? " · 已撤销" : " · 当前有效性待后端核验"} · 操作：{record.review.permitted_operations.join("、")}
      </label>)}
    </>}
  </fieldset>;
}
