# VLV (Video Listen View) — 项目交接文档

> 版本 0.3.0 · 2026-04-15 · MIT License

---

## 一、项目概述

VLV 是一个 **B 站 / 本地视频内容智能提取与分析** 桌面工具，核心流程为：输入视频链接或本地文件 → 自动拉取字幕、弹幕、可选 Whisper 转写 → 合并文稿 → 多平台 LLM 生成总结与对话 → 可选多模态画面深度理解。项目使用 Python 3.11+，GUI 基于 tkinter，目标平台为 Windows 10/11，采用便携部署模式（嵌入式 Python + 打包依赖）。

---

## 二、仓库结构

```
bilibili-transcript-oneclick-vision/
│
├── run_gui.py                  # GUI 入口（双击 START.bat 等价）
├── src/bilibili_vision/        # 全部应用源码（20 个模块）
│
├── requirements.txt            # 核心依赖（yt-dlp, playwright, faster-whisper 等）
├── requirements-gpu.txt        # 可选 CUDA 加速
├── requirements-vision.txt     # 可选 OCR / VLM 依赖说明
├── requirements-gemma4-4bit.txt# Gemma 4 本地服务依赖
│
├── local_api_keys.example.py   # API Key 配置模板
├── .credentials/               # B 站 cookies（gitignored）
├── out/                        # 运行时输出（gitignored）
├── ffmpeg/                     # 本地 FFmpeg 可执行文件（gitignored）
│
├── *.bat / *.ps1               # Windows 便携启动与工具脚本
├── .github/workflows/ci.yml    # CI：Python 语法检查
├── docs/screenshots/           # README 配图
├── LICENSE / README.md / SECURITY.md
└── .gitignore
```

---

## 三、模块架构与数据流

### 分层架构

项目分为六层，每层职责清晰，上层调用下层：

| 层级 | 模块 | 职责 |
|------|------|------|
| **入口** | `run_gui.py`、`bilibili_pipeline.py` | GUI 启动 / CLI 子命令（`extract`、`login`） |
| **GUI** | `gui.py`（~5800 行）、`gui_common.py` | tkinter 浅色工作台界面，含三大页面（提取分析 / 对话 / API 设置），高 DPI 支持 |
| **提取** | `browser_bilibili.py` → `extract_bilibili_text.py` → `transcribe_local.py` | Playwright 登录 → yt-dlp 拉字幕弹幕视频 → faster-whisper 本地转写 |
| **分析** | `analyze_transcript.py` → `llm_analyze.py` | 合并文稿生成结构化总结；纯 urllib 多平台 LLM 调用（Gemini / OpenAI / Groq / Anthropic / xAI） |
| **Vision** | `vision_deep_pipeline.py` → `local_vlm_openai_client.py` → `serve_gemma4_4bit.py` | 抽帧去重 → OCR → VLM 看图 → 分层深度分析；`video_context_builder.py` 拼接结构化文本 |
| **工具** | `paths.py`、`output_session.py`、`ffmpeg_utils.py`、`check_local_model.py` 等 | 路径常量、输出目录管理、FFmpeg 定位、服务健康检查、模型预下载 |

### 数据流

```
B站链接 / 本地文件
    │
    ├─→ browser_bilibili（Playwright 登录，导出 cookies.txt）
    │
    ├─→ extract_bilibili_text（yt-dlp 拉字幕 + 弹幕 XML + 可选整片视频下载）
    │       │
    │       └─→ transcribe_local（无字幕时 faster-whisper ASR）
    │
    ├─→ output_session（out/日期/时间戳_标题/ 组织输出）
    │
    ├─→ analyze_transcript（合并文稿 → 结构化总结）
    │       │
    │       └─→ llm_analyze（多平台 LLM API，纯 urllib 无 SDK）
    │
    └─→ vision_deep_pipeline（可选多模态深度管线）
            │
            ├─→ FFmpeg 抽帧 → pHash 去重
            ├─→ PaddleOCR 文字识别
            ├─→ local_vlm_openai_client → serve_gemma4_4bit（本机 VLM）
            └─→ video_context_builder → 分层深度分析 → md/json/srt 输出
```

---

## 四、关键设计决策

### LLM 调用零 SDK

`llm_analyze.py` 用纯 `urllib.request` 对接五大平台 API，不引入 `openai`、`anthropic` 等重型 SDK。这是为了避免原生扩展在 Windows 便携环境（嵌入式 Python）下的兼容问题。所有 HTTP 请求、SSE 流式解析、错误重试均手写实现。

### 便携部署

项目设计为 Windows 便携包，由 `准备便携环境.ps1` 一键组装：嵌入式 Python（`python_embed/`）、Playwright Chromium（`pw-browsers/`）、Whisper 权重（`whisper-models/`）、FFmpeg（`ffmpeg/`）。这些目录全部 gitignored，不入库。

### 路径分离

`paths.py` 中 `PROJECT_ROOT` 通过向上查找标记文件（`README.md`、`requirements.txt`、`run_gui.py`）定位仓库根，确保 `out/`、`models/`、`.credentials/` 等运行时数据始终在仓库根而非包内。

### 凭据管理

API 密钥有三个来源，优先级为：系统环境变量 > `local_api_keys.py` 文件 > GUI 保存的 `local_llm_prefs.json`。B 站登录凭据存放在 `.credentials/cookies.txt` 和 `.credentials/browser_state.json`，均已 gitignored。

