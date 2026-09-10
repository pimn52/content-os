# Content OS 当前状态

更新时间：2026-09-10（Asia/Shanghai）

## 当前事实

- 基线 HEAD：`f1ef82c Fix provider execution accounting boundaries`。工作区只保留用户提供且未跟踪的复审证据 `CONTENT_OS_REVIEW_2026-09-10.md`、`reproduce.py`、`test-results.txt`；没有 reset、checkout、删除用户素材或调用外部付费 Provider。
- 当前统一目标已更新为：在明确授权、可用样本和预算具备后，验证“新文案 → 本人新声音 → 本人新的 Talking/口型片段 → 30–60 秒成片”。现有导入旁白、source-led 剪辑、旧原声/旧口型只保留为基础或历史路径，不标记为此目标完成。
- 工作包 A / P1 已提交并有定向证据：
  - P1-A：Provider 请求摘要绑定项目、操作、Provider/模型和输入；同键同输入回放持久结果，同键改输入返回 `409 provider_idempotency_conflict`，未知运行状态不盲目重发。
  - P1-B：全局预算作为工作区账本生命周期上限，项目预算为额外限制，SQLite `BEGIN IMMEDIATE` 保护并发预留。
  - P1-C：实际运行时规划和语义检索进入统一执行/账本边界；多场景路由批量嵌入并记一笔真实调用；无项目归属时拒绝运行时语义检索。
  - P1-D：新旁白先检查原始 Clip 边界与帧容量，超长旁白在装配阶段明确拒绝，不再延后到渲染失败。
- 定向回归已通过：`57 passed`（预算、Provider 执行、场景规划、路由、检索、装配、Job handler）；`reproduce.py` 当前输出证明 P1-A/P1-B 已由 `200/409` 和跨项目 `409` 修复，P1-D 在原越界位置抛出预期 `InsufficientSourceDuration`。该复现附件没有捕获异常，Windows 临时 SQLite 清理会随之报文件占用；正式回归已覆盖该行为。
- 全量 `pytest -q` 已在同一测试会话内跑至 `[100%]` 并以退出码 `0` 结束；当前 P1 定向集为 `57 passed`，`py_compile`、契约导出和 `git diff --check` 通过。不要把历史的 324/319 通过数作为本次证据。
- 工作包 B 的无副作用探针已完成：本机为 AMD 16 逻辑处理器、NVIDIA RTX 3060 Laptop GPU（6 GiB VRAM）；Remotion 附带的 `ffmpeg`/`ffprobe` 与本地渲染可用。缓存中只有 `faster-whisper-small`；Python 环境没有 `torch`、TTS、Chatterbox、MuseTalk/Wav2Lip/LatentSync 等声音或口型依赖，运行时也如实将 TTS/Talking 标为 `not_developed`。
- 已导入的四段用户指定 MP4 都保留了 production 授权标签，但抽查画面显示素材混有不同出镜主体、旧成片和 B-roll；现有元数据没有可靠的本人身份、清晰正脸/嘴部质量或 Voice/Talking 候选结论，不能据此自动登记或调用本人 Profile。未读取桌面密钥文件、未输出任何密钥值。
- B 的首选待验证路线是本地 Chatterbox Multilingual V3（新声音）加 MuseTalk 1.5（新口型）：二者公开许可允许商用，MuseTalk 官方在 Windows 4 GiB 显存设备上验证过 fp16 的 8 秒生成，因而该机 6 GiB 显存具备有限小样验证条件。模型权重、PyTorch/依赖和实际样本授权尚未就绪；唯一保留的云备选是需要单独账号/预付额度和同意流程的 HeyGen，未接入或调用。
- 工作包 C 的可追溯草稿链已完成并作了真实浏览器验收：新文案、IP 版本、证据引用和输入指纹会持久化为不可变的 Draft revision；脚本、场景文案或 IP 更新会由服务端清除旧 ScenePlan 后继的路由、候选、VideoSpec 和本地渲染预览，旧标签页不能重新提交它们。浏览器实际创建项目、保存文案、两次更新 IP 后，草稿从 `v1/r1` 变为 `v3/r3` 并即时显示失效状态；在无 Provider 配置时“生成文案 + ScenePlan”如实返回 `scene planner is not configured`，没有以夹具或旧稿冒充模型结果。

## 当前工作包

**D：主声音时间线与成片不截断（进行中；B 的 U-Voice 验收待定）**

工作包 A 已提交，C 的服务端/浏览器实现与回归已完成。B 的本机能力、候选路线和样本缺口已记录；不在未取得声音/形象同意前下载模型、读取密钥或产生外部调用。现在移除“每场景一段音频”的人为限制，改为一个完整的本人新声音主时间线驱动多场景视频、字幕和 Talking 片段；该工程不依赖 U-Voice 的实际样本。

## 下一个 ready 任务

**D：主声音时间线与不截断装配（进行中）**

以一条完整、经授权且有真实时间轴的旁白作为主轨，自动按其句界分配场景、字幕和 Talking 片段。视觉不足时拆分/补齐视觉而不是延长源 Clip；渲染只播放一次主音轨并静音被覆盖的原声。之后接续 E 的 Voice/Talking Job 状态机。

## 当前阻塞与授权边界

- B 尚缺 U-Voice：确认哪一位出镜者是本人、允许以哪些清晰声音/正脸样本做本地声音与口型生成、接受两条未见新台词的试听/预览，以及允许一次本地模型/权重下载。不得将先前的“素材可用于生产”或桌面暂存密钥解释为声音克隆/口型生成的无限授权。
- Talking/TTS 生成仍未实现；不能以旧素材、导入旁白或通用头像冒充新 Talking。
- 不存在新的外部 API 调用或费用。本机环境变量/桌面文件只可在明确路线、费用和 U-Voice 授权后被最小范围地配置使用；现有 Kimi/Anthropic 凭据不是本地 Voice/Talking 路线的直接配置。

## 恢复命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe .\reproduce.py .
git diff --check
```

历史状态日志保留在 Git 基线 `f4da613:STATUS.md`；索引见 [`docs/history/STATUS_HISTORY.md`](docs/history/STATUS_HISTORY.md)。
