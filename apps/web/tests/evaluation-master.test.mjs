import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { componentHarness } from './hook-harness.mjs';
const file = fileURLToPath(new URL('../src/EvaluationMasterPanel.tsx', import.meta.url));
function Audition() {}
function fixture() {
  const calls = []; let saved = [];
  let preflight = {project_id:'P', audio_id:'A', master_stage_eligible:true, whole_run_ready:false, reasons:[], downstream_reasons:['evaluation_talking_execution_not_integrated'], fingerprint:'f1'};
  const run = () => ({id:'R',project_id:'P',created_at:'2026-10-12',status:'evaluation_master_bound',waiting_stop_reasons:['evaluation_render_execution_not_integrated'],evaluation_master:{current_eligible:true,whole_run_ready:false,audio_id:'A',duration_ms:5640,copy:'Exact narration'}});
  const request = async (path, options) => {
    calls.push({path,options});
    if(path.endsWith('/candidates'))return [{audio_id:'A',text:'Exact narration',duration_ms:5640,reviewed_at:'2026-10-12',human_approved:true,current_review_eligible:true,reasons:[]}];
    if(path.endsWith('/admissions'))return [{id:'U',project_id:'P',revoked_at:null,created_at:'2026-10-12',review:{selection:{audio_id:'A'},intended_activity:'Exact local Master use'}}];
    if(path.endsWith('/preflight'))return structuredClone(preflight);
    if(options?.method==='POST' && path.endsWith('/runs')){saved=[run()];return run();}
    if(path.endsWith('/cancel')){saved=[{...run(),status:'cancelled',evaluation_master:{...run().evaluation_master,current_eligible:false}}];return saved[0];}
    if(path.endsWith('/runs'))return structuredClone(saved);
    return structuredClone(saved[0]);
  };
  const create = changes => componentHarness(file,'EvaluationMasterPanel',{projectId:'P',draftVersion:1,scriptRevision:1,draftDirty:false,styleTokens:{},request,...changes},{modules:{'./AudioAudition':{AudioAudition:Audition}}});
  return {calls,create,request,setPreflight:value=>preflight={...preflight,...value}};
}
async function select(h){await h.flush();await h.change('完整旁白','A');await h.change('主旁白用途准入','U');}
test('normal selection persists parent and scoped preview; fresh component restores from backend list without IDs',async()=>{
  const f=fixture(),h=f.create();await select(h);await h.click('检查主旁白匹配与用途');
  assert.match(h.text(),/整条制作尚未就绪/);await h.click('保存方案主旁白引用');
  const writes=f.calls.filter(c=>c.options?.method==='POST');assert.equal(writes.length,2);
  const body=JSON.parse(writes[1].options.body);assert.equal(body.audio_id,'A');assert.equal(body.admission_id,'U');assert.equal(body.expected_fingerprint,'f1');
  assert.equal(h.nodes(Audition)[0].props.accessBase,'/projects/P/evaluation-master/runs/R/audio');
  assert.equal(h.nodes('button').some(b=>String(b.props.children).includes('续接')),false);
  const fresh=f.create();await fresh.flush();await fresh.change('已保存的主旁白执行','R');
  assert.match(fresh.text(),/原音频已绑定/);await fresh.click('取消此主旁白执行');assert.match(fresh.text(),/执行已取消/);
  assert.equal(fresh.nodes(Audition).length,0);h.unmount();fresh.unmount();
});
test('blocked rights remain explicit; choosing different receipt clears preflight and cannot create',async()=>{
  const f=fixture(),h=f.create();await select(h);f.setPreflight({master_stage_eligible:false,reasons:['evaluation_master_use_admission_revoked']});
  await h.click('检查主旁白匹配与用途');assert.match(h.text(),/主旁白用途准入已撤销/);
  assert.equal(h.find('button','保存方案主旁白引用').props.disabled,true);
  await h.change('主旁白用途准入','');assert.equal(h.nodes('button').some(b=>String(b.props.children).includes('保存方案主旁白引用')),false);
});
test('dirty draft and plan/style changes clear inspected state; late response cannot revive old selection',async()=>{
  const f=fixture();let resolve;const pending=new Promise(r=>resolve=r);
  const h=f.create({request:(path,options)=>path.endsWith('/preflight')?pending:f.request(path,options)});
  await select(h);await h.click('检查主旁白匹配与用途');
  h.setProps({draftDirty:true});await h.flush();resolve({project_id:'P',audio_id:'A',master_stage_eligible:true,reasons:[],downstream_reasons:[],fingerprint:'old'});await h.flush();
  assert.equal(h.nodes('button').some(b=>String(b.props.children).includes('保存方案主旁白引用')),false);
  assert.equal(h.input('完整旁白').props.disabled,true);h.unmount();
});
test('missing current use receipts never become automatic generation authority',async()=>{
  const f=fixture();const h=f.create({request:(path,options)=>path.endsWith('/admissions')?Promise.resolve([]):f.request(path,options)});
  await h.flush();await h.change('完整旁白','A');assert.match(h.text(),/缺少该音频/);
  assert.equal(f.calls.some(c=>c.path.includes('provider-use-admissions')||c.path.includes('voice-drafts')),false);
});
