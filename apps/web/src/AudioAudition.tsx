import { useEffect, useRef, useState } from "react";

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type Access = { audio_id: string; state: string; reasons: string[] };

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
  useEffect(() => {
    const token = ++epoch.current;
    clearMedia(); setStatus(null); setError(""); setBusy(false);
    request<Access>(base + "/access").then(value => {
      if (token === epoch.current && value.audio_id === audioId) setStatus(value);
    }).catch(reason => { if (token === epoch.current) setError(String(reason.message ?? reason)); });
    return () => { ++epoch.current; if (blob.current) URL.revokeObjectURL(blob.current); blob.current = ""; };
  }, [audioId, projectId, request, accessBase]);

  async function listen() {
    const token = ++epoch.current; const expected = scope;
    const active = () => token === epoch.current && expected === current.current;
    clearMedia(); setBusy(true); setError("");
    try {
      const value = await request<Access>(base + "/access");
      if (!active()) return;
      if (value.audio_id !== audioId) throw new Error("音频权限状态与当前素材不一致。");
      setStatus(value);
      if (value.state === "blocked") throw new Error(value.reasons.join("、"));
      const headers = new Headers();
      const accessToken = window.sessionStorage.getItem("content-os-access-token");
      if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
      const response = await fetch(base + "/media", { headers, cache: "no-store" });
      if (!response.ok) throw new Error(`试听不可用 (${response.status})；请检查素材文件与入库授权记录。`);
      const bytes = await response.blob();
      if (!active()) return;
      blob.current = URL.createObjectURL(bytes); setUrl(blob.current);
    } catch (reason) { if (active()) setError(String((reason as Error).message ?? reason)); }
    finally { if (active()) setBusy(false); }
  }
  return <div aria-label="音频试听权限">
    {status?.state === "blocked" && <p className="error">当前播放被阻止：{status.reasons.join("、")}</p>}
    <button type="button" className="button" disabled={busy || !status} onClick={() => void listen()}>试听</button>
    {url && <audio controls preload="none" src={url} style={{ width: "100%", marginTop: 12 }} />}
    {error && <p className="error">{error}</p>}
  </div>;
}
