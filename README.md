# Content OS

本地优先的创作者短视频工作台：新主题 → 文案与视觉方案 → 能力/授权/预算预检 → 必要的声音与人物画面 → 剪辑、渲染和复核。当前仍是内部 Alpha，真实进度见 [STATUS.md](STATUS.md)。

## 目录

- apps/web：React 工作台；apps/renderer：Remotion。
- services/api：FastAPI、SQLite、Job/Worker 与提供商适配。
- docs/product：模块行为；docs/architecture：技术边界；scripts：本地检查。
- [START_HERE.md](START_HERE.md) 是阅读入口。

## Windows 本地运行

在仓库根目录：

    py -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e ".[test]"
    .\scripts\start.ps1

打开 http://127.0.0.1:8000/app/ 。只读检查时可用 start.ps1 -NoWorker。对已有数据库设 CONTENT_OS_DB_STARTUP_MODE=existing、绝对 CONTENT_OS_DB_PATH 和 CONTENT_OS_DATA_ROOT；此模式要求完整现有 schema，不运行迁移。

已配置的 Worker 可加 `--production-run-id <执行 ID>`，仅连续处理该视频的声音、人物画面、QA 和渲染；完成即退出，失败不重试，旧待办保留。`--once` 只处理其中一项。

    .\.venv\Scripts\python.exe -m pytest -q
    npm --prefix apps\web run build
    .\.venv\Scripts\python.exe scripts\check_docs.py

首次 Web 安装用 npm --prefix apps\web install。运行依赖本地 FFmpeg/FFprobe 与锁定的 Node/Remotion；版本和再分发注意事项见 [依赖清单](docs/DEPENDENCIES.md)。

## 可选能力

ScenePlan 优先用 CONTENT_OS_LLM_API_KEY_FILE 指向本机密钥文件，配合 CONTENT_OS_LLM_BASE_URL、CONTENT_OS_LLM_MODEL；密钥无需放进环境变量。显式文件不可用时直接报错，不回退其他凭据。Voice、Talking、ASR 由对应 Worker 配置启用。

外部视频内置 Commons；Pexels、Pixabay、Coverr 分别用 CONTENT_OS_PEXELS_API_KEY_FILE、CONTENT_OS_PIXABAY_API_KEY_FILE、CONTENT_OS_COVERR_API_KEY_FILE 指向只含对应密钥的本机文件。环境变量只存文件路径，密钥不随媒体下载外发。已配置来源可在普通 UI 选择并参与有界自动获取；配置不代表鉴权或网络已验证，Coverr 须符合所选套餐。来源、版本和未评估的质量状态随入库保存，实际验证边界见[素材模块](docs/product/MEDIA_ASSET_SYSTEM.md)。

API 可用 CONTENT_OS_ACCESS_TOKEN 限制本地访问。素材权利在导入时登记，人物同意在 Profile 登记；密钥、许可原文和私人媒体不要提交仓库。

网页发现可复用已选择的 DeepSeek 规划凭据与 Moonshot 视觉凭据；也可分别用 CONTENT_OS_DEEPSEEK_WEB_API_KEY_FILE、CONTENT_OS_KIMI_WEB_API_KEY_FILE 指定本机凭据文件。搜索与必要的正文抓取按次计入原预算，不调用聊天模型。自动素材源顺序可用 CONTENT_OS_EXTERNAL_SOURCE_ORDER 设置，例如 `pixabay,pexels,coverr,wikimedia_commons`，未配置的 Key 源会跳过；不在列表内的来源不自动使用。

可选本地 ASR 依赖不随默认安装下载模型。任何真实模型调用、媒体渲染和商业发布都须通过当前能力、许可、预算、QA 与产品复核；具体运行状态以普通 UI 和 [STATUS](STATUS.md) 为准。
