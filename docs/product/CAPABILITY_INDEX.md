# 核心能力权威索引

> 自动生成，请勿手工编辑。来源：[CAPABILITIES.json](CAPABILITIES.json)；生成：`python scripts/sync_capability_docs.py`。

每个能力的完整行为规则、边界和已落地范围只在其“唯一权威”文档中定义。其他文档只链接，不复制第二套规则。
当前运行事实见 [STATUS](../../STATUS.md)，持久架构决定见 [DECISIONS](../../DECISIONS.md)。

## 核心内容能力

### 场景规划

- 唯一权威：[SCENE_PLANNING.md](SCENE_PLANNING.md)
- 职责边界：把主题、创作者立场、真实素材事实和制作约束转成可执行 ScenePlan；文案完整、镜头意图可核验。
- 代码入口：[services/api/app/scene_planning.py](../../services/api/app/scene_planning.py)<br>[services/api/app/providers/scene_planner.py](../../services/api/app/providers/scene_planner.py)<br>[services/api/app/planning_input.py](../../services/api/app/planning_input.py)<br>[services/api/app/video_production.py](../../services/api/app/video_production.py)
- 验证入口：[services/api/tests/test_scene_planner.py](../../services/api/tests/test_scene_planner.py)<br>[services/api/tests/test_scene_plan_api.py](../../services/api/tests/test_scene_plan_api.py)<br>[services/api/tests/test_planning_input.py](../../services/api/tests/test_planning_input.py)<br>[services/api/tests/test_automatic_production.py](../../services/api/tests/test_automatic_production.py)
- 关联模块：[Voice 克隆旁白](VOICE.md)、[Talking 对口型](TALKING.md)、[媒体资产与选择](MEDIA_ASSET_SYSTEM.md)、[生产资源调度](PRODUCTION_SCHEDULING.md)、[时间线与渲染](TIMELINE_RENDER_LEARNING.md)

### Voice 克隆旁白

- 唯一权威：[VOICE.md](VOICE.md)
- 职责边界：从已授权声线与版本化配方生成 MasterNarration；停顿、分段、独立 QA、修复和来源可追溯。
- 代码入口：[services/api/app/voice_recipe.py](../../services/api/app/voice_recipe.py)<br>[services/api/app/voice_pronunciation.py](../../services/api/app/voice_pronunciation.py)<br>[services/api/app/voice_syntax.py](../../services/api/app/voice_syntax.py)<br>[services/api/app/voice_emphasis.py](../../services/api/app/voice_emphasis.py)<br>[services/api/app/narration_performance.py](../../services/api/app/narration_performance.py)<br>[services/api/app/voice_seam.py](../../services/api/app/voice_seam.py)<br>[services/api/app/jobs/handlers.py](../../services/api/app/jobs/handlers.py)<br>[services/api/app/worker_cli.py](../../services/api/app/worker_cli.py)
- 验证入口：[services/api/tests/test_voice_generation.py](../../services/api/tests/test_voice_generation.py)<br>[services/api/tests/test_voice_performance.py](../../services/api/tests/test_voice_performance.py)<br>[services/api/tests/test_voice_seam.py](../../services/api/tests/test_voice_seam.py)<br>[services/api/tests/test_voice_profiles_api.py](../../services/api/tests/test_voice_profiles_api.py)<br>[services/api/tests/test_voice_pronunciation.py](../../services/api/tests/test_voice_pronunciation.py)<br>[services/api/tests/test_voice_syntax.py](../../services/api/tests/test_voice_syntax.py)<br>[services/api/tests/test_voice_emphasis.py](../../services/api/tests/test_voice_emphasis.py)<br>[services/api/tests/test_voice_repair.py](../../services/api/tests/test_voice_repair.py)<br>[services/api/tests/test_voice_segment_repair.py](../../services/api/tests/test_voice_segment_repair.py)
- 关联模块：[场景规划](SCENE_PLANNING.md)、[Talking 对口型](TALKING.md)、[生产资源调度](PRODUCTION_SCHEDULING.md)、[时间线与渲染](TIMELINE_RENDER_LEARNING.md)

### Talking 对口型

