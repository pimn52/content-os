import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash, webcrypto } from 'node:crypto';
import { transformSync } from 'esbuild';
import { componentHarness } from './hook-harness.mjs';
const source = readFileSync(new URL('./recovery-state.ts', import.meta.url), 'utf8');
const { code } = transformSync(source, { loader: 'ts', format: 'esm' });
const { createRecoveryFixture, recoveryIds, renderHashes, manualComparison } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
const media = Object.fromEntries(['before', 'after'].map(kind => [kind, readFileSync(new URL(`./media/recovery-${kind}.mp4`, import.meta.url))]));
const paths = { voice: new URL('../src/ProductionVoicePanel.tsx', import.meta.url).pathname.replace(/^\/(\w:)/, '$1'), presentation: new URL('../src/PresentationRecoveryPanel.tsx', import.meta.url).pathname.replace(/^\/(\w:)/, '$1') };
function setup(kind, storageMap = new Map()) {
  const storage = { getItem: key => storageMap.get(key) ?? null, setItem: (key, value) => storageMap.set(key, String(value)), removeItem: key => storageMap.delete(key) };
  const fixture = createRecoveryFixture(storage, kind); if (!storage.getItem(fixture.key)) fixture.seed();
  return { fixture, storage, storageMap };
}
function mount(kind, f, extra = {}) {
  let harness;
  const props = { projectId: f.fixture.projectId, draftVersion: 1, scriptRevision: 1, run: f.fixture.load().run,
    request: f.fixture.request, confirmAction: () => true, onRunChange: run => harness.setProps({ run }) };
  harness = componentHarness(decodeURI(paths[kind]), kind === 'voice' ? 'ProductionVoicePanel' : 'PresentationRecoveryPanel', props,
    { localStorage: f.storage, fetch: path => Promise.resolve(new Response(String(path).endsWith(recoveryIds.oldRender) ? media.before : media.after)), ...extra });
  return harness;
}
async function observe(harness, finding = 'duplicate_text') {
  await harness.change('场景', 'scene-1'); await harness.change('所见问题', finding);
  await harness.change('观察理由', 'fixture observed duplicate'); await harness.change('证据引用', 'fixture:00:00');
  await harness.submit('登记此 Render');
}

test('a qualified material replacement describes its exact scope and retains the existing render action',async()=>{
  const f=setup('presentation');const h=mount('presentation',f);
  h.setProps({request:async(path,options)=>{
    const value=await f.fixture.request(path,options);
    return path.includes('/presentation-repair-plan?')?{...value,action:'render_revision',assessment_state:'known',
      responsible_stage:'material',material_replacements:[{scene_id:'scene-1'}],semantic_recheck_calls:1,external_charge_ceiling:'unknown',required_capability:[],
      diagnoses:[{observation_id:'material-observation',finding:'semantic_mismatch',scope:{scene_id:'scene-1',start_ms:0,end_ms:3000},
        responsible_stage:'material',assessment_state:'known',evidence_current:true,machine_diagnosis:{state:'known'}}]}:value;
  }});
  await h.flush();await observe(h,'semantic_mismatch');await h.click('读取修复候选');
  assert.match(h.text(),/仅替换问题场景画面/);assert.match(h.text(),/保留原时轴、字幕文字、旁白及其它人物画面/);
  assert.match(h.text(),/新输出再进行 1 次实际画面复验，之后仍待整片人审/);
  assert.match(h.text(),/画面复验 1 次/);assert.match(h.text(),/外部费用待核实，受现有预算约束/);
  assert.doesNotMatch(h.text(),/外部收费上限 0/);
  assert.ok(h.nodes('button').some(node=>String(node.props.children).includes('按此方案建立新 Render')));
  assert.equal(f.fixture.load().records.length,0);assert.equal(f.fixture.load().used,0);h.unmount();
});

