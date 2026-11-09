import { useEffect, useState, type FormEvent } from "react";

type RuntimeSettings = {
  planner_key_file: string; planner_base_url: string; planner_model: string;
  planner_protocol: string; planner_chat_output_mode: string; planner_thinking_type: string; planner_reasoning_effort: string;
  planner_max_output_tokens: string; planner_timeout_seconds: string;
  vision_key_file: string; vision_base_url: string; vision_model: string;
  vision_protocol: string; vision_video_input: string; vision_thinking_type: string;
  pexels_key_file: string;
  voice_config_file: string; talking_config_file: string; voice_qa_model_path: string;
};

const empty: RuntimeSettings = {
  planner_key_file: "", planner_base_url: "", planner_model: "", planner_protocol: "",
  planner_chat_output_mode: "", planner_thinking_type: "", planner_reasoning_effort: "",
  planner_max_output_tokens: "", planner_timeout_seconds: "", vision_key_file: "", vision_base_url: "",
  vision_model: "", vision_protocol: "", vision_video_input: "", vision_thinking_type: "", pexels_key_file: "", voice_config_file: "",
  talking_config_file: "", voice_qa_model_path: "",
};

type Request = <T>(path: string, options?: RequestInit) => Promise<T>;

export function RuntimeSettingsPanel({ request }: { request: Request }) {
  const [settings, setSettings] = useState<RuntimeSettings>(empty);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    let active = true;
    request<RuntimeSettings>("/runtime/settings").then((value) => {
      if (active) { setSettings({ ...empty, ...value }); setLoaded(true); }
    }).catch(() => { if (active) setMessage("读取运行配置失败，请检查服务状态。"); });
    return () => { active = false; };
  }, [request]);

  function change(key: keyof RuntimeSettings, value: string) {
    setSettings((previous) => ({ ...previous, [key]: value }));
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // 只接收本机路径，不接受直接粘贴的密钥。
    const pathKeys: Array<keyof RuntimeSettings> = ["planner_key_file", "vision_key_file", "pexels_key_file", "voice_config_file", "talking_config_file", "voice_qa_model_path"];
    if (pathKeys.some((key) => settings[key].trim() && !/^(?:[A-Za-z]:[\\/]|\/)/.test(settings[key].trim()))) {
      setMessage("凭据和模型配置请填写本机绝对路径，不要粘贴密钥。");
      return;
    }
    setBusy(true);
    try {
      const value = await request<RuntimeSettings>("/runtime/settings", { method: "PUT", body: JSON.stringify(settings) });
      setSettings({ ...empty, ...value });
      setMessage("已保存；请在当前制作结束后重启服务生效。");
    } catch {
      setMessage("保存失败，请检查文件路径、模型或服务地址。");
    } finally { setBusy(false); }
  }

  const input = (label: string, key: keyof RuntimeSettings) => <label>{label}
    <input type="text" autoComplete="off" value={settings[key]} onChange={(event) => change(key, event.target.value)} />
  </label>;
  const select = (label: string, key: keyof RuntimeSettings, options: Array<[string, string]>) => <label>{label}
    <select value={settings[key]} onChange={(event) => change(key, event.target.value)}>
      <option value="">沿用启动配置</option>{options.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
    </select>
  </label>;

  return <section className="card" aria-label="运行配置">
    <div className="section-heading"><div><h2>运行配置</h2><p className="muted">仅填写本机文件路径，密钥内容保留在原文件中。空项沿用启动配置；保存后重启服务生效。</p></div></div>
    <form onSubmit={(event) => void save(event)}>
      <div className="columns compact">
        <div><h3>内容规划</h3>
          {input("规划凭据文件路径", "planner_key_file")}
        </div>
        <div><h3>画面理解</h3>
          {input("视觉凭据文件路径", "vision_key_file")}
          {input("图库凭据文件路径（可选）", "pexels_key_file")}
        </div>
        <div><h3>本机生成</h3>
          {input("声音配置文件路径", "voice_config_file")}
          {input("对口型配置文件路径", "talking_config_file")}
          {input("语音核验模型目录", "voice_qa_model_path")}
        </div>
      </div>
      <details>
        <summary>高级选项</summary>
        <div className="columns compact">
          <div><h3>内容规划</h3>
            {input("规划服务地址", "planner_base_url")}
            {input("规划模型", "planner_model")}
            {select("规划接口格式", "planner_protocol", [["chat_completions", "Chat Completions"], ["responses", "Responses"]])}
            {select("规划输出格式", "planner_chat_output_mode", [["json_schema", "JSON Schema"], ["json_object", "JSON Object"], ["prompt", "提示词 JSON"]])}
            {select("规划思考模式", "planner_thinking_type", [["disabled", "关闭"], ["enabled", "开启"]])}
            {select("规划推理强度", "planner_reasoning_effort", [["none", "不推理（DeepSeek）"], ["low", "低"], ["high", "高"], ["max", "最高"]])}
            {input("规划输出上限（token）", "planner_max_output_tokens")}
            {input("规划超时时间（秒）", "planner_timeout_seconds")}
          </div>
          <div><h3>画面理解</h3>
            {input("视觉服务地址", "vision_base_url")}
            {input("视觉模型", "vision_model")}
            {select("视觉接口格式", "vision_protocol", [["chat_completions", "Chat Completions"], ["responses", "Responses"]])}
            {select("视频输入能力", "vision_video_input", [["true", "支持"], ["false", "不支持"]])}
            {select("视觉思考模式", "vision_thinking_type", [["disabled", "关闭"], ["enabled", "开启"]])}
          </div>
        </div>
      </details>
      <button className="button primary" disabled={!loaded || busy}>保存运行配置</button>
      {message && <p className="muted" role="status">{message}</p>}
    </form>
  </section>;
}
