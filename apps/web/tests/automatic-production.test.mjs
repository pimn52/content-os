import test from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {componentHarness} from './hook-harness.mjs';

const file=fileURLToPath(new URL('../src/AutomaticProductionPanel.tsx',import.meta.url));
function mount(request){
  let poll;
  const recovery=()=>null;
  const h=componentHarness(file,'AutomaticProductionPanel',{projectId:'p1',topic:'视频压缩',request},{
    setInterval:fn=>{poll=fn;return 1;},clearInterval:()=>{},modules:{'./PresentationRecoveryPanel':{PresentationRecoveryPanel:recovery}},
  });
  return {h,recovery,poll:async()=>{await poll();await h.flush();}};
}

for(const outcome of ['pass','failed','unknown'])test(`one start waits for replaced-scene recheck and polls its ${outcome} result without another write`,async()=>{
  let status=null;const calls=[];
  const f=mount(async(path,options)=>{
    calls.push({path,options});
    if(path.endsWith('/talking-pool'))return null;
    if(options?.method==='POST'){
      status={start_job_id:'job1',status:'render_running',next_action:'continue',error_code:null,elapsed_ms:4000,
        run:{id:'run1',status:'render_running',waiting_stop_reasons:[]},phases:[]};
      return {start_job_id:'job1'};
    }
    return status;
  });
  await f.h.flush();await f.h.submit('开始制作短视频');
  status={...status,status:'render_completed_awaiting_review',next_action:'verify_material',responsible_stage:'material',
    run:{...status.run,status:'render_completed_awaiting_review'},material_recheck:null};
  await f.poll();await f.poll();
  assert.match(f.h.text(),/正在复核替换后的实际画面/);
  assert.doesNotMatch(f.h.text(),/成片已输出，待完整质量审核/);
  assert.equal(f.h.find('button','开始制作短视频').props.disabled,true);
  assert.equal(f.h.nodes(f.recovery).length,0);
  status={...status,next_action:outcome==='pass'?'review':'stopped',material_recheck:{state:outcome,
    assessment_state:outcome==='unknown'?'unknown':'known',responsible_stage:'material',evidence_current:true,
    limitations:['audio_not_observed'],required_capability:[],scope:{scene_id:'detail'}}};
  await f.poll();await f.poll();
  assert.match(f.h.text(),outcome==='pass'?/替换场景的机器画面复验通过，仍待整片人审/:outcome==='failed'?/画面复验未通过；保留现有成果/:/画面尚未确认符合讲述含义；保留现有成果/);
  assert.match(f.h.text(),/不评价听感自然度、口型同步，也不代表最终人审通过/);
  assert.equal(f.h.nodes(f.recovery).length,1);
  assert.ok(!f.h.nodes('button').some(button=>/QA|重配音|重新生成旁白/.test(String(button.props.children))));
  assert.equal(calls.filter(value=>value.options?.method).length,1);
  assert.ok(calls.every(value=>['/projects/p1/auto-produce','/projects/p1/talking-pool'].includes(value.path)));
  f.h.unmount();
});