for(const outcome of ['pass','failed','unknown'])test(`material history waits for actual picture recheck then reads ${outcome} without a QA action`,async()=>{
  const f=setup('presentation');let poll;let recheck=null;const calls=[];
  const h=mount('presentation',f,{setInterval:fn=>{poll=fn;return 1;},clearInterval:()=>{}});
  h.setProps({request:async(path,options)=>{
    calls.push({path,options});
    if(path.endsWith('/presentation-repairs'))return [{id:'material-repair',reason:'仅替换场景素材',replacement_job_id:recoveryIds.oldJob,
      status:'completed',final_review_state:'not_submitted',technical_qa:{state:'verified'},semantic_recheck:recheck,
      plan:{predecessor_job:{id:'predecessor',payload:{}},changes:[],semantic_recheck_calls:1,material_replacements:[{scene_id:'scene-1'}]}}];
    return f.fixture.request(path,options);
  }});
  await h.flush();
  assert.match(h.text(),/正在复核替换后的实际画面/);
  assert.equal(h.nodes('section').filter(node=>node.props['aria-label']==='完整成片审核').length,0);
  assert.ok(!h.nodes('button').some(button=>/接受此版|QA|重配音/.test(String(button.props.children))));
  assert.equal(typeof poll,'function');
  recheck={state:outcome,assessment_state:outcome==='unknown'?'unknown':'known',responsible_stage:'material',
    evidence_current:true,limitations:['audio_not_observed'],required_capability:[],scope:{scene_id:'scene-1'}};
  await poll();await h.flush();
  assert.match(h.text(),outcome==='pass'?/替换场景的机器画面复验通过，仍待整片人审/:outcome==='failed'?/画面复验未通过；保留现有成果/:/画面尚未确认符合讲述含义；保留现有成果/);
  assert.match(h.text(),/不评价听感自然度、口型同步，也不代表整片通过/);
  assert.equal(h.nodes('section').filter(node=>node.props['aria-label']==='完整成片审核').length,1);
  assert.ok(calls.every(value=>!value.options?.method));
  assert.equal(f.fixture.load().records.length,0);assert.equal(f.fixture.load().used,0);h.unmount();
});
test('Voice UI replaces failed take, keeps pending/completed history and exposes a fresh six-dimension review after QA', async () => {
  const f = setup('voice'); const h = mount('voice', f); await h.flush();
  await h.click('检查 Voice 修复方案');
  assert.match(h.text(), /whole|完整|整段/); assert.match(h.text(), /剩余额度 1/);
  await h.change('修复理由', 'fixture technical recovery'); await h.submit('按方案修复声音');
  assert.match(h.text(), /Voice 修复历史/); assert.match(h.text(), new RegExp(recoveryIds.newJob));
  assert.equal(h.nodes('form').length, 0); assert.equal(f.fixture.load().run.voice_audio_id, null);
  f.fixture.complete(); h.setProps({ run: f.fixture.load().run }); await h.flush();
  await h.click('推进独立 Voice QA');
  assert.match(h.text(), new RegExp(recoveryIds.newAudio)); assert.match(h.text(), new RegExp(recoveryIds.qa));
  const review = h.find('form', '六维 U-Voice');
  assert.equal(h.nodes('select').length, 6); assert.ok(h.nodes('select').every(node => node.props.defaultValue === ''));
  assert.match(h.text(), /Voice 修复历史/);
  await h.submit('六维 U-Voice', { likeness: 'pass', naturalness: 'pass', emphasis: 'pass', pace: 'pass', pauses: 'pass', rhythm: 'pass', findings: 'synthetic only', evidence_reference: 'fixture:review' });
  assert.equal(f.fixture.load().review.evidence_reference, 'fixture:review');
  assert.match(h.text(), /已存在不可覆盖/); assert.equal(h.nodes('form').length, 0);
  assert.deepEqual(f.fixture.load().operations, ['read_voice_plan', 'apply_voice_repair', 'advance_voice_qa', 'submit_new_u_voice']);
  h.unmount(); const next = mount('voice', setup('voice', f.storageMap)); await next.flush();
  assert.match(next.text(), /已存在不可覆盖/); assert.match(next.text(), /Voice 修复历史/);
  assert.ok(review); next.unmount();
});
test('Voice segment repair describes the retained good audio and still submits the current plan', async () => {
  const f = setup('voice'); const h = mount('voice', f); const confirmations = [];
  h.setProps({ request: async (path, options) => {
    const value = await f.fixture.request(path, options);
    return path.endsWith('/voice-repair-plan') ? { ...value, execution_granularity: 'segment', responsible_stage: 'voice', assessment_state: 'known',
      scope: { scene_id: null, start_ms: 1000, end_ms: 2400, generation_segment_index: 1, start_char: 8, end_char: 19 },
      preserves: { master_audio_id: recoveryIds.oldAudio, reused_segment_sha256: ['retained-good-pcm-hash'] }, required_capability: [] } : value;
  }, confirmAction: message => { confirmations.push(message); return true; } });
  await h.flush(); await h.click('检查 Voice 修复方案');
  assert.match(h.text(), /责任工序：声音/); assert.match(h.text(), /当前声音 · 1000–2400 ms/);
  assert.match(h.text(), /原生成第 2 段 · 文案字符 8–19/); assert.match(h.text(), /retained-good-pcm-hash/);
  await h.change('修复理由', '已定位第二段缺词'); await h.submit('按方案修复声音');
  assert.match(confirmations[0], /只重做已定位的缺陷段，保留其它合格声音/);
  assert.equal(f.fixture.load().records.length, 1);
  assert.match(h.text(), /Voice 修复历史/); h.unmount();
});
test('presentation UI binds the exact playable fixture hash, displays the semantic candidate and preserves unreviewed successor on reload', async () => {
  for (const kind of ['before', 'after']) assert.equal(createHash('sha256').update(media[kind]).digest('hex'), renderHashes[kind]);
  const f = setup('presentation'); const h = mount('presentation', f); await h.flush(); await observe(h);
  assert.equal(f.fixture.load().observations[0].request.render_sha256, renderHashes.before);
  await h.click('读取修复候选'); assert.match(h.text(), /Fixture complete narration/); assert.match(h.text(), /Fixture short point/);
  assert.match(h.text(), /Voice 0 次 · Talking 0 次/); assert.match(h.text(), new RegExp(recoveryIds.oldAudio));
  await h.change('本次修复理由', 'fixture typography revision'); await h.submit('按此方案建立新 Render');
  assert.equal(f.fixture.load().run.render_job_id, recoveryIds.newJob); assert.match(h.text(), /not_submitted/);
  assert.equal(h.nodes('form').length, 0); f.fixture.complete(); h.unmount();
  const next = mount('presentation', setup('presentation', f.storageMap)); await next.flush();
  assert.match(next.text(), new RegExp(renderHashes.after)); assert.match(next.text(), /not_submitted/); assert.match(next.text(), /技术 QA：verified/);
  assert.equal(next.input('观察理由').props.value, ''); assert.equal(next.input('场景').props.value, '');
  assert.deepEqual(f.fixture.load().operations, ['record_presentation_observation', 'read_presentation_plan', 'apply_presentation_repair']);
  assert.equal(manualComparison.historical_action_count, null); assert.equal(manualComparison.automatic_binding_actions, 0);
  await observe(next); await next.click('读取修复候选'); assert.match(next.text(), /run_repair_allowance_exhausted/);
  assert.ok(!next.nodes('button').some(node => String(node.props.children).includes('按此方案建立新 Render')));
  next.unmount();
});
test('joint presentation repair shows every diagnosis and submits one revision for the complete candidate', async () => {
  const f=setup('presentation');const h=mount('presentation',f);
  h.setProps({request:async(path,options)=>{
    const value=await f.fixture.request(path,options);
    if(!path.includes('/presentation-repair-plan'))return value;
    return {...value,responsible_stage:'assembly',assessment_state:'known',required_capability:[],diagnoses:[
      {observation_id:'duplicate',finding:'duplicate_text',scope:{scene_id:'scene-1',start_ms:0,end_ms:3000},assessment_state:'known'},
      {observation_id:'overlay',finding:'text_overlay',scope:{scene_id:'scene-1',start_ms:0,end_ms:3000},assessment_state:'known'},
    ]};
  }});
  await h.flush();await observe(h);await h.click('读取修复候选');
  assert.match(h.text(),/本次一并处理 2 项问题，只建立一个修订版本/);
  assert.match(h.text(),/新标题重复了旁白全文/);assert.match(h.text(),/文字与人物画面重叠/);
  assert.match(h.text(),/Voice 0 次 · Talking 0 次/);
  await h.change('本次修复理由','合并修复同一版本');await h.submit('按此方案建立新 Render');
  assert.equal(f.fixture.load().records.length,1);
  assert.equal(f.fixture.load().operations.filter(value=>value==='apply_presentation_repair').length,1);
  h.unmount();
});
test('unknown/source findings preserve results and do not send the creator to rewrite the whole draft', async () => {
  const f = setup('presentation'); const h = mount('presentation', f); await h.flush(); await observe(h, 'unknown_subtitles');
  await h.click('读取修复候选'); assert.match(h.text(), /presentation_source_interval_or_layout_replan_required/);
  assert.equal(h.nodes('form').filter(node => JSON.stringify(node.props.children).includes('本次修复理由')).length, 0);
  assert.match(h.text(), /保留现有成果/);
  assert.ok(!h.nodes('a').some(node => ['#production-preflight', '#project-draft-editor'].includes(node.props.href))); h.unmount();
  const v = setup('voice'); v.fixture.setDenied(true); const vh = mount('voice', v); await vh.flush(); await vh.click('检查 Voice 修复方案');
  assert.match(vh.text(), /voice_repair_consent_changed/); assert.equal(vh.nodes('form').length, 0); vh.unmount();
});
test('scoped quality findings expose the responsible stage, exact retained work and missing capability without generation', async () => {
  for (const [finding, stage, label, capability, capabilityText] of [
    ['voice_naturalness', 'voice', '声音', 'exact_audio_naturalness_diagnosis', '诊断确切音频的自然度'],
    ['talking_naturalness', 'talking', '人物画面', 'exact_talking_naturalness_diagnosis', '诊断确切人物输出的自然度'],
    ['visible_sync', 'unknown', '责任待定位', 'render_vs_talking_sync_diagnosis', '对照确切成片、人物段与旁白，定位生成或装配造成的音画不同步'],
    ['semantic_mismatch', 'unknown', '责任待定位', 'exact_scene_narration_visual_semantics_diagnosis', '诊断本场景讲述与画面含义的匹配'],
    ['narrative_rhythm', 'timing', '配时与节奏', 'exact_scene_delivery_vs_edit_timing_diagnosis', '区分声音讲述与剪辑配时造成的节奏问题'],
  ]) {
    const f = setup('presentation'); const h = mount('presentation', f);
    h.setProps({ request: async (path, options) => {
      const value = await f.fixture.request(path, options);
      return path.includes('/presentation-repair-plan?') ? { ...value, action: 'scoped_fix_required',
        responsible_stage: stage, assessment_state: 'unknown', scope: { scene_id: 'scene-1', start_ms: 240, end_ms: 960 },
        preserves: { master_audio_id: recoveryIds.oldAudio, talking_job_ids: ['retained-talking-job'],
          talking_output_asset_ids: ['retained-talking-output'], scene_plan_ids: ['retained-scene-plan'] },
        required_capability: [capability] } : value;
    } });
    await h.flush();
    assert.ok(h.nodes('option').some(node=>node.props.value===finding));
    await observe(h, finding); await h.click('读取修复候选');
    assert.match(h.text(), new RegExp(`责任工序：${label}`)); assert.match(h.text(), /待补证据，尚未定位可执行修复/);
    assert.match(h.text(), /scene-1 · 240–960 ms/); assert.match(h.text(), new RegExp(capabilityText));
    for (const id of [recoveryIds.oldAudio, 'retained-talking-job', 'retained-talking-output', 'retained-scene-plan']) assert.ok(h.text().includes(id));
    assert.ok(!h.nodes('button').some(node => String(node.props.children).includes('按此方案建立新 Render')));
    assert.ok(!h.nodes('a').some(node => ['#production-preflight', '#project-draft-editor'].includes(node.props.href)));
    assert.equal(f.fixture.load().records.length, 0); assert.equal(f.fixture.load().used, 0); h.unmount();
  }
});
test('machine semantic diagnoses keep every finding, explain current evidence and never expose media repair',async()=>{
  for(const [stage,current] of [['assembly',true],['material',true],['assembly',false]]){
    const f=setup('presentation');const h=mount('presentation',f);const requests=[];
    h.setProps({request:async(path,options)=>{
      requests.push({path,options});const value=await f.fixture.request(path,options);
      if(!path.includes('/presentation-repair-plan?'))return value;
      return {...value,action:'scoped_fix_required',responsible_stage:current?stage:'unknown',assessment_state:current?'known':'unknown',changes:[],
        required_capability:['configured_video_comparison_provider','current_render_semantic_diagnosis','semantic_layout_correction','scoped_material_replacement'],
        diagnoses:[{observation_id:'semantic',finding:'semantic_mismatch',responsible_stage:stage,assessment_state:current?'known':'unknown',
          scope:{scene_id:'scene-1',start_ms:0,end_ms:3000},evidence_current:current,
          machine_diagnosis:{source:'machine_semantic_comparison',method:'same_scene_render_source_video_comparison',
            provider:'fixture-hidden-provider',model:'fixture-hidden-model',call_id:'fixture-hidden-call',state:'known',limitations:['audio_not_observed']}},
          {observation_id:'duplicate',finding:'duplicate_text',scope:{scene_id:'scene-1',start_ms:0,end_ms:3000},assessment_state:'known'}]};
    }});
    await h.flush();await observe(h,'semantic_mismatch');await h.click('读取修复候选');await h.flush();
    assert.match(h.text(),/本版保留 2 项问题，尚未建立修订版本/);
    assert.match(h.text(),/新标题重复了旁白全文/);
    assert.match(h.text(),/机器仅对照静音画面与旁白文字/);
    assert.match(h.text(),/不评价听感自然度、口型同步，也不代表最终人审通过/);
    assert.match(h.text(),current?new RegExp(`已定位本项责任：${stage==='assembly'?'字幕与装配':'素材与用途'}，局部修正仍待核验`):/原诊断证据已失效，需重新定位/);
    for(const label of ['配置可对照视频的画面核验能力','核验当前成片与原素材的画面语义','修正受影响场景的语义呈现','仅替换受影响场景的素材'])assert.ok(h.text().includes(label));
    assert.doesNotMatch(h.text(),/fixture-hidden|configured_video_comparison_provider|current_render_semantic_diagnosis|semantic_layout_correction|scoped_material_replacement/);
    assert.ok(!h.nodes('button').some(node=>String(node.props.children).includes('按此方案建立新 Render')));
    assert.equal(f.fixture.load().records.length,0);assert.equal(f.fixture.load().used,0);
    const writes=requests.filter(value=>value.options?.method);
    assert.equal(writes.length,1);assert.ok(writes[0].path.endsWith('/presentation-observations'));
    assert.ok(!f.fixture.load().operations.includes('apply_presentation_repair'));h.unmount();
  }
});
test('an explicitly unknown assessment cannot expose a render action and automatic observations keep their source', async () => {
  const f = setup('presentation'); const h = mount('presentation', f);
  h.setProps({ request: async (path, options) => {
    const value = await f.fixture.request(path, options);
    return path.includes('/presentation-repair-plan?') ? { ...value, assessment_state: 'unknown', responsible_stage: 'unknown',
      observation: { ...value.observation, source: 'deterministic_spec_qa' }, required_capability: ['需核对确切成片与装配方案'] } : value;
  } });
  await h.flush(); await observe(h); await h.click('读取修复候选');
  assert.match(h.text(), /自动方案检查引用/); assert.match(h.text(), /责任待定位/);
  assert.ok(!h.nodes('button').some(node => String(node.props.children).includes('按此方案建立新 Render')));
  assert.equal(f.fixture.load().records.length, 0); h.unmount();
});
test('unsupported or unknown Voice quality plans retain exact work and never offer a whole take regeneration', async () => {
  for (const action of ['replan', 'scoped_fix_required', 'regenerate_take']) {
    const f = setup('voice'); const h = mount('voice', f);
    h.setProps({ request: async (path, options) => {
      const value = await f.fixture.request(path, options);
      return path.endsWith('/voice-repair-plan') ? { ...value, action, failure_kind: 'subjective', assessment_state: 'unknown', responsible_stage: 'voice',
        scope: { scene_id: null, start_ms: null, end_ms: null }, stop_reasons: [], required_capability: ['exact_audio_quality_failure_localization'],
        preserves: { master_audio_id: recoveryIds.oldAudio, master_audio_sha256: 'original-audio-hash', talking_job_ids: ['keep-talking-job'],
          talking_output_asset_ids: ['keep-talking-output'], scene_plan_ids: ['keep-scene-plan'], render_job_id: 'keep-render-job' } } : value;
    } });
    await h.flush(); await h.click('检查 Voice 修复方案');
    assert.match(h.text(), /保留当前音频和其它合格成果/); assert.match(h.text(), /先补齐声音诊断/);
    assert.match(h.text(), /责任工序：声音/); assert.match(h.text(), /待补证据，尚未定位可执行修复/); assert.match(h.text(), /将声音质量缺陷定位到确切片段/);
    for (const id of [recoveryIds.oldAudio, 'original-audio-hash', 'keep-talking-job', 'keep-talking-output', 'keep-scene-plan', 'keep-render-job']) assert.ok(h.text().includes(id));
    assert.ok(!h.nodes('a').some(node => ['#production-preflight', '#project-draft-editor'].includes(node.props.href)));
    assert.ok(!h.nodes('button').some(node => String(node.props.children).includes('按方案修复声音')));
    assert.equal(f.fixture.load().records.length, 0); h.unmount();
  }
});
test('server observations survive loss of the local observation cache', async () => {
  const f=setup('presentation'); const h=mount('presentation',f); await h.flush(); await observe(h,'source_change'); h.unmount();
  for (const key of [...f.storageMap.keys()]) if (key.startsWith('content-os-presentation-observation:')) f.storageMap.delete(key);
  const restored=mount('presentation',setup('presentation',f.storageMap));await restored.flush();
  assert.equal(restored.input('观察理由').props.value,'fixture observed duplicate');
  assert.equal(restored.input('所见问题').props.value,'source_change');restored.unmount();
});
test('final video rejection persists against the exact render without another approval or model call', async () => {
  const f=setup('presentation');const h=mount('presentation',f);await h.flush();
  h.setProps({confirmAction:()=>false});await h.flush();await h.change('成片反馈','人物动作重复');await h.click('需修改');
  assert.equal(f.fixture.load().run.review_state,'rejected');assert.equal(f.fixture.load().used,0);
  assert.match(h.text(),/用户已拒绝/);h.unmount();
});

