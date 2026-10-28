import test from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {componentHarness} from './hook-harness.mjs';

const file=fileURLToPath(new URL('../src/AutomaticProductionPanel.tsx',import.meta.url));
function mount(request){
  let poll;
  const h=componentHarness(file,'AutomaticProductionPanel',{projectId:'p1',topic:'视频压缩',request},{
    setInterval:fn=>{poll=fn;return 1;},clearInterval:()=>{},modules:{'./PresentationRecoveryPanel':{PresentationRecoveryPanel:()=>null}},
  });
  return {h,poll:async()=>{await poll();await h.flush();}};
}
test('one start submits only the topic; all subsequent progress is read-only polling',async()=>{
  const calls=[];let status=null;
  const f=mount(async(path,options)=>{
    calls.push({path,options});
    if(options?.method==='POST'){status={start_job_id:'job1',status:'voice_running',elapsed_ms:4000,run:null,phases:[{type:'generate_voice',status:'running',duration_ms:null}]};return {start_job_id:'job1'};}
    return status;
  });
  await f.h.flush();await f.h.submit('开始制作短视频');
  assert.equal(f.h.find('button','开始制作短视频').props.disabled,true);
  status={...status,status:'render_completed_awaiting_review',elapsed_ms:25000,phases:[{type:'render',status:'completed',duration_ms:3000}]};
  await f.poll();await f.poll();
  assert.match(f.h.text(),/成片已输出，待完整质量审核/);
  assert.match(f.h.text(),/25.0 秒/);
  const writes=calls.filter(value=>value.options?.method);
  assert.equal(writes.length,1);
  assert.equal(JSON.parse(writes[0].options.body).topic,'视频压缩');
  assert.ok(calls.every(value=>['/projects/p1/auto-produce','/projects/p1/talking-pool'].includes(value.path)));
  f.h.unmount();
});

test('creator motion pool exposes total and continuous duration without an extra approval',async()=>{
  const writes=[];
  const f=mount(async(path,options)=>{
    if(options?.method)writes.push(options);
    return path.endsWith('/talking-pool')?{state:'available',occupancy_state:'known',usable_total_ms:42000,longest_continuous_ms:30000}:null;
  });
  await f.h.flush();
  assert.match(f.h.text(),/可用总时长 42.0 秒，最长连续 30.0 秒/);
  assert.match(f.h.text(),/声音参考独立配置/);
  assert.equal(writes.length,0);
  f.h.unmount();
});
test('uncertain start response reuses its idempotency key and never silently retries',async()=>{
  const writes=[];
  const f=mount(async(path,options)=>{if(options?.method==='POST'){writes.push(JSON.parse(options.body));throw new Error('连接中断');}return null;});
  await f.h.flush();await f.h.submit('开始制作短视频');await f.poll();
  assert.equal(writes.length,1);
  await f.h.submit('开始制作短视频');assert.equal(writes.length,2);
  assert.equal(writes[0].idempotency_key,writes[1].idempotency_key);
  f.h.unmount();
});

test('comparison generation declares its parent without requiring version selection',async()=>{
  const writes=[];
  const status={start_job_id:'job1',status:'render_completed_awaiting_review',elapsed_ms:25000,run:{id:'run1',waiting_stop_reasons:[]},phases:[]};
  const f=mount(async(path,options)=>{if(options?.method==='POST'){writes.push(JSON.parse(options.body));return {start_job_id:'job2'};}return status;});
  await f.h.flush();await f.h.find('button','制作同作品对比版').props.onClick({preventDefault(){}});await f.h.flush();
  assert.equal(writes.length,1);
  assert.equal(writes[0].candidate_of_run_id,'run1');
  assert.equal(writes[0].topic,'视频压缩');
  f.h.unmount();
});

test('rejected video stays rejected and does not ask for the same review again',async()=>{
  const status={start_job_id:'job1',status:'render_completed_awaiting_review',elapsed_ms:25000,
    run:{id:'run1',review_state:'rejected',waiting_stop_reasons:[]},phases:[]};
  const f=mount(async()=>status);await f.h.flush();
  assert.match(f.h.text(),/成片已打回，需修复/);
  assert.doesNotMatch(f.h.text(),/待完整质量审核/);
  f.h.unmount();
});

test('missing external and AI visuals show the saved purpose without claiming acquisition',async()=>{
  const calls=[];
  const status={start_job_id:'job1',status:'failed',error_code:'material_source_unavailable:nebula:stock',elapsed_ms:5000,
    run:null,phases:[],material_requests:[{scene_id:'nebula',source_kind:'stock',query:'星云观测画面',reason:'展示真实对象',viewer_takeaway:'看清观测结构'},
      {scene_id:'scale',source_kind:'ai_broll',query:'尺度关系示意',reason:'解释抽象关系',viewer_takeaway:'理解大小差异'}]};
  const f=mount(async(path,options)=>{calls.push(options);return status;});await f.h.flush();
  assert.match(f.h.text(),/待满足的画面需求/);
  assert.match(f.h.text(),/外部素材.*星云观测画面/);
  assert.match(f.h.text(),/AI 补充画面.*尺度关系示意/);
  assert.match(f.h.text(),/表达目的：.*理解大小差异/);
  assert.ok(calls.every(value=>!value?.method));
  f.h.unmount();
});

