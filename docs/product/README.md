# 产品模块

能力行为的唯一权威来源是下表中的模块文档；当前事实只看 [STATUS](../../STATUS.md)，持久决定只看 [DECISIONS](../../DECISIONS.md)。
本表和 [能力权威索引](CAPABILITY_INDEX.md) 由 [CAPABILITIES.json](CAPABILITIES.json) 自动生成，不手工维护。

| 模块（点击进入唯一权威） | 职责边界 |
|---|---|
| [场景规划](SCENE_PLANNING.md) | 把主题、创作者立场、真实素材事实和制作约束转成可执行 ScenePlan；文案完整、镜头意图可核验。 |
| [Voice 克隆旁白](VOICE.md) | 从已授权声线与版本化配方生成 MasterNarration；停顿、分段、独立 QA、修复和来源可追溯。 |
| [Talking 对口型](TALKING.md) | 把确切 Master 区间投射到授权人物参考；连续窗口、LatentSync 配方、独立 QA、复用和修复分别留证。 |
| [生产资源调度](PRODUCTION_SCHEDULING.md) | 跨 Planning、Voice、Talking、素材、预算和 Render 选择有界可行方案；不是第四种内容能力。 |
| [创作者与选题](CREATOR_INTELLIGENCE.md) | 工作区身份、受众、知识、选题来源、文案偏好和反馈事实；ScenePlan 契约交给场景规划。 |
| [媒体资产与选择](MEDIA_ASSET_SYSTEM.md) | 素材导入、权利与来源、区间事实、外部供给和媒体候选选择；跨能力执行顺序交给资源调度。 |
| [发布包装与学习闭环](PUBLISHING_LEARNING.md) | 完成 Render 才能绑定发布包装与回执；历史节目可关联素材；不可覆盖指标快照形成同创作者自身基线并进入下一轮建议。 |
| [执行、能力与预算](EXECUTION_COMPUTE.md) | Run/Job、实际 provider/model、预算、幂等、租约、失败与恢复；不定义跨能力调度算法。 |
| [时间线与渲染](TIMELINE_RENDER_LEARNING.md) | EditPlan、VideoSpec、渲染、整片技术 QA、版本判断和反馈；消费上游能力结果，不重定义其配方。 |

机器注册表：[CAPABILITIES.json](CAPABILITIES.json)。修改权威文档路径或模块关系时先更新注册表，再运行 `python scripts/sync_capability_docs.py`。
`python scripts/check_docs.py` 会核对权威 marker、唯一边界、代码/测试路径、相对链接及生成文件是否过期。

从 [产品总规格](../../CONTENT_OS_EXECUTION_SPEC.md) 理解 R1，通过 [STATUS](../../STATUS.md) 看当前事实。模块文件只写应当如何工作及已经实现到哪一步；过程记录留在 Git。