test('ordinary finished video can be selected or discarded without rendering again',async()=>{
  const f=setup('presentation');const h=mount('presentation',f);await h.flush();
  assert.match(h.text(),/已保留占用，同作品版本可继续比较/);
  assert.match(h.text(),/选用此版将锁定本版用料/);
  await h.click('选用此版');assert.match(h.text(),/当前选用版/);
  await h.click('不予使用');assert.match(h.text(),/未发布占用已释放/);
  assert.equal(f.fixture.load().used,0);
  h.unmount();const restored=mount('presentation',setup('presentation',f.storageMap));await restored.flush();
  assert.match(restored.text(),/未发布占用已释放/);restored.unmount();
});
test('Run/input switch cancels pending Voice plan and unlocks controls even after returning to the original identity', async () => {
  const f = setup('voice'); let resolve;
  const waiting = new Promise(value => { resolve = value; });
  const original = f.fixture.load().run;
  const request = (path, options) => path.endsWith('/voice-repair-plan') ? waiting : f.fixture.request(path, options);
  const h = mount('voice', f); h.setProps({ request }); await h.flush();
  await h.click('检查 Voice 修复方案'); h.setProps({ run: { ...original, preflight_fingerprint: 'changed' } }); await h.flush();
  h.setProps({ run: original }); await h.flush();
  resolve(await f.fixture.request(f.fixture.base + '/voice-repair-plan')); await h.flush();
  assert.equal(h.find('button', '检查 Voice 修复方案').props.disabled, false); assert.equal(h.nodes('form').length, 0); h.unmount();
});
test('presentation observation cannot submit old bytes after input switches during async digest', async () => {
  const f = setup('presentation'); let release, intercepted = false;
  const crypto = { subtle: { digest: (...args) => intercepted ? new Promise(resolve => { release = () => webcrypto.subtle.digest(...args).then(resolve); }) : webcrypto.subtle.digest(...args) } };
  const h = mount('presentation', f, { crypto }); await h.flush();
  await h.change('场景', 'scene-1'); await h.change('观察理由', 'fixture'); await h.change('证据引用', 'fixture:evidence');
  intercepted = true; await h.submit('登记此 Render'); assert.ok(release);
  h.setProps({ run: { ...f.fixture.load().run, preflight_fingerprint: 'changed-input' } }); await h.flush();
  await release(); await h.flush(); assert.equal(f.fixture.load().observations.length, 0);
  assert.equal(h.input('观察理由').props.value, ''); h.unmount();
});
test('observation storage failure preserves successful server observation and explains reload limitation', async () => {
  const f = setup('presentation'); const storage = { ...f.storage, setItem(key, value) { if (key.startsWith('content-os-presentation-observation:')) throw new Error('storage denied'); f.storage.setItem(key, value); } };
  const h = mount('presentation', f, { localStorage: storage }); await h.flush(); await observe(h);
  assert.equal(f.fixture.load().observations.length, 1); assert.match(h.text(), /观察已在后台保存/);
  assert.match(h.text(), /刷新后需重新登记观察/); await h.click('读取修复候选'); assert.match(h.text(), /render_revision/); h.unmount();
});
test('late authorization/stale input denial clears the executable plan without creating a replacement', async () => {
  for (const kind of ['voice', 'presentation']) {
    for (const cause of ['authorization', 'version']) {
      const f = setup(kind); const h = mount(kind, f); await h.flush();
      if (kind === 'voice') { await h.click('检查 Voice 修复方案'); await h.change('修复理由', 'fixture'); }
      else { await observe(h); await h.click('读取修复候选'); await h.change('本次修复理由', 'fixture'); }
      if (cause === 'authorization') f.fixture.setDenied(true); else f.fixture.changeInput();
      await h.submit(kind === 'voice' ? '按方案修复声音' : '按此方案建立新 Render');
      assert.equal(f.fixture.load().records.length, 0); assert.match(h.text(), cause === 'authorization' ? /changed/ : /stale/);
      assert.ok(!h.nodes('button').some(node => String(node.props.children).includes(kind === 'voice' ? '按方案修复声音' : '按此方案建立新 Render')));
      h.unmount();
    }
  }
});
test('editing an observation invalidates its old candidate; saving feedback needs no extra approval', async () => {
  const f = setup('presentation'); const h = mount('presentation', f); await h.flush(); await observe(h); await h.click('读取修复候选');
  await h.change('所见问题', 'face_out_of_frame');
  assert.ok(!h.nodes('button').some(node => String(node.props.children).includes('按此方案建立新 Render')));
  h.setProps({ confirmAction: () => false }); await h.flush(); await h.submit('登记此 Render');
  assert.equal(f.fixture.load().observations.length, 2); assert.equal(f.fixture.load().records.length, 0); h.unmount();
});
test('replaying the exact Voice/presentation repair keeps successor progress and never restores old bindings', async () => {
  for (const kind of ['voice', 'presentation']) {
    const f = setup(kind); const h = mount(kind, f); let body;
    const request = (path, options) => { if (path.endsWith(`/${kind}-repair`) && options?.method === 'POST') body = JSON.parse(options.body); return f.fixture.request(path, options); };
    h.setProps({ request }); await h.flush();
    if (kind === 'voice') { await h.click('检查 Voice 修复方案'); await h.change('修复理由', 'fixture'); await h.submit('按方案修复声音'); }
    else { await observe(h); await h.click('读取修复候选'); await h.change('本次修复理由', 'fixture'); await h.submit('按此方案建立新 Render'); }
    f.fixture.complete(); const before = f.fixture.load();
    await f.fixture.request(f.fixture.base + `/${kind}-repair`, { method: 'POST', body: JSON.stringify(body) });
    const next = setup(kind, f.storageMap).fixture.load(); assert.deepEqual(next.run, before.run); assert.deepEqual(next.records, before.records);
    assert.equal(next.records.length, 1); assert.equal(next.used, 1);
    h.unmount();
  }
});
