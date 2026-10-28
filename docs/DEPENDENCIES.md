# 依赖与许可

确切版本以 pyproject.toml、各 package-lock.json 和当前安装为准；下表是已记录的开发环境，不代替发布时复核。

| 组件 | 已记录版本 | 用途与许可要点 |
|---|---:|---|
| Python / FastAPI / Pydantic | 3.12.13 / 0.141.1 / 2.13.4 | API；分别核对 PSF、MIT、MIT |
| Node / npm | 24.15.0 / 11.12.1 | Web 与渲染工具链 |
| React / Vite / TypeScript | 18.2.0 / 5.4.8 / 5.5.4 | Web；MIT、MIT、Apache-2.0 |
| Remotion | 4.0.522 | 本地视频渲染；使用范围受其许可条款限制 |
| FFmpeg / FFprobe | 外部安装 | 媒体处理；具体构建和再分发许可须单独核对 |
| faster-whisper / CTranslate2 | 可选，1.2.1 / 4.8.2 | 本地 ASR；权重下载和模型条款另行核对 |

默认安装不打包模型权重，也不自动证明第三方商业用途。发布前对实际构建、传递依赖、浏览器二进制、模型文件和 FFmpeg 构建做一次许可与安全复核。
