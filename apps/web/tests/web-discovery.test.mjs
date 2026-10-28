import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { componentHarness } from './hook-harness.mjs';

const file=fileURLToPath(new URL('../src/WebDiscoveryPanel.tsx',import.meta.url));

function setup(pages,providers=[{id:'deepseek',label:'DeepSeek',configured:true,estimated_request_cny:'0.005'}]){
  const calls=[],resolved=[];
  const search={call_id:'search',provider:providers[0].id,query:'coffee making',cached:true,pages};
  const request=async(path,options)=>{
    calls.push({path,options});
    if(path.endsWith('/web-providers'))return providers;
    if(path.endsWith('/web-searches'))return [search];
    if(path.endsWith('/resolve-page'))return {call_id:'resolved',query:'https://esahubble.org/videos/actual/',candidates:[]};
    throw new Error('Unexpected request: '+path);
  };
  return {calls,resolved,h:componentHarness(file,'WebDiscoveryPanel',{projectId:'P',request,onResolved:value=>resolved.push(value)})};
}

test('only an available backend resolver offers original-file parsing; unavailable sources explain why',async()=>{
  const pages=[
    {url:'https://www.pexels.com/video/coffee-41/',title:'Pexels coffee',kind:'video_detail',evidence:[],can_resolve:false,resolution_code:'public_page_unsupported_source',resolution_reason:'该来源的原文件网页解析尚未接通'},
    {url:'https://pixabay.com/videos/coffee-42/',title:'Pixabay coffee',kind:'video_detail',evidence:[],can_resolve:false,resolution_code:'public_page_unsupported_source',resolution_reason:'请使用已连接图库的素材搜索'},
    {url:'https://coverr.co/videos/coffee-43',title:'Coverr coffee',kind:'video_detail',evidence:[],can_resolve:false,resolution_code:'public_page_source_not_admitted',resolution_reason:'Coverr 免费网页未纳入来源供给'},
    {url:'https://esahubble.org/videos/actual/',title:'Hubble detail',kind:'video_detail',evidence:[],can_resolve:true,resolution_code:'resolver_available',resolution_reason:'已接公开页解析，仍需核对编码尺寸'},
  ];
  const s=setup(pages);await s.h.flush();
  assert.equal(s.h.nodes('button').filter(button=>button.props.children==='尝试解析原文件').length,1);
  assert.match(s.h.text(),/该来源的原文件网页解析尚未接通/);
  assert.match(s.h.text(),/Coverr 免费网页未纳入来源供给/);
  await s.h.click('尝试解析原文件');
  const actual=s.calls.filter(call=>call.path.endsWith('/resolve-page'));
  assert.equal(actual.length,1);
  assert.deepEqual(JSON.parse(actual[0].options.body).url,pages[3].url);
  assert.equal(s.resolved.length,1);
});

test('legacy or unsupported page results never expose a guaranteed-failure parsing button',async()=>{
  const s=setup([
    {url:'https://www.pexels.com/video/coffee-41/',title:'Older page',kind:'video_detail',evidence:[]},
    {url:'https://example.org/search/coffee',title:'Directory',kind:'collection',evidence:[],can_resolve:false,resolution_code:'not_video_detail',resolution_reason:'仅具体详情页可尝试'},
  ]);
  await s.h.flush();
  assert.equal(s.h.nodes('button').some(button=>button.props.children==='尝试解析原文件'),false);
  assert.match(s.h.text(),/该来源的原文件解析尚未接通/);
  assert.equal(s.calls.some(call=>call.options?.method==='POST'),false);
});

test('a public page pending metadata can be checked without pretending it is a video',async()=>{
  const s=setup([{url:'https://open.example.org/item/coffee',title:'公开条目',kind:'unknown',evidence:['尚待核实'],
    can_resolve:true,resolution_code:'page_metadata_pending',resolution_reason:'原文件与来源元数据待核实'}]);
  await s.h.flush();
  assert.match(s.h.text(),/尚未识别.*尚待核实/);
  assert.match(s.h.text(),/原文件与来源元数据待核实/);
  await s.h.click('尝试解析原文件');
  assert.equal(s.calls.filter(call=>call.path.endsWith('/resolve-page')).length,1);
});

test('a different search-only connection uses declared capabilities and does not invent a price',async()=>{
  const s=setup([{url:'https://open.example.org/coffee',title:'公开条目',kind:'unknown',evidence:[],can_resolve:true}],
    [{id:'other-web',label:'其他搜索连接',configured:true,search:true,fetch:false,estimated_request_cny:null}]);
  await s.h.flush();
  assert.equal(s.h.nodes('select')[0].props.value,'other-web');
  assert.equal(s.h.nodes('button').some(button=>button.props.children==='读取正文（按次计费）'),false);
  assert.match(s.h.text(),/按连接计费/);
  assert.doesNotMatch(s.h.text(),/¥null/);
});