test('explicit resume posts the saved failed attempt and draft version; progress polling never resumes automatically',async()=>{
  const calls=[];
  let status={start_job_id:'failed-attempt',origin_job_id:'original-job',topic:'视频压缩',
    status:'failed',error_code:'material_video_analysis_timeout',can_resume:true,resume_draft_version:7,
    elapsed_ms:5000,run:null,phases:[]};
  const f=mount(async(path,options)=>{
    calls.push({path,options});
    if(options?.method==='POST'){
      status={...status,start_job_id:'resumed-job',status:'pending',error_code:null,
        can_resume:false,resume_draft_version:null,elapsed_ms:0};
      return {start_job_id:'resumed-job',status:'pending'};
    }
    return status;
  });
  await f.h.flush();
  assert.equal(f.h.find('button','沿用稿件继续制作').props.disabled,false);
  assert.match(f.h.text(),/沿用当前稿件和已完成取材/);
  await f.poll();await f.poll();
  assert.equal(calls.filter(value=>value.options?.method==='POST').length,0);
  f.h.find('button','沿用稿件继续制作').props.onClick({preventDefault(){}});await f.h.flush();
  const writes=calls.filter(value=>value.options?.method==='POST');
  assert.equal(writes.length,1);
  const body=JSON.parse(writes[0].options.body);
  assert.deepEqual(Object.keys(body).sort(),['expected_draft_version','idempotency_key','resume_from_job_id','topic']);
  assert.equal(body.topic,'视频压缩');
  assert.equal(body.resume_from_job_id,'failed-attempt');
  assert.equal(body.expected_draft_version,7);
  assert.equal(typeof body.idempotency_key,'string');
  assert.throws(()=>f.h.find('button','沿用稿件继续制作'),/missing button/);
  assert.equal(f.h.find('button','开始制作短视频').props.disabled,true);
  assert.match(f.h.text(),/本次恢复用时/);
  await f.poll();await f.poll();
  assert.equal(calls.filter(value=>value.options?.method==='POST').length,1);
  assert.ok(calls.every(value=>['/projects/p1/auto-produce','/projects/p1/talking-pool'].includes(value.path)));
  f.h.unmount();
});

test('uncertain resume keeps its idempotency key for an explicit second click without background retry',async()=>{
  const writes=[];
  let status={start_job_id:'failed-attempt',origin_job_id:'original-job',topic:'视频压缩',
    status:'failed',error_code:'material_video_analysis_timeout',can_resume:true,resume_draft_version:7,
    elapsed_ms:5000,run:null,phases:[]};
  const f=mount(async(path,options)=>{
    if(options?.method==='POST'){
      writes.push({path,body:JSON.parse(options.body)});
      if(writes.length===1)throw new Error('恢复请求响应连接中断');
      status={...status,start_job_id:'resumed-job',status:'pending',error_code:null,
        can_resume:false,resume_draft_version:null};
      return {start_job_id:'resumed-job',status:'pending'};
    }
    return status;
  });
  await f.h.flush();
  f.h.find('button','沿用稿件继续制作').props.onClick({preventDefault(){}});await f.h.flush();
  assert.match(f.h.text(),/恢复请求响应连接中断/);
  assert.equal(f.h.find('button','沿用稿件继续制作').props.disabled,false);
  await f.poll();await f.poll();
  assert.equal(writes.length,1);
  f.h.find('button','沿用稿件继续制作').props.onClick({preventDefault(){}});await f.h.flush();
  assert.equal(writes.length,2);
  assert.deepEqual(writes[1],writes[0]);
  assert.equal(writes[0].body.resume_from_job_id,'failed-attempt');
  assert.equal(writes[0].body.expected_draft_version,7);
  await f.poll();assert.equal(writes.length,2);
  assert.throws(()=>f.h.find('button','沿用稿件继续制作'),/missing button/);
  f.h.unmount();
});

test('resume requires backend permission and the unchanged topic rather than silently starting a different draft',async()=>{
  const writes=[];
  let status={start_job_id:'failed-attempt',topic:'视频压缩',status:'failed',can_resume:false,
    resume_draft_version:null,error_code:'automatic_draft_changed',elapsed_ms:5000,run:null,phases:[]};
  const f=mount(async(path,options)=>{if(options?.method==='POST')writes.push(options);return status;});
  await f.h.flush();
  assert.throws(()=>f.h.find('button','沿用稿件继续制作'),/missing button/);
  status={...status,can_resume:true,resume_draft_version:7};await f.poll();
  assert.equal(f.h.find('button','沿用稿件继续制作').props.disabled,false);
  f.h.nodes('textarea')[0].props.onChange({target:{value:'创作者新选的另一个主题'}});await f.h.flush();
  assert.equal(f.h.find('button','沿用稿件继续制作').props.disabled,true);
  await f.poll();assert.equal(writes.length,0);
  f.h.unmount();
});
