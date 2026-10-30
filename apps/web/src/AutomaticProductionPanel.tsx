import {useEffect,useRef,useState,type FormEvent} from "react";
import {PresentationRecoveryPanel} from "./PresentationRecoveryPanel";
import {preflightReasonLabel,productionLabels,type ProductionNextAction,type ProductionRun} from "./production";
type Request=<T>(path:string,options?:RequestInit)=>Promise<T>;
type MotionPool={state:string;occupancy_state:string;usable_total_ms:number;longest_continuous_ms:number};
type MachineDiagnosis={source:string;method:string;provider?:string|null;model?:string|null;call_id?:string|null;state:string;limitations?:string[]};
type MaterialRecheck={state:"pass"|"failed"|"unknown";assessment_state:string;responsible_stage:string;limitations:string[];
  required_capability:string[];evidence_current:boolean|null;scope?:{scene_id?:string;scene_ids?:string[]}|null};
type QualityFinding={finding:string;responsible_stage:string;assessment_state:string;scope:{scene_id:string;start_ms:number|null;end_ms:number|null};required_capability:string[];
  machine_diagnosis?:MachineDiagnosis;evidence_current?:boolean};
type Status={start_job_id:string;status:string;error_code:string|null;elapsed_ms:number;run:ProductionRun|null;output_width?:number;output_height?:number;
  next_action:ProductionNextAction;responsible_stage?:string;stop_reasons?:string[];
  quality_diagnosis?:(QualityFinding&{findings?:QualityFinding[]})|null;
  material_recheck?:MaterialRecheck|null;
  topic?:string;can_resume?:boolean;resume_draft_version?:number;origin_job_id?:string;
  material_requests?:Array<{scene_id:string;source_kind:string;query:string;reason:string;viewer_takeaway:string|null}>;
  phases:Array<{type:string;status:string;duration_ms:number|null}>};
