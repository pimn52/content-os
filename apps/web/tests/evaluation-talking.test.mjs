import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { componentHarness } from './hook-harness.mjs';
const file=fileURLToPath(new URL('../src/EvaluationTalkingPanel.tsx',import.meta.url));
const previewFile=fileURLToPath(new URL('../src/EvaluationTalkingPreview.tsx',import.meta.url));
function Preview(){}
function Review(){}
function fixture(){
  const calls=[];let saved=[];
  const selection={policy:'evaluation-talking-run-v1',purpose:'internal_evaluation',master_run_id:'M',scene_plan_id:'S',talking_profile_id:'T',reference_clip_id:'C',source_admission_id:'SR',capability_profile_id:'CAP',execution_admission_id:'EX',authorization_reference:'exact use',brief:{require_no_burned_subtitles:true},use_admission_id:null};
  const receipt={id:'U',project_id:'P',revoked_at:null,created_at:'today',review:{selection,intended_activity:'Opinion scene exact use',proof:{profile:{name:'Creator'},scene:{scene_id:'intro',voice_text:'Exact text'},master_start_ms:0,master_end_ms:5640}}};
  let preflight={policy:selection.policy,purpose:selection.purpose,eligible:true,reasons:[],fingerprint:'f1'};
  const operation={id:'O',project_id:'P',master_run_id:'M',scene_plan_id:'S',created_at:'today',state:'in_progress',current_eligible:true,stages:{generation:'pending',qa:'not_created',preview:'not_created'},reasons:[],use_blockers:[],talking_run_id:null,review_policy_version:2};
  const request=async(path,options)=>{
    calls.push({path,options});
    if(path.endsWith('/admissions'))return [structuredClone(receipt)];
    if(path.endsWith('/preflight'))return structuredClone(preflight);
    if(path.endsWith('/operations')&&options?.method==='POST'){saved=[structuredClone(operation)];return saved[0];}
    if(path.endsWith('/operations'))return structuredClone(saved);
    if(path.endsWith('/cancel')){saved[0]={...saved[0],state:'cancelled',current_eligible:false};return {};}
    if(path.endsWith('/qa')){saved[0].stages.qa='pending';return {};}
    if(path.endsWith('/preview')){saved[0].stages.preview='pending';return {};}
    return structuredClone(saved.find(r=>path.endsWith('/'+r.id)));
  };
  const create=changes=>componentHarness(file,'EvaluationTalkingPanel',{projectId:'P',draftVersion:1,scriptRevision:1,draftDirty:false,styleTokens:{},request,...changes},{modules:{'./EvaluationTalkingPreview':{EvaluationTalkingPreview:Preview},'./EvaluationTalkingReview':{EvaluationTalkingReview:Review}}});
  return {calls,request,create,receipt,operation,setSaved:values=>saved=structuredClone(values),setPreflight:values=>preflight={...preflight,...values}};
}
test('exact adopted selection creates once; remount finds backend history without IDs or POST',async()=>{
  const f=fixture(),h=f.create();await h.flush();assert.equal(f.calls.some(c=>c.options?.method==='POST'),false);
  await h.change('已采用的人物制作用途','U');assert.match(h.text(),/Creator/);
  await h.click('检查人物制作准入');await h.click('保存人物制作操作');
  const writes=f.calls.filter(c=>c.options?.method==='POST');assert.equal(writes.length,2);
  const body=JSON.parse(writes[1].options.body);assert.equal(body.use_admission_id,'U');assert.equal(body.reference_clip_id,'C');assert.equal(body.execution_admission_id,'EX');assert.equal(body.expected_fingerprint,'f1');
  assert.equal(h.find('button','提交人物技术 QA').props.disabled,true);
  const fresh=f.create();await fresh.flush();await fresh.change('已保存的人物制作','O');
  assert.equal(f.calls.filter(c=>c.options?.method==='POST').length,2);
  await fresh.click('取消人物操作');assert.match(fresh.text(),/已取消/);h.unmount();fresh.unmount();
});

