"""
一键检查本地 OpenAI 兼容服务（默认 Gemma 18090）：端口 → /v1/models → 最小 chat/completions。
可选尝试 nvidia-smi。用法：

  venv_gemma4\\Scripts\\python.exe check_local_model.py
  venv_gemma4\\Scripts\\python.exe check_local_model.py --base http://127.0.0.1:11434/v1 --model llama3.2
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse


def _get(url: str, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "check_local_model/1"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, body
    except urllib.error.URLError as e:
        return -1, str(e.reason or e)


def _post_json(url: str, payload: dict, timeout: float) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "check_local_model/1",
            "Authorization": "Bearer EMPTY",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, body
    except urllib.error.URLError as e:
        return -1, str(e.reason or e)


def _tcp_listen(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2.0):
            return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="检查本地 OpenAI 兼容 HTTP 服务")
    ap.add_argument(
        "--base",
        default="http://127.0.0.1:18090/v1",
        help="根 URL，须含 /v1",
    )
    ap.add_argument("--model", default="gemma-4-31b-4bit", help="chat/completions 请求的 model")
    ap.add_argument("--timeout-models", type=float, default=15.0)
    ap.add_argument("--timeout-chat", type=float, default=120.0)
    args = ap.parse_args()

    base = args.base.rstrip("/")
    if not base.endswith("/v1"):
        print("警告：base 通常应以 /v1 结尾。", file=sys.stderr)
    pu = urlparse(base if "://" in base else "http://" + base.lstrip("/"))
    host = pu.hostname or "127.0.0.1"
    port = pu.port or (443 if pu.scheme == "https" else 80)

    print(f"① TCP {host}:{port} … ", end="", flush=True)
    if _tcp_listen(host, port):
        print("可连接")
    else:
        print("失败（无进程监听或防火墙拦截）")
        print("  → 若尚未启动：先运行 SERVE_GEMMA4_4BIT.bat 或 GUI「启动」")
        print("  → 若已启动仍失败：netstat -ano | findstr :" + str(port))
        return 1

    models_url = base + "/models"
    print(f"② GET {models_url} … ", end="", flush=True)
    code, body = _get(models_url, args.timeout_models)
    if code == 200:
        print(f"HTTP {code} OK")
        try:
            j = json.loads(body)
            ids = [
                str(x.get("id", ""))
                for x in (j.get("data") or [])
                if isinstance(x, dict)
            ]
            if ids:
                print("   服务端声明的 model id:", ", ".join(ids))
                if args.model and args.model not in ids:
                    print(
                        f"   ⚠ 你指定的 --model {args.model!r} 不在列表中，"
                        "请求可能被 400 拒绝；请与 --listen-model-id 一致。",
                    )
        except json.JSONDecodeError:
            print("（响应非 JSON）", body[:400])
    else:
        print(f"HTTP {code}")
        print(body[:1200] if body else "(empty)")
        if code == 404:
            print("  → 常见：base URL 少了 /v1 或端口上不是本服务")
        return 1

    chat_url = base + "/chat/completions"
    print(f"③ POST {chat_url}（max_tokens=8）… ", end="", flush=True)
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    c2, b2 = _post_json(chat_url, payload, args.timeout_chat)
    if c2 == 200:
        print(f"HTTP {c2} OK")
        try:
            j2 = json.loads(b2)
            msg = j2["choices"][0]["message"]["content"]
            print("   回复节选:", (msg or "")[:200])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            print("   原始:", b2[:500])
    else:
        print(f"HTTP {c2}")
        print(b2[:1500] if b2 else "(empty)")
        return 1

    print("④ nvidia-smi（可选）… ", end="", flush=True)
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if r.returncode == 0 and r.stdout.strip():
            print("OK")
            for ln in r.stdout.strip().splitlines()[:4]:
                print("  ", ln)
        else:
            print("跳过或失败（未装驱动或非 NVIDIA）")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        print("不可用")

    print("\n全部通过：本机 API 与最小推理正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
