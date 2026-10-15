export type ScenePresentation = {
  visual_requirement?: "unknown" | "creator_speaking" | "action_evidence" | "explanatory";
  visual_requirement_reason?: string | null;
  evidence_refs: string[];
  graphic_plan?: {kind: "none" | "text_card" | "unsupported"; treatment: "headline" | "key_point" | "contrast" | null; points: string[]; reason: string | null} | null;
};

export function ScenePresentationEditor({scene, disabled, onChange}: {
  scene: ScenePresentation; disabled: boolean; onChange: (patch: Partial<ScenePresentation>) => void;
}) {
  const graphic = scene.graphic_plan;
  function chooseGraphic(kind: string) {
    onChange({graphic_plan: kind === "legacy" ? null : kind === "text_card"
      ? {kind, treatment: "key_point", points: [], reason: null}
      : {kind: kind as "none" | "unsupported", treatment: null, points: [], reason: ""}});
  }
  return <details className="scene-presentation"><summary>画面需求与图文计划</summary>
    <p className="muted">决定本段需要展示什么；不自动判断哪种更好，也不替代素材与成片审核。修改后先保存草稿，再重新预检。</p>
    <label>画面需求<select disabled={disabled} value={scene.visual_requirement ?? "unknown"} onChange={event => onChange({visual_requirement: event.target.value as ScenePresentation["visual_requirement"]})}>
      <option value="unknown">尚未明确</option><option value="creator_speaking">必须展示本人说话</option><option value="action_evidence">必须展示实际动作或操作证据</option><option value="explanatory">解释信息，可选图文</option>
    </select></label>
    <label>需求理由<textarea disabled={disabled} rows={2} maxLength={1000} value={scene.visual_requirement_reason ?? ""} onChange={event => onChange({visual_requirement_reason: event.target.value || null})} /></label>
    <label>判断依据（每行一项）<textarea disabled={disabled} rows={2} value={scene.evidence_refs.join("\n")} onChange={event => onChange({evidence_refs: event.target.value.split("\n")})} /></label>
    <small>记录可追溯的来源或素材依据；填写引用不代表已验证内容相关性或更优效果。</small>
    <label>额外图文<select disabled={disabled} value={graphic?.kind ?? "legacy"} onChange={event => chooseGraphic(event.target.value)}>
      <option value="legacy">保留现有自动规则（效果未验证）</option><option value="none">不加额外图文，保留主画面</option><option value="text_card">静态图文素材计划</option><option value="unsupported">需要当前不支持的图形</option>
    </select></label>
    {graphic?.kind === "text_card" ? <>
      <label>图文形式<select disabled={disabled} value={graphic.treatment ?? "key_point"} onChange={event => onChange({graphic_plan: {...graphic, treatment: event.target.value as "headline" | "key_point" | "contrast"}})}><option value="headline">单个标题</option><option value="key_point">要点</option><option value="contrast">两项对比</option></select></label>
      <label>图文内容（每行一项）<textarea disabled={disabled} rows={4} value={graphic.points.join("\n")} onChange={event => onChange({graphic_plan: {...graphic, points: event.target.value.split("\n")}})} /></label>
      <small>标题1项、对比2项、要点最多4项；每项最多24字、总计72字。作为字卡或声明的备选图文，不自动叠在人物画面上，不实现动画或流程图。</small>
    </> : graphic ? <label>{graphic.kind === "none" ? "不加图文的理由" : "所需图形与不支持原因"}<textarea disabled={disabled} rows={2} maxLength={1000} value={graphic.reason ?? ""} onChange={event => onChange({graphic_plan: {...graphic, reason: event.target.value}})} /></label> : null}
    <small>没有所需人物、动作或足够长的主画面时，不能用字卡悄悄填补；不支持的需求会在预检中明确阻塞。</small>
  </details>;
}