test('a late negative source-use fact returns to material work without another UI request',async()=>{
  const calls=[];
  const status={start_job_id:'job1',status:'failed',next_action:'stopped',responsible_stage:'material',
    error_code:'source_semantic_use_rejected',elapsed_ms:4000,run:null,phases:[]};
  const f=mount(async(path,options)=>{calls.push(options);return path.endsWith('/talking-pool')?null:status;});
  await f.h.flush();await f.poll();
  assert.match(f.h.text(),/当前工序：素材与用途/);
  assert.match(f.h.text(),/来源区间已确认不适合相同表达任务/);
  assert.match(f.h.text(),/其它用途和合格成果保留/);
  assert.ok(calls.every(options=>!options?.method));f.h.unmount();
});
test('production center owns diagnosis and stop decisions shown by the ordinary UI',async()=>{
  let status={start_job_id:'job1',status:'diagnosing_qa',next_action:'diagnose_qa',responsible_stage:'talking',
    stop_reasons:[],error_code:null,elapsed_ms:4000,run:{id:'run1',waiting_stop_reasons:['planned_talking_qa_failed']},phases:[]};
  const calls=[];
  const f=mount(async(path,options)=>{calls.push(options);return path.endsWith('/talking-pool')?null:status;});
  await f.h.flush();
  assert.match(f.h.text(),/正在检查并安排局部修复/);
  assert.match(f.h.text(),/当前工序：人物画面/);
  assert.equal(f.h.find('button','开始制作短视频').props.disabled,true);
  status={...status,status:'failed',next_action:'stopped',stop_reasons:['automatic_stop:talking_scene_repair_already_attempted']};
  await f.poll();
  assert.match(f.h.text(),/本次制作未完成/);
  assert.equal(f.h.find('button','开始制作短视频').props.disabled,false);
  status={...status,status:'render_completed_awaiting_review'};
  await f.poll();
  assert.match(f.h.text(),/本次制作未完成/);
  assert.doesNotMatch(f.h.text(),/成片已输出/);
  assert.ok(calls.every(value=>!value?.method));
  f.h.unmount();
});

test('a missing production decision cannot infer completion or permit another start from raw status',async()=>{
  let status={start_job_id:'job1',status:'render_completed_awaiting_review',error_code:null,elapsed_ms:4000,
    can_resume:true,topic:'视频压缩',run:{id:'run1',status:'render_completed_awaiting_review',
      waiting_stop_reasons:['automatic_stop:old_failure']},phases:[]};
  const calls=[];
  const f=mount(async(path,options)=>{calls.push(options);return path.endsWith('/talking-pool')?null:status;});
  await f.h.flush();
  for(const rawStatus of ['render_completed_awaiting_review','voice_failed','cancelled']){
    status={...status,status:rawStatus};await f.poll();
    assert.match(f.h.text(),/正在读取制作状态/);
    assert.doesNotMatch(f.h.text(),/成片已输出，待完整质量审核|本次制作未完成/);
    assert.equal(f.h.find('button','开始制作短视频').props.disabled,true);
    assert.equal(f.h.nodes(f.recovery).length,0);
    assert.throws(()=>f.h.find('button','制作同作品对比版'),/missing button/);
    assert.throws(()=>f.h.find('button','沿用稿件继续制作'),/missing button/);
    await f.h.submit('开始制作短视频');
  }
  assert.ok(calls.every(value=>!value?.method));
  status={...status,next_action:'review'};await f.poll();
  assert.match(f.h.text(),/成片已输出，待完整质量审核/);
  assert.equal(f.h.find('button','开始制作短视频').props.disabled,false);
  assert.equal(f.h.nodes(f.recovery).length,1);
  f.h.unmount();
});

