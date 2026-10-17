import { useEffect, useRef, useState } from "react";
import { ExecutionPurposeControl, commercialChoice, type ExecutionChoice } from "./ExecutionPurposeControl";
import { VoiceEvaluationQA } from "./VoiceEvaluationQA";
import { VoiceEvaluationHumanReview } from "./VoiceEvaluationHumanReview";
import { AudioAudition } from "./AudioAudition";
import { recoveryIdempotencyKey } from "./production";

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type Profile = { id: string; name: string; provider: string; consent: { confirmed: boolean } };
type Capability = { id: string; capability: string; provider: string; mode: string; model: string; readiness: string; quality_status: string };
type Reference = { selection: Record<string, unknown>; transcript: string; duration_ms: number };
type SavedDraft = { job_id: string; project_id: string; policy: string; job_status: string; created_at: string; text_preview: string };
type PlanSource = { version: number; scriptRevision: number; text: string; hasScenes: boolean; dirty: boolean };
type Status = { target_text?: string | null; policy?: string; job_id: string; project_id: string; state: string; job_status: string; reasons: string[]; error_code: string | null;
  generation_eligibility?: { state: string; reasons: string[]; dispatch_authorized: false };
  recovery_action: string | null; outputs: Array<{ audio_id: string; qa_state: string | null;
    qa_consumption?: { state: string; purpose: string; reasons: string[]; execution_configuration: string; dispatch_authorized: false } }> };

