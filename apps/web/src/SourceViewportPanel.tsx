import { useEffect, useRef, useState, type FormEvent } from "react";
import { recoveryIdempotencyKey } from "./production";
type Asset = { id: string; source_file: string; content_hash: string; width: number; height: number; duration_ms: number };
type Clip = { id: string; asset_id: string; start_ms: number; end_ms: number };
type Rectangle = { x: number; y: number; width: number; height: number };
type Feasibility = { request: { source_content_hash: string; output_height: number; source_subtitle_state: string; known_subtitle_regions: Rectangle[]; proposal: { source_asset_id: string; source_clip_id: string; start_ms: number; end_ms: number; crop_window: Rectangle } }; status: "blocked" | "needs_review"; stop_reasons: string[]; source_hash_verified: boolean; crop_assessment: "unknown" | "unusable"; retained_source_fraction: number | null; upscale_factor: number | null };
const stopLabels: Record<string, string> = { source_identity_changed: "素材记录已变化，请刷新", source_bytes_changed: "源文件字节已变化，请重新核实来源", source_file_unavailable: "源文件不可读取", interval_outside_source_clip: "区间超出所选 Clip", crop_outside_source: "裁剪框超出画面", crop_aspect_mismatch: "裁剪框与视口比例不同", source_subtitles_unknown: "原字幕情况尚未检查", subtitle_region_outside_source: "原字幕区域超出画面", known_subtitle_overlap: "裁剪框仍包含原字幕", persisted_face_boundary_failure: "该准确裁剪与区间已有脸部越界证据" };
type Props = { assets: Asset[]; clips: Clip[]; request<T>(path: string, options?: RequestInit): Promise<T>; onCreated(): Promise<void> };
function rectangle(value: unknown): Rectangle {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("区域必须是像素矩形。");
  const record = value as Record<string, unknown>;
  if (Object.keys(record).sort().join(",") !== "height,width,x,y") throw new Error("矩形仅接受 x、y、width、height。");
  for (const key of ["x", "y", "width", "height"]) if (typeof record[key] !== "number" || !Number.isInteger(record[key]) || Number(record[key]) < (key === "x" || key === "y" ? 0 : 1)) throw new Error("坐标须为非负整数，宽高须为正整数。");
  return record as Rectangle;
}
function pixelRectangle(fields: Record<string, string>): Rectangle {
  if (!Object.values(fields).every(value => /^\d+$/.test(value))) throw new Error("请填写完整的整数像素矩形。");
  return rectangle(Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, Number(value)])));
}
export function SourceViewportPanel({ assets, clips, request, onCreated }: Props) {
  const [assetId, setAssetId] = useState(""); const [clipId, setClipId] = useState("");
  const [height, setHeight] = useState("1440"); const [crop, setCrop] = useState({ x: "", y: "", width: "", height: "" });
  const [start, setStart] = useState(""); const [end, setEnd] = useState("");
  const [subtitles, setSubtitles] = useState(""); const [regions, setRegions] = useState({ x: "", y: "", width: "", height: "" });
  const [review, setReview] = useState(""); const [reference, setReference] = useState("");
  const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [busy, setBusy] = useState(false);
  const [previewUrl, setPreviewUrl] = useState(""); const [previewBusy, setPreviewBusy] = useState(false);
  const [feasibility, setFeasibility] = useState<Feasibility | null>(null);
  const previewObject = useRef(""); const previewLock = useRef(false);
  const lock = useRef(false); const epoch = useRef(0);
  const asset = assets.find(item => item.id === assetId);
  const identity = asset ? `${asset.id}:${asset.content_hash}:${asset.width}:${asset.height}:${asset.duration_ms}` : "none";
  const currentIdentity = useRef(identity); currentIdentity.current = identity;
  const geometryIdentity = JSON.stringify([identity, clipId, height, crop, start, end, subtitles, regions]);
  const geometryRef = useRef(geometryIdentity); geometryRef.current = geometryIdentity;
  useEffect(() => {
    ++epoch.current; lock.current = false; setBusy(false); setClipId(""); setCrop({ x: "", y: "", width: "", height: "" }); setStart(""); setEnd("");
    setSubtitles(""); setRegions({ x: "", y: "", width: "", height: "" }); setReview(""); setReference(""); setError(""); setNotice("");
    setPreviewUrl(""); setPreviewBusy(false); previewLock.current = false;
    setFeasibility(null);
    return () => { ++epoch.current; if (previewObject.current) URL.revokeObjectURL(previewObject.current); previewObject.current = ""; };
  }, [identity]);
  function invalidateReview() { setReview(""); setNotice(""); setFeasibility(null); }
  async function inspectFeasibility() {
    if (!asset || lock.current) return;
    const token = epoch.current; const expected = geometryIdentity;
    const active = () => token === epoch.current && geometryRef.current === expected;
    setFeasibility(null); setError("");
    try {
      const clip = clips.find(item => item.id === clipId && item.asset_id === asset.id);
      if (!clip || ![start, end].every(value => /^\d+$/.test(value)) || Number(end) <= Number(start)) throw new Error("请选择 Clip 并填写有效的完整毫秒区间。");
      const window = pixelRectangle(crop);
      const body = { proposal: { source_asset_id: asset.id, source_clip_id: clip.id, start_ms: Number(start), end_ms: Number(end), crop_window: window }, source_content_hash: asset.content_hash, output_height: Number(height), source_subtitle_state: subtitles || "unknown", known_subtitle_regions: subtitles === "known_burned_in" ? [pixelRectangle(regions)] : [] };
      lock.current = true; setBusy(true);
      const result = await request<Feasibility>(`/assets/${asset.id}/viewport-feasibility`, { method: "POST", body: JSON.stringify(body) });
      if (!active()) return;
      const proposal = result.request?.proposal;
      const regionIdentity = (values: Rectangle[]) => JSON.stringify(values.map(value => [value.x, value.y, value.width, value.height]));
      if (result.request.source_content_hash !== asset.content_hash || result.request.output_height !== Number(height) || result.request.source_subtitle_state !== body.source_subtitle_state || regionIdentity(result.request.known_subtitle_regions) !== regionIdentity(body.known_subtitle_regions) || !proposal || proposal.source_asset_id !== asset.id || proposal.source_clip_id !== clip.id || proposal.start_ms !== Number(start) || proposal.end_ms !== Number(end) || ["x", "y", "width", "height"].some(key => proposal.crop_window[key as keyof Rectangle] !== window[key as keyof Rectangle])) throw new Error("检查结果与当前素材、区间或裁剪不同；已拒绝使用。");
      setFeasibility(result); if (result.status === "blocked") setReview("");
    } catch (cause) { if (active()) setError(cause instanceof Error ? cause.message : "裁剪前检查失败。"); }
    finally { if (token === epoch.current) { lock.current = false; setBusy(false); } }
  }
  async function loadPreview() {
    if (!asset || previewLock.current) return;
    const token = epoch.current; const expected = identity;
    const active = () => epoch.current === token && currentIdentity.current === expected;
    previewLock.current = true; setPreviewBusy(true); setError("");
    try {
      const headers = new Headers(); const bearer = window.sessionStorage.getItem("content-os-access-token");
      if (bearer) headers.set("Authorization", `Bearer ${bearer}`);
      const response = await fetch(`/assets/${encodeURIComponent(asset.id)}/media`, { headers });
      if (!response.ok) throw new Error(`源视频读取失败 (${response.status})`);
      const blob = await response.blob();
      const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
      const hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
      if (!active()) return;
      if (hash !== asset.content_hash) throw new Error("源视频文件已变化；请刷新素材信息，不沿用原检查声明。");
      if (previewObject.current) URL.revokeObjectURL(previewObject.current);
      const url = URL.createObjectURL(blob); previewObject.current = url; setPreviewUrl(url);
    } catch (cause) { if (active()) { setReview(""); setError(cause instanceof Error ? cause.message : "源视频读取失败。"); } }
    finally { if (active()) { previewLock.current = false; setPreviewBusy(false); } }
  }
  const cropPreview = asset && Object.values(crop).every(value => /^\d+$/.test(value))
    && Number(crop.width) > 0 && Number(crop.height) > 0
    && Number(crop.x) + Number(crop.width) <= asset.width && Number(crop.y) + Number(crop.height) <= asset.height;
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (lock.current || !asset) return;
    setError(""); setNotice(""); const token = epoch.current; const expected = identity;
    const active = () => epoch.current === token && currentIdentity.current === expected;
    try {
      const clip = clips.find(item => item.id === clipId && item.asset_id === asset.id);
      if (!clip || !/^[0-9a-f]{64}$/.test(asset.content_hash)) throw new Error("请选择当前素材的 Clip；源文件须有完整 SHA-256。");
      if (![start, end].every(value => /^\d+$/.test(value))) throw new Error("请填写整数毫秒区间。");
      const startMs = Number(start), endMs = Number(end), outputHeight = Number(height), window = pixelRectangle(crop);
      if (startMs < clip.start_ms || endMs > clip.end_ms || endMs <= startMs) throw new Error("区间须完整位于所选 Clip 内，且结束晚于开始。");
      if (window.x + window.width > asset.width || window.y + window.height > asset.height) throw new Error("裁剪框超出源画面。");
      if (![1080, 1440, 1680, 1920].includes(outputHeight) || window.width * outputHeight !== window.height * 1080) throw new Error("裁剪框比例须与源视口一致；系统不会拉伸人物。");
      if (!["not_observed", "known_burned_in"].includes(subtitles)) throw new Error("须先确认原字幕情况；未知状态不可提交。");
      const knownRegions = subtitles === "known_burned_in" ? [pixelRectangle(regions)] : [];
      if (knownRegions.some(region => window.x < region.x + region.width && region.x < window.x + window.width && window.y < region.y + region.height && region.y < window.y + window.height)) throw new Error("裁剪框与已知原字幕重叠；调整后重新检查完整区间。");
      if (review !== "reviewed" || !reference.trim()) throw new Error("须完成当前裁剪完整区间检查并填写证据引用。");
      if (feasibility?.status === "blocked") throw new Error("当前裁剪前检查有阻塞；先调整输入再重新检查。");
      const payload = { source_asset_id: asset.id, source_clip_id: clip.id, start_ms: startMs, end_ms: endMs,
        crop_window: window, output_height: outputHeight, source_subtitle_state: subtitles, known_subtitle_regions: knownRegions,
        evidence: { source_asset_id: asset.id, source_clip_id: clip.id, source_content_hash: asset.content_hash, crop_window: window,
          covered_start_ms: startMs, covered_end_ms: endMs, method: "human_full_interval_review", method_version: "1",
          coverage: "full_interval_continuous", confidence: "reviewed", face_head_clearance_verified: true,
          crop_stability_verified: true, source_subtitle_clearance_verified: true, evidence_reference: reference.trim() } };
      lock.current = true; setBusy(true);
      const idempotency_key = await recoveryIdempotencyKey("source-viewport", asset.id, clip.id, asset.content_hash, payload);
      if (!active()) return;
      const result = await request<{ asset: Asset; clip: Clip }>("/assets/vertical-derivations", { method: "POST", body: JSON.stringify({ idempotency_key, ...payload }) });
      if (!active()) return;
      try { await onCreated(); } catch { if (active()) setNotice("新素材已创建，素材库刷新失败；请刷新页面。相同请求重试会复用结果。"); return; }
      if (active()) setNotice(`新素材 ${result.asset.width}×${result.asset.height} 已进入素材库，可重新检索选择。成片构图和人物质量仍需实际检查。`);
    } catch (cause) { if (active()) setError(cause instanceof Error ? cause.message : "裁剪失败；检查后台原因后调整。"); }
    finally { if (active()) { lock.current = false; setBusy(false); } }
  }
  return <form className="subform" onSubmit={event => void submit(event)}>
    <h3>人物画面比例适配</h3>
    <p className="muted">优先使用构图合适的全屏竖屏画面。方形或较宽视口是素材受限时的折中方案；接受一次折中，不会将它设为所有视频的首选。</p>
    <p className="muted">源视口与最终9:16成片独立。较宽视口保留更多肩部和环境；较窄视口可能放大人物，也可能切掉动作。固定裁剪不跟踪人物、不去除字幕、不恢复清晰度。原素材保留，新素材不含原声，旁白单独绑定。</p>
    <label>适配素材<select value={assetId} disabled={busy} onChange={event => setAssetId(event.target.value)}><option value="">选择素材</option>{assets.map(item => <option key={item.id} value={item.id}>{item.source_file.split(/[\\/]/).pop()} · {item.width}×{item.height}</option>)}</select></label>
    {asset && <>
      <button type="button" className="button" disabled={busy || previewBusy} onClick={() => void loadPreview()}>{previewBusy ? "读取源视频…" : "播放源视频并查看裁剪框"}</button>
      {previewUrl && <><div style={{ position: "relative", width: "100%", maxWidth: 360, margin: "0 auto", aspectRatio: `${asset.width}/${asset.height}`, background: "#111" }}>
        <video controls preload="metadata" src={previewUrl} style={{ width: "100%", height: "100%", objectFit: "contain" }} />
        {cropPreview && <div aria-label="当前固定裁剪范围" style={{ position: "absolute", pointerEvents: "none", boxSizing: "border-box", border: "3px solid #ffcf40", left: `${100 * Number(crop.x) / asset.width}%`, top: `${100 * Number(crop.y) / asset.height}%`, width: `${100 * Number(crop.width) / asset.width}%`, height: `${100 * Number(crop.height) / asset.height}%` }} />}
      </div><small>黄色框为当前固定裁剪；请播放所选开始至结束的完整区间，检查人物移动。单帧预览不能证明完整区间安全。</small></>}
      <label>来源区间<select value={clipId} disabled={busy} onChange={event => { setClipId(event.target.value); invalidateReview(); }}><option value="">选择 Clip</option>{clips.filter(item => item.asset_id === asset.id).map(item => <option key={item.id} value={item.id}>{item.start_ms}–{item.end_ms} ms</option>)}</select></label>
      <label>源视口比例<select value={height} disabled={busy} onChange={event => { setHeight(event.target.value); invalidateReview(); }}><option value="1440">9:12 · 更宽人物画面</option><option value="1680">9:14 · 中间比例</option><option value="1920">9:16 · 较窄竖屏</option><option value="1080">1:1 · 较宽环境</option></select></label>
      <p>输出1080×{height}；放大低分辨率裁剪不会增加细节。若各比例都切掉人物，可保留完整画面使用 contain 或换素材；无需用大文字填满剩余空间。</p>
      <div className="columns compact">{(["x", "y", "width", "height"] as const).map(key => <label key={key}>裁剪{({ x: "左边距", y: "上边距", width: "宽度", height: "高度" })[key]}（像素）<input type="number" min={key === "x" || key === "y" ? 0 : 1} step="1" value={crop[key]} disabled={busy} onChange={event => { setCrop(current => ({ ...current, [key]: event.target.value })); invalidateReview(); }} /></label>)}</div>
      <label>开始毫秒<input type="number" min="0" step="1" value={start} disabled={busy} onChange={event => { setStart(event.target.value); invalidateReview(); }} /></label>
      <label>结束毫秒<input type="number" min="1" step="1" value={end} disabled={busy} onChange={event => { setEnd(event.target.value); invalidateReview(); }} /></label>
      <label>原画面字幕<select value={subtitles} disabled={busy} onChange={event => { setSubtitles(event.target.value); setRegions({ x: "", y: "", width: "", height: "" }); invalidateReview(); }}><option value="">未知 / 尚未检查</option><option value="not_observed">检查后未观察到原字幕</option><option value="known_burned_in">存在已知原字幕</option></select></label>
      {subtitles === "known_burned_in" && <><p>填写包含全部原字幕的区域；裁剪须避开此区域。</p><div className="columns compact">{(["x", "y", "width", "height"] as const).map(key => <label key={key}>原字幕{({ x: "左边距", y: "上边距", width: "宽度", height: "高度" })[key]}（像素）<input type="number" min={key === "x" || key === "y" ? 0 : 1} step="1" value={regions[key]} disabled={busy} onChange={event => { setRegions(current => ({ ...current, [key]: event.target.value })); invalidateReview(); }} /></label>)}</div></>}
      <button type="button" className="button" disabled={busy || !clipId} onClick={() => void inspectFeasibility()}>检查裁剪可行性（不创建素材）</button>
      {feasibility && <div role="status"><p>保留源画面 {feasibility.retained_source_fraction === null ? "无法计算（裁剪越界）" : `${(100 * feasibility.retained_source_fraction).toFixed(1)}%`} · {feasibility.upscale_factor === null ? "比例或边界无效，不能等比例缩放" : `${feasibility.upscale_factor > 1 ? "放大" : "缩小"} ${feasibility.upscale_factor.toFixed(2)} 倍`}。像素比例不等于内容重要性；放大不会恢复细节。</p>
        <p>当前源字节：{feasibility.source_hash_verified ? "与记录一致" : "未验证"} · 人物完整区间安全：未知 · 执行许可：仍由创建接口复核。</p>
        {feasibility.stop_reasons.length ? <ul>{feasibility.stop_reasons.map(reason => <li className="error" key={reason}>{stopLabels[reason] ?? reason}</li>)}</ul> : <p>几何与已有否定证据未发现阻塞；仍须检查人物移动、头部空间和实际清晰度，不能据此宣称成片达标。</p>}
      </div>}
      <label>完整区间证据引用<input value={reference} disabled={busy} onChange={event => { setReference(event.target.value); setReview(""); setNotice(""); }} /></label>
      <label>当前裁剪完整区间检查<select value={review} disabled={busy} onChange={event => setReview(event.target.value)}><option value="">尚未完成</option><option value="reviewed">已连续检查：脸与头完整、固定框稳定、避开原字幕</option></select></label>
      <small>这是你对准确区间和裁剪框的检查声明；系统未自动判断人物安全，不等同于成片通过审核。修改参数后须重新检查。</small>
    </>}
    {error && <p className="error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    <button className="button" disabled={busy || !asset || !clipId || review !== "reviewed" || !reference.trim() || feasibility?.status === "blocked"}>创建已检查的比例素材</button>
  </form>;
}