---

## 五、GUI 架构

GUI 使用 tkinter 构建，采用 Cursor / VS Code 风格的浅色工作台布局：

| 区域 | 说明 |
|------|------|
| **左侧导航栏** | 三个入口：内容提取与分析、对话、API 与模型；底部有设置占位 |
| **顶部状态栏** | 全局状态文字 + 本地推理服务状态 + 重新载入按钮 |
| **内容提取与分析页** | 三个子标签（运行 / 报告 / 日志）；运行页使用可折叠卡片布局（输入源、分析配置、本地推理服务、画面管线详细设置），底部固定操作栏 |
| **对话页** | 左侧会话列表 + 右侧聊天区；支持本地 OpenAI 兼容 / 云端 API 两种后端；可附图 |
| **API 与模型页** | 首选提供商选择 + 对话模型配置 + 五大平台凭据（每个平台一个可折叠卡片，带绿/灰状态指示点） |

### GUI 关键文件

`gui_common.py` 定义了所有共享常量（主题色、间距网格 `SPACING_XS/SM/MD/LG/XL`、卡片样式、状态色）、DPI 工具、字体工具，以及 `CollapsibleCard` 可折叠卡片组件。`gui.py` 是主界面实现，约 5800 行，包含 `App` 主窗口类、`LocalChatHub` 对话页、`AgentChatPanel` 报告对话面板等。

---

## 六、开发环境搭建

### 最小开发环境

```bash
# 1. 克隆仓库
git clone https://github.com/Jcxu97/vlv.git
cd vlv

# 2. 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 3. 安装核心依赖
pip install -r requirements.txt

# 4. 安装 Playwright 浏览器（B 站登录需要）
playwright install chromium

# 5. 复制 API Key 模板并填写
copy local_api_keys.example.py local_api_keys.py
# 编辑 local_api_keys.py，填入至少一个 LLM 平台的 API Key

# 6. 启动 GUI
python run_gui.py
```

### 可选：GPU 加速 Whisper

```bash
pip install -r requirements-gpu.txt
```

### 可选：本地 VLM（Gemma 4 4-bit）

```bash
pip install -r requirements-gemma4-4bit.txt
# 需要 NVIDIA GPU（建议 16GB+ VRAM）
# 启动服务：SERVE_GEMMA4_4BIT.bat 或 python -m bilibili_vision.serve_gemma4_4bit
```

---

## 七、运行时目录约定

| 路径 | 说明 | 是否入库 |
|------|------|----------|
| `out/` | 每次运行输出（`out/YYYYMMDD/HHMMSS_标题/`） | 否 |
| `.credentials/` | `cookies.txt`、`browser_state.json` | 否 |
| `local_api_keys.py` | 用户 API 密钥 | 否 |
| `local_llm_prefs.json` | GUI 保存的 LLM 偏好 | 否 |
| `local_vision_prefs.json` | GUI 保存的 Vision 偏好 | 否 |
| `whisper-models/` | faster-whisper CTranslate2 模型 | 否 |
| `models/` | Gemma 4 等本地模型权重 | 否 |
| `python_embed/` | 便携嵌入式 Python | 否 |
| `pw-browsers/` | Playwright Chromium | 否 |
| `ffmpeg/` | FFmpeg 可执行文件 | 否 |

---

## 八、CI / CD

项目使用 GitHub Actions 做基础语法检查（`.github/workflows/ci.yml`），对 `src/bilibili_vision` 执行 `python -m compileall -q`。Dependabot 配置为月度检查 GitHub Actions 依赖更新。

目前没有自动化测试套件。代码质量依赖 CI 语法检查和手动验证。

---

## 九、已知限制与待办

### 已知限制

1. **GUI 单文件体量大**：`gui.py` 约 5800 行，虽已提取 `gui_common.py`，但仍可进一步拆分（如将 `LocalChatHub`、`AgentChatPanel`、`LocalInferenceServerPanel` 等大类提取为独立模块）。
2. **无自动化测试**：项目目前没有 pytest 测试套件，所有功能验证依赖手动操作。
3. **Windows 专属**：便携部署脚本（`.bat`、`.ps1`）、DPI 处理、CUDA DLL 注册等均针对 Windows，macOS/Linux 支持有限。
4. **Gemma 4 本地服务稳定性**：4-bit 量化模型在 Windows 上偶发 `0xC0000005` Access Violation，通常与 GPU VRAM 未完全释放有关，需重启 GPU 上下文。

### 可改进方向

1. 将 `gui.py` 中的 `LocalChatHub`、`AgentChatPanel`、`LocalInferenceServerPanel` 等大类拆分为独立文件
2. 添加 pytest 测试覆盖核心提取和分析逻辑
3. 考虑 macOS/Linux 兼容（路径分隔符、DPI 处理、便携部署脚本）
4. 将 `llm_analyze.py` 中的 SSE 解析抽取为独立工具模块

---

## 十、联系方式与仓库

| 项目 | 信息 |
|------|------|
| GitHub | [github.com/Jcxu97/vlv](https://github.com/Jcxu97/vlv) |
| 协议 | MIT |
| Python | 3.11+ |
| 当前版本 | 0.3.0 |
