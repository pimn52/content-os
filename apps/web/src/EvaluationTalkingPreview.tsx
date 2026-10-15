import { useEffect, useRef, useState } from "react";
type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type Access = { operation_id: string; state: string; reasons: string[]; asset_id: string | null; content_hash: string | null };

export function EvaluationTalkingPreview({ projectId, operationId, revision, request }: {
  projectId: string; operationId: string; revision: string; request: Request;
}) {
  const [access, setAccess] = useState<Access | null>(null), [url, setUrl] = useState("");
  const [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const epoch = useRef(0), blob = useRef(""), locked = useRef(false);
  const identity = `${projectId}:${operationId}:${revision}`;
  const current = useRef(identity); current.current = identity;
  const base = `/projects/${projectId}/evaluation-talking/operations/${operationId}/preview`;
  function clear() { if (blob.current) URL.revokeObjectURL(blob.current); blob.current = ""; setUrl(""); }
  useEffect(() => {
    const token = ++epoch.current;
    clear(); setAccess(null); setError(""); setBusy(false); locked.current = false;
    request<Access>(`${base}/access?purpose=internal_evaluation`).then(value => {
      if (token !== epoch.current) return;
      if (value.operation_id !== operationId) throw new Error("预览记录与当前操作不一致。");
      setAccess(value);
    }).catch(reason => { if (token === epoch.current) setError(String(reason.message ?? reason)); });
    return () => { ++epoch.current; if (blob.current) URL.revokeObjectURL(blob.current); blob.current = ""; };
  }, [identity, request]);
  async function play() {
    if (locked.current) return;
    const token = ++epoch.current, expected = identity;
    const active = () => token === epoch.current && current.current === expected;
    locked.current = true; setBusy(true); setError(""); clear();
    try {
      const value = await request<Access>(`${base}/access?purpose=internal_evaluation`);
      if (!active()) return;
      if (value.operation_id !== operationId) throw new Error("预览记录与当前操作不一致。");
      setAccess(value);
      if (value.state !== "review_allowed" || !value.asset_id || !value.content_hash) throw new Error(value.reasons.join("、") || "预览当前不可读取。");
      const headers = new Headers(), key = window.sessionStorage.getItem("content-os-access-token");
      if (key) headers.set("Authorization", `Bearer ${key}`);
      const response = await fetch(`${base}/media?purpose=internal_evaluation`, { headers, cache: "no-store" });
      if (!response.ok) throw new Error(`预览读取被拒绝 (${response.status})。请重新核对状态。`);
      const bytes = await response.blob();
      if (!active()) return;
      blob.current = URL.createObjectURL(bytes); setUrl(blob.current);
    } catch (reason) { if (active()) setError(String((reason as Error).message ?? reason)); }
    finally { if (active()) { locked.current = false; setBusy(false); } }
  }
  return <div aria-label="内部评估人物预览">
    <p>此预览仅用于内部复核。播放不代表六项人审通过或成片可发布。</p>
    {access?.state === "blocked" && <p role="status">当前预览不可用：{access.reasons.join("、")}</p>}
    <button type="button" className="button" disabled={busy || access?.state !== "review_allowed"} onClick={() => void play()}>检查权限并播放人物预览</button>
    {url && <video controls preload="none" controlsList="nodownload" src={url} style={{ width: "100%", maxWidth: 360 }} />}
    {error && <p role="alert">{error}</p>}
  </div>;
}
