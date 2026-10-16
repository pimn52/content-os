import {useEffect, useRef, useState} from "react";

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;

type Recovery = {predecessor_id: string; scene_plan_id: string; successor_id: string | null; reasons: string[]; choices: {selection: Record<string,unknown>; start_fingerprint: string; fingerprint: string}[]};

// Use admission remains an operator action. Selection, start and
// playback belong to the ordinary ProductionPanel and its existing Run.
export function EvaluationCompositionPanel({projectId, draftVersion, scriptRevision, draftDirty, request, onAdopt, enableGenerationRecovery = false}: {
  projectId: string; draftVersion: number; scriptRevision: number; draftDirty: boolean; request: Request; onAdopt?: () => void; enableGenerationRecovery?: boolean;
}) {
  const [kind,setKind] = useState("admissions"), [reference,setReference] = useState("");
  const [hash,setHash] = useState(""), [key,setKey] = useState("");
  const [busy,setBusy] = useState(false), [error,setError] = useState("");
  const [recoveries,setRecoveries] = useState<Recovery[]>([]);
  const [recoveryRef,setRecoveryRef] = useState(""),[recoveryHash,setRecoveryHash] = useState("");
  const operator=key, setOperator=setKey;
  const epoch = useRef(0), locked = useRef(false);
  useEffect(() => {
    ++epoch.current; locked.current=false; setBusy(false); setError("");
    setReference("");setHash("");setKey("");setRecoveries([]);setRecoveryRef("");setRecoveryHash("");
    const token=epoch.current;
    if(enableGenerationRecovery) request<Recovery[]>(`/projects/${projectId}/evaluation-talking/recoveries`)
      .then(value=>{if(token===epoch.current)setRecoveries(value);})
      .catch(e=>{if(token===epoch.current)setError(String(e.message));});
    return () => {++epoch.current;};
  },[projectId,draftVersion,scriptRevision,draftDirty,enableGenerationRecovery,request]);
  async function adopt() {
    if(locked.current || draftDirty || !reference || !/^[a-f0-9]{64}$/.test(hash) || !key)return;
    const token=epoch.current;locked.current=true;setBusy(true);setError("");
    try {
      await request(`/projects/${projectId}/evaluation-composition/${kind}`,{method:"POST",
        headers:{"x-content-os-execution-admission-key":key},body:JSON.stringify({
          idempotency_key:`web-composition-${kind}:${projectId}:${hash}`,review_reference:reference,review_sha256:hash})});
      if(token===epoch.current){setKey("");setReference("");setHash("");onAdopt?.();}
    } catch(e){if(token===epoch.current)setError(String((e as Error).message??e));}
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
      if(token===epoch.current){setOperator("");setRecoveryRef("");setRecoveryHash("");const fresh=await request<Recovery[]>(`/projects/${projectId}/evaluation-talking/recoveries`);if(token===epoch.current)setRecoveries(fresh);}
    } catch(e){if(token===epoch.current)setError(String((e as Error).message??e));}
    finally{if(token===epoch.current){locked.current=false;setBusy(false);}}
  }
  return <details><summary>操作员：已审核素材的本次成片用途与额度</summary>
    <p>普通方案自动选择匹配的完整旁白和人物画面。用途许可单独核对，仅限已记录的本地非商业测试，不授权新模型调用。</p>
    <label>核对结果类型 <select value={kind} disabled={busy} onChange={e=>{setKind(e.target.value);setKey("");}}><option value="admissions">素材成片用途</option><option value="batches">一次本地渲染额度</option></select></label>
    <label>核对记录 <input value={reference} onChange={e=>setReference(e.target.value)} /></label>
    <label>记录摘要 <input value={hash} onChange={e=>setHash(e.target.value)} /></label>
    <label>操作员凭据 <input type="password" autoComplete="off" value={key} onChange={e=>setKey(e.target.value)} /></label>
    <button disabled={busy||draftDirty||!reference||!/^[a-f0-9]{64}$/.test(hash)||!key} onClick={()=>void adopt()}>采用核对结果</button>
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
    {error&&<p role="alert">{error}</p>}
  </details>;
}
