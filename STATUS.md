# Content OS 开发交接

日期：2026-09-07。

## 本批次范围

已完成 Task 001 核心契约、Task 002a 后端运行骨架、Task 003 SQLite 持久化与最小任务基础、Task 004A 本地媒体导入/去重/探测、Task 005/006 连续镜头切分、音频/关键帧提取与持久化媒体分析切片、Task 007 可替换 ASR Provider 与 Clip 转写持久化，以及 provider-neutral Vision/Embedding、本地 Clip 向量索引与过滤检索、三类 Asset Job 的执行边界、自动心跳、最小 Job/Search API、素材库 UI 和可控轮询 Worker CLI。渲染骨架尚未完成，因此不将整个 Gate 1 标记通过。

- Terra：核心数据模型、JSON Schema、示例、校验测试。
- Luna：FastAPI、健康检查、Python 项目配置、Windows 启动文档。
- Sol：G0 时间/序列化/渲染契约审查。
- 主 Agent：基线对齐、集成与交付。
- Luna：SQLite 迁移、IPProfile / Project / Asset / Clip / Job Repository、事务与关系校验。
- Terra：任务幂等入队、原子领取、租约、心跳、重试上限与崩溃恢复。
- Luna：文件/流式媒体导入、内容寻址存储、ffprobe 封装与基础 fixture 测试。
- Terra：媒体并发去重、数据库/文件原子性、探测回退与迁移失败强化。
- Terra：连续 Clip 检测、时间边界合并、确定性 Clip ID 与原子 Clip 集替换。
- Luna：标准分析音频与关键帧派生文件；Terra 完成跨模块幂等/并发集成。
- Terra：provider-neutral ASR 契约、OpenAI-compatible BYOK 适配器、安全错误分类与本地 HTTP 边界测试。
- Luna：时间戳转写到连续 Clip 的重叠映射、权威快照替换与 SQLite 原子持久化。
- Luna：Asset Job target 迁移、原子绑定、跨进程幂等与冲突回滚。
- Terra：同步 JobRunner、终态/重试/租约丢失语义，以及媒体分析/ASR handler 集成。
- Terra：长任务独立 SQLite 连接自动心跳、续租失败检测与线程清理。
- Luna：持久化默认路径、Asset Job 入队/状态 API、lifespan 连接管理与并发请求验证。
- Terra：可中断轮询 Worker、启动租约恢复、按已注册类型安全领取与优雅停机。
- Luna：Worker CLI 运行时组装、信号处理、BYOK 配置校验与 console script。
- Terra：provider-neutral Vision 契约、OpenAI-compatible Responses 适配器与本地 HTTP 边界测试。
- Luna：视觉元数据映射、原子持久化与视觉管线隔离测试；主 Agent 完成 `INDEX_CLIPS` handler/Worker/API 接线。
- Terra：provider-neutral Embedding 契约与 OpenAI-compatible 批量适配器。
- Luna：SQLite Clip 向量持久化与余弦过滤检索；主 Agent 完成版本化迁移、索引管线、自然语言搜索 API 与 Vision→Embedding 作业串联。
- Luna：Task 010 本地素材库页面、只读 Asset/Clip/媒体 API；主 Agent 修正语义搜索接线并完成浏览器交互验收。
- Terra：Task 011 provider-neutral ScenePlanner、OpenAI-compatible 严格 JSON Schema 适配器；主 Agent 完成 Project→ScenePlan API。
- Terra：Task 012 provider-neutral Asset Router、本地连续 Clip 多因子排序与补拍缺口；主 Agent 加固时长/来源约束并完成路由 API。

未调用任何生成供应商，不产生本产品的媒体生成 API 费用；Agent 本身的运行消耗由当前平台计量。

## 当前限制

- 已能导入、探测、切分视频、提取音频/关键帧，通过可替换 ASR/Vision 边界写回 Clip，并将 transcript + Vision + Clip metadata 生成 Embedding 后在 SQLite 本地 Top-K 检索；尚未实现自动写稿、自动剪辑、克隆声音、口播生成或发布。
- Project / Asset / Clip / Job 的基础外键，以及 Clip 与数据库 Asset 时长一致性已由 Repository 校验；源文件是否存在、数据库时长是否与真实媒体一致，留待导入/ffprobe 与 assembler 验证。
- 授权记录是数据凭据，无法仅凭字符串核验权利；撤销授权与生成前检查在对应业务流程实现。
- 任务状态、幂等、领取、租约、自动心跳、有限重试、崩溃恢复、媒体/ASR handler、最小任务 API 与前台轮询 Worker CLI 已接通；尚无系统后台服务、预算预留/对账或任务 UI。
- Python 依赖范围不是锁文件；Windows 安装与完整依赖审计仍待后续。FFmpeg/ffprobe 当前作为外部工具，不随仓库分发，发布前需按具体构建核验 GPL/LGPL 与编解码库许可。

