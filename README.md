<div align="center">

# VLV · Video Listen View

**V**ideo **L**isten **V**iew：**本地文件 / B 站 / YouTube / 抖音 / 任何 yt-dlp 支持的站点** → 字幕 / 弹幕 / 可选 **Whisper** 转写 → 合并文稿 → **多模型 LLM** 分析与对话 →（可选）**抽帧 + 本地 VLM** 深度画面理解。

跨 Windows / macOS / Linux，中英双语界面，一键导出诊断包，API Key 本地加密存储。GitHub 仓库 **`vlv`**（旧名 `bilibili-transcript-oneclick-vision` 访问时会重定向）。

[![CI](https://github.com/Jcxu97/vlv/actions/workflows/ci.yml/badge.svg)](https://github.com/Jcxu97/vlv/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-0078D6)]()

[**预览**](#预览) · [**功能**](#功能) · [**快速开始**](#快速开始) · [**命令行**](#命令行) · [**项目结构**](#项目结构) · [**升级指南**](#从-v03-升级到-v10) · [**开发**](#开发与贡献) · [**English**](#english)

</div>

---

## 预览

### 提取与分析
![VLV — 提取与分析](docs/screenshots/gui-extract.png)

### API 与模型
![VLV — API 与模型](docs/screenshots/gui-api-models.png)

### 分析与对话
![VLV — 分析与对话](docs/screenshots/gui-analysis-chat.png)

---

## 功能

| 能力 | 说明 |
|------|------|
| **多源抽取** | B 站 / YouTube / 抖音 / 任何 yt-dlp 支持的站点；URL 自动路由到对应 `PlatformAdapter` |
| **跨 OS** | Windows / macOS / Linux 原生运行，CI 矩阵全覆盖 |
| **图形工作台** | 提取 → 总结 → 多轮对话；可选「深度内容分析」；中英双语 UI |
| **B 站登录** | Playwright 登录、会话持久化、`cookies.txt` 供 yt-dlp |
| **无字幕** | yt-dlp 拉音频 + **faster-whisper**（CPU / 可选 CUDA） |
| **多模型云端** | Gemini / OpenAI / Groq / Anthropic / xAI 等；纯 `urllib`，**零 SDK** |
| **本地 OpenAI 兼容** | 对接本机 **Gemma 4 4-bit** 等服务（HTTP `/v1`） |
| **Vision** | 抽帧、OCR、`vision_deep_pipeline` + 本地 VLM |
| **任务系统** | 所有子进程经 `TaskManager` 跑，GUI「停止」按钮必响应（Windows `taskkill /T` / POSIX `SIGTERM→SIGKILL`） |
| **GPU 看门狗** | 本地推理前预检显存；连续崩溃自保护退出，GUI 可自动降级到云端 LLM |
| **结构化日志** | 每次运行产出 `out/log/<stamp>_session.jsonl`；一键「导出诊断包」打包最近 5 次 log + 环境信息 + 配置指纹（脱敏） |
| **错误码体系** | 用户可见的异常都带 `VLV_E***` 稳定错误码，便于复述与检索 |
| **API Key 加密** | 可选主密码保护；AES-GCM（有 `cryptography`）/ HMAC-SHA256 回退（纯 stdlib） |

---

## 大模型建议

- **云端 API 最省事**：GUI **「API 与模型」** 里填 Gemini / OpenAI / Groq / Anthropic / xAI 等 Key 即可。
- **本地部署可选**：仓库里的 Gemma / Qwen / OpenAI 兼容服务（如 `SERVE_GEMMA4_4BIT.bat`）需自备显卡、CUDA、权重。Mac 无 CUDA，本地 Gemma / Qwen 不可用；走云端。
- **Vision** 同理：有云端多模态 API 时优先云端，本地 VLM 按需。

---

## 快速开始

### Windows（便携，推荐）

1. **克隆**
   ```bash
   git clone https://github.com/Jcxu97/vlv.git
   cd vlv
   ```
2. **一键准备便携目录**（PowerShell）：
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
   .\准备便携环境.ps1
   ```
   脚本会下载嵌入式 Python、安装依赖、（可选）预下 Whisper 权重、安装 Chromium 到 `pw-browsers/`，并把 `..\src` 写入 `python_embed/*._pth`。
3. **GPU 转写**（可选）：
   ```powershell
   .\python_embed\python.exe -m pip install -r requirements-gpu.txt
   ```
4. **启动 GUI**：双击 **`START.bat`** 或 **`启动.bat`**。
5. **API Keys**：GUI **「API 与模型」** 里至少填一个云端 Key。

**离线 / 内网**：拷完整便携目录，用 **`START_OFFLINE.bat`**。

### macOS / Linux

需要 Python 3.11+，已安装系统 `ffmpeg`（Linux `apt install ffmpeg` / macOS `brew install ffmpeg`）。

```bash
git clone https://github.com/Jcxu97/vlv.git
cd vlv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.lock -r requirements.txt
python -m playwright install chromium   # 仅用 B 站登录需要

./start.sh                               # 或 python run_gui.py
```

本地 CUDA 推理（Linux）：按 `serve_gemma4_4bit.sh` 内说明先装 PyTorch。

### 开发者安装（可编辑）

```bash
pip install -e ".[dev]"
pytest -m "not slow and not e2e and not gpu"
```

---

## 命令行

新的 `vlv` 命令（`pip install -e .` 之后可用）：

```bash
vlv extract https://www.bilibili.com/video/BVxxxx
vlv extract https://youtu.be/dQw4w9WgXcQ
vlv extract https://www.douyin.com/video/XXXX
vlv extract --no-playlist --skip-video https://...
vlv analyze out/2026-04-20/HHMMSS_Title_bilibili_BVxxxx/
vlv diagnostics                         # 导出诊断包
vlv gui                                  # 等价于双击 START.bat
```

兼容旧路径（仍有效）：

```bash
# Windows 便携
python_embed\python.exe -m bilibili_vision.bilibili_pipeline extract "https://..."

# 开发
PYTHONPATH=src python -m bilibili_vision.bilibili_pipeline --help
```

---

## 项目结构

```text
.
├── run_gui.py                 # 根入口：加 src 到 path 并启动 GUI
├── start.sh / START.bat       # 跨 OS 启动脚本
├── pyproject.toml             # 打包 + setuptools_scm 版本
├── requirements.lock          # CPU 基线依赖锁
├── requirements-cuda{11,12}.lock   # GPU 变体
├── src/bilibili_vision/
│   ├── platform/              # PlatformAdapter：bilibili / youtube / douyin / generic
│   ├── tasks/                 # TaskManager + CancellationToken
│   ├── locales/{zh_CN,en_US}/ # i18n 语料
│   ├── log_config.py          # 日志与诊断会话
│   ├── errors.py              # VLV_E*** 错误码
│   ├── diagnostics.py         # 诊断包导出
│   ├── gpu_watchdog.py        # 显存预检 + 崩溃计数器
│   ├── secret_store.py        # 加密 API Key
│   ├── i18n.py                # gettext 封装
│   ├── cli.py                 # `vlv` 命令
│   └── (pipeline / gui / LLM / vision 等)
├── tests/                     # 105+ pytest，CI 全绿
├── docs/                      # architecture.md / contributing.md
├── scripts/compile_locales.py # 编译 .po → .mo
├── out/                       # 运行输出（.gitignore）
└── .github/workflows/         # CI + Release
```

**路径约定**：`bilibili_vision.paths.PROJECT_ROOT` 指仓库根，用于 `out/`、`models/`、`.credentials/` 等；与包内源码目录分离。

---

## 从 v0.3 升级到 v1.0

| 旧 | 新 |
|----|----|
| URL 仅支持 B 站 | B 站 / YouTube / 抖音 / 任何 yt-dlp 站点，自动路由 |
| 仅 Windows | Windows / macOS / Linux |
| `print` 到 stdout | 结构化 JSONL 日志 + 「导出诊断包」 |
| 散发 `AssertionError` / `RuntimeError` | `VLVError` 层级 + `VLV_E***` 错误码 |
| 子进程直接 `subprocess.run` | `TaskManager.run_subprocess()`，GUI 可取消 |
| API Key 明文 JSON | `SecretStore` 可选加密；旧明文自动迁移 |
| 硬编码中文 UI | gettext，`设置 → 语言`切换 zh_CN / en_US |
| 手写版本号 | `setuptools_scm` 从 git tag 读 |
| 仅 syntax-check CI | pytest 多 OS × 多 Python 矩阵 + coverage |

**无破坏性**：旧命令路径（`python -m bilibili_vision.bilibili_pipeline extract …`）仍然有效，`.credentials/`、`out/`、`local_api_keys.py` 都兼容；新的 `vlv` CLI 只是更短的别名。

---

## 开发与贡献

- 架构与扩展指南：[`docs/architecture.md`](docs/architecture.md)（加一个新平台/新 LLM 后端/新本地模型的完整 recipe）
- 贡献流程：[`docs/contributing.md`](docs/contributing.md)（dev loop、PR checklist、release flow）
- 测试分层：

  ```bash
  pytest -m "not slow and not e2e and not gpu"   # 默认快速单元（CI）
  pytest -m slow                                  # 重型 import
  pytest -m e2e                                   # 真实网络 / 真实 LLM
  pytest -m gpu                                   # 需要 CUDA
  ```
- 多国语言：改 `src/bilibili_vision/locales/*/LC_MESSAGES/vlv.po`，然后：
  ```bash
  python scripts/compile_locales.py
  ```

---

## 本仓库不包含

体积大 / 机器相关 / 私密内容在 `.gitignore`：

| 路径 | 说明 |
|------|------|
| `python_embed/` | 嵌入式 Python（`准备便携环境.ps1`） |
| `pw-browsers/` | Playwright Chromium |
| `whisper-models/` | Whisper 权重 |
| `ffmpeg/*.exe` | 可选本地 FFmpeg |
| `models/` | 本地大模型权重 |
| `venv_gemma4/`、`venv_qwen35/`、`venv/` | 虚拟环境 |
| `out/` | 运行输出（含日志、转写、分析） |
| `.credentials/` | cookies、登录状态 |
| `local_api_keys.py`、`local_llm_prefs.json` | 本机密钥与偏好 |
| `.hf_cache/` | HuggingFace 下载缓存 |

---

## 诊断与报错

遇到问题：

1. GUI **「帮助 → 导出诊断包」**（或 `vlv diagnostics`）产出 `out/diagnostics/vlv_diagnostics_<stamp>.zip`。
2. 里面有 `environment.json`（Python / OS / GPU / ffmpeg）、最近 5 次会话日志、配置文件 SHA-256 指纹（**不含原始内容**）。
3. 把 zip 发给维护者，同时附上对应 `VLV_E***` 错误码。

详见 [`docs/architecture.md` §错误码](docs/architecture.md#4-错误码)。

---

## 安全

- 勿公开：`local_api_keys.py`、`local_llm_prefs.json`、`cookies.txt`、`browser_state.json`。
- API Key 首次存储时可设主密码开启加密；见 [`SECURITY.md`](SECURITY.md)。
- 诊断包只写 SHA-256 指纹，绝不把 key / cookie 明文入 zip。

---

## 推送到 GitHub

```powershell
gh auth login -h github.com -p https -w
powershell -ExecutionPolicy Bypass -File ".\一键推送GitHub.ps1"
```

或：`git remote add origin …` → `git push -u origin main`。勿提交密钥。

---

## 相关项目

| 项目 | 侧重点 |
|------|--------|
| [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) | 通用音视频下载；VLV 通过 yt-dlp 拉流 |
| [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 高效 Whisper 推理；VLV ASR 基于此 |
| [lanbinleo/bili2text](https://github.com/lanbinleo/bili2text) | B 站 → 文字，多引擎与多端 |
| [pluja/whishper](https://github.com/pluja/whishper) | 本地转写 Web UI |

**VLV 差异**：多平台统一 adapter、弹幕 + 字幕合并、Playwright 登录 + cookie、Windows 便携组装、合并文稿上的 LLM 报告 + 同页对话，以及 Vision / 本地 VLM 管线。

---

## 许可证

[MIT License](LICENSE)

---

## English

**VLV** (**V**ideo **L**isten **V**iew) ingests from **Bilibili, YouTube, Douyin, and any yt-dlp-supported site**, transcribes (optionally via faster-whisper), merges into a canonical text file, and runs multi-model LLM analysis + multi-turn chat on top, with an optional vision pipeline. Runs on **Windows / macOS / Linux**, UI in Chinese & English.

**Highlights (v1.0):**
- `PlatformAdapter` abstraction — drop a file in `src/bilibili_vision/platform/` to add a source.
- `TaskManager` with cancellation tokens — GUI Stop always kills the process tree.
- Structured JSONL logs per session + one-click diagnostics zip (`vlv diagnostics`).
- Stable error codes (`VLV_E***`).
- AES-GCM / HMAC-SHA256 encrypted API key store (stdlib fallback, no mandatory `cryptography`).
- gettext i18n (zh_CN + en_US shipped).
- Tag-triggered release workflow builds portable bundles for Windows / macOS / Linux.

**Quick start:**

```bash
git clone https://github.com/Jcxu97/vlv.git
cd vlv
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.lock -r requirements.txt
python -m playwright install chromium   # only needed for Bilibili login
./start.sh                              # or: python run_gui.py
```

On Windows, double-click `START.bat` after running `准备便携环境.ps1`.

**CLI:**

```bash
vlv extract <url>            # Bilibili / YouTube / Douyin / anything yt-dlp handles
vlv analyze <session_dir>
vlv diagnostics              # export support bundle
vlv gui
```

**Cloud LLMs are recommended.** Local Gemma / Qwen / Whisper are optional and require a CUDA GPU (no Mac). See `docs/architecture.md` for the full module map and how to add a new platform, and `docs/contributing.md` for the dev loop.
