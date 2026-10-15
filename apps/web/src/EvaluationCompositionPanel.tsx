import {useEffect, useRef, useState} from "react";

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;
type Selection = {policy: "evaluation-composition-v1"; purpose: "internal_evaluation"; master_run_id: string; talking_runs: Record<string,string>; tail: "neutral_background"};
type Candidates = {masters: {id: string; eligible: boolean; reasons: string[]; scenes: {id: string; label: string}[]}[]; talking: {id: string; master_run_id: string; scene_plan_id: string; state: string; current_eligible: boolean; talking_run_id: string | null}[]};
type Plan = {eligible: boolean; fingerprint?: string; missing_scenes: string[]; reasons: string[]};
type Run = {id: string; project_id: string; state: string; reasons: string[]; current_eligible: boolean; technical_qa: unknown};
type Admission = {id: string; revoked_at: string | null; review: {request_sha256: string; selection: Selection}};
type Batch = {id: string; review: {master_run_id: string}};
type Recovery = {predecessor_id: string; scene_plan_id: string; successor_id: string | null; reasons: string[]; choices: {selection: Record<string,unknown>; start_fingerprint: string; fingerprint: string}[]};
const label = (reason: string) => reason.startsWith("composition_missing_talking:") ? "仍有片段需要完成人物画面并通过审核。" : ({
  composition_use_admission_required: "需要核对这些素材用于本次内部成片的许可。",
  composition_current_policy2_master_required: "主旁白当前不可用，请先检查旁白记录。",
  composition_preflight_changed: "素材或方案已变化，请重新检查。",
  composition_render_allowance_spent: "本批成片额度已占用，请恢复已有记录。",
  composition_source_use_excluded: "这项素材呈现曾被排除，请调整选择。",
  composition_talking_binding_mismatch: "人物片段与当前文案不匹配。",
  composition_use_admission_revoked_or_changed: "成片用途许可已失效，请核对。",
} as Record<string,string>)[reason] ?? "素材、许可或审核记录需要核对；请刷新检查。";