## 继续开发阶段

Task 012 Asset Router 最小可运行切片已完成，当前继续进入 Task 013 VideoSpec 组装。执行器不会在长耗时媒体/网络调用期间持有 SQLite 写事务，也不会把供应商密钥持久化。

真实媒体验收阶段再使用用户授权的 3–5 个视频和一个脚本。没有这批真实素材前，不能声称个性化成片质量通过。

## 本批次验证结果

- `python -m pip install -e ".[test]"` 成功，验证了项目安装配置。
- `python -m pytest`：11 passed，2 条依赖弃用警告。
- 13 份 JSON Schema 导出成功；3 个关联示例 JSON 往返校验通过。
- Uvicorn 实际启动及 `/health` 请求此前已通过。
- Sol 初审提出的非有限 metadata、时间线标识/时长问题已修复；Sol 最终复核受用量限制未完成，主 Agent 已检查最新实现并运行测试。G0 为本批次内部通过，不宣称完整独立审查通过。
- Windows 本地 Python 3.12 虚拟环境、PowerShell 启动脚本及 `/health` 已验证。
- Task 003 完整测试为 `25 passed`；文件型 SQLite 多连接并发入队/领取压力用例连续复跑 20 轮通过，`pip check` 与 `compileall` 通过。
- 已建立本地 Git 基线提交 `0c033aa`，后续开发变更均可通过 Git 审查与回滚。
- Task 004A 使用真实 FFmpeg 9.0.1 / ffprobe 生成并导入带音频视频，完整测试 `54 passed`；媒体并发导入压力用例连续复跑 10 轮通过。
- Task 005/006 使用三段硬切画面及真实音轨验证连续 Clip 时间戳、WAV 时长/起点/采样率、JPEG 尺寸、源文件不变与重复运行稳定；集成后完整测试 `76 passed`。
- Task 007 通过本地 fake HTTP server 验证 multipart、segment 时间戳、错误分类、BYOK 不泄露及禁用携密重定向；Clip 映射验证跨镜头重叠、清理过期转写、重复执行稳定与事务回滚；完整测试 `105 passed`。
- Job 执行边界验证 Asset target 同事务入队、并发幂等、handler 事务外执行、重试/终态/中断恢复、错误脱敏，以及 ASR segment 经 Worker 写回 Clip；集成后完整测试 `126 passed`。
- 自动心跳与 Job API 集成后完整测试 `135 passed`；8 路 API 并发入队及 heartbeat/runner 聚焦套件连续复跑 10 轮通过。
- 可控轮询 Worker/CLI 已验证空闲中断、多任务、启动恢复、部分类型隔离、handler 内停机等待、参数安全与连接关闭；console script 实际安装/帮助命令通过，完整测试 `156 passed`。
- Vision 阶段通过本地 fake transport 验证 Responses 请求、JPEG data URL、严格 JSON Schema、错误分类、携密重定向禁用与 BYOK 脱敏；视觉管线验证 Clip/keyframe 身份绑定、供应商失败零写入、原子替换、重复执行稳定，并完成 `INDEX_CLIPS` API/Worker 接线；完整测试 `183 passed`。
- Embedding/检索阶段通过本地 fake transport 验证批量顺序、维度/有限值、错误分类、携密重定向禁用与 BYOK 脱敏；SQLite schema v4 保存向量与 `embedding_ref`，支持 asset/orientation/talking filters、稳定余弦 Top-K，并以“本人坐在电脑前操作软件”用例返回合理 Clip；完整测试 `197 passed`，未调用真实供应商。
- Task 010 素材库通过 API 测试验证 Asset/Clip 列表、详情、404、媒体文件与区间校验；实际浏览器验证页面加载、空库状态、查询输入和 `/clips/search` 错误反馈，完整测试 `199 passed`。
- Task 011 通过本地 fake server 验证 Project/脚本/主题请求、严格 ScenePlan JSON Schema、连续 order、唯一 scene_id、真实素材优先、错误分类、禁携密重定向与 BYOK 脱敏；Project→ScenePlan API 验证项目绑定、404、运行时配置和拒绝请求内密钥，完整测试 `209 passed`。
- Task 012 以本地 SQLite fixture 验证“本人坐在电脑前操作软件”返回合理连续 Clip Top‑1/Top‑3、真实素材来源过滤、时长满足、语义/视觉质量/镜头适配/新鲜度/复用惩罚、稳定排序、零供应商成本和补拍缺口；路由 API 验证 Project 绑定，完整测试 `215 passed`。
- 便携 FFmpeg 仅放在本机临时目录用于开发验收，下载包 SHA-256 已按发布方值核对，未提交仓库。
- 尚未使用用户真实 IP 素材；当前仅证明媒体工程链路，不代表个性化素材理解通过。