test('rejected exact narration stops at voice while retaining completed production facts',async()=>{
  const status={start_job_id:'job1',status:'failed',next_action:'stopped',responsible_stage:'voice',
    stop_reasons:['voice_quality_review_rejected'],error_code:'voice_quality_review_rejected',elapsed_ms:4000,
    run:{id:'run1',status:'u_voice_rejected',waiting_stop_reasons:[]},phases:[]};
  const calls=[];const f=mount(async(path,options)=>{calls.push(options);return path.endsWith('/talking-pool')?null:status;});
  await f.h.flush();await f.poll();
  assert.match(f.h.text(),/本次制作未完成/);assert.match(f.h.text(),/当前工序：旁白/);
  assert.match(f.h.text(),/本次旁白已被质量审核打回/);
  assert.equal(f.h.find('button','开始制作短视频').props.disabled,false);
  assert.ok(calls.every(value=>!value?.method));f.h.unmount();
});
test('whole-output quality returns to its responsible stage and never silently regenerates',async()=>{
  const calls=[];
  const status={start_job_id:'job1',status:'failed',next_action:'stopped',responsible_stage:'voice',
    error_code:'quality_scoped_fix_required:voice',elapsed_ms:4000,run:{id:'run1',status:'render_completed_awaiting_review',waiting_stop_reasons:[]},phases:[],
    quality_diagnosis:{finding:'voice_naturalness',responsible_stage:'voice',assessment_state:'unknown',
      scope:{scene_id:'opening',start_ms:0,end_ms:2400},required_capability:['exact_audio_naturalness_diagnosis']}};
  const f=mount(async(path,options)=>{calls.push(options);return path.endsWith('/talking-pool')?null:status;});
  await f.h.flush();await f.poll();
  assert.match(f.h.text(),/当前工序：旁白/);
  assert.match(f.h.text(),/质量问题定位于场景 opening/);
  assert.match(f.h.text(),/尚未确定可执行的局部修正/);
  assert.match(f.h.text(),/保留已有成果/);
  assert.doesNotMatch(f.h.text(),/编辑当前稿件|重新预检整条方案/);
  assert.ok(calls.every(value=>!value?.method));
  f.h.unmount();
});
test('semantic comparison progress and machine evidence use read-only polling without regenerating media',async()=>{
  const semantic={finding:'semantic_mismatch',responsible_stage:'unknown',assessment_state:'unknown',
    scope:{scene_id:'detail',start_ms:2400,end_ms:4800},required_capability:['current_render_semantic_diagnosis']};
  const voice={...semantic,finding:'voice_naturalness',responsible_stage:'voice'};
  const machine={source:'machine_semantic_comparison',method:'same_scene_render_source_video_comparison',
    provider:'fixture-hidden-provider',model:'fixture-hidden-model',call_id:'fixture-hidden-call',state:'known',limitations:['audio_not_observed']};
  let status={start_job_id:'job1',status:'diagnosing_qa',next_action:'diagnose_semantic',responsible_stage:'unknown',
    error_code:null,elapsed_ms:4000,run:null,phases:[],quality_diagnosis:{...semantic,findings:[semantic,voice]}};
  const calls=[];const f=mount(async(path,options)=>{calls.push({path,options});return path.endsWith('/talking-pool')?null:status;});
  await f.h.flush();await f.poll();
  assert.match(f.h.text(),/正在对照成片与原素材/);
  assert.match(f.h.text(),/机器仅对照静音画面与旁白文字/);
  assert.match(f.h.text(),/不评价听感自然度、口型同步，也不代表最终人审通过/);
  assert.match(f.h.text(),/声音自然度/);
  assert.equal(f.h.find('button','开始制作短视频').props.disabled,true);
  const located={...semantic,responsible_stage:'assembly',assessment_state:'known',machine_diagnosis:machine,evidence_current:true};
  status={...status,status:'failed',next_action:'stopped',responsible_stage:'assembly',
    quality_diagnosis:{...located,findings:[located,voice]}};
  await f.poll();await f.poll();
  assert.match(f.h.text(),/机器画面语义对照：已定位本项责任，待核验局部修正/);
  assert.match(f.h.text(),/声音自然度/);
  assert.doesNotMatch(f.h.text(),/fixture-hidden|修复人物|重新生成|制作同作品对比版/);
  status={...status,quality_diagnosis:{...located,findings:[{...located,evidence_current:false},voice]}};
  await f.poll();
  assert.match(f.h.text(),/原诊断证据已失效，需重新定位/);
  assert.ok(calls.every(value=>!value.options?.method));
  assert.ok(calls.every(value=>['/projects/p1/auto-produce','/projects/p1/talking-pool'].includes(value.path)));
  f.h.unmount();
});
test('independent findings stay visible together and do not trigger a partial replacement',async()=>{
  const one={finding:'duplicate_text',responsible_stage:'assembly',assessment_state:'unknown',scope:{scene_id:'opening',start_ms:0,end_ms:2400},required_capability:[]};
  const status={start_job_id:'job1',status:'failed',next_action:'stopped',responsible_stage:'unknown',
    error_code:'multiple_quality_findings_require_joint_scoped_plan',elapsed_ms:4000,run:null,phases:[],
    quality_diagnosis:{...one,scope:{scene_id:'整片',start_ms:null,end_ms:null},findings:[one,{...one,finding:'voice_naturalness',responsible_stage:'voice'}]}};
  const calls=[];const f=mount(async(path,options)=>{calls.push(options);return path.endsWith('/talking-pool')?null:status;});
  await f.h.flush();await f.poll();
  assert.match(f.h.text(),/重复文字/);assert.match(f.h.text(),/声音自然度/);
  assert.match(f.h.text(),/不只修最后一个问题/);
  assert.ok(calls.every(value=>!value?.method));f.h.unmount();
});
test('one start submits only the topic; all subsequent progress is read-only polling',async()=>{
  const calls=[];let status=null;
  const f=mount(async(path,options)=>{
    calls.push({path,options});
    if(options?.method==='POST'){status={start_job_id:'job1',status:'voice_running',next_action:'continue',elapsed_ms:4000,run:null,phases:[{type:'generate_voice',status:'running',duration_ms:null}]};return {start_job_id:'job1'};}
    return status;
  });
  await f.h.flush();await f.h.submit('开始制作短视频');
  assert.equal(f.h.find('button','开始制作短视频').props.disabled,true);
  status={...status,status:'render_completed_awaiting_review',next_action:'review',elapsed_ms:25000,phases:[{type:'render',status:'completed',duration_ms:3000}]};
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
  const status={start_job_id:'job1',status:'render_completed_awaiting_review',next_action:'review',elapsed_ms:25000,run:{id:'run1',waiting_stop_reasons:[]},phases:[]};
  const f=mount(async(path,options)=>{if(options?.method==='POST'){writes.push(JSON.parse(options.body));return {start_job_id:'job2'};}return status;});
  await f.h.flush();await f.h.find('button','制作同作品对比版').props.onClick({preventDefault(){}});await f.h.flush();
  assert.equal(writes.length,1);
  assert.equal(writes[0].candidate_of_run_id,'run1');
  assert.equal(writes[0].topic,'视频压缩');
  f.h.unmount();
});

