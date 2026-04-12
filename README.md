<div align="center">

# Bilibili Transcript One-Click · Vision

**本地音视频与哔哩哔哩链接** → 字幕 / 弹幕 / 可选 **faster-whisper** 转写 → 合并文稿 → 可选 **多模型 LLM** 分析与对话  

*本目录为 [bilibili-transcript-oneclick](https://github.com/Jcxu97/bilibili-transcript-oneclick) 的**完整副本**（含便携目录中的 embed / 模型等本地文件）；在此之上规划 **画面文字 OCR、幻灯片/录屏关键帧、多模态视频理解（VLM）** 与口播稿合并。*

*Windows 便携向：嵌入式 Python + Playwright 登录，双击 `START.bat` 即用*

[![Upstream CI](https://github.com/Jcxu97/bilibili-transcript-oneclick/actions/workflows/ci.yml/badge.svg)](https://github.com/Jcxu97/bilibili-transcript-oneclick/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)

[**界面预览**](#screenshots) · [**快速开始**](#quickstart) · [**API 配置**](#api-keys) · [**相关项目**](#related)

</div>

---

## 亮点

| | |
|:---|:---|
| **一体化** | B 站拉流、登录态、字幕与弹幕合并、无字幕时 ASR、分析报告与**底部多轮对话**在同套 GUI 内完成 |
| **多模型** | 支持 **Gemini / OpenAI / Groq / Anthropic / xAI** 等，纯 `urllib` 实现分析，无额外 LLM SDK 依赖 |
| **便携** | 脚本引导下载 embed Python、Chromium、Whisper 模型；大文件不入 Git，见下表 |
| **本地优先** | **faster-whisper** + **ctranslate2**；可选 `requirements-gpu.txt` 安装 CUDA 12 运行库以稳定用 GPU |
| **Vision（规划中）** | 抽帧 + **PaddleOCR**（中英界面/硬字幕）+ 可选 **Qwen-VL** 等做幻灯片/操作演示语义；详见 [`requirements-vision.txt`](requirements-vision.txt) 与下节 |

---

## 目录

- [画面 / Vision（规划）](#vision)
- [功能概览](#features)
- [界面预览](#screenshots)
- [环境要求](#requirements)
- [快速开始](#quickstart)
- [本仓库不包含](#not-in-repo)
- [API / 大模型（可选）](#api-keys)
- [命令行](#cli)
- [GPU（Windows）](#gpu)
- [推送到 GitHub（维护者）](#github-push)
- [相关项目](#related)
- [安全](#security)
- [许可证](#license)
- [English](#english)

---

<a id="vision"></a>

## 画面 / Vision（规划）

目标：在**不丢原有音频转写链路**的前提下，为「任意类型视频」增加**画面侧**信息。

### 本地 **Qwen3.5-27B** 多模态（已提供脚本）

> **显存说明**：全精度 **Qwen/Qwen3.5-27B（BF16）** 单卡 32GB 级显存通常不够；默认使用官方 **Qwen/Qwen3.5-27B-GPTQ-Int4**（4-bit，单卡 5090 级可跑）。若你有多卡或更大显存，可在启动前 `set QWEN35_MODEL=Qwen/Qwen3.5-27B` 并自行保证 `transformers serve` / vLLM 能加载。

1. **安装独立环境**（勿装进 `python_embed`，避免与 faster-whisper 冲突）：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
.\install_qwen35_venv.ps1
```

2. **启动服务**（首次会自动从 Hugging Face 拉取权重，约 20GB+；可设代理或镜像）：

```bat
SERVE_QWEN35.bat
```

   - 接口：`http://127.0.0.1:8000/v1`（OpenAI 兼容 `chat/completions`）
   - **transformers 5.x**：模型名为 `serve` 的**位置参数**（脚本已写为 `serve "%QWEN35_MODEL%" ...`）
   - **RTX 5090**：`install_qwen35_venv.ps1` 使用 **PyTorch cu128**；勿再用 cu124 轮子
   - 可选：`set HF_HOME=某盘:\hf_cache` 指定缓存目录

3. **联调画面**（另一终端；需 `ffmpeg/` 下有 `ffmpeg.exe` 以便从视频抽帧）：

```bat
TEST_QWEN35_VISION.bat "docs\screenshots\gui-extract.png"
```

   或直接：

```bat
venv_qwen35\Scripts\python.exe qwen35_vision_client.py --image 某图.jpg --prompt "描述画面文字与界面"
venv_qwen35\Scripts\python.exe qwen35_vision_client.py --video 某视频.mp4 --at 3 --prompt "这一帧里有什么？"
```

| 能力 | 思路 | 硬件参考 |
|------|------|----------|
| 幻灯片 / UI 文字（精确 OCR） | 抽帧去重 + **PaddleOCR**（见 `requirements-vision.txt`） | 5090 + 64G RAM |
| 操作演示 / 语义概括 | 关键帧 + **Qwen3.5**（上表脚本） | 默认 GPTQ-Int4 单卡 |
| 烧录字幕条 | 裁条 + OCR | 同上 |

GUI 内一键入口将在后续版本接入；当前以 **服务 + 客户端脚本** 先跑通。

---

<a id="features"></a>

## 功能概览

- **图形界面**：`START.bat` / `启动.bat` → `gui.py`
- **命令行**：`bilibili_pipeline.py extract <URL | 本地媒体路径>`
- **B 站登录**：Playwright Chromium，会话 `browser_state.json`，并导出 `cookies.txt` 供 yt-dlp
- **无字幕转写**：yt-dlp 拉音频 + faster-whisper；GUI 可选 **large-v3** / **small**
- **FFmpeg**：优先使用目录 `ffmpeg/` 下可执行文件，其次系统 `PATH`，再次依赖包内 static-ffmpeg（可能访问外网）

---

<a id="screenshots"></a>

## 界面预览

便携环境就绪后启动 GUI 的主要页面：

<table>
<tr>
<td width="33%" align="center"><b>提取与日志</b><br/>链接 / 本地文件、Whisper、运行日志</td>
<td width="33%" align="center"><b>API 与模型</b><br/>多平台 Key、首选提供商</td>
<td width="33%" align="center"><b>分析报告 + 对话</b><br/>结构化报告与多轮问答</td>
</tr>
<tr>
<td valign="top"><img src="docs/screenshots/gui-extract.png" alt="提取与日志" width="100%"/></td>
<td valign="top"><img src="docs/screenshots/gui-api-models.png" alt="API 与模型" width="100%"/></td>
<td valign="top"><img src="docs/screenshots/gui-analysis-chat.png" alt="分析报告与对话" width="100%"/></td>
</tr>
</table>

<details>
<summary>若表格内图片未显示，可点此查看与正文相同的 Markdown 图片链接</summary>

![提取与日志界面](docs/screenshots/gui-extract.png)

![API 与模型界面](docs/screenshots/gui-api-models.png)

![分析报告与对话界面](docs/screenshots/gui-analysis-chat.png)

</details>

---

<a id="requirements"></a>

## 环境要求

- **Windows 10 / 11 x64**（当前脚本与说明以此为主）
- 首次准备环境时需能访问 **python.org、pip、Playwright CDN、Hugging Face**（按需）

---

<a id="quickstart"></a>

## 快速开始

1. 克隆本仓库。
2. 在仓库根目录打开 **PowerShell**，执行（需联网）：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
.\准备便携环境.ps1
```

3. 若需 **GPU 转写**，可额外执行：  
   `.\python_embed\python.exe -m pip install -r requirements-gpu.txt`
4. 双击 **`START.bat`** 或 **`启动.bat`** 打开 GUI。首次使用 B 站功能时按提示在 Chromium 中登录。

**公司内网 / 无外网（仅本机音视频转写）**：把整个便携目录拷过去（须含 `python_embed/`、你要用的 **`whisper-models/<模型名>/`**、`ffmpeg/` 下的 `ffmpeg.exe` 与 `ffprobe.exe`）。在公司电脑双击 **`START_OFFLINE.bat`**（会设置 `BILIBILI_OFFLINE` / `HF_HUB_OFFLINE`，避免误连外网下载模型或 static-ffmpeg）。Whisper 下拉框会**默认选中 `whisper-models/` 里已存在**的模型（例如压缩包只带了 `small` 时默认就是 `small`）。请用「浏览」选本地 mp3/mp4 等路径；**B 站链接**仍依赖外网拉流，离线环境无法使用。

**跳过 Whisper 预下载**（加快首次脚本时间）：

```powershell
.\准备便携环境.ps1 -SkipWhisperModel
```

**仅下载 Whisper 模型**（默认 large-v3 + small）：

```powershell
.\DOWNLOAD_WHISPER.bat
# 或
.\python_embed\python.exe .\download_whisper_models.py
```

**补充 tkinter**（embed 默认无 GUI 库）：`.\INSTALL_TKINTER.bat` 或 `.\add_tkinter_to_embed.ps1`。

**Chromium 下载失败**：多试 `install_chromium.bat`，或按 [Playwright 文档](https://playwright.dev/docs/browsers) 将浏览器放入 `pw-browsers`。

---

<a id="not-in-repo"></a>

## 本仓库不包含

以下路径因体积或环境差异 **不在 Git 中**（见 `.gitignore`），由 `准备便携环境.ps1` 或你本机自行准备：

| 路径 | 说明 |
|------|------|
| `python_embed/` | 官方 Embeddable Python + pip 依赖 |
| `pw-browsers/` | Playwright Chromium |
| `whisper-models/` | 可选；缺失时首次 ASR 从 Hugging Face 拉取 |
| `ffmpeg/*.exe` | 可选；放入 `ffmpeg.exe` / `ffprobe.exe` 可减少对外网依赖 |

---

<a id="api-keys"></a>

## API / 大模型（可选）

在 GUI **「API 与模型」** 中填写 **Gemini / OpenAI / Groq / Anthropic / xAI** 的 Key 与模型 ID，选择「首选提供商」或「自动」。

也可复制 `local_api_keys.example.py` 为 `local_api_keys.py`（**勿提交**），或设置环境变量。逻辑见 `llm_analyze.py`。

---

<a id="cli"></a>

## 命令行

```text
python_embed\python.exe bilibili_pipeline.py extract "https://www.bilibili.com/video/BVxxxx"
python_embed\python.exe bilibili_pipeline.py extract --asr-if-no-subs --whisper-model small "https://..."
```

---

<a id="gpu"></a>

## GPU（Windows）

无字幕转写使用 **faster-whisper / ctranslate2**。若缺 `cublas64_12.dll` 等，可安装与 wheel 匹配的 **CUDA 12** 运行库，或在 embed 环境中执行：

```powershell
.\python_embed\python.exe -m pip install -r requirements-gpu.txt
```

详见仓库内 `requirements-gpu.txt`。仍需可用的 **NVIDIA 驱动**。

---

<a id="github-push"></a>

## 推送到 GitHub（维护者）

本仓库含 **GitHub Actions**（`.github/workflows/ci.yml`）与 **`SECURITY.md`**。

**推荐**：安装 Git 与 [GitHub CLI](https://cli.github.com/) 后执行：

```powershell
powershell -ExecutionPolicy Bypass -File ".\一键推送GitHub.ps1"
```

**手动**：网页新建空仓库后 `git remote add origin …` 与 `git push -u origin main`。勿提交 `local_api_keys.py`、`cookies.txt` 等（已在 `.gitignore`）。

---

<a id="related"></a>

## 相关项目

下列开源项目与「B 站 / 字幕 / Whisper」相关，**侧重不同**，可与本仓库对照选用：

| 项目 | 侧重点 | 说明 |
|------|--------|------|
| [lanbinleo/bili2text](https://github.com/lanbinleo/bili2text) | B 站 → 文字 | 下载、分段、多引擎转写（Whisper / SenseVoice / 云 API），有 CLI / Web / 桌面 |
| [LuShan123888/Bilibili-Captions](https://github.com/LuShan123888/Bilibili-Captions)（`video-captions`） | 多平台字幕 | B 站 / YouTube / 本地；API 字幕优先，无字幕后 ASR；含 MCP |
| [pluja/whishper](https://github.com/pluja/whishper) | 本地转写 Web UI | faster-whisper + 字幕编辑；URL 依赖 yt-dlp，非 B 站专用 |
| [ShadyLeaf/Bili2Text](https://github.com/ShadyLeaf/Bili2Text) | 轻量示例 | Whisper 转 B 站视频，小体量脚本向 |
| [Frewen/BiliSpeech2Text](https://github.com/Frewen/BiliSpeech2Text) | 流水线脚本 | 下载、切分、Whisper，支持合集等 |

**本仓库差异（简要）**：在常见「B 站转文字」之外，强调 **弹幕与字幕合并**、**Playwright 登录与 cookie**、**Windows embed 便携组装**，以及 **合并文稿上的 LLM 分析报告 + 同页多轮对话**。

---

<a id="security"></a>

## 安全

勿公开分享含密钥的文件（如 `local_api_keys.py`、`local_llm_prefs.json`、`cookies.txt`、`browser_state.json`）。说明见 [`SECURITY.md`](SECURITY.md)。

---

<a id="license"></a>

## 许可证

[MIT License](LICENSE)

---

<a id="english"></a>

## English

**Bilibili Transcript One-Click · Vision** is a **full copy** of the upstream Windows toolkit (same audio / Bilibili / Whisper / LLM flow), plus a **planned** track for on-screen text (OCR) and multimodal video understanding. Upstream: [Jcxu97/bilibili-transcript-oneclick](https://github.com/Jcxu97/bilibili-transcript-oneclick).

Large binaries (`python_embed`, `pw-browsers`, Whisper weights, optional `ffmpeg` exes) are **not** in Git; run `准备便携环境.ps1` to bootstrap. Use `START.bat` to launch the UI. For **air-gapped** local-file transcription only, copy a full portable tree (embed + `whisper-models/<model>` + `ffmpeg/` exes) and run **`START_OFFLINE.bat`**; the UI defaults to whichever bundled model folder exists (e.g. `small` if `large-v3` was not copied). Bilibili URLs still need network access.

Optional vision-stack hints: see **`requirements-vision.txt`** (PaddleOCR, VLM deps are not installed by default).

**Related projects**: see the [table above](#related) — tools like [bili2text](https://github.com/lanbinleo/bili2text) focus on transcription pipelines; this repo adds merged danmaku/subtitle workflow, portable embed layout, and integrated LLM report + chat.
