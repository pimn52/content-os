# Content OS 当前状态

更新时间：2026-09-10（Asia/Shanghai）

## 当前事实

- 基线 HEAD：`f4da613 Implement Content OS execution workflow`。工作区保留用户提供的复审证据 `CONTENT_OS_REVIEW_2026-09-10.md`、`reproduce.py`、`test-results.txt`，以及当前工作包 A 的未提交实现；没有 reset、checkout、删除用户素材或调用外部付费 Provider。
- 当前统一目标已更新为：在明确授权、可用样本和预算具备后，验证“新文案 → 本人新声音 → 本人新的 Talking/口型片段 → 30–60 秒成片”。现有导入旁白、source-led 剪辑、旧原声/旧口型只保留为基础或历史路径，不标记为此目标完成。
- 工作包 A / P1 已实现并有定向证据：
  - P1-A：Provider 请求摘要绑定项目、操作、Provider/模型和输入；同键同输入回放持久结果，同键改输入返回 `409 provider_idempotency_conflict`，未知运行状态不盲目重发。
  - P1-B：全局预算作为工作区账本生命周期上限，项目预算为额外限制，SQLite `BEGIN IMMEDIATE` 保护并发预留。
  - P1-C：实际运行时规划和语义检索进入统一执行/账本边界；多场景路由批量嵌入并记一笔真实调用；无项目归属时拒绝运行时语义检索。
  - P1-D：新旁白先检查原始 Clip 边界与帧容量，超长旁白在装配阶段明确拒绝，不再延后到渲染失败。
- 定向回归已通过：`57 passed`（预算、Provider 执行、场景规划、路由、检索、装配、Job handler）；`reproduce.py` 当前输出证明 P1-A/P1-B 已由 `200/409` 和跨项目 `409` 修复，P1-D 在原越界位置抛出预期 `InsufficientSourceDuration`。该复现附件没有捕获异常，Windows 临时 SQLite 清理会随之报文件占用；正式回归已覆盖该行为。
- 全量 `pytest -q` 已在同一测试会话内跑至 `[100%]` 并以退出码 `0` 结束；当前 P1 定向集为 `57 passed`，`py_compile`、契约导出和 `git diff --check` 通过。不要把历史的 324/319 通过数作为本次证据。

## 当前工作包

**A：收口基础与 P1（完成，待提交）**

已完成 P1 修复、回归、契约导出、规格/入口同步和差异检查。下一个动作是提交只包含本工作包实现与证据入口的变更，不纳入用户提供的未跟踪审查/复现文件。

## 下一个 ready 任务

**B：真实 Voice/Talking 能力探针（进行中，仅本机只读准备）**

在 A 验证通过后，检查 CPU/GPU/VRAM、已授权素材质量、现有本地能力和可见 Provider 配置；选择一条可行路线及至多一个备选。不会读取/输出密钥或发起外部调用。若没有明确声音/形象授权、足够清晰的样本、已批准 Provider 和预算，整理为一次 U-Voice 决策。

## 当前阻塞与授权边界

- 还没有 U-Voice 对声音/形象生成使用、样本可接受性、具体 Provider 和预算的集中确认；不得将先前的“素材可用于生产”或桌面暂存密钥解释为声音克隆/口型生成的无限授权。
- Talking/TTS 生成仍未实现；不能以旧素材、导入旁白或通用头像冒充新 Talking。
- 不存在新的外部 API 调用或费用。本机环境变量/桌面文件只可在明确路线、费用和 U-Voice 授权后被最小范围地配置使用。

## 恢复命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe .\reproduce.py .
git diff --check
```

历史状态日志保留在 Git 基线 `f4da613:STATUS.md`；索引见 [`docs/history/STATUS_HISTORY.md`](docs/history/STATUS_HISTORY.md)。