const names:Record<string,string>={plan_content:"内容与素材规划",generate_voice:"克隆旁白",verify_voice:"旁白质检",generate_talking:"人物画面",verify_talking:"人物画面质检",render:"成片渲染"};
const stages:Record<string,string>={planning:"内容与素材规划",material:"素材与用途",timing:"讲述配时",voice:"旁白",talking:"人物画面",assembly:"装配与呈现",unknown:"缺陷定位",render:"成片渲染",review:"整片审核"};
const qualityNames:Record<string,string>={duplicate_text:"重复文字",known_subtitle_conflict:"字幕冲突",text_overlay:"文字遮挡",face_out_of_frame:"人物出框",source_change:"来源变化",unknown_subtitles:"原字幕待核验",voice_naturalness:"声音自然度",talking_naturalness:"人物自然度",visible_sync:"口型与声音同步",semantic_mismatch:"内容语义",narrative_rhythm:"讲述节奏"};
const phaseLabels:Record<string,string>={pending:"等待",running:"进行中",completed:"完成",failed:"失败",cancelled:"已取消"};
export function AutomaticProductionPanel({projectId,topic,request}:{projectId:string;topic:string;request:Request}) {
  const [input,setInput]=useState(topic),[status,setStatus]=useState<Status|null>(null),[error,setError]=useState("");
  const [motionPool,setMotionPool]=useState<MotionPool|null>(null);
  const [busy,setBusy]=useState(false);const epoch=useRef(0),locked=useRef(false);
  const pendingKey=useRef<{topic:string;key:string;parent:string|null;resume:string|null}|null>(null);
  useEffect(()=>{const token=++epoch.current;setInput(topic);setStatus(null);setError("");setBusy(false);locked.current=false;pendingKey.current=null;
    setMotionPool(null);void request<MotionPool>(`/projects/${projectId}/talking-pool`).then(value=>{if(token===epoch.current&&value&&Number.isFinite(value.usable_total_ms)&&Number.isFinite(value.longest_continuous_ms))setMotionPool(value);}).catch(()=>{});
    const read=()=>request<Status|null>(`/projects/${projectId}/auto-produce`).then(value=>{if(token===epoch.current)setStatus(value);}).catch(cause=>{if(token===epoch.current)setError(String(cause.message??cause));});
    void read();const timer=setInterval(()=>void read(),2000);return()=>{++epoch.current;clearInterval(timer);};
  },[projectId,request]);
  const stopped=status?.next_action==="stopped";
  const done=status?.next_action==="review";
  const verifyingMaterial=status?.next_action==="verify_material";
  const running=!!status&&!done&&!stopped;
  const qualityFindings=status?.quality_diagnosis?.findings??(status?.quality_diagnosis?[status.quality_diagnosis]:[]);
  const hasSemanticDiagnosis=qualityFindings.some(finding=>!!finding.machine_diagnosis);
  async function start(event:FormEvent,comparison=false,resume=false){event.preventDefault();if(locked.current||running||!input.trim())return;
    const token=epoch.current;locked.current=true;setBusy(true);setError("");
    const parent=comparison?status?.run?.id??null:null;
    const source=resume?status?.start_job_id??null:null;
    if(pendingKey.current?.topic!==input.trim()||pendingKey.current.parent!==parent||pendingKey.current.resume!==source)pendingKey.current={topic:input.trim(),key:`video:${projectId}:${crypto.randomUUID()}`,parent,resume:source};
    try{await request(`/projects/${projectId}/auto-produce`,{method:"POST",body:JSON.stringify({topic:input.trim(),idempotency_key:pendingKey.current.key,...(parent?{candidate_of_run_id:parent}:{}),
      ...(source?{resume_from_job_id:source,expected_draft_version:status?.resume_draft_version}:{})})});
      const value=await request<Status>(`/projects/${projectId}/auto-produce`);if(token===epoch.current){setStatus(value);pendingKey.current=null;}
    }catch(cause){if(token===epoch.current)setError(String((cause as Error).message??cause));}
    finally{if(token===epoch.current){locked.current=false;setBusy(false);}}
  }
  return <section aria-label="输入主题自动制作" className="draft-editor"><h3>输入主题，制作短视频</h3>
    <form onSubmit={event=>void start(event)}><label>这条视频讲什么<textarea value={input} onChange={event=>setInput(event.target.value)} required maxLength={5000} disabled={busy||running}/></label>
      <button className="button primary" disabled={busy||running||!input.trim()}>开始制作短视频</button>
      {stopped&&status?.can_resume&&<button type="button" className="button" disabled={busy||running||input.trim()!==status.topic} onClick={event=>void start(event,false,true)}>沿用稿件继续制作</button>}
      {done&&status?.run&&<button type="button" className="button" disabled={busy||running||!input.trim()} onClick={event=>void start(event,true)}>制作同作品对比版</button>}</form>
    <p className="muted">沿用已配置的创作者声音、人像、素材和预算。后台完成规划、生成、质检与渲染，再审核整片。</p>
    {motionPool&&<p className="muted">本人动作素材：{motionPool.occupancy_state!=="known"?"占用情况待核实":
      `可用总时长 ${(motionPool.usable_total_ms/1000).toFixed(1)} 秒，最长连续 ${(motionPool.longest_continuous_ms/1000).toFixed(1)} 秒`}。声音参考独立配置，实际出镜按旁白时长分配。</p>}
    {error&&<p role="alert" className="error">{error}</p>}
    {status&&<><h4>{!status.next_action?"正在读取制作状态":done&&status.run?.review_state==="rejected"?"成片已打回，需修复":done?"成片已输出，待完整质量审核":stopped?"本次制作未完成":verifyingMaterial?"正在复核替换后的实际画面":status.next_action==="diagnose_semantic"?"正在对照成片与原素材":["diagnose_qa","diagnose_quality"].includes(status.next_action)?"正在检查并安排局部修复":productionLabels[status.status]??(status.status==="pending"?"等待后台开始":"正在规划制作")}</h4>
      {status.responsible_stage&&<p>当前工序：{stages[status.responsible_stage]??"视频制作"}</p>}
      {verifyingMaterial&&<p className="muted">旁白、人物结果和其它场景继续保留；后台复验后再交整片审核。</p>}
      {status.material_recheck&&<p className="muted">{status.material_recheck.evidence_current===false?"替换画面的复验结果已失效；保留成果，需重新定位。":status.material_recheck.state==="pass"?"替换场景的机器画面复验通过，仍待整片人审。":status.material_recheck.state==="failed"?"替换后的画面复验未通过；保留现有成果，需修正受影响场景。":"替换后的画面尚未确认符合讲述含义；保留现有成果，需继续定位。"}
        {status.material_recheck.scope?.scene_id&&` 复验场景：${status.material_recheck.scope.scene_id}。`}</p>}
      {status.quality_diagnosis&&<p className="muted">质量问题定位于场景 {status.quality_diagnosis.scope.scene_id}，交回{stages[status.quality_diagnosis.responsible_stage]??"缺陷定位"}处理。
        {status.next_action==="diagnose_semantic"?"后台正在核验本场景画面与讲述含义，保留已有成果。":status.next_action==="diagnose_quality"?"后台正在合并核验局部修正候选，保留合格旁白与人物结果。":status.quality_diagnosis.assessment_state==="unknown"?"尚未确定可执行的局部修正；保留已有成果，不据此重做全文、旁白或全部人物画面。":hasSemanticDiagnosis?"已记录画面问题的责任；局部修正仍需具备对应能力，当前成果继续保留。":"修正仅影响已定位的依赖，之后仍需局部及整片复检。"}</p>}
      {(status.next_action==="diagnose_semantic"||hasSemanticDiagnosis||verifyingMaterial||status.material_recheck)&&<p className="muted">机器仅对照静音画面与旁白文字，不评价听感自然度、口型同步，也不代表最终人审通过。</p>}
      {!!qualityFindings.length&&<ul>{qualityFindings.map((finding,index)=><li key={index}>{finding.scope.scene_id} · {qualityNames[finding.finding]??"质量问题"} · {stages[finding.responsible_stage]??"缺陷定位"}
        {finding.machine_diagnosis&&` · 机器画面语义对照：${finding.evidence_current===false?"原诊断证据已失效，需重新定位":finding.machine_diagnosis.state==="known"?"已定位本项责任，待核验局部修正":"尚未确定本项责任"}`}</li>)}</ul>}
      <p>{status.origin_job_id&&status.origin_job_id!==status.start_job_id?"本次恢复用时":`从启动到${done?"输出":"现在"}`}：{(status.elapsed_ms/1000).toFixed(1)} 秒</p>
      {stopped&&status.can_resume&&<p className="muted">继续会沿用当前稿件和已完成取材，只执行尚未完成的阶段；新调用仍计入现有预算。</p>}
      {status.output_width&&status.output_height&&<p>成片画幅：{status.output_width} × {status.output_height}</p>}
      <ul>{status.phases.map((phase,index)=><li key={index}>{names[phase.type]??phase.type}：{phaseLabels[phase.status]??phase.status} · {phase.duration_ms==null?"用时待记录":`${(phase.duration_ms/1000).toFixed(1)} 秒`}</li>)}</ul>
      {status.error_code&&<p className="error">{preflightReasonLabel(status.error_code)}</p>}
      {!!status.material_requests?.length&&<div><h4>待满足的画面需求</h4><p className="muted">这些画面仍需取得或核验用途；文件下载完成不代表表达匹配。</p><ul>{status.material_requests.map(item=><li key={item.scene_id}>
        <strong>{item.source_kind==="stock"?"外部素材":"AI 补充画面"}</strong>：{item.query}<p>{item.reason}</p>
        {item.viewer_takeaway&&<small>表达目的：{item.viewer_takeaway}</small>}</li>)}</ul></div>}
      {(status.stop_reasons??status.run?.waiting_stop_reasons.filter(value=>value.startsWith("automatic_stop:"))??[]).filter(value=>value.replace(/^automatic_stop:/,"")!==status.error_code).map(value=><p className="error" key={value}>{preflightReasonLabel(value.replace(/^automatic_stop:/,""))}</p>)}
    </>}
    {(done||stopped&&status?.run?.status==="render_completed_awaiting_review")&&status?.run&&<PresentationRecoveryPanel projectId={projectId} run={status.run} request={request} onRunChange={run=>setStatus(previous=>previous?{...previous,run}:previous)}/>}
  </section>;
}
