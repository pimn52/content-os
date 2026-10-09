import { useEffect, useRef, useState, type FormEvent } from "react";
import { canDispatchVoice, recoveryIdempotencyKey, recoveryIdentity, voiceCandidateMatches, type ProductionRun } from "./production";

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type VoiceProfile = {
  id: string; name: string; provider: string; provider_profile_id: string; consent: { subject_name: string; confirmed: boolean; authorization_reference: string | null };
};
type Capability = {
  id: string; scope_key: string; capability: string; mode: string; provider: string; model: string; runtime: string; machine_id: string;
  readiness: string; quality_status: string; commercial_status: string; evidence_reference: string | null;
  license_evidence_reference: string | null; provenance_source: string; feature_support: Record<string, string>;
};
type VoiceAudio = {
  id: string; content_hash: string; duration_ms: number; authorization_reference: string;
  metadata: Record<string, unknown> & { voice_generation?: Record<string, unknown> };
};
type RepairPlan = {
  project_id: string; run_id: string; fingerprint: string; action: "regenerate_take" | "replan" | "stop";
  failure_kind: string; stop_reasons: string[]; remaining_run_repairs: number; max_tts_calls: number;
  max_qa_asr_calls: number; external_charge_ceiling: string; local_compute_cost: null; user_active_minutes: null;
  execution_granularity: string; review_scope: string; worker_readiness: string; evidence: Record<string, unknown>;
};
type RepairRecord = {
  id: string; reason: string; replacement_job_id: string; old_job_ids: Array<string | null>;
  old_audio_id: string | null; successor_qa_job_id?: string | null; replacement_job_status?: string;
  successor_audio?: Array<{ id: string; content_hash: string; duration_ms: number }>;
  provider_calls?: Array<{ operation: string; status: string; estimated_cost: { amount: string | null }; actual_cost: { amount: string | null } | null }>;
};
const dimensions = [
  ["likeness", "本人相似度"], ["naturalness", "自然度"], ["emphasis", "重点表达"],
  ["pace", "语速"], ["pauses", "停顿"], ["rhythm", "节奏"],
] as const;
function errorMessage(reason: unknown) { return reason instanceof Error ? reason.message : "请求失败；请刷新执行状态后重试。"; }

