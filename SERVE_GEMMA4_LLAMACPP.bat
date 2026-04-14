@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

REM =============================================================================
REM Gemma 4 31B · Q5_K_XL（GGUF）+ llama.cpp llama-server，5090 CUDA 全层卸载示例
REM
REM 说明：
REM   1) models\Gemma-4-31B-it-abliterated 里是 HuggingFace 权重，不是 Q5_K_XL。
REM      Q5_K_XL 必须是单独的 .gguf 文件（从 HF 上搜 “Gemma-4” + “Q5_K_XL” 或自行转换）。
REM   2) 需自行安装/解压带 CUDA 的 llama.cpp，并把 llama-server.exe 加入 PATH 或设 LLAMA_SERVER。
REM   3) 启动本脚本后，在 GUI「API 与模型」：
REM        首选提供商：OpenAI
REM        OpenAI API Key：任意非空（如 local）
REM        API 根 URL：http://127.0.0.1:%GEMMA_PORT%/v1
REM        模型名：与下方 GEMMA_OPENAI_MODEL 一致（或与你客户端里填的相同）
REM      对话若走「本地 OpenAI 兼容」，同上 URL + 模型名即可。
REM =============================================================================

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

REM --- 修改：你的 Q5_K_XL.gguf 实际路径 ---
if not defined GEMMA_GGUF set "GEMMA_GGUF=%ROOT%\models\gemma-4-31b-it-abliterated.Q5_K_XL.gguf"

REM llama-server.exe：若在 PATH 里可直接 "llama-server"；否则写完整路径
if not defined LLAMA_SERVER set "LLAMA_SERVER=llama-server"

REM 与 GUI / OPENAI_BASE_URL 端口一致
if not defined GEMMA_PORT set "GEMMA_PORT=8088"

REM 客户端传给 /v1/chat/completions 的 model 字段（单模型服务时一般任意固定字符串即可）
if not defined GEMMA_OPENAI_MODEL set "GEMMA_OPENAI_MODEL=gemma-4-31b-it-q5_k_xl"

REM 上下文长度：显存够可加大（如 32768）
if not defined GEMMA_CTX set "GEMMA_CTX=16384"

REM GPU 层数：99 表示尽量多放 GPU（llama.cpp 会截断到实际层数）
if not defined GEMMA_NGL set "GEMMA_NGL=99"

if not exist "%GEMMA_GGUF%" (
  echo [错误] 找不到 GGUF 文件：
  echo   %GEMMA_GGUF%
  echo.
  echo 请将 Q5_K_XL 量化的 .gguf 放到上述路径，或设置环境变量 GEMMA_GGUF=完整路径
  echo 当前 HF 目录不含 Q5_K_XL；需单独下载或转换 GGUF。
  exit /b 1
)

echo.
echo 使用 GGUF : %GEMMA_GGUF%
echo 监听      : http://127.0.0.1:%GEMMA_PORT%/v1
echo 模型 ID   : %GEMMA_OPENAI_MODEL%
echo.

REM --jinja 按你使用的 llama.cpp 版本支持情况开启（Gemma 聊天模板）
REM -a / --alias：OpenAI API 里使用的 model id（与 GUI 里 OPENAI_MODEL 一致）
"%LLAMA_SERVER%" ^
  -m "%GEMMA_GGUF%" ^
  --host 127.0.0.1 ^
  --port %GEMMA_PORT% ^
  -c %GEMMA_CTX% ^
  -ngl %GEMMA_NGL% ^
  -a "%GEMMA_OPENAI_MODEL%" ^
  --no-mmap

endlocal