export function VoiceDraftPanel({ projectId, request, planSource }: { projectId: string; request: Request; planSource?: PlanSource }) {
  const [textSource, setTextSource] = useState(planSource ? "saved-plan" : "standalone");
  const planScope = JSON.stringify(planSource ?? null);
  const [policy, setPolicy] = useState("voice-draft-v1");
  const [choice, setChoice] = useState<ExecutionChoice>(commercialChoice);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [references, setReferences] = useState<Reference[]>([]);
  const [profileId, setProfileId] = useState("");
  const [capabilityId, setCapabilityId] = useState("");
  const [referenceId, setReferenceId] = useState("");
  const [text, setText] = useState("");
  const effectiveText = textSource === "saved-plan" ? planSource?.text ?? "" : text;
  const planReady = textSource !== "saved-plan" || !!planSource && !planSource.dirty && planSource.hasScenes && !!effectiveText.trim();
  const [authorization, setAuthorization] = useState("");
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState("");
  const selectedJob = useRef(""); selectedJob.current = `${projectId}:${jobId}`;
  const [savedDrafts, setSavedDrafts] = useState<SavedDraft[]>([]);
  const [historyError, setHistoryError] = useState("");
  const historyEpoch = useRef(0);
  const epoch = useRef(0);
  const catalogEpoch = useRef(0);
  const referenceEpoch = useRef(0);
  const locked = useRef(false);
  const scope = JSON.stringify([projectId, policy, choice, profileId, capabilityId, referenceId, effectiveText, authorization, textSource, planScope]);
  const current = useRef(scope); current.current = scope;
  const bookmark = `content-os:voice-draft:${projectId}`;

  useEffect(() => {
    const token = ++catalogEpoch.current; ++epoch.current;
    locked.current = false; setBusy(false); setChoice(commercialChoice()); setPolicy("voice-draft-v1"); setProfiles([]); setCapabilities([]);
    setProfileId(""); setCapabilityId(""); setReferenceId(""); setReferences([]); setText(""); setAuthorization("");
    setStatus(null); setError(""); setNotice(""); setJobId("");
    setSavedDrafts([]); setHistoryError("");
    try { setJobId(localStorage.getItem(bookmark) ?? ""); } catch { setNotice("本地恢复记录不可读；已保存的后台任务不受影响。"); }
    Promise.all([request<Profile[]>(`/voice-profiles?project_id=${projectId}&default_only=true`), request<Capability[]>("/execution-settings/capabilities")]).then(([voice, capabilities]) => {
      if (token !== catalogEpoch.current) return;
      setProfiles(voice.filter(p => p.consent.confirmed && p.provider === "omnivoice"));
      setProfileId(voice.find(p => p.consent.confirmed && p.provider === "omnivoice")?.id ?? "");
      if (!voice.length) setNotice("当前项目 IP 尚未指定授权声线，请先完成 IP 与 Voice Profile 绑定。");
      setCapabilities(capabilities.filter(c => c.capability === "voice" && c.provider === "omnivoice" && c.mode === "local"));
    }).catch(reason => { if (token === catalogEpoch.current) setError(String(reason.message ?? reason)); });
    return () => { ++catalogEpoch.current; ++epoch.current; };
  }, [projectId, request]);

  const planBoundary = `${planScope}:${textSource}`;
  const priorPlanBoundary = useRef(planBoundary);
  useEffect(() => {
    if (priorPlanBoundary.current === planBoundary) return;
    priorPlanBoundary.current = planBoundary;
    ++epoch.current; locked.current = false; setBusy(false); setError("");
    if (textSource === "saved-plan") { setChoice(commercialChoice()); setAuthorization(""); }
  }, [planScope, textSource]);

  async function loadSavedDrafts() {
    const token = ++historyEpoch.current;
    try {
      const values = await request<SavedDraft[]>(`/projects/${projectId}/voice-draft-jobs`);
      if (token !== historyEpoch.current) return;
      if (values.some(value => value.project_id !== projectId)) throw new Error("草稿列表与当前项目不一致。");
      setSavedDrafts(values); setHistoryError("");
    } catch (reason) {
      if (token === historyEpoch.current) setHistoryError(String((reason as Error).message ?? reason));
    }
  }
  useEffect(() => {
    void loadSavedDrafts();
    return () => { ++historyEpoch.current; };
  }, [projectId, request]);

  useEffect(() => {
    const token = ++referenceEpoch.current;
    setReferences([]); setReferenceId("");
    if (profileId) request<Reference[]>(`/voice-profiles/${profileId}/reference-windows`).then(value => {
      if (token === referenceEpoch.current) setReferences(value);
    }).catch(reason => { if (token === referenceEpoch.current) setError(String(reason.message ?? reason)); });
    return () => { ++referenceEpoch.current; };
  }, [profileId, projectId, request]);

  async function refresh(identity = jobId) {
    if (!identity) return;
    const expectedProject = projectId;
    const value = await request<Status>(`/projects/${projectId}/voice-draft-jobs/${encodeURIComponent(identity)}`);
    if (value.project_id !== expectedProject || value.job_id !== identity) throw new Error("草稿状态与当前项目/任务不一致。");
    return value;
  }
  useEffect(() => {
    let active = true;
    setStatus(null);
    if (jobId) refresh().then(value => { if (active && value) setStatus(value); }).catch(reason => { if (active) setError(String(reason.message ?? reason)); });
    return () => { active = false; };
  }, [projectId, jobId, request]);

  async function submit() {
    const selected = references[Number(referenceId)];
    if (locked.current || !planReady || !selected || referenceId === "" || choice.purpose !== "internal_evaluation") return;
    const token = ++epoch.current; const expected = scope;
    const active = () => token === epoch.current && expected === current.current;
    locked.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const body = { draft_policy: policy, purpose: choice.purpose, use_admission_ids: choice.use_admission_ids,
        execution_capability_profile_id: capabilityId, voice_profile_id: profileId, text: textSource === "saved-plan" ? effectiveText : effectiveText.trim(),
        ...(textSource === "saved-plan" && planSource ? { saved_plan_source: { version: planSource.version, script_revision: planSource.scriptRevision } } : {}),
        authorization_reference: authorization.trim(), reference_window: selected.selection };
      const key = await recoveryIdempotencyKey("voice-draft", projectId, profileId, policy, body);
      if (!active()) return;
      const job = await request<{ id: string; project_id: string }>(`/projects/${projectId}/voice-jobs`, { method: "POST", body: JSON.stringify({ ...body, idempotency_key: key }) });
      if (!active()) return;
      if (job.project_id !== projectId) throw new Error("创建结果与当前项目不一致。");
      setJobId(job.id);
      try { localStorage.setItem(bookmark, job.id); } catch { setNotice("任务已保存；浏览器恢复记录写入失败。请保留任务编号或从后台任务列表找回。"); }
      void loadSavedDrafts();
      const updated = await refresh(job.id);
      if (active() && updated) setStatus(updated);
    } catch (reason) { if (active()) setError(String((reason as Error).message ?? reason)); }
    finally { if (token === epoch.current) { locked.current = false; setBusy(false); } }
  }
  const selectedCapability = capabilities.find(c => c.id === capabilityId);
  function selectSavedDraft(identity: string) {
    setStatus(null); setError(""); setJobId(identity);
    try { localStorage.setItem(bookmark, identity); } catch { setNotice("后台草稿已找回；浏览器恢复记录写入失败。"); }
  }
  async function inspectStatus() {
    const expected = scope, expectedJob = `${projectId}:${jobId}`;
    try { const value = await refresh(); if (expected === current.current && expectedJob === selectedJob.current && value) setStatus(value); }
    catch (reason) { if (expected === current.current && expectedJob === selectedJob.current) setError(String((reason as Error).message ?? reason)); }
  }
  const ready = choice.purpose === "internal_evaluation" && choice.use_admission_ids.length > 0 && profileId && referenceId !== ""
    && (policy === "voice-calibration-v1" ? ["configured", "available", "verified"].includes(selectedCapability?.readiness ?? "") : selectedCapability?.readiness === "verified" && selectedCapability?.quality_status === "verified") && effectiveText.trim() && authorization.trim() && planReady;
  return <section className="card" aria-label="Voice 评估草稿">
    <label>已保存的语音草稿<select disabled={busy} value={jobId} onChange={e => selectSavedDraft(e.target.value)}>
      <option value="">选择已有草稿</option>
      {jobId && !savedDrafts.some(value => value.job_id === jobId) && <option value={jobId}>已记住的草稿</option>}
      {savedDrafts.map(value => <option key={value.job_id} value={value.job_id}>{value.text_preview} · {value.job_status} · {value.created_at}</option>)}
    </select></label>
    <p>显示本项目最近 50 条评估草稿；选择已有草稿不会再次生成，试听仍按当前权限检查。</p>
    <button className="button" disabled={busy} onClick={() => void loadSavedDrafts()}>刷新已保存草稿</button>
    {historyError && <p className="error">草稿列表读取失败：{historyError}</p>}
    <h3>Voice 评估草稿</h3>
    <p className="muted">为新文案创建一条可试听草稿。需要当前生成凭据、精确配置证据、授权参考与有限次数预算；本地耗时与成本未知。草稿不会自动成为 Master。</p>
    <ExecutionPurposeControl projectId={projectId} value={choice} onChange={setChoice} disabled={busy} request={request} lane="voice-draft-v1" />
    {choice.purpose !== "internal_evaluation" && <p>此草稿入口需要显式内部评估用途；普通生产流程仍使用原有入口。</p>}
    <label>草稿验证方式<select disabled={busy} value={policy} onChange={e => { setPolicy(e.target.value); setChoice({ ...choice, use_admission_ids: [] }); }}><option value="voice-draft-v1">已验证配置草稿</option><option value="voice-calibration-v1">首次验证草稿（质量未知，仅一次）</option></select></label>
    {policy === "voice-calibration-v1" && <p>需要绑定本次文案和参考的一次性准入记录；这一条草稿同时验证运行，不自动确认声线质量。失败或中断后须核对调用账本。</p>}
    <label>草稿声线<select disabled={busy} value={profileId} onChange={e => setProfileId(e.target.value)}><option value="">选择已授权声线</option>{profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
    <label>精确 Voice 配置<select disabled={busy} value={capabilityId} onChange={e => setCapabilityId(e.target.value)}><option value="">选择配置证据</option>{capabilities.map(c => <option key={c.id} value={c.id}>{c.model} · {c.readiness} / {c.quality_status}</option>)}</select></label>
    <label>授权参考片段<select disabled={busy} value={referenceId} onChange={e => setReferenceId(e.target.value)}><option value="">选择参考窗口</option>{references.map((r, index) => <option key={index} value={String(index)}>{r.transcript} · {r.duration_ms}ms</option>)}</select></label>
    {planSource && <label>旁白文案来源<select disabled={busy} value={textSource} onChange={e => setTextSource(e.target.value)}><option value="saved-plan">当前保存方案的完整文案</option><option value="standalone">单独评估文案</option></select></label>}
    {textSource === "saved-plan" && <p>{planReady ? `使用保存方案 v${planSource?.version} / 文案修订 ${planSource?.scriptRevision}；场景文案按顺序合并，已有完整脚本优先。` : "请先保存完整文案与场景方案；未保存内容不能创建此方案的旁白。"} 此处仅选择原文，表演方向不会自动应用；创建后仍须技术 QA、人审和主旁白用途准入。</p>}
    <label>草稿文案<textarea disabled={busy} readOnly={textSource === "saved-plan"} value={effectiveText} onChange={e => { if (textSource === "standalone") setText(e.target.value); }} /></label>
    <label>本次生成授权引用<input disabled={busy} value={authorization} onChange={e => setAuthorization(e.target.value)} /></label>
    <button className="button" disabled={busy || !ready} onClick={() => void submit()}>创建一条评估草稿</button>
    {jobId && <><p>验证方式：{status?.policy ?? policy}</p><p>任务：{jobId} · {status?.job_status ?? "读取中"} · {status?.state}</p><button className="button" disabled={busy} onClick={() => void inspectStatus()}>刷新草稿状态</button></>}
    {status?.target_text != null && <p>已保存草稿完整文案：{status.target_text}</p>}
    {planSource && status?.target_text != null && <p>{!planSource.dirty && planSource.hasScenes && status.target_text === planSource.text ? "已保存草稿与当前保存方案的完整文案一致；主旁白用途仍需独立检查。" : "此历史草稿与当前方案不匹配或方案未保存；可按原权限审核，不能据此宣称当前方案主旁白就绪。"}</p>}
    {status?.reasons.length ? <p className="error">{status.reasons.join("、")}</p> : null}
    {status?.job_status === "completed" && status.generation_eligibility?.state === "blocked" && <p>已有草稿的试听权限单独检查；当前配置不能用于新的生成：{status.generation_eligibility.reasons.join("、")}。</p>}
    {status?.recovery_action && <p>恢复步骤：{status.recovery_action}。核对已有调用记录后处理；不会自动再次生成。</p>}
    {status?.outputs.map(audio => <div key={audio.audio_id}><p>技术 QA 状态：{audio.qa_state ?? "未知"}；人审单独记录，生产使用仍需准入。</p>
      {audio.qa_consumption && <p>{audio.qa_consumption.state === "blocked"
        ? `商业 QA 的用途检查未通过：${audio.qa_consumption.reasons.join("、")}。`
        : "商业 QA 用途检查已通过，执行配置仍未检查。"} 试听权限不代表 QA 可执行；需要适用用途准入与独立 ASR 配置。</p>}
      <VoiceEvaluationQA projectId={projectId} audioId={audio.audio_id} request={request}
        onResult={identity => { if (identity === audio.audio_id) void inspectStatus(); }} />
      <AudioAudition audioId={audio.audio_id} projectId={projectId} request={request} />
      <VoiceEvaluationHumanReview projectId={projectId} audioId={audio.audio_id} qaState={audio.qa_state} request={request} /></div>)}
    {notice && <p>{notice}</p>}{error && <p className="error">{error}</p>}
  </section>;
}