- 唯一权威：[TALKING.md](TALKING.md)
- 职责边界：把确切 Master 区间投射到授权人物参考；连续窗口、LatentSync 配方、独立 QA、复用和修复分别留证。
- 代码入口：[services/api/app/providers/maintained_talking.py](../../services/api/app/providers/maintained_talking.py)<br>[services/api/app/domain/talking_recipe.py](../../services/api/app/domain/talking_recipe.py)<br>[services/api/app/talking/pool.py](../../services/api/app/talking/pool.py)<br>[services/api/app/talking/scheduling.py](../../services/api/app/talking/scheduling.py)<br>[services/api/app/talking/sync_probe.py](../../services/api/app/talking/sync_probe.py)<br>[services/api/app/talking/sync_probe_child.py](../../services/api/app/talking/sync_probe_child.py)<br>[services/api/app/jobs/handlers.py](../../services/api/app/jobs/handlers.py)
- 验证入口：[services/api/tests/test_maintained_talking_dual.py](../../services/api/tests/test_maintained_talking_dual.py)<br>[services/api/tests/test_talking_pool.py](../../services/api/tests/test_talking_pool.py)<br>[services/api/tests/test_talking_scheduling.py](../../services/api/tests/test_talking_scheduling.py)<br>[services/api/tests/test_planned_talking_source_reuse.py](../../services/api/tests/test_planned_talking_source_reuse.py)<br>[services/api/tests/test_talking_sync_probe.py](../../services/api/tests/test_talking_sync_probe.py)
- 关联模块：[场景规划](SCENE_PLANNING.md)、[Voice 克隆旁白](VOICE.md)、[媒体资产与选择](MEDIA_ASSET_SYSTEM.md)、[生产资源调度](PRODUCTION_SCHEDULING.md)、[时间线与渲染](TIMELINE_RENDER_LEARNING.md)

## 生产资源调度

### 生产资源调度

- 唯一权威：[PRODUCTION_SCHEDULING.md](PRODUCTION_SCHEDULING.md)
- 职责边界：跨 Planning、Voice、Talking、素材、预算和 Render 选择有界可行方案；不是第四种内容能力。
- 代码入口：[services/api/app/video_production.py](../../services/api/app/video_production.py)<br>[services/api/app/production_runs.py](../../services/api/app/production_runs.py)<br>[services/api/app/media/planning.py](../../services/api/app/media/planning.py)<br>[services/api/app/talking/scheduling.py](../../services/api/app/talking/scheduling.py)<br>[services/api/app/budget.py](../../services/api/app/budget.py)
- 验证入口：[services/api/tests/test_automatic_production.py](../../services/api/tests/test_automatic_production.py)<br>[services/api/tests/test_production_preflight.py](../../services/api/tests/test_production_preflight.py)<br>[services/api/tests/test_production_runs.py](../../services/api/tests/test_production_runs.py)<br>[services/api/tests/test_material_resolution.py](../../services/api/tests/test_material_resolution.py)<br>[services/api/tests/test_talking_scheduling.py](../../services/api/tests/test_talking_scheduling.py)
- 关联模块：[场景规划](SCENE_PLANNING.md)、[Voice 克隆旁白](VOICE.md)、[Talking 对口型](TALKING.md)、[媒体资产与选择](MEDIA_ASSET_SYSTEM.md)、[执行、能力与预算](EXECUTION_COMPUTE.md)、[时间线与渲染](TIMELINE_RENDER_LEARNING.md)

## 支撑模块

| 模块（点击进入唯一权威） | 职责边界 |
|---|---|
| [创作者与选题](CREATOR_INTELLIGENCE.md) | 工作区身份、受众、知识、选题来源、文案偏好和反馈事实；ScenePlan 契约交给场景规划。 |
| [媒体资产与选择](MEDIA_ASSET_SYSTEM.md) | 素材导入、权利与来源、区间事实、外部供给和媒体候选选择；跨能力执行顺序交给资源调度。 |
| [发布包装与学习闭环](PUBLISHING_LEARNING.md) | 完成 Render 才能绑定发布包装与回执；历史节目可关联素材；不可覆盖指标快照形成同创作者自身基线并进入下一轮建议。 |
| [执行、能力与预算](EXECUTION_COMPUTE.md) | Run/Job、实际 provider/model、预算、幂等、租约、失败与恢复；不定义跨能力调度算法。 |
| [时间线与渲染](TIMELINE_RENDER_LEARNING.md) | EditPlan、VideoSpec、渲染、整片技术 QA、版本判断和反馈；消费上游能力结果，不重定义其配方。 |

## 维护规则

- 行为变化先改对应权威文档；跨模块关系只改注册表，索引自动再生。
- 新增核心能力必须提供唯一权威文件、marker、代码与测试入口，并加入关联模块。
- `check_docs.py` 在权威文件缺失、重复、marker 漂移、代码/测试路径或相对链接失效、索引过期时失败。
