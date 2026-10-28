import { useEffect, useState, type FormEvent } from "react";
import type { ExternalMediaSearch } from "./ExternalMediaPanel";

type Provider={id:string;label:string;configured:boolean;estimated_request_cny:string|null;search?:boolean;fetch?:boolean};
type Page={url:string;title:string;kind:string;evidence:string[];can_resolve:boolean;resolution_code:string;resolution_reason:string};
type Search={call_id:string;provider:string;query:string;cached:boolean;pages:Page[]};
type Props={projectId:string;request<T>(path:string,options?:RequestInit):Promise<T>;onResolved(value:ExternalMediaSearch):void};
const kinds:Record<string,string>={video_detail:"具体视频页",collection:"目录或搜索页",image:"图片",document:"文档",challenge:"访问验证页",unknown:"尚未识别",invalid:"无效地址"};

export function WebDiscoveryPanel({projectId,request,onResolved}:Props){
  const [providers,setProviders]=useState<Provider[]>([]);
  const [provider,setProvider]=useState("deepseek");
  const [result,setResult]=useState<Search|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [content,setContent]=useState("");
  const base=`/projects/${projectId}/external-media`;
  useEffect(()=>{
    let active=true;
    Promise.all([request<Provider[]>(`${base}/web-providers`),request<Search[]>(`${base}/web-searches`)]).then(([sources,history])=>{
      if(active){setProviders(sources);setProvider(sources.find(s=>s.configured&&s.search!==false)?.id??sources[0]?.id??"");setResult(history[0]??null);setContent("");setError("");}
    }).catch(e=>{if(active)setError(String(e));});
    return()=>{active=false;};
  },[projectId]);
  async function search(event:FormEvent){
    event.preventDefault();
    const query=String(new FormData(event.currentTarget as HTMLFormElement).get("query")??"").trim();
    const refresh=((event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement|null)?.value==="refresh";
    setBusy(true);setError("");setContent("");
    try{setResult(await request<Search>(`${base}/web-search`,{method:"POST",body:JSON.stringify({provider,query,refresh,idempotency_key:`web-search:${crypto.randomUUID()}`})}));}
    catch(e){setError(e instanceof Error?e.message:String(e));}
    finally{setBusy(false);}
  }
  async function fetchPage(page:Page){
    if(!result)return;
    setBusy(true);setError("");
    try{const value=await request<{title:string;content:string}>(`${base}/web-fetch`,{method:"POST",body:JSON.stringify({search_call_id:result.call_id,url:page.url,idempotency_key:`web-fetch:${crypto.randomUUID()}`})});setContent(`${value.title}\n${value.content}`);}
    catch(e){setError(e instanceof Error?e.message:String(e));}
    finally{setBusy(false);}
  }
  async function resolvePage(page:Page){
    if(!result)return;
    setBusy(true);setError("");
    try{onResolved(await request<ExternalMediaSearch>(`${base}/resolve-page`,{method:"POST",body:JSON.stringify({search_call_id:result.call_id,url:page.url,idempotency_key:`resolve-page:${crypto.randomUUID()}`})}));}
    catch(e){setError(e instanceof Error?e.message:String(e));}
    finally{setBusy(false);}
  }
  return <details className="subform"><summary>通过已有搜索连接发现素材页</summary>
    <p className="muted">使用已配置的网页搜索连接，自动制作可在图库不足时沿用。近期相同查询优先复用；页面线索仍须取得原文件并核验用途。</p>
    <form onSubmit={search}><label>搜索连接<select value={provider} onChange={e=>setProvider(e.target.value)} disabled={busy}>{providers.map(p=><option key={p.id} value={p.id} disabled={!p.configured||p.search===false}>{p.label}{p.configured?(p.estimated_request_cny?`（估计每次 ¥${p.estimated_request_cny}）`:"（按连接计费）"):"（未连接）"}</option>)}</select></label>
      <label>具体对象与动作<input name="query" required minLength={2} maxLength={128} defaultValue={result?.query??""} placeholder="具体对象、动作和所需素材页"/></label>
      <button className="button" disabled={busy||!providers.some(p=>p.configured&&p.search!==false&&p.id===provider)}>{busy?"正在获取…":"发现素材页面"}</button><button className="button" value="refresh" disabled={busy||!providers.some(p=>p.configured&&p.search!==false&&p.id===provider)}>重新请求搜索</button></form>
    {error&&<p role="alert" className="error">{error}</p>}
    {result?.cached&&<p className="muted">已复用近期结果，本次未请求搜索服务。</p>}
    {result?.pages.map((page,index)=><article className="asset-row" key={`${page.url}:${index}`}><div>{page.kind==="invalid"?<strong>{page.title}</strong>:<a href={page.url} target="_blank" rel="noreferrer">{page.title||page.url}</a>}<small>{kinds[page.kind]??page.kind} · {page.evidence.join("；")}</small>{(page.kind==="video_detail"||page.can_resolve)&&<small className="muted">{page.resolution_reason??"该来源的原文件解析尚未接通"}</small>}</div>
      {(page.kind==="video_detail"||page.kind==="unknown"&&page.can_resolve)&&<>{page.can_resolve&&<button className="button" disabled={busy} onClick={()=>void resolvePage(page)}>尝试解析原文件</button>}{providers.find(p=>p.id===result.provider)?.fetch!==false&&<button className="button" disabled={busy} onClick={()=>void fetchPage(page)}>读取正文（按次计费）</button>}</>}</article>)}
    {content&&<pre style={{whiteSpace:"pre-wrap",maxHeight:300,overflow:"auto"}}>{content}</pre>}
  </details>;
}
