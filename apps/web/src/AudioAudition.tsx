import { useEffect, useRef, useState } from "react";

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type Access = { audio_id: string; state: string; reasons: string[]; manifest: { lineage: string; scopes: Array<{ purpose: string }> } };

export function AudioAudition({ audioId, projectId, request, accessBase }: { audioId: string; projectId: string; request: Request; accessBase?: string }) {
  const [status, setStatus] = useState<Access | null>(null);
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const epoch = useRef(0);
  const blob = useRef("");
  const scope = `${projectId}:${audioId}:${accessBase ?? ""}`;
  const current = useRef(scope); current.current = scope;
  function clearMedia() {
    if (blob.current) URL.revokeObjectURL(blob.current);
    blob.current = ""; setUrl("");
  }
  const base = accessBase ?? `/audio-assets/${encodeURIComponent(audioId)}`;
  function context(purpose: string) { return `?project_id=${encodeURIComponent(projectId)}&purpose=${purpose}`; }

  useEffect(() => {
    const token = ++epoch.current;
    clearMedia(); setStatus(null); setError(""); setBusy(false);
    request<Access>(base + "/access" + context("commercial_production")).then(value => {
      if (token === epoch.current && value.audio_id === audioId) setStatus(value);
    }).catch(reason => { if (token === epoch.current) setError(String(reason.message ?? reason)); });
    return () => { ++epoch.current; if (blob.current) URL.revokeObjectURL(blob.current); blob.current = ""; };
  }, [audioId, projectId, request, accessBase]);

  async function listen(purpose: string) {
    const token = ++epoch.current; const expected = scope;
    const active = () => token === epoch.current && expected === current.current;
    clearMedia(); setBusy(true); setError("");
    try {
      const value = await request<Access>(base + "/access" + context(purpose));
      if (!active()) return;
      if (value.audio_id !== audioId) throw new Error("音频权限状态与当前素材不一致。");
      setStatus(value);
      if (value.state === "blocked") throw new Error(value.reasons.join("、"));
      const headers = new Headers();
      const accessToken = window.sessionStorage.getItem("content-os-access-token");
      if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
      const response = await fetch(base + "/media" + context(purpose), { headers, cache: "no-store" });
      if (!response.ok) throw new Error(`试听不可用 (${response.status})；请重新检查用途权限。`);
      const bytes = await response.blob();
      if (!active()) return;
      blob.current = URL.createObjectURL(bytes); setUrl(blob.current);
    } catch (reason) { if (active()) setError(String((reason as Error).message ?? reason)); }
    finally { if (active()) setBusy(false); }
  }
  const restricted = status?.manifest.scopes.some(item => item.purpose === "internal_evaluation");
  return <div aria-label="音频试听权限">
    {restricted && <p className="muted">此素材受限于内部评估用途。试听不代表质量通过，也不授权下载、生产或发布。权限撤销阻止新的读取，无法收回已经播放的内容。</p>}
    {status?.state === "blocked" && <p className="error">当前播放被阻止：{status.reasons.join("、")}</p>}
    {status && <details><summary>素材用途与来源记录</summary><pre>{JSON.stringify(status.manifest, null, 2)}</pre></details>}
    <button type="button" className="button" disabled={busy || !status} onClick={() => void listen(restricted ? "internal_evaluation" : "commercial_production")}>
      {restricted ? "按内部评估用途检查并试听" : "检查权限并试听"}
    </button>
    {url && <audio controls preload="none" src={url} style={{ width: "100%", marginTop: 12 }} />}
    {error && <p className="error">{error}</p>}
  </div>;
}
