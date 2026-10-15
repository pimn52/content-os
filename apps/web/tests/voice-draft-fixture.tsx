import { createRoot } from "react-dom/client";
import { useState } from "react";
import { VoiceDraftPanel } from "../src/VoiceDraftPanel";
import "../src/styles.css";

const project = "fixture-project-1";
const saved = "voice-draft-fixture:status";
const log = "voice-draft-fixture:requests";
function record(path: string, options?: RequestInit) {
  const rows = JSON.parse(localStorage.getItem(log) ?? "[]"); rows.push({ path, method: options?.method ?? "GET", body: options?.body });
  localStorage.setItem(log, JSON.stringify(rows));
}
const status = () => JSON.parse(localStorage.getItem(saved) ?? "null");
function access(purpose: string) {
  return { audio_id: "fixture-audio", state: purpose === "internal_evaluation" ? "review_available" : "blocked",
    reasons: purpose === "internal_evaluation" ? [] : ["evaluation_scope_not_commercial"],
    manifest: { lineage: "known", scopes: [{ project_id: project, purpose: "internal_evaluation" }], commercial_authorized: false, execution_authorized: false } };
}
const request = async <T,>(path: string, options?: RequestInit): Promise<T> => {
  record(path, options);
  if (path === "/voice-profiles") return [{ id: "fixture-profile", name: "合成测试声线", provider: "omnivoice", consent: { confirmed: true } }] as T;
  if (path === "/execution-settings/capabilities") return [{ id: "fixture-capability", capability: "voice", provider: "omnivoice", mode: "local", model: "合成配置", readiness: "verified", quality_status: "verified" }] as T;
  if (path.endsWith("/provider-use-admissions")) return [{ id: "fixture-receipt", project_id: project, revoked_at: null, review: { intended_activity: "合成浏览器测试", license_version: "fixture-only", permitted_operations: ["generation", "review"], identity: { capability: "voice", provider: "omnivoice", model: "合成配置" } } }] as T;
  if (path.endsWith("/reference-windows")) return [{ selection: { source_content_hash: "a".repeat(64), clip_id: "fixture-clip" }, transcript: "仅用于测试的合成参考", duration_ms: 1000 }] as T;
  if (path === `/projects/${project}/voice-jobs`) {
    const body = JSON.parse(String(options?.body));
    if (body.idempotency_key.length > 500) throw new Error("fixture API-contract: idempotency key exceeds 500");
    if (body.purpose !== "internal_evaluation" || body.use_admission_ids[0] !== "fixture-receipt") throw new Error("fixture invalid explicit scope");
    if (!status()) localStorage.setItem(saved, JSON.stringify({ job_id: "fixture-job", project_id: project, state: "pending", job_status: "pending", reasons: [], outputs: [], recovery_action: null }));
    return { id: "fixture-job", project_id: project } as T;
  }
  if (path === `/projects/${project}/voice-draft-jobs/fixture-job`) return status() as T;
  if (path.startsWith("/audio-assets/fixture-audio/access")) return access(new URLSearchParams(path.split("?")[1]).get("purpose") ?? "") as T;
  throw new Error("synthetic fixture has no matching route");
};
const originalFetch = window.fetch.bind(window);
window.fetch = async (input, options) => {
  if (String(input).startsWith("/audio-assets/fixture-audio/media")) {
    const query = new URLSearchParams(String(input).split("?")[1]); record(String(input), options);
    if (query.get("project_id") !== project || query.get("purpose") !== "internal_evaluation") return new Response("blocked", { status: 403 });
    return originalFetch(new URL("./media/voice-draft-synthetic.wav", import.meta.url));
  }
  return originalFetch(input, options);
};
function Fixture() {
  const [projectId, setProjectId] = useState(project);
  return <main className="app-shell"><h1>仅合成浏览器夹具 — 不连接生产 API，不调用模型</h1>
    <label>测试项目<select value={projectId} onChange={e => setProjectId(e.target.value)}><option value={project}>项目一</option><option value="fixture-project-2">项目二</option></select></label>
    <button onClick={() => { const current = status(); if (current) localStorage.setItem(saved, JSON.stringify({ ...current, state: "completed", job_status: "completed", outputs: [{ audio_id: "fixture-audio", qa_state: "pending" }] })); }}>模拟后台完成</button>
    <VoiceDraftPanel key={projectId} projectId={projectId} request={request} />
  </main>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
