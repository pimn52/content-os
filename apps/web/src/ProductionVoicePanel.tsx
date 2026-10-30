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
  project_id: string; run_id: string; fingerprint: string; action: "regenerate_take" | "scoped_fix_required" | "replan" | "stop";
  responsible_stage?: "material" | "timing" | "voice" | "talking" | "assembly" | "unknown"; assessment_state?: "known" | "unknown";
  scope?: { scene_id?: string | null; start_ms?: number | null; end_ms?: number | null; generation_segment_index?: number | null; start_char?: number | null; end_char?: number | null };
  preserves?: { master_audio_id?: string | null; master_audio_sha256?: string | null; talking_job_ids?: string[]; talking_output_asset_ids?: string[]; scene_plan_ids?: string[]; render_job_id?: string | null; reused_segment_sha256?: string[] };
  required_capability?: string[];
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
const repairStages: Record<string, string> = { material: "素材与用途", timing: "配时与节奏", voice: "声音", talking: "人物画面", assembly: "字幕与装配", unknown: "责任待定位" };
const repairCapabilities: Record<string, string> = {
  exact_audio_naturalness_diagnosis: "诊断确切音频的自然度",
  consumed_voice_dependency_repair: "声音修改后更新受影响的人物画面与成片",
  exact_audio_quality_failure_localization: "将声音质量缺陷定位到确切片段",
  exact_audio_failure_diagnosis: "诊断确切音频或生成失败的原因",
  voice_request_correction: "修正声音生成请求中的具体问题",
  voice_generation_segment_delivery_binding: "将声音生成片段与讲述意图准确对应",
  exact_scene_defect_diagnosis: "定位本场景缺陷的具体原因",
};
function errorMessage(reason: unknown) { return reason instanceof Error ? reason.message : "请求失败；请刷新执行状态后重试。"; }

