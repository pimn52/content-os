import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { SourceViewportPanel } from '../src/SourceViewportPanel';
import { ProductionPanel } from '../src/ProductionPanel';
import '../src/styles.css';
const assetId = '00000000-0000-0000-0000-000000000101';
const clipId = '00000000-0000-0000-0000-000000000102';
const projectId = 'fixture-adaptation';
const calls: Array<{ path: string; body: Record<string, unknown> | null }> = [];
const nativeFetch = window.fetch.bind(window);
const fixtureMedia = new URL('./media/recovery-before.mp4', location.href).href;
window.fetch = (input, options) => String(input) === `/assets/${assetId}/media` ? nativeFetch(fixtureMedia, options) : nativeFetch(input, options);
const clip = { id: clipId, asset_id: assetId, start_ms: 0, end_ms: 1000 };
let sourceHash = '';
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const body = options?.body ? JSON.parse(String(options.body)) : null; calls.push({ path, body });
  let result: unknown;
  if (path === '/assets/vertical-derivations') result = { asset: { id: 'fixture-derived', source_file: 'fixture-derived.mp4', content_hash: 'd'.repeat(64), width: 1080, height: body.output_height, duration_ms: 1000 }, clip: { ...clip, id: 'fixture-derived-clip', asset_id: 'fixture-derived' } };
  else if (path.endsWith('production-preflight')) result = { project_id: projectId, draft_version: 1, script_revision: 1, fingerprint: 'a'.repeat(64), status: 'blocked', evidence_level: 'fixture', cost_estimate: { known_amount: '0', currency: 'USD', unknown_cost_count: 1 }, estimated_local_runtime_ms: null, estimated_user_active_minutes: null, stop_reasons: ['fixture_no_actual_provider_admission'], authorization_needs: [], actions: [], scenes: [] };
  else if (path.endsWith('production-runs')) result = { id: 'fixture-run', project_id: projectId, status: 'cancelled', talking_dependencies: [], waiting_stop_reasons: [], voice_qa_auto_stop_reasons: [] };
  else if (path.endsWith('/fixture-run')) result = { id: 'fixture-run', project_id: projectId, status: 'cancelled', talking_dependencies: [], waiting_stop_reasons: [], voice_qa_auto_stop_reasons: [] };
  else throw new Error(`Fixture unexpected endpoint: ${path}`);
  document.getElementById('fixture-calls')!.textContent = JSON.stringify(calls, null, 2);
  return result as T;
}
function Fixture() {
  const [ready, setReady] = useState(false); const [refreshes, setRefreshes] = useState(0);
  useEffect(() => { void nativeFetch(fixtureMedia).then(response => response.arrayBuffer()).then(async bytes => {
    const hash = await crypto.subtle.digest('SHA-256', bytes);
    sourceHash = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join(''); setReady(true);
  }); }, []);
  return <main className="shell"><h1>本地浏览器夹具 · 比例适配</h1><p>仅测试组件和请求形状；合成视频不代表人物/成片质量。检查声明只用于夹具，未写入真实数据库。</p>
    {ready && <section className="card"><SourceViewportPanel assets={[{ id: assetId, source_file: 'synthetic108x192.mp4', content_hash: sourceHash, width: 108, height: 192, duration_ms: 1000 }]} clips={[clip]} request={request} onCreated={async () => { setRefreshes(value => value + 1); }} /></section>}
    <p>夹具素材库刷新次数：<span id="refreshes">{refreshes}</span></p>
    <ProductionPanel projectId={projectId} draftVersion={1} scriptRevision={1} hasScenes request={request} />
    <details><summary>夹具请求记录</summary><pre id="fixture-calls"></pre></details>
  </main>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
