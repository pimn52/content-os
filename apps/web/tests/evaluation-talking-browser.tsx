// Isolated normal components, synthetic API state and existing test video.
// Never forwards calls to the real API/database or issues a quality judgment.
import React from "react";
import { createRoot } from "react-dom/client";
import { EvaluationTalkingPanel } from "../src/EvaluationTalkingPanel";
import "../src/styles.css";
const calls: unknown[] = [], hash="a".repeat(64);
const calibration=new URLSearchParams(location.search).has("calibration");
const selection={policy:calibration?"evaluation-talking-calibration-v1":"evaluation-talking-run-v1",purpose:"internal_evaluation",master_run_id:"M",scene_plan_id:"S",
  talking_profile_id:"T",reference_clip_id:"C",source_admission_id:"SR",capability_profile_id:"CAP",execution_admission_id:"EX",
  authorization_reference:"synthetic-only",brief:{},use_admission_id:null,...(calibration?{trial_duration_limit_ms:1200,trial_processing_limit_ms:1600}:{})};
const receipt={id:"U",project_id:"P",revoked_at:null,created_at:"2026-10-13",review:{selection,intended_activity:"合成人物用途（浏览器测试）",
  proof:{profile:{name:"测试人物"},scene:{scene_id:"intro",voice_text:"合成测试旁白"},master_start_ms:0,master_end_ms:1000}}};
const operation={id:"O",project_id:"P",master_run_id:"M",scene_plan_id:"S",state:"awaiting_review",created_at:"2026-10-13",
  current_eligible:true,reasons:[],stages:{generation:"completed",qa:"completed",preview:"completed"},use_blockers:[],talking_run_id:null,review_policy_version:2,
  policy:selection.policy,quality_observation:null};
const review={preview_asset_id:"A",preview_sha256:hash,planned_origin_sha256:"b".repeat(64),review_policy_version:2,
  readiness:"ready_for_human_continuity_review",blockers:[] as string[],continuity_review_state:"not_submitted",
  continuity_review_evidence_reference:null,review_concerns:[] as unknown[]};
let revoked=false;
const originalFetch=window.fetch.bind(window);
window.fetch=async(input,options)=>{
  const path=String(input);calls.push({media:path,cache:options?.cache});
  if(path === "/projects/P/evaluation-talking/operations/O/preview/media?purpose=internal_evaluation") {
    if(revoked)return new Response("revoked",{status:403});
    return originalFetch("/app/tests/media/recovery-before.mp4",{cache:"no-store"});
  }
  throw new Error("隔离页面拒绝未声明请求");
};
const request=async<T,>(path:string,options?:RequestInit):Promise<T>=>{
  calls.push({path,method:options?.method ?? "GET",body:options?.body ? JSON.parse(String(options.body)):null});
  if(path.endsWith("/candidates"))return (calibration?[{capability_profile_id:"CAP",provider:"latentsync",model:"1.5",readiness:"configured",quality_status:"unknown",verified_duration_ms:null,calibration_grant_available:true}]:[]) as T;
  if(path.endsWith("/admissions"))return structuredClone([receipt]) as T;
  if(path.endsWith("/preflight"))return {policy:selection.policy,purpose:selection.purpose,eligible:true,reasons:[],fingerprint:"f1"} as T;
  if(path.endsWith("/operations"))return structuredClone(options?.method === "POST" ? operation:[operation]) as T;
  if(path.endsWith("/access?purpose=internal_evaluation"))return {operation_id:"O",state:revoked?"blocked":"review_allowed",reasons:revoked?["synthetic_revoked"]:[],asset_id:"A",content_hash:hash} as T;
  if(path.endsWith("/concerns")){
    const value=JSON.parse(String(options?.body));review.review_concerns.push({id:"K",finding:value.finding,evidence_reference:value.evidence_reference,answer:null});
    review.blockers=["talking_local_review_pending:K"];return {} as T;
  }
  if(path.endsWith("/review")){
    if(options?.method === "POST")throw new Error("浏览器验证不创建任何六项通过记录");
    return structuredClone(review) as T;
  }
  if(path.endsWith("/O"))return structuredClone(operation) as T;
  throw new Error("隔离页面拒绝未声明操作");
};
Object.assign(window,{talkingFixture:{calls,revoke:()=>{revoked=true;}}});
createRoot(document.getElementById("root")!).render(<main className="app-shell"><p>隔离合成页面：不连接真实项目，不创建质量通过记录。</p>
  <div className="card"><EvaluationTalkingPanel projectId="P" draftVersion={1} scriptRevision={1} draftDirty={false} styleTokens={{}} request={request}/></div></main>);
