import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { ProductionPanel } from "../src/ProductionPanel";
import { createRecoveryFixture, manualComparison, recoveryIds, renderHashes } from "./recovery-state";
import beforeUrl from "./media/recovery-before.mp4?url";
import afterUrl from "./media/recovery-after.mp4?url";
import "../src/styles.css";

const kind = new URLSearchParams(location.search).get("kind") === "presentation" ? "presentation" : "voice";
const fixture = createRecoveryFixture(localStorage, kind);
if (!localStorage.getItem(fixture.key)) fixture.seed();
async function prepareReviewState() {
  fixture.seed();
  if (kind === "voice") {
    const plan = await fixture.request<{ fingerprint: string }>(fixture.base + "/voice-repair-plan");
    await fixture.request(fixture.base + "/voice-repair", { method: "POST", body: JSON.stringify({
      expected_fingerprint: plan.fingerprint, idempotency_key: "fixture:voice-review-state", confirmed_action: "regenerate_take", reason: "synthetic fixture preparation" }) });
    fixture.complete(); await fixture.request(fixture.base + "/voice-qa-advance", { method: "POST" });
  } else {
    const observation = await fixture.request<{ id: string }>(fixture.base + "/presentation-observations", { method: "POST", body: JSON.stringify({
      idempotency_key: "fixture:observation-review-state", render_sha256: renderHashes.before,
      scene_id: "scene-1", finding: "duplicate_text", reason: "synthetic fixture preparation", evidence_reference: "fixture:00:00" }) });
    const plan = await fixture.request<{ fingerprint: string }>(fixture.base + `/presentation-repair-plan?observation_id=${observation.id}`);
    await fixture.request(fixture.base + "/presentation-repair", { method: "POST", body: JSON.stringify({ observation_id: observation.id,
      expected_fingerprint: plan.fingerprint, idempotency_key: "fixture:presentation-review-state", confirmed_action: "render_revision", reason: "synthetic fixture preparation" }) });
    fixture.complete();
  }
  window.location.reload();
}
function silentWav() {
  const bytes = new ArrayBuffer(48044); const view = new DataView(bytes);
  const tag = (at: number, value: string) => [...value].forEach((char, i) => view.setUint8(at + i, char.charCodeAt(0)));
  tag(0, "RIFF"); view.setUint32(4, 48036, true); tag(8, "WAVE"); tag(12, "fmt "); view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, 24000, true); view.setUint32(28, 48000, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); tag(36, "data"); view.setUint32(40, 48000, true);
  return bytes;
}
function Fixture() {
  const [revision, setRevision] = useState(fixture.load().version);
  const [operations, setOperations] = useState<string[]>(fixture.load().operations);
  useEffect(() => {
    const original = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const path = String(input);
      if (path === `/audio-assets/${recoveryIds.newAudio}/media`) return Promise.resolve(new Response(silentWav(), { headers: { "Content-Type": "audio/wav" } }));
      if (path === `/projects/${fixture.projectId}/renders/${recoveryIds.oldRender}`) return original(beforeUrl);
      if (path === `/projects/${fixture.projectId}/renders/${recoveryIds.newRender}`) return original(afterUrl);
      // No fixture action is permitted to fall through to real product APIs.
      return Promise.reject(new Error(`isolated_fixture_unhandled_media:${path}`));
    };
    const listener = () => setOperations(fixture.load().operations);
    window.addEventListener("recovery-fixture-action", listener);
    return () => { window.fetch = original; window.removeEventListener("recovery-fixture-action", listener); };
  }, []);
  const request = React.useCallback(async <T,>(path: string, options?: RequestInit) => {
    try { return await fixture.request<T>(path, options); }
    finally { window.dispatchEvent(new Event("recovery-fixture-action")); }
  }, []);
  return <main><h1>V76b6 {kind} recovery · isolated fixture</h1>
    <p>全部 API 为浏览器本地状态。音频为静音、视频为 1 秒纯色合成媒体；界面状态不代表真实 QA/主体人审。页面不能调用后端或 provider。</p>
    <div className="actions"><a href="?kind=voice">Voice fixture</a><a href="?kind=presentation">呈现 fixture</a>
      <button onClick={() => { fixture.seed(); window.location.reload(); }}>重置此 fixture</button>
      <button onClick={() => void prepareReviewState()}>一键准备待验收状态（仅 fixture）</button>
      <button onClick={() => { fixture.complete(); window.location.reload(); }}>模拟 Worker 完成并重载</button>
      <button onClick={() => { fixture.changeInput(); setRevision(fixture.load().version); }}>修改当前输入版本</button>
      <label><input type="checkbox" defaultChecked={fixture.load().denied} onChange={event => fixture.setDenied(event.target.checked)}/>模拟授权变化拒绝</label></div>
    <p role="status">成功工作流请求 {operations.length}：{operations.join(" → ") || "尚无"}。仅计读取方案、登记观察、提交修复、推进 QA 和提交人审；填表、确认对话框、刷新及 Worker 模拟不计，主动分钟未知。</p>
    <p>{manualComparison.historical_evidence} 当前 fixture 中人工填写/更换内部 Job、Audio、Render 绑定的动作数为 {manualComparison.automatic_binding_actions}。原有 Voice 主观审核与最终成片审核仍保留。</p>
    <div id="project-draft-editor"><p>重规划入口：普通产品中回到当前稿件/场景编辑并保存，再使用下面整条预检。此 fixture 不生成新文案或派发生产。</p></div>
    <ProductionPanel projectId={fixture.projectId} draftVersion={revision} scriptRevision={1} hasScenes request={request}/>
  </main>;
}
createRoot(document.getElementById("root")!).render(<React.StrictMode><Fixture/></React.StrictMode>);
