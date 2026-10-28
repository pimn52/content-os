import { useEffect, useState, type FormEvent } from "react";
import { WebDiscoveryPanel } from "./WebDiscoveryPanel";

type Video = { id: string; title: string; description: string; page_url: string; width: number; height: number; dimensions_exact?: boolean; size_bytes: number | null; duration_ms?: number | null; author: string; license_name: string; license_url: string; attribution: string; admissible: boolean; admission_reason: string };
export type ExternalMediaSearch = { call_id: string; query: string; candidates: Video[]; provider?: string | null; cached?: boolean };
type Search = ExternalMediaSearch;
type Provider = { id: string; label: string; configured: boolean };
type Imported = { asset_id: string; analysis_job_id: string; attribution: string };
type LibraryItem = { candidate_id: string; asset_id: string; analysis_status: string };
type Props = { library: LibraryItem[]; projectId: string; request<T>(path: string, options?: RequestInit): Promise<T>; onImported(): Promise<void> };

export function ExternalMediaPanel({ projectId, request, onImported, library }: Props) {
  const [result, setResult] = useState<Search | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [imports, setImports] = useState<Record<string, Imported>>({});
  const [providers, setProviders] = useState<Provider[]>([]);
  const [provider, setProvider] = useState("wikimedia_commons");
  const base = `/projects/${projectId}/external-media`;

  useEffect(() => {
    let active = true;
    Promise.all([request<Search[]>(`${base}/searches`), request<Provider[]>(`${base}/providers`)]).then(([values, sources]) => {
      if (!active) return;
      setProviders(sources);
      setProvider(sources.find(v => v.configured)?.id ?? "wikimedia_commons");
      if (values.length) setResult(values[0]);
    }).catch(value => { if (active) setError(String(value)); });
    return () => { active = false; };
  }, [projectId]);

  async function search(event: FormEvent) {
    event.preventDefault();
    const query = String(new FormData(event.currentTarget as HTMLFormElement).get("query") ?? "").trim();
    const refresh = ((event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null)?.value === "refresh";
    setBusy("search"); setError("");
    try {
      setResult(await request<Search>(`${base}/search`, { method: "POST", body: JSON.stringify({ query, provider, refresh, idempotency_key: `media-search:${crypto.randomUUID()}` }) }));
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(""); }
  }

  async function importVideo(video: Video) {
    if (!result) return;
    setBusy(video.id); setError("");
    try {
      const value = await request<Imported>(`${base}/imports`, { method: "POST", body: JSON.stringify({ search_call_id: result.call_id, candidate_id: video.id, idempotency_key: `media-import:${crypto.randomUUID()}` }) });
      setImports(values => ({ ...values, [video.id]: value }));
      await onImported();
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(""); }
  }

  return <section className="subform" aria-label="外部素材">
    <h3>查找外部视频</h3>
    <WebDiscoveryPanel key={projectId} projectId={projectId} request={request} onResolved={setResult}/>
    <p className="muted">可使用已连接的 Pexels、Pixabay、Coverr，或免 Key 的 Wikimedia Commons。仅发送画面关键词，按项目画幅筛选视频；来源自动保存，入库后再核验用途。连接状态不代表鉴权或网络已验证，Coverr 须符合所选套餐。</p>
    <label>素材来源<select value={provider} onChange={event => setProvider(event.target.value)} disabled={!!busy}>{providers.map(source => <option key={source.id} value={source.id} disabled={!source.configured}>{source.label}{source.configured ? "" : "（尚未连接）"}</option>)}</select></label>
    <form key={result?.call_id ?? "new"} onSubmit={search}><label>{provider === "pexels" ? "画面关键词或 Pexels 视频链接" : "画面关键词"}<input name="query" defaultValue={result?.query ?? ""} required minLength={2} maxLength={provider === "pixabay" ? 100 : 200} placeholder={provider === "pexels" ? "具体对象与动作，或 Pexels 视频详情页" : "例如：galaxy collision（星系碰撞）"} /></label><button className="button" disabled={!!busy}>{busy === "search" ? "正在搜索…" : "搜索外部视频"}</button><button className="button" value="refresh" disabled={!!busy} title="重新请求来源，消耗来源额度；适用于刷新结果或修复连接后重试">重新查询来源</button></form>
    {result?.cached && <p className="muted">已复用近期检索结果，本次未请求来源。</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {result && <div className="asset-list">{result.candidates.map(video => { const saved = library.find(v => v.candidate_id === video.id); const imported = saved ?? imports[video.id]; const status = saved?.analysis_status; return <article className="asset-row" key={video.id}><div>
      <strong><a href={video.page_url} target="_blank" rel="noreferrer">{video.title}</a></strong>
      <small>{video.dimensions_exact === false ? "目录最大尺寸 " : ""}{video.width}×{video.height} · {video.size_bytes === null ? "大小以实际下载为准" : `${(video.size_bytes / 1048576).toFixed(1)} MiB`}{video.duration_ms ? ` · ${(video.duration_ms / 1000).toFixed(1)} 秒` : ""} · <a href={video.license_url || video.page_url} target="_blank" rel="noreferrer">{video.license_name || "许可待明确"}</a></small>
      <small>{video.description.slice(0, 220)}</small><small className="muted">{video.admission_reason}</small>
      {imported && <><small className={status === "failed" ? "error" : "ok"}>{status === "completed" ? "已入库，镜头提取完成；具体用途以场景核验为准。" : status === "failed" ? "素材已保留，本地分析失败，可手动重试。" : "已入库，本地镜头分析已入队；完成后按场景核验用途。"}</small><small>{video.attribution}</small><a href={`/assets/${imported.asset_id}/media`} target="_blank" rel="noreferrer">查看本地视频</a></>}
    </div><button type="button" className="button" disabled={!!busy || !video.admissible || !!imported} onClick={() => void importVideo(video)}>{busy === video.id ? "正在下载入库…" : imported ? "已入库" : "下载入库"}</button>{saved && status === "failed" && <button type="button" className="button" disabled={!!busy} onClick={async () => { setBusy(video.id); setError(""); try { await request(`/assets/${saved.asset_id}/jobs/analyze_asset`, { method: "POST", body: JSON.stringify({ idempotency_key: `media-analysis:${crypto.randomUUID()}` }) }); await onImported(); } catch (value) { setError(value instanceof Error ? value.message : String(value)); } finally { setBusy(""); } }}>重试本地分析</button>}</article>; })}{!result.candidates.length && <p className="muted">没有找到合适视频，可换用具体对象、动作或其他来源。</p>}</div>}
  </section>;
}