test('bounded calibration retains policy and signed limits; reload does not infer general quality',async()=>{
  const f=fixture();f.receipt.review.selection={...f.receipt.review.selection,policy:'evaluation-talking-calibration-v1',trial_duration_limit_ms:6000,trial_processing_limit_ms:6400};
  f.setPreflight({policy:'evaluation-talking-calibration-v1'});
  const h=f.create();await h.flush();await h.change('已采用的人物制作用途','U');
  assert.match(h.text(),/试验旁白上限 6000 ms/);assert.match(h.text(),/最多一次生成/);
  await h.click('检查人物制作准入');await h.click('保存人物制作操作');
  const body=JSON.parse(f.calls.find(c=>c.path.endsWith('/operations')&&c.options?.method==='POST').options.body);
  assert.equal(body.policy,'evaluation-talking-calibration-v1');assert.equal(body.trial_processing_limit_ms,6400);
  f.setSaved([{...f.operation,policy:'evaluation-talking-calibration-v1',state:'admitted',talking_run_id:'R',quality_observation:{scope:'exact_sample_only',observed_pass_duration_ms:5640}}]);
  await h.click('重新核对人物操作');assert.match(h.text(),/精确样本已通过，观察时长 5640 ms/);assert.match(h.text(),/一般质量与时长范围仍未验证/);
  assert.equal(f.calls.filter(c=>c.path.endsWith('/operations')&&c.options?.method==='POST').length,1);
});

test('a calibration preflight cannot substitute the ordinary policy',async()=>{
  const f=fixture();f.receipt.review.selection.policy='evaluation-talking-calibration-v1';
  const h=f.create();await h.flush();await h.change('已采用的人物制作用途','U');await h.click('检查人物制作准入');
  assert.match(h.text(),/预检用途与当前选择不一致/);assert.equal(h.nodes('button').some(b=>String(b.props.children).includes('保存人物制作操作')),false);
});