test('rejected video stays rejected and does not ask for the same review again',async()=>{
  const status={start_job_id:'job1',status:'render_completed_awaiting_review',next_action:'review',elapsed_ms:25000,
    run:{id:'run1',review_state:'rejected',waiting_stop_reasons:[]},phases:[]};
  const f=mount(async()=>status);await f.h.flush();
  assert.match(f.h.text(),/成片已打回，需修复/);
  assert.doesNotMatch(f.h.text(),/待完整质量审核/);
  f.h.unmount();
});

test('missing external and AI visuals show the saved purpose without claiming acquisition',async()=>{
  const calls=[];
  const status={start_job_id:'job1',status:'failed',next_action:'stopped',error_code:'material_source_unavailable:nebula:stock',elapsed_ms:5000,
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
    status:'failed',next_action:'stopped',error_code:'material_video_analysis_timeout',can_resume:true,resume_draft_version:7,
    elapsed_ms:5000,run:null,phases:[]};
  const f=mount(async(path,options)=>{
    calls.push({path,options});
    if(options?.method==='POST'){
      status={...status,start_job_id:'resumed-job',status:'pending',next_action:'continue',error_code:null,
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
    status:'failed',next_action:'stopped',error_code:'material_video_analysis_timeout',can_resume:true,resume_draft_version:7,
    elapsed_ms:5000,run:null,phases:[]};
  const f=mount(async(path,options)=>{
    if(options?.method==='POST'){
      writes.push({path,body:JSON.parse(options.body)});
      if(writes.length===1)throw new Error('恢复请求响应连接中断');
      status={...status,start_job_id:'resumed-job',status:'pending',next_action:'continue',error_code:null,
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
  let status={start_job_id:'failed-attempt',topic:'视频压缩',status:'failed',next_action:'stopped',can_resume:false,
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
