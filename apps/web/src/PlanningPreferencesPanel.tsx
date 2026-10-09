import { useEffect, useRef, useState } from "react";
const errorText = (value: unknown) => value instanceof Error ? value.message : String(value);

type Observation = { id: string; request: { finding: string; reason: string; evidence_reference: string; render_sha256: string } };
type Preference = { id: string; rule: string; version: number; state: string; classification: string; authority_source?: string; profile_version: number; profile_revision_origin?: string; current_stop_reasons?: string[]; stop_reasons: string[]; retained_case: { before: { caption_emphasis: string[] }; after: { caption_emphasis: string[] }; passed: boolean } | null };
type RetainedChoice = { manifest_path: string; expected_manifest_sha256: string; evidence_level: string; original_review: { approved: false; reviewed_sha256: string; evidence_reference: string; reviewed_at: string; scope: string; findings: string[] } };
type RetainedUse = { use_id: string; scene_id: string; source_label: string; start_ms: number; end_ms: number; scope_authority: string; presentation: { width: number; height: number; visual_role: string; framing_policy: string; portrait_presentation: string; burned_in_subtitles: string; subtitle_treatment: string; graphic_treatment: string; graphic_text: string | null } | null };
type UseConstraint = { id: string; version: number; state: "candidate" | "enabled" | "disabled"; classification: string; evidence_class: "assisted_test" | "fixture"; reason: string; current_stop_reasons: string[]; original_review: RetainedChoice["original_review"]; uses: RetainedUse[]; adopted_use_ids: string[] };
type Props = { projectId: string; request<T>(path: string, options?: RequestInit): Promise<T> };