export function ProductionVoicePanel({ projectId, draftVersion, scriptRevision, run, request, confirmAction = message => window.confirm(message), onRunChange }: {
  projectId: string; draftVersion: number; scriptRevision: number; run: ProductionRun; request: Request; confirmAction?: (message: string) => boolean; onRunChange: (run: ProductionRun) => void;
}) {
  const [profiles, setProfiles] = useState<VoiceProfile[]>([]);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [profileId, setProfileId] = useState("");
  const [capabilityId, setCapabilityId] = useState("");
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
      if (value.action !== "regenerate_take" || value.assessment_state === "unknown") setNotice("当前尚无可执行的声音修复；下面列出责任范围、待补能力与停止原因。");
    } catch (reason) { if (active()) setError(errorMessage(reason)); }
    finally { if (active()) { locked.current = false; setBusy(false); } }
  }

  async function submitRepair(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!repairPlan || repairPlan.action !== "regenerate_take" || repairPlan.assessment_state === "unknown" || repairPlan.stop_reasons.length || repairPlan.remaining_run_repairs <= 0 || !repairReason.trim() || locked.current) return;
    const repairDescription = repairPlan.execution_granularity === "segment" ? "只重做已定位的缺陷段，保留其它合格声音" : "重新生成同一旁白全文";
    if (!confirmAction(`${repairDescription}。至多 1 次本地 TTS 与 1 次独立 QA，外部收费上限 ${repairPlan.external_charge_ceiling}。原结果与证据保留，新主旁白重新完整核验并待复核。确认执行？`)) return;
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
      onRunChange(updated); setNotice("已创建声音修复任务；新主旁白完整核验后待复核。");
    } catch (reason) { if (active()) { setRepairPlan(null); setError(errorMessage(reason)); } }
    finally { if (active()) { locked.current = false; setBusy(false); } }
  }

  useEffect(() => {
    const token = ++choiceEpoch.current;
    setProfiles([]); setCapabilities([]); setProfileId(""); setCapabilityId(""); setError(""); setNotice("");
    Promise.all([
      request<VoiceProfile[]>(`/voice-profiles?project_id=${projectId}&default_only=true`),
      request<Capability[]>("/execution-settings/capabilities"),
    ]).then(([voice, evidence]) => {
      if (token !== choiceEpoch.current) return;
      setProfiles(voice);
      setProfileId(voice[0]?.id ?? "");
      if (!voice.length) setNotice("当前项目 IP 尚未指定授权声线，请先完成 IP 与 Voice Profile 绑定。");
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
    if (locked.current || action === "voice-dispatch" && (!selectedProfile || !selectedCapability)) return;
    const token = ++actionEpoch.current;
    const expectedScope = scope;
    const active = () => token === actionEpoch.current && expectedScope === currentScope.current;
    locked.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const body = action === "voice-dispatch" ? JSON.stringify({ voice_profile_id: profileId, capability_profile_id: capabilityId }) : undefined;
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
  const dispatchAllowed = canDispatchVoice(selectedProfile, selectedCapability);
  const eligibleForDispatch = run.status === "awaiting_approved_master";
  const eligibleForQa = run.status === "voice_completed_awaiting_qa" || run.status === "voice_qa_failed";

  return <section className="draft-editor" aria-label="此执行的主旁白">
    <div className="row-between"><div><span className="kicker">VOICE / MASTER NARRATION</span><h3>此执行的主旁白</h3></div><span className="badge">{run.status}</span></div>
    {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="ok">{notice}</p>}
    {eligibleForDispatch && <>
      <p className="muted">选择已登记且有明确本人同意的 Voice Profile，再选择匹配供应商的持久化能力档案。所有档案原样列出；资格、质量、许可与预算由后台派发时重验。这里不执行自动推荐。</p>
      <label>Voice Profile<select value={profileId} disabled={busy} onChange={event => setProfileId(event.target.value)}><option value="">请选择</option>{profiles.map(item => <option key={item.id} value={item.id}>{item.name} · {item.provider} · 同意：{item.consent.confirmed ? "已记录" : "未知"} · {item.consent.subject_name}</option>)}</select></label>
      <label>能力档案<select value={capabilityId} disabled={busy} onChange={event => setCapabilityId(event.target.value)}><option value="">请选择</option>{capabilities.map(item => <option key={item.id} value={item.id}>{item.provider} / {item.model} · {item.runtime} · {item.machine_id} · readiness={item.readiness} · quality={item.quality_status} · license={item.commercial_status}</option>)}</select></label>
      {selectedCapability && <p className="muted">来源：{selectedCapability.provenance_source} · 质量证据：{selectedCapability.evidence_reference ?? "未知"} · 许可证据：{selectedCapability.license_evidence_reference ?? "未知"} · scope：{selectedCapability.scope_key}</p>}
      <button type="button" className="button primary" disabled={busy || !dispatchAllowed} onClick={() => void perform("voice-dispatch")}>按当前执行方案启动旁白（后台校验）</button>
    </>}
    {eligibleForQa && <button type="button" className="button" disabled={busy} onClick={() => void perform("voice-qa-advance")}>推进独立 Voice QA（后台校验）</button>}
    {(run.status === "voice_failed" || run.status === "voice_qa_failed" || run.status === "u_voice_rejected" || repairPlan || repairRecords.length > 0) && <div className="subform">
      <h4>声音修复</h4><p>有确切分段核验时仅重做缺陷段，保留其它合格声音。新主旁白须重新完整核验并待复核。</p>
      {!repairPlan && ["voice_failed", "voice_qa_failed", "u_voice_rejected"].includes(run.status) && <button type="button" className="button" disabled={busy} onClick={() => void inspectRepair()}>检查 Voice 修复方案（只读）</button>}
      {repairPlan && <><p>判断：{repairPlan.failure_kind} · {repairPlan.action} · Run 剩余额度 {repairPlan.remaining_run_repairs} · QA：{repairPlan.review_scope}</p>
        <p>责任工序：{repairStages[repairPlan.responsible_stage ?? "voice"]} · 诊断：{repairPlan.assessment_state === "known" ? "已有本项诊断证据" : repairPlan.assessment_state === "unknown" ? "待补证据，尚未定位可执行修复" : "由后台候选复核"}</p>
        <p>问题范围：{repairPlan.scope?.scene_id ? `场景 ${repairPlan.scope.scene_id}` : "当前声音"}{repairPlan.scope?.start_ms != null && repairPlan.scope?.end_ms != null ? ` · ${repairPlan.scope.start_ms}–${repairPlan.scope.end_ms} ms` : " · 时间区间待定位"}</p>
        {repairPlan.scope?.generation_segment_index != null && <p>原生成第 {repairPlan.scope.generation_segment_index + 1} 段{repairPlan.scope.start_char != null && repairPlan.scope.end_char != null ? ` · 文案字符 ${repairPlan.scope.start_char}–${repairPlan.scope.end_char}` : ""}</p>}
        {!!repairPlan.required_capability?.length && <div><p>待补能力或证据：</p><ul>{repairPlan.required_capability.map(capability => <li key={capability}>{repairCapabilities[capability] ?? capability}</li>)}</ul></div>}
        {repairPlan.preserves && <><p>保留主旁白：{repairPlan.preserves.master_audio_id ?? "当前尚无主旁白"}{repairPlan.preserves.master_audio_sha256 ? ` · SHA-256 ${repairPlan.preserves.master_audio_sha256}` : ""}</p>
          <p>保留人物 Job：{repairPlan.preserves.talking_job_ids?.join(" · ") || "当前执行无已绑定人物 Job"}</p><p>保留人物输出：{repairPlan.preserves.talking_output_asset_ids?.join(" · ") || "当前执行无已绑定人物输出"}</p>
          <p>保留场景方案：{repairPlan.preserves.scene_plan_ids?.join(" · ") || "身份未知"} · 保留成片 Job：{repairPlan.preserves.render_job_id ?? "当前无成片"}</p></>}
        {!!repairPlan.preserves?.reused_segment_sha256?.length && <p>复用合格声音段 SHA-256：{repairPlan.preserves.reused_segment_sha256.join(" · ")}</p>}
        <p>后台范围：{repairPlan.execution_granularity} · TTS 上限 {repairPlan.max_tts_calls} · QA ASR 上限 {repairPlan.max_qa_asr_calls} · 外部收费上限 {repairPlan.external_charge_ceiling} · Worker 实时就绪：{repairPlan.worker_readiness} · 本机成本/用户时间未知</p>
        {repairPlan.stop_reasons.length > 0 && <ul>{repairPlan.stop_reasons.map(reason => <li className="error" key={reason}>{reason}</li>)}</ul>}
        {repairPlan.action === "regenerate_take" && repairPlan.assessment_state !== "unknown" && <form className="subform" onSubmit={event => void submitRepair(event)}>
          <label>修复理由<input value={repairReason} disabled={busy} onChange={event => setRepairReason(event.target.value)} required maxLength={2000} /></label>
          <button className="button primary" disabled={busy || !repairReason.trim() || repairPlan.stop_reasons.length > 0 || repairPlan.remaining_run_repairs <= 0}>按方案修复声音</button>
        </form>}
        {(repairPlan.action === "scoped_fix_required" || repairPlan.action === "replan" || repairPlan.assessment_state === "unknown") && <p className="muted">此声音缺陷尚未定位可执行的修复范围。保留当前音频和其它合格成果，先补齐声音诊断，再确定需修改的语音片段及下游复检范围。</p>}
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
