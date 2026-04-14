# 复制本文件为 local_api_keys.py，填入 Key（local_api_keys.py 已被 .gitignore）
# 也可在 GUI「API 与模型」中填写并保存。
# 首选大模型平台（GUI 下拉框会写入；也可手改）
# LLM_PROVIDER = "auto"   # auto | gemini | openai | groq | anthropic | xai

GEMINI_API_KEY = ""
OPENAI_API_KEY = ""
GROQ_API_KEY = ""
ANTHROPIC_API_KEY = ""
XAI_API_KEY = ""

# 可选；留空则用程序默认
# GEMINI_MODEL = "gemini-2.5-flash"
# OPENAI_MODEL = "gpt-4o-mini"
# OPENAI_BASE_URL = "https://api.openai.com/v1"
# GROQ_MODEL = "llama-3.3-70b-versatile"
# ANTHROPIC_MODEL = "claude-3-5-haiku-20241022"
# XAI_MODEL = "grok-2-latest"

# --- 本地 Gemma 4（llama.cpp llama-server + Q5_K_XL.gguf，见 SERVE_GEMMA4_LLAMACPP.bat）---
# LLM_PROVIDER = "openai"
# OPENAI_API_KEY = "local"
# OPENAI_BASE_URL = "http://127.0.0.1:8088/v1"
# OPENAI_MODEL = "gemma-4-31b-it-q5_k_xl"
# 可选：首包很慢时 export LLM_HTTP_TIMEOUT_SEC=1200

# --- Gemma 4 31B 本地 4-bit（serve_gemma4_4bit.py / SERVE_GEMMA4_4BIT.bat，默认端口 8090）---
# LLM_PROVIDER = "openai"
# OPENAI_API_KEY = "local"
# OPENAI_BASE_URL = "http://127.0.0.1:8090/v1"
# OPENAI_MODEL = "gemma-4-31b-4bit"