export function EvaluationCompositionPanel({projectId, draftVersion, scriptRevision, draftDirty, request}: {
  projectId: string; draftVersion: number; scriptRevision: number; draftDirty: boolean; request: Request;
}) {
  const base = `/projects/${projectId}/evaluation-composition`;
  const [candidates,setCandidates] = useState<Candidates>({masters:[],talking:[]});
  const [selection,setSelection] = useState<Selection | null>(null);
  const [plan,setPlan] = useState<Plan | null>(null);
  const [runs,setRuns] = useState<Run[]>([]);
  const [admissions,setAdmissions] = useState<Admission[]>([]);
  const [batches,setBatches] = useState<Batch[]>([]);
  const [recoveries,setRecoveries] = useState<Recovery[]>([]);
  const [recoveryRef,setRecoveryRef] = useState(""),[recoveryHash,setRecoveryHash] = useState("");
  const [busy,setBusy] = useState(false), [error,setError] = useState("");
  const [reviewRef,setReviewRef] = useState(""),[reviewHash,setReviewHash] = useState(""),[operator,setOperator] = useState("");
  const [adoptKind,setAdoptKind] = useState<"admissions"|"batches">("admissions");
  const epoch = useRef(0), locked = useRef(false);
  const selected = useRef(selection); selected.current = selection;
  async function load(token: number) {
    const [choices,saved,history,uses,grants,recovery] = await Promise.all([
      request<Candidates>(base+"/candidates"), request<{selection:Selection}[]>(base+"/selections"),
      request<Run[]>(base+"/runs"),request<Admission[]>(base+"/admissions"),request<Batch[]>(base+"/batches"),
      request<Recovery[]>(`/projects/${projectId}/evaluation-talking/recoveries`),
    ]);
    if (token !== epoch.current) return;
    setCandidates(choices); setRuns(history); setAdmissions(uses); setBatches(grants);
    setRecoveries(recovery);
    if (!selected.current && saved.length) setSelection(saved[0].selection);
  }
  useEffect(() => {
    const token = ++epoch.current;
    setSelection(null); selected.current=null; setPlan(null);setRuns([]);setRecoveries([]);setRecoveryRef("");setRecoveryHash("");setError("");setOperator("");setReviewRef("");setReviewHash("");setBusy(true);locked.current=true;
    load(token).catch(e=>{if(token===epoch.current)setError(e.message);}).finally(()=>{if(token===epoch.current){setBusy(false);locked.current=false;}});
    return ()=>{++epoch.current;};
  },[projectId,draftVersion,scriptRevision,draftDirty,request]);
  const master = candidates.masters.find(m=>m.id===selection?.master_run_id);
  const receipt = admissions.find(a=>!a.revoked_at && a.review.request_sha256===plan?.fingerprint);
  const batch = batches.find(b=>b.review.master_run_id===selection?.master_run_id);
  function change(next: Selection) {setSelection(next);selected.current=next;setPlan(null);setError("");}
  async function perform(action: "refresh"|"check"|"save"|"start"|"adopt") {
    if (locked.current || draftDirty && action!=="refresh") return;
    const token=epoch.current, inspected=selection;
    locked.current=true;setBusy(true);setError("");
    try {
      if(action==="refresh") {setPlan(null);await load(token);}
      else if(action==="adopt") {
        await request(base+"/"+adoptKind,{method:"POST",headers:{"x-content-os-execution-admission-key":operator},body:JSON.stringify({
          idempotency_key:`composition-use:${reviewHash}`,review_reference:reviewRef,review_sha256:reviewHash})});
        if(token===epoch.current){setOperator("");await load(token);}
      } else if(inspected) {
        const result = action==="check" ? await request<Plan>(base+"/preflight",{method:"POST",body:JSON.stringify(inspected)})
          : await request(base+(action==="save"?"/selections":"/runs"),{method:"POST",body:JSON.stringify({selection:inspected,
            expected_fingerprint:plan?.fingerprint,idempotency_key:`composition:${action}:${plan?.fingerprint}`,
            ...(action==="start"?{admission_id:receipt?.id,batch_id:batch?.id}:{})})});
        if(token===epoch.current && selected.current===inspected){if(action==="check")setPlan(result as Plan);else await load(token);}
      }
    } catch(e) {if(token===epoch.current)setError(String((e as Error).message??e));}
    finally {if(token===epoch.current){setBusy(false);locked.current=false;}}
  }
  async function reconcile(id: string) {
    if(locked.current)return;
    const token=epoch.current;locked.current=true;setBusy(true);setError("");
    try{await request(base+`/runs/${id}/reconcile`,{method:"POST"});await load(token);}
    catch(e){if(token===epoch.current)setError(String((e as Error).message??e));}
    finally{if(token===epoch.current){locked.current=false;setBusy(false);}}
  }
  async function recover(value: Recovery) {
    if(locked.current||draftDirty||!value.choices.length)return;
    const token=epoch.current, choice=value.choices[0];locked.current=true;setBusy(true);setError("");
    try {
      await request(`/projects/${projectId}/evaluation-talking/operations/${value.predecessor_id}/recovery`,{
        method:"POST",headers:{"x-content-os-execution-admission-key":operator},body:JSON.stringify({
          start:{...choice.selection,expected_fingerprint:choice.start_fingerprint,idempotency_key:`no-dispatch:${value.predecessor_id}:${choice.fingerprint}`},
          review_reference:recoveryRef,review_sha256:recoveryHash})});
      if(token===epoch.current){setOperator("");setRecoveryRef("");setRecoveryHash("");await load(token);}
    } catch(e){if(token===epoch.current)setError(String((e as Error).message??e));}
    finally{if(token===epoch.current){locked.current=false;setBusy(false);}}
  }
  return <section className="scene" aria-label="内部评估成片">
    <h3>用已通过的素材完成成片</h3>
    <p>选择完整旁白和各段已通过审核的人物画面。保留完整声音与字幕，尾部无对白时使用纯色背景。</p>
    <label>完整旁白 <select disabled={busy||draftDirty} value={selection?.master_run_id??""} onChange={e=>change({policy:"evaluation-composition-v1",purpose:"internal_evaluation",master_run_id:e.target.value,talking_runs:{},tail:"neutral_background"})}>
      <option value="">请选择已审核旁白</option>{candidates.masters.map((m,i)=><option key={m.id} value={m.id} disabled={!m.eligible}>旁白方案 {i+1}{m.eligible?"":"（当前不可用）"}</option>)}
    </select></label>
    {master?.scenes.map((s,index)=><label key={s.id} style={{display:"block"}}>第 {index+1} 段：{s.label}
      <select aria-label={`第${index+1}段人物画面`} disabled={busy||draftDirty} value={selection?.talking_runs[s.id]??""} onChange={e=>{
        if(!selection)return;const next={...selection.talking_runs};if(e.target.value)next[s.id]=e.target.value;else delete next[s.id];change({...selection,talking_runs:next});
      }}><option value="">尚未选择；请先完成人物片段审核</option>{candidates.talking.filter(t=>t.master_run_id===master.id&&t.scene_plan_id===s.id&&t.state==="admitted"&&t.current_eligible&&t.talking_run_id).map((t,i)=><option key={t.id} value={t.talking_run_id!}>已通过的画面 {i+1}</option>)}</select>
    </label>)}
    <button disabled={busy} onClick={()=>void perform("refresh")}>刷新素材和进度</button>
    <button disabled={busy||draftDirty||!selection} onClick={()=>void perform("check")}>检查成片方案</button>
    <button disabled={busy||draftDirty||!plan?.fingerprint} onClick={()=>void perform("save")}>保存素材选择</button>
    <button disabled={busy||draftDirty||!plan?.eligible||!receipt||!batch} onClick={()=>void perform("start")}>开始本批内部成片</button>
    {plan?.reasons.map((r,i)=><p key={i}>{label(r)}</p>)}
    {plan?.eligible&&!receipt&&<p>素材已齐备，仍需采用对应的成片用途核对结果。</p>}
    {!batch&&<p>本项目尚无已记录的成片执行额度。</p>}
    <details><summary>操作员：采用成片用途核对结果</summary>
      <label>核对结果类型 <select value={adoptKind} onChange={e=>{setAdoptKind(e.target.value as "admissions"|"batches");setOperator("");}}><option value="admissions">素材成片用途</option><option value="batches">已授权的执行额度</option></select></label>
      <label>核对记录 <input value={reviewRef} onChange={e=>setReviewRef(e.target.value)} /></label>
      <label>记录摘要 <input value={reviewHash} onChange={e=>setReviewHash(e.target.value)} /></label>
      <label>操作员凭据 <input type="password" autoComplete="off" value={operator} onChange={e=>setOperator(e.target.value)} /></label>
      <button disabled={busy||draftDirty||!reviewRef||!/^[a-f0-9]{64}$/.test(reviewHash)||!operator} onClick={()=>void perform("adopt")}>采用核对结果</button>
    </details>
    {recoveries.length>0&&<details><summary>操作员：接续调用前中断的人物片段</summary>
      <p>旧任务与记录保留。只有确认尚未调用模型、当前许可齐备的片段才能沿用原额度接续一次；已调用或执行情况不明时无法接续。</p>
      <p>当前仅支持采用已验证程序修正后的接续，暂不支持原样重建同一任务。</p>
      <label>恢复核对记录 <input value={recoveryRef} onChange={e=>setRecoveryRef(e.target.value)} /></label>
      <label>恢复记录摘要 <input value={recoveryHash} onChange={e=>setRecoveryHash(e.target.value)} /></label>
      <p>使用上方操作员凭据采用恢复核对。</p>
      {recoveries.map((r,i)=><article key={r.predecessor_id}><p>中断片段 {i+1}：{r.successor_id?"已有接续任务，原额度保持不变":r.choices.length?"当前条件齐备，可沿用原额度接续":"尚缺当前准入或恢复证据"}</p>
        {r.reasons.includes("talking_recovery_same_identity_unsupported")&&<p>当前运行身份没有可采用的修正，暂不支持接续。</p>}
        {!r.successor_id&&<button disabled={busy||draftDirty||!r.choices.length||!operator||!recoveryRef||!/^[a-f0-9]{64}$/.test(recoveryHash)} onClick={()=>void recover(r)}>采用恢复核对并接续片段 {i+1}</button>}
      </article>)}
    </details>}
    {runs.map((r,i)=><article key={r.id}><p>成片 {i+1}：{({pending:"等待制作",running:"制作中",failed:"制作失败，额度保留为已消耗",awaiting_u_product:"技术检查通过，等待成片审核",reconciliation_required:"已有结果，需要核对恢复"} as Record<string,string>)[r.state]??"暂不可用"}</p>
      {r.reasons.map((reason,j)=><p key={j}>{label(reason)}</p>)}
      {r.state==="reconciliation_required"&&<button disabled={busy} onClick={()=>void reconcile(r.id)}>核对已有成片并恢复</button>}
      {r.state==="awaiting_u_product"&&r.current_eligible&&<video controls preload="metadata" src={`${base}/runs/${r.id}/media?purpose=internal_evaluation`} style={{maxWidth:360,width:"100%"}} />}
    </article>)}
    {error&&<p role="alert">{error}</p>}
  </section>;
}