export function ProductionVoicePanel({ projectId, draftVersion, scriptRevision, run, request, confirmAction = message => window.confirm(message), onRunChange }: {
  projectId: string; draftVersion: number; scriptRevision: number; run: ProductionRun; request: Request; confirmAction?: (message: string) => boolean; onRunChange: (run: ProductionRun) => void;
}) {
  const [profiles, setProfiles] = useState<VoiceProfile[]>([]);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [profileId, setProfileId] = useState("");
  const [capabilityId, setCapabilityId] = useState("");
  const [authorization, setAuthorization] = useState("");
  const [audio, setAudio] = useState<VoiceAudio | null>(null);
  const [mediaUrl, setMediaUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [repairPlan, setRepairPlan] = useState<RepairPlan | null>(null);
  const [repairRecords, setRepairRecords] = useState<RepairRecord[]>([]);
  const [repairReason, setRepairReason] = useState("");
  const choiceEpoch = useRef(0);
  const candidateEpoch = useRef(0);
  const actionEpoch = useRef(0);
  const currentScope = useRef("");
  const locked = useRef(false);
  const scope = `${projectId}:${draftVersion}:${scriptRevision}:${run.preflight_fingerprint}:${run.id}:${run.status}:${run.voice_job_id ?? "none"}:${run.voice_audio_id ?? "none"}:${run.voice_qa_job_id ?? "none"}`;
  currentScope.current = scope;
  const selectedProfile = profiles.find(item => item.id === profileId);
  const selectedCapability = capabilities.find(item => item.id === capabilityId);

  useEffect(() => {
    const token = ++actionEpoch.current; const expected = scope;
    locked.current = false; setBusy(false);
    setRepairPlan(null); setRepairRecords([]); setRepairReason("");
    const base = `/projects/${projectId}/production-runs/${run.id}`;
    request<RepairRecord[]>(base + "/voice-repairs").then(value => {
      if (token === actionEpoch.current && expected === currentScope.current) setRepairRecords(value);
    }).catch(reason => { if (token === actionEpoch.current && expected === currentScope.current) setError(errorMessage(reason)); });
    return () => { if (token === actionEpoch.current) ++actionEpoch.current; };
  }, [projectId, run.id, scope, request]);

  async function inspectRepair() {
    if (locked.current) return;
    const token = ++actionEpoch.current; const expected = scope;
    const active = () => token === actionEpoch.current && expected === currentScope.current;
    setBusy(true); locked.current = true; setError(""); setNotice("");
    try {
      const value = await request<RepairPlan>(`/projects/${projectId}/production-runs/${run.id}/voice-repair-plan`);
      if (!active()) return;
      if (value.project_id !== projectId || value.run_id !== run.id) throw new Error("后台修复方案与当前项目/执行不一致，已拒绝使用。");
      setRepairPlan(value);
      if (value.action !== "regenerate_take") setNotice("后台建议暂停或重规划；下面列出完整停止原因。");
    } catch (reason) { if (active()) setError(errorMessage(reason)); }
    finally { if (active()) { locked.current = false; setBusy(false); } }
  }

  async function submitRepair(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!repairPlan || repairPlan.action !== "regenerate_take" || repairPlan.stop_reasons.length || repairPlan.remaining_run_repairs <= 0 || !repairReason.trim() || locked.current) return;
    if (!confirmAction(`将以同一旁白全文重新生成一次（至多 1 次本地 TTS + 1 次独立本地 QA ASR；外部收费上限 ${repairPlan.external_charge_ceiling}，本机耗时未知）。原 AudioAsset/QA/U-Voice 保留为历史。新结果必须重新做完整 QA 和 U-Voice 判断。确认吗？`)) return;
    const token = ++actionEpoch.current; const expected = scope;
    const active = () => token === actionEpoch.current && expected === currentScope.current;
    locked.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const body = { expected_fingerprint: repairPlan.fingerprint,
        idempotency_key: await recoveryIdempotencyKey("voice-repair", projectId, run.id, repairPlan.fingerprint, repairReason.trim()),
        confirmed_action: "regenerate_take", reason: repairReason.trim() };
      if (!active()) return;
      await request<RepairRecord>(`/projects/${projectId}/production-runs/${run.id}/voice-repair`, { method: "POST", body: JSON.stringify(body) });
      const [updated, records] = await Promise.all([
        request<ProductionRun>(`/projects/${projectId}/production-runs/${run.id}`),
        request<RepairRecord[]>(`/projects/${projectId}/production-runs/${run.id}/voice-repairs`),
      ]);
      if (!active() || updated.id !== run.id || updated.project_id !== projectId) return;
      setRepairRecords(records); setRepairPlan(null); setRepairReason("");
      onRunChange(updated); setNotice("已创建整段 Voice 新候选；等待 Worker 完成后，将独立 QA 并提交新的 U-Voice 判断。");
    } catch (reason) { if (active()) { setRepairPlan(null); setError(errorMessage(reason)); } }
    finally { if (active()) { locked.current = false; setBusy(false); } }
  }

  useEffect(() => {
    const token = ++choiceEpoch.current;
    setProfiles([]); setCapabilities([]); setProfileId(""); setCapabilityId(""); setAuthorization(""); setError(""); setNotice("");
    Promise.all([
      request<VoiceProfile[]>("/voice-profiles"),
      request<Capability[]>("/execution-settings/capabilities"),
    ]).then(([voice, evidence]) => {
      if (token !== choiceEpoch.current) return;
      setProfiles(voice);
      setCapabilities(evidence.filter(item => item.capability === "voice"));
    }).catch(reason => { if (token === choiceEpoch.current) setError(String(reason.message ?? reason)); });
    return () => { ++choiceEpoch.current; };
  }, [projectId, request]);

  useEffect(() => {
    const token = ++candidateEpoch.current;
    setAudio(null); setMediaUrl(""); setError("");
    if (!run.voice_audio_id || !run.voice_job_id) return () => { ++candidateEpoch.current; };
    const expectedVoiceJobId = run.voice_job_id;
    let objectUrl: string | null = null;
    async function loadCandidate() {
      try {
        const candidate = await request<VoiceAudio>(`/audio-assets/${run.voice_audio_id}`);
        if (!voiceCandidateMatches(candidate.metadata, projectId, expectedVoiceJobId)) {
          throw new Error("运行记录绑定的音频与 Voice Job/项目来源不一致，已停止试听和审核。");
        }
        const headers = new Headers();
        const accessToken = window.sessionStorage.getItem("content-os-access-token");
        if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
        const response = await fetch(`/audio-assets/${candidate.id}/media`, { headers });
        if (!response.ok) throw new Error(`旁白候选试听加载失败 (${response.status})`);
        objectUrl = URL.createObjectURL(await response.blob());
        if (token !== candidateEpoch.current) { URL.revokeObjectURL(objectUrl); objectUrl = null; return; }
        setAudio(candidate); setMediaUrl(objectUrl);
      } catch (reason) { if (token === candidateEpoch.current) setError(errorMessage(reason)); }
    }
    void loadCandidate();
    return () => { ++candidateEpoch.current; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [projectId, draftVersion, scriptRevision, request, run.id, run.voice_audio_id, run.voice_job_id, run.status]);

  async function perform(action: "voice-dispatch" | "voice-qa-advance") {
    if (locked.current || action === "voice-dispatch" && (!selectedProfile || !selectedCapability || !authorization.trim())) return;
    const token = ++actionEpoch.current;
    const expectedScope = scope;
    const active = () => token === actionEpoch.current && expectedScope === currentScope.current;
    locked.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const body = action === "voice-dispatch" ? JSON.stringify({ voice_profile_id: profileId, capability_profile_id: capabilityId, authorization_reference: authorization.trim() }) : undefined;
      const updated = await request<ProductionRun>(`/projects/${projectId}/production-runs/${run.id}/${action}`, {
        method: "POST", ...(body ? { body } : {}),
      });
      if (updated.project_id !== projectId || updated.id !== run.id) throw new Error("后台返回的执行记录与当前项目不一致。");
      if (active()) { onRunChange(updated); setNotice(action === "voice-dispatch" ? "Voice 已按当前方案交由后台派发；请查看运行状态与停止原因。" : "已请求后台推进独立 Voice QA；等待状态更新后再审核。"); }
    } catch (reason) { if (active()) setError(errorMessage(reason)); }
    finally { if (active()) { locked.current = false; setBusy(false); } }
  }

  async function submitReview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (locked.current || !audio || run.status !== "awaiting_u_voice_review") return;
    const form = new FormData(event.currentTarget);
    const outcomes = Object.fromEntries(dimensions.map(([key]) => [key, String(form.get(key) ?? "")])) as Record<string, string>;
    if (Object.values(outcomes).some(value => value !== "pass" && value !== "needs_revision")) { setError("请逐项完成六个维度的判断。"); return; }
    const findings = String(form.get("findings") ?? "").split(/\r?\n/).map(item => item.trim()).filter(Boolean);
    const evidenceReference = String(form.get("evidence_reference") ?? "").trim();
    if (!findings.length || !evidenceReference) { setError("请填写审核证据引用和至少一条具体发现。"); return; }
    if (!confirmAction(`此次 U-Voice 判断将固定到 AudioAsset ${audio.id}（SHA-256 ${audio.content_hash}），且不能覆盖。确定提交吗？`)) return;
    const token = ++actionEpoch.current;
    const expectedScope = scope;
    const active = () => token === actionEpoch.current && expectedScope === currentScope.current;
    locked.current = true; setBusy(true); setError(""); setNotice("");
    try {
      await request<VoiceAudio>(`/projects/${projectId}/voice-assets/${audio.id}/human-review`, { method: "POST", body: JSON.stringify({
        approved: Object.values(outcomes).every(value => value === "pass"), evidence_reference: evidenceReference, findings, ...outcomes,
      }) });
      if (!active()) return;
      const updatedRun = await request<ProductionRun>(`/projects/${projectId}/production-runs/${run.id}`);
      if (!active()) return;
      onRunChange(updatedRun); setNotice("此 U-Voice 判断已绑定到当前 AudioAsset。生产继续需由你显式续跑。");
    } catch (reason) { if (active()) setError(errorMessage(reason)); }
    finally { if (active()) { locked.current = false; setBusy(false); } }
  }

  const generation = audio?.metadata.voice_generation;
  const existingReview = generation?.human_review as { approved?: boolean; evidence_reference?: string; findings?: string[]; likeness?: string; naturalness?: string; emphasis?: string; pace?: string; pauses?: string; rhythm?: string } | undefined;
  const dispatchAllowed = canDispatchVoice(selectedProfile, selectedCapability, authorization);
  const eligibleForDispatch = run.status === "awaiting_approved_master";
  const eligibleForQa = run.status === "voice_completed_awaiting_qa" || run.status === "voice_qa_failed";

  return <section className="draft-editor" aria-label="此执行的主旁白">
    <div className="row-between"><div><span className="kicker">VOICE / MASTER NARRATION</span><h3>此执行的主旁白</h3></div><span className="badge">{run.status}</span></div>
    {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="ok">{notice}</p>}
    {eligibleForDispatch && <>
      <p className="muted">选择已登记且有明确本人同意的 Voice Profile，再选择匹配供应商的持久化能力档案。所有档案原样列出；资格、质量、许可与预算由后台派发时重验。这里不执行自动推荐。</p>
      <label>Voice Profile<select value={profileId} disabled={busy} onChange={event => { setProfileId(event.target.value); setAuthorization(""); }}><option value="">请选择</option>{profiles.map(item => <option key={item.id} value={item.id}>{item.name} · {item.provider} · 同意：{item.consent.confirmed ? "已记录" : "未知"} · {item.consent.subject_name}</option>)}</select></label>
      <label>能力档案<select value={capabilityId} disabled={busy} onChange={event => setCapabilityId(event.target.value)}><option value="">请选择</option>{capabilities.map(item => <option key={item.id} value={item.id}>{item.provider} / {item.model} · {item.runtime} · {item.machine_id} · readiness={item.readiness} · quality={item.quality_status} · license={item.commercial_status}</option>)}</select></label>
      {selectedCapability && <p className="muted">来源：{selectedCapability.provenance_source} · 质量证据：{selectedCapability.evidence_reference ?? "未知"} · 许可证据：{selectedCapability.license_evidence_reference ?? "未知"} · scope：{selectedCapability.scope_key}</p>}
      <label>此次 Voice 授权引用<input value={authorization} disabled={busy} onChange={event => setAuthorization(event.target.value)} required /></label>
      <button type="button" className="button primary" disabled={busy || !dispatchAllowed} onClick={() => void perform("voice-dispatch")}>按当前执行方案启动旁白（后台校验）</button>
    </>}
    {eligibleForQa && <button type="button" className="button" disabled={busy} onClick={() => void perform("voice-qa-advance")}>推进独立 Voice QA（后台校验）</button>}
    {(run.status === "voice_failed" || run.status === "voice_qa_failed" || run.status === "u_voice_rejected" || repairPlan || repairRecords.length > 0) && <div className="subform">
      <h4>Voice 整段恢复</h4><p>范围：同一完整旁白，不剪接。可执行时最多 1 次本地 TTS 与 1 次独立本地 QA ASR；外部收费上限 0，本地耗时未知。新候选需完整 QA 与新的 U-Voice 判断。</p>
      {!repairPlan && ["voice_failed", "voice_qa_failed", "u_voice_rejected"].includes(run.status) && <button type="button" className="button" disabled={busy} onClick={() => void inspectRepair()}>检查 Voice 修复方案（只读）</button>}
      {repairPlan && <><p>判断：{repairPlan.failure_kind} · {repairPlan.action} · Run 剩余额度 {repairPlan.remaining_run_repairs} · QA：{repairPlan.review_scope}</p>
        <p>后台范围：{repairPlan.execution_granularity} · TTS 上限 {repairPlan.max_tts_calls} · QA ASR 上限 {repairPlan.max_qa_asr_calls} · 外部收费上限 {repairPlan.external_charge_ceiling} · Worker 实时就绪：{repairPlan.worker_readiness} · 本机成本/用户时间未知</p>
        {repairPlan.stop_reasons.length > 0 && <ul>{repairPlan.stop_reasons.map(reason => <li className="error" key={reason}>{reason}</li>)}</ul>}
        {repairPlan.action === "regenerate_take" && <form className="subform" onSubmit={event => void submitRepair(event)}>
          <label>本次整段修复理由<input value={repairReason} disabled={busy} onChange={event => setRepairReason(event.target.value)} required maxLength={2000} /></label>
          <button className="button primary" disabled={busy || !repairReason.trim() || repairPlan.stop_reasons.length > 0 || repairPlan.remaining_run_repairs <= 0}>按此方案创建整段 Voice 候选</button>
        </form>}
        {repairPlan.action === "replan" && <p className="muted"><a href="#project-draft-editor">编辑当前稿件</a>，保存后<a href="#production-preflight">重新预检整条方案</a>。主观不合格需要重规划；此处不会剪接原音频。</p>}
      </>}
      {repairRecords.map(record => <article className="scene" key={record.id}><strong>Voice 修复历史 · {record.replacement_job_status ?? "排队"}</strong>
        <p>理由：{record.reason} · 前序 Job：{record.old_job_ids.filter(Boolean).join("、") || "无"} · 前序 AudioAsset：{record.old_audio_id ?? "无"}</p>
        <p>替代 Job：{record.replacement_job_id} · 新 QA Job：{record.successor_qa_job_id ?? "等待 QA"}</p>
        {(record.successor_audio ?? []).map(candidate => <p key={candidate.id}>新候选 AudioAsset {candidate.id} · {candidate.duration_ms} ms · SHA-256 {candidate.content_hash}</p>)}
        {(record.provider_calls ?? []).map((call, index) => <small key={index}>{call.operation} · {call.status} · 估算 {call.estimated_cost?.amount ?? "未知"} · 实际 {call.actual_cost?.amount ?? "未知"}</small>)}
      </article>)}
    </div>}
    {run.voice_qa_job_id && <p>技术 QA Job：{run.voice_qa_job_id}</p>}
    {run.voice_qa_auto_stop_reasons.length > 0 && <ul>{run.voice_qa_auto_stop_reasons.map(reason => <li className="error" key={reason}>{reason}</li>)}</ul>}
    {audio && <article className="scene" aria-label="Run 绑定的旁白候选">
      <h4>Run 绑定的旁白候选</h4><p>AudioAsset {audio.id} · {audio.duration_ms} ms · SHA-256 {audio.content_hash}</p>
      {mediaUrl ? <audio controls preload="none" src={mediaUrl} /> : <p role="alert">试听不可用</p>}
      <p>QA 状态：{String(generation?.qa_state ?? "unknown")} · Run Voice Job：{run.voice_job_id}</p>
      {typeof generation?.qa_findings === "string" && <p>QA 发现：{generation.qa_findings}</p>}
      {existingReview && <div><strong>已存在不可覆盖的 U-Voice 判断：{existingReview.approved ? "通过" : "未通过"}</strong><p>{existingReview.evidence_reference} · {existingReview.findings?.join("；")}</p><p>{dimensions.map(([key, label]) => `${label}：${existingReview[key] ?? "unknown"}`).join(" · ")}</p></div>}
      {run.status === "awaiting_u_voice_review" && !existingReview && <form key={`${run.id}:${audio.id}`} onSubmit={event => void submitReview(event)} className="subform">
        <h4>针对上述音频提交六维 U-Voice 判断</h4>
        {dimensions.map(([key, label]) => <label key={key}>{label}<select name={key} defaultValue="" required><option value="" disabled>请选择</option><option value="pass">通过</option><option value="needs_revision">需修改</option></select></label>)}
        <label>审核证据引用<input name="evidence_reference" required /></label>
        <label>具体发现（每行一条）<textarea name="findings" required rows={3} /></label>
        <button className="button primary" disabled={busy}>提交不可覆盖的判断（仅绑定此音频）</button>
      </form>}
    </article>}
    {!run.voice_audio_id && run.voice_job_id && <p>旁白任务正在处理或等待 QA；候选音频尚未由此 Run 绑定。</p>}
    <small className="muted">人审仅评价上方显示的同一 AudioAsset；不完成其他场景的人物画面、不自动续跑，也不替代 TalkingRun/成片审核。</small>
  </section>;
}