test('configured unknown candidate without an adopted grant remains visible and cannot create',async()=>{
  const f=fixture(),h=f.create({request:(path,options)=>path.endsWith('/candidates')?Promise.resolve([{capability_profile_id:'CAP',provider:'latentsync',model:'1.5',readiness:'configured',quality_status:'unknown',verified_duration_ms:null,calibration_grant_available:false}]):path.endsWith('/admissions')?Promise.resolve([]):f.request(path,options)});
  await h.flush();assert.match(h.text(),/配置状态 已配置，质量 未知/);assert.match(h.text(),/尚无采用的校准用途组合，不能开始生成/);
  assert.equal(h.find('button','检查人物制作准入').props.disabled,true);assert.equal(f.calls.some(c=>c.options?.method==='POST'),false);
});
test('receipt or dirty/style change clears preflight; stale response cannot restore generation button',async()=>{
  const f=fixture();let resolve;const pending=new Promise(r=>resolve=r);
  const h=f.create({request:(path,options)=>path.endsWith('/preflight')?pending:f.request(path,options)});
  await h.flush();await h.change('已采用的人物制作用途','U');await h.click('检查人物制作准入');
  h.setProps({styleTokens:{presentation:'subject_first'},draftDirty:true});await h.flush();
  resolve({policy:'evaluation-talking-run-v1',purpose:'internal_evaluation',eligible:true,reasons:[],fingerprint:'stale'});await h.flush();
  assert.equal(h.nodes('button').some(b=>String(b.props.children).includes('保存人物制作操作')),false);
  assert.equal(h.input('已采用的人物制作用途').props.disabled,true);
});
test('blocked preflight, missing receipt and foreign project never become authority',async()=>{
  const f=fixture(),h=f.create();await h.flush();await h.change('已采用的人物制作用途','U');
  f.setPreflight({eligible:false,reasons:['exact_talking_capability_not_verified']});await h.click('检查人物制作准入');
  assert.equal(h.find('button','保存人物制作操作').props.disabled,true);assert.match(h.text(),/exact_talking_capability_not_verified/);
  await h.change('已采用的人物制作用途','');assert.equal(h.nodes('button').some(b=>String(b.props.children).includes('保存人物制作操作')),false);
  const missing=f.create({request:(path,options)=>path.endsWith('/admissions')?Promise.resolve([]):f.request(path,options)});await missing.flush();assert.match(missing.text(),/尚无已采用/);
  const foreign=f.create({request:(path,options)=>path.endsWith('/admissions')?Promise.resolve([{...f.receipt,project_id:'other'}]):f.request(path,options)});await foreign.flush();assert.match(foreign.text(),/其他项目/);
});
test('technical processing advances only by explicit POST; failed operation has no retry',async()=>{
  const f=fixture();f.setSaved([{...f.operation,stages:{generation:'completed',qa:'not_created',preview:'not_created'}}]);
  const h=f.create();await h.flush();await h.change('已保存的人物制作','O');assert.equal(f.calls.some(c=>c.options?.method==='POST'),false);
  await h.click('提交人物技术 QA');assert.equal(f.calls.filter(c=>c.path.endsWith('/qa')).length,1);
  f.setSaved([{...f.operation,state:'failed',stages:{generation:'completed',qa:'failed',preview:'not_created'}}]);await h.click('重新核对人物操作');
  assert.equal(h.find('button','提交精确旁白预览').props.disabled,true);assert.equal(h.find('button','提交人物技术 QA').props.disabled,true);
  assert.match(h.text(),/失败不会自动重试/);
});
test('unknown cost is explicit and blocks create; permitted unknown estimate is never zero',async()=>{
  const f=fixture(),h=f.create();await h.flush();await h.change('已采用的人物制作用途','U');
  f.setPreflight({eligible:false,reasons:['budget_unknown_cost']});await h.click('检查人物制作准入');
  assert.match(h.text(),/成本未知，预算未允许/);assert.equal(h.find('button','保存人物制作操作').props.disabled,true);
  f.setPreflight({eligible:true,reasons:[],budget_state:'checked_not_reserved',estimated_cost:{amount:null,currency:null}});await h.click('检查人物制作准入');
  assert.match(h.text(),/生成估价：未知/);assert.match(h.text(),/预检未占用额度/);
});
test('cancelled history keeps stage evidence and exact-operation preview request',async()=>{
  const f=fixture();f.setSaved([{...f.operation,state:'cancelled',current_eligible:false,stages:{generation:'completed',qa:'completed',preview:'completed'},reasons:['evaluation_talking_operation_cancelled']}]);
  const h=f.create();await h.flush();await h.change('已保存的人物制作','O');
  assert.match(h.text(),/人物生成：已完成/);assert.equal(h.nodes(Preview)[0].props.operationId,'O');assert.equal(f.calls.some(c=>c.options?.method==='POST'),false);
});
test('failed current reload removes stale processing controls and decoded preview owner',async()=>{
  const f=fixture();f.setSaved([{...f.operation,stages:{generation:'completed',qa:'completed',preview:'completed'}}]);let fail=false;
  const h=f.create({request:(path,options)=>fail&&path.endsWith('/O')?Promise.reject(new Error('current read failed')):f.request(path,options)});
  await h.flush();await h.change('已保存的人物制作','O');assert.equal(h.nodes(Preview).length,1);
  fail=true;await h.click('重新核对人物操作');assert.equal(h.nodes(Preview).length,0);
  assert.equal(h.nodes('button').some(b=>String(b.props.children).includes('提交人物技术 QA')),false);assert.match(h.text(),/current read failed/);
});
test('preview mount reads permission only; click uses scoped/authenticated media and revokes on refresh',async()=>{
  const calls=[],fetches=[],revoked=[];
  const access={operation_id:'O',state:'review_allowed',reasons:[],asset_id:'A',content_hash:'a'.repeat(64)};
  const request=async path=>{calls.push(path);return access;};
  const h=componentHarness(previewFile,'EvaluationTalkingPreview',{projectId:'P',operationId:'O',revision:'1',request},{
    fetch:async(path,options)=>{fetches.push({path,options});return new Response('fixture video');},
    window:{sessionStorage:{getItem:()=> 'fixture-token'}},URL:{createObjectURL:()=> 'blob:fixture',revokeObjectURL:value=>revoked.push(value)}});
  await h.flush();assert.equal(fetches.length,0);await h.click('检查权限并播放人物预览');
  assert.equal(fetches[0].path,'/projects/P/evaluation-talking/operations/O/preview/media?purpose=internal_evaluation');assert.equal(fetches[0].options.headers.get('Authorization'),'Bearer fixture-token');assert.equal(h.nodes('video').length,1);
  h.setProps({revision:'2'});await h.flush();assert.equal(h.nodes('video').length,0);assert.deepEqual(revoked,['blob:fixture']);assert.equal(fetches.length,1);
});
test('late preview bytes cannot attach to another project; blocked permission does not fetch',async()=>{
  let resolve;const pending=new Promise(r=>resolve=r),fetches=[];
  const request=async path=>({operation_id:'O',state:path.includes('/P/')?'review_allowed':'blocked',reasons:['revoked'],asset_id:'A',content_hash:'a'.repeat(64)});
  const h=componentHarness(previewFile,'EvaluationTalkingPreview',{projectId:'P',operationId:'O',revision:'1',request},{fetch:path=>{fetches.push(path);return pending;}});
  await h.flush();await h.click('检查权限并播放人物预览');h.setProps({projectId:'other'});await h.flush();resolve(new Response('late bytes'));await h.flush();
  assert.equal(h.nodes('video').length,0);assert.equal(h.find('button','检查权限并播放人物预览').props.disabled,true);assert.equal(fetches.length,1);
});