export function PlanningPreferencesPanel({ projectId, request }: Props) {
  const [observations, setObservations] = useState<Observation[]>([]);
  const [preferences, setPreferences] = useState<Preference[]>([]);
  const [retainedChoices, setRetainedChoices] = useState<RetainedChoice[]>([]);
  const [useConstraints, setUseConstraints] = useState<UseConstraint[]>([]);
  const [selectedManifest, setSelectedManifest] = useState("");
  const [evidenceClass, setEvidenceClass] = useState<"" | "assisted_test" | "fixture">("");
  const [selectedUseIds, setSelectedUseIds] = useState<Record<string, string[]>>({});
  const [useHistories, setUseHistories] = useState<Record<string, UseConstraint[]>>({});
  const [selectionScope, setSelectionScope] = useState<{ profile_version: number; rule: string } | null>(null);
  const [classification, setClassification] = useState("creator_preference");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const epoch = useRef(0);
  const readGeneration = useRef(0);
  const lock = useRef(false);
  const root = `/projects/${projectId}`;
  async function load(token: number) {
    const generation = ++readGeneration.current;
    const [obs, rules, scope, choices, constraints] = await Promise.all([
      request<Observation[]>(`${root}/planning-observations`), request<Preference[]>(`${root}/planning-preferences`),
      request<{ profile_version: number; rule: string }>(`${root}/planning-preference-selection`),
      request<RetainedChoice[]>(`${root}/retained-render-rejections`),
      request<UseConstraint[]>(`${root}/source-use-constraints`),
    ]);
    if (epoch.current === token && generation === readGeneration.current) { setObservations(obs); setPreferences(rules); setSelectionScope(scope); setRetainedChoices(choices); setUseConstraints(constraints); }
  }
  useEffect(() => {
    const token = ++epoch.current;
    lock.current = false;
    setBusy(false); setObservations([]); setPreferences([]); setSelectionScope(null); setRetainedChoices([]); setUseConstraints([]); setSelectedManifest(""); setEvidenceClass(""); setSelectedUseIds({}); setUseHistories({}); setReason(""); setError(""); setClassification("creator_preference");
    void load(token).catch(e => { if (epoch.current === token) setError(errorText(e)); });
    return () => { ++epoch.current; };
  }, [projectId, request]);
  async function act(path: string, body: object, confirmation?: string, method = confirmation ? "PUT" : "POST") {
    if (lock.current || (confirmation && !window.confirm(confirmation))) return;
    const token = epoch.current;
    ++readGeneration.current;
    lock.current = true; setBusy(true); setError("");
    try {
      await request(path, { method, body: JSON.stringify(body) });
      if (epoch.current === token) { setReason(""); await load(token); }
    } catch (e) { if (epoch.current === token) { setError(errorText(e)); await load(token).catch(() => {}); } }
    finally { if (epoch.current === token) { lock.current = false; setBusy(false); } }
  }
  const confirmationFor = (operation: string) => `确认${operation}？这会新增一条创作者范围的未来用法设置；原成片拒绝及其判断范围保持原样，不表示源素材已通过审核。`;
  async function intakeSelected() {
    const choice = retainedChoices.find(item => item.manifest_path === selectedManifest);
    if (!choice || !evidenceClass) return;
    await act(`${root}/source-use-constraints`, { manifest_path: choice.manifest_path,
      expected_manifest_sha256: choice.expected_manifest_sha256, evidence_class: evidenceClass,
      confirmed_source: true, reason, idempotency_key: `retained-intake:${crypto.randomUUID()}` },
      confirmationFor("确认此保留文件来自本创作者项目，并加入待确认拒绝证据"), "POST");
  }
  async function changeUseConstraint(rule: UseConstraint, enabled: boolean) {
    const selected = enabled ? (selectedUseIds[rule.id] ?? rule.adopted_use_ids) : [];
    await act(`${root}/source-use-constraints/${rule.id}`, { expected_version: rule.version,
      enabled, use_ids: selected, reason, idempotency_key: `retained-use:${crypto.randomUUID()}` },
      enabled ? confirmationFor("仅为所选素材区间和呈现方式采纳未来避用范围") : confirmationFor("禁用这条未来避用范围"), "PUT");
  }
  async function loadUseHistory(rule: UseConstraint) {
    const token = epoch.current;
    try {
      const history = await request<UseConstraint[]>(`${root}/source-use-constraints/${rule.id}/versions`);
      if (epoch.current === token) setUseHistories(current => ({ ...current, [rule.id]: history }));
    } catch (e) { if (epoch.current === token) setError(errorText(e)); }
  }
  return <section className="panel"><h3>反馈采纳到下一次规划</h3>
    <p className="muted">支持直接选择“图形要点优先已有语义短句”，或从准确成片观察采纳偏好。两者来源分开。只影响新请求的 ScenePlan；不修改现有旁白、成片或审核，不授予素材许可。资产缺陷/配置限制只分类留存，不能成为通用偏好。</p>
    {error && <p className="error">{error}</p>}
    <label>采纳 / 禁用理由<input value={reason} onChange={e => setReason(e.target.value)} /></label>
    <button className="button" disabled={busy || !selectionScope || !reason.trim()} onClick={() => selectionScope && void act(`${root}/planning-preference-selection`, { expected_profile_version: selectionScope.profile_version, rule: selectionScope.rule, reason }, "确认将语义短句优先保存为此创作者的偏好候选？这不是成片审核，也不会立即启用。", "POST")}>保存我的短句偏好候选（无需成片缺陷）</button>
    <label>反馈分类<select value={classification} onChange={e => setClassification(e.target.value)}>
      <option value="creator_preference">创作者呈现偏好（需明确采纳）</option><option value="asset_defect">资产缺陷（不泛化）</option><option value="configuration_limit">配置限制（不泛化）</option>
    </select></label>
    <button className="button" disabled={busy} onClick={() => { const token = epoch.current; void load(token).catch(e => { if (epoch.current === token) setError(errorText(e)); }); }}>刷新反馈与偏好</button>
    <div className="panel">
      <h3>保留旧成片拒绝，并单独选择未来避用范围</h3>
      <p className="muted">导入只保留原始整片拒绝及其证据。下面列出的区间是从成片方案读出的未来避用建议，不是人审逐场景结论。先确认来源，再单独选择范围；不会自动采纳，也不代表素材或新版成片通过审核。</p>
      <label>保留的拒绝成片<select value={selectedManifest} onChange={event => setSelectedManifest(event.target.value)}>
        <option value="">请选择本项目中的保留文件</option>{retainedChoices.map(choice => <option key={choice.manifest_path} value={choice.manifest_path}>{choice.manifest_path} · {choice.original_review.scope}</option>)}
      </select></label>
      <label>证据来源<select value={evidenceClass} onChange={event => setEvidenceClass(event.target.value as typeof evidenceClass)}>
        <option value="">请选择</option><option value="assisted_test">已有成片的人审证据</option><option value="fixture">仅测试夹具</option>
      </select></label>
      <button className="button" disabled={busy || !selectedManifest || !evidenceClass || !reason.trim()} onClick={() => void intakeSelected()}>确认来源并加入待确认拒绝</button>
      {retainedChoices.map(choice => <div className="asset-row" key={choice.manifest_path}>
        <strong>原审核结论：拒绝 · {choice.original_review.scope}</strong>
        <p>{choice.original_review.findings.map((finding, index) => <span key={index}>{finding}{index + 1 < choice.original_review.findings.length ? "；" : ""}</span>)}</p>
        <small>{choice.original_review.evidence_reference} · {choice.original_review.reviewed_at} · 被审成片 SHA-256 {choice.original_review.reviewed_sha256}</small>
      </div>)}
      {useConstraints.map(rule => {
        const stops = rule.current_stop_reasons ?? [];
        const chosen = selectedUseIds[rule.id] ?? rule.adopted_use_ids;
        const usable = rule.uses.filter(use => use.presentation !== null);
        const canAdopt = !busy && rule.evidence_class !== "fixture" && stops.length === 0 && chosen.length > 0;
        return <div className="panel" key={rule.id}>
          <strong>未来用法设置 · v{rule.version} · {rule.state === "candidate" ? "待明确范围" : rule.state === "enabled" ? "已采纳" : "已禁用"}</strong>
          <p>原审核：拒绝 · {rule.original_review.scope} · {rule.original_review.evidence_reference}</p>
          <p className="muted">范围来源：从完整成片方案提取，仅表示用户可选择的未来避用建议；不是分场景审核。证据类别：{rule.evidence_class === "fixture" ? "测试夹具（不可采纳为规则）" : "既有辅助人审"}。理由：{rule.reason}</p>
          {stops.length > 0 && <p className="error">当前不可采纳：{stops.join(" · ")}</p>}
          {!usable.length && <p className="error">没有可准确还原的呈现范围；此证据只能保留，不能用于自动避用。</p>}
          {rule.uses.map(use => <label className="asset-row" key={use.use_id}>
            <input type="checkbox" disabled={busy || rule.evidence_class === "fixture" || stops.length > 0 || use.presentation === null || rule.state === "enabled"}
              checked={chosen.includes(use.use_id)} onChange={event => setSelectedUseIds(current => ({ ...current,
                [rule.id]: event.target.checked ? [...new Set([...chosen, use.use_id])] : chosen.filter(id => id !== use.use_id) }))} />
            <span><strong>{use.scene_id} · {use.source_label} · 原素材 {use.start_ms}–{use.end_ms} ms</strong>
              {use.presentation ? <small>{use.presentation.width}×{use.presentation.height} · {use.presentation.visual_role} · {use.presentation.portrait_presentation} · 源字幕 {use.presentation.burned_in_subtitles} · 新字幕 {use.presentation.subtitle_treatment} · 图文 {use.presentation.graphic_treatment}{use.presentation.graphic_text ? `：${use.presentation.graphic_text}` : ""}</small>
                : <small>呈现细节无法完整还原，需先重新规划；不能按此范围自动决策。</small>}</span>
          </label>)}
          <p>已选范围：{chosen.length} 段。适用时仍需满足原有来源准入、人物画面、授权、成本和成片审核要求。</p>
          {rule.state !== "enabled" ? <button className="button" disabled={!canAdopt || !reason.trim()} onClick={() => void changeUseConstraint(rule, true)}>明确采纳所选未来避用范围</button>
            : <button className="button" disabled={busy || !reason.trim()} onClick={() => void changeUseConstraint(rule, false)}>禁用并恢复原推荐</button>}
          <button className="button" disabled={busy} onClick={() => void loadUseHistory(rule)}>查看采纳与禁用历史</button>
          {(useHistories[rule.id] ?? []).map(version => <p className="muted" key={`${version.id}:${version.version}`}>v{version.version} · {version.state === "enabled" ? "已采纳" : version.state === "disabled" ? "已禁用" : "候选"} · {version.reason} · 范围 {(version.adopted_use_ids ?? []).map(id => { const use = version.uses.find(item => item.use_id === id); return use ? `${use.scene_id} ${use.source_label} ${use.start_ms}–${use.end_ms} ms` : ""; }).filter(Boolean).join("；") || "未采纳"}</p>)}
          <details><summary>查看原始拒绝证据</summary><p>{rule.original_review.findings.map((finding, index) => <span key={index}>{finding}{index + 1 < rule.original_review.findings.length ? "；" : ""}</span>)}</p>
            <small>审核时间 {rule.original_review.reviewed_at} · 成片 SHA-256 {rule.original_review.reviewed_sha256} · 范围 {rule.original_review.scope}</small></details>
        </div>;
      })}
      {!retainedChoices.length && !useConstraints.length && <p className="muted">当前项目没有发现可导入的保留拒绝文件或未来避用记录。</p>}
    </div>
    {!observations.length && <p className="muted">此项目尚无准确 Render 绑定的呈现观察。文本反馈不是媒体资格证据。</p>}
    {observations.map(obs => <div className="panel" key={obs.id}><strong>{obs.request.finding}</strong><p>{obs.request.reason}</p><small>{obs.request.evidence_reference} · SHA-256 {obs.request.render_sha256}</small>
      <button className="button" disabled={busy} onClick={() => void act(`${root}/planning-preferences`, { observation_id: obs.id, classification })}>分类并验证候选（不启用）</button>
    </div>)}
    {preferences.map(rule => { const stops = rule.current_stop_reasons ?? rule.stop_reasons; return <div className="panel" key={rule.id}>
      <strong>{rule.rule} · v{rule.version} · {rule.state}</strong><p>分类 {rule.classification} · 此创作者 IP 修订 {rule.profile_version}</p>
      <p className="muted">来源：{rule.authority_source === "creator_selection" ? "创作者主动选择；不属于成片质量证据" : "准确成片观察；保留原判断"}</p>
      {rule.profile_revision_origin === "migration_snapshot_not_historical_revision" && <p className="muted">IP v1 为迁移时保存的当前基线，不冒称旧时的历史修订记录。</p>}
      {rule.retained_case && <p>留存案例：{rule.retained_case.before.caption_emphasis[0]} → {rule.retained_case.after.caption_emphasis[0]} · 回归 {rule.retained_case.passed ? "通过（非真实质量认可）" : "未通过"}</p>}
      {stops.length > 0 && <p className="error">停止：{stops.join(" · ")}</p>}
      <button className="button" disabled={busy || !reason.trim() || (rule.state !== "enabled" && stops.length > 0)} onClick={() => void act(`${root}/planning-preferences/${rule.id}`, { expected_version: rule.version, enabled: rule.state !== "enabled", reason }, rule.state === "enabled" ? "确认禁用此偏好？下次规划恢复原有规则，现有稿件不变。" : "确认此偏好仅适用于本创作者？新主题会选择已提供的语义短句代替重复全文的图形文字；缺少替代时仍须重规划。现有稿件不变。")}>{rule.state === "enabled" ? "禁用并恢复原规划行为" : "明确启用此创作者偏好"}</button>
    </div>; })}
  </section>;
}
