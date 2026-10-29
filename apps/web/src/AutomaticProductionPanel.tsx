import {useEffect,useRef,useState,type FormEvent} from "react";
import {PresentationRecoveryPanel} from "./PresentationRecoveryPanel";
import {preflightReasonLabel,productionLabels,type ProductionRun} from "./production";
type Request=<T>(path:string,options?:RequestInit)=>Promise<T>;
type MotionPool={state:string;occupancy_state:string;usable_total_ms:number;longest_continuous_ms:number};
type Status={start_job_id:string;status:string;error_code:string|null;elapsed_ms:number;run:ProductionRun|null;output_width?:number;output_height?:number;
  next_action?:string;responsible_stage?:string;stop_reasons?:string[];
  topic?:string;can_resume?:boolean;resume_draft_version?:number;origin_job_id?:string;
  material_requests?:Array<{scene_id:string;source_kind:string;query:string;reason:string;viewer_takeaway:string|null}>;
  phases:Array<{type:string;status:string;duration_ms:number|null}>};
const names:Record<string,string>={plan_content:"内容与素材规划",generate_voice:"克隆旁白",verify_voice:"旁白质检",generate_talking:"人物画面",verify_talking:"人物画面质检",render:"成片渲染"};
const stages:Record<string,string>={planning:"内容与素材规划",voice:"旁白",talking:"人物画面",render:"成片渲染",review:"整片审核"};
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
  const stopped=!!status && (status.next_action?status.next_action==="stopped":(status.status==="failed" || status.status.endsWith("_failed") || status.status==="cancelled"
    || !!status.run?.waiting_stop_reasons.some(reason=>reason.startsWith("automatic_stop:"))));
  const done=!!status&&(status.next_action?status.next_action==="review":status.status==="render_completed_awaiting_review");
  const running=!!status&&!done&&!stopped;
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
    {status&&<><h4>{done&&status.run?.review_state==="rejected"?"成片已打回，需修复":done?"成片已输出，待完整质量审核":stopped?"本次制作未完成":status.next_action==="diagnose_qa"?"正在检查并安排局部修复":productionLabels[status.status]??(status.status==="pending"?"等待后台开始":"正在规划制作")}</h4>
      {status.responsible_stage&&<p>当前工序：{stages[status.responsible_stage]??"视频制作"}</p>}
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
    {done&&status?.run&&<PresentationRecoveryPanel projectId={projectId} run={status.run} request={request} onRunChange={run=>setStatus(previous=>previous?{...previous,run}:previous)}/>}
  </section>;
}
