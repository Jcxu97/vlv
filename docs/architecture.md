# VLV architecture

This document describes the v1.0-era module layout produced by the 12-week
productisation roadmap. It is the canonical reference for adding a new
platform, wiring a new LLM backend, or integrating a new local model.

## Module map

```
src/bilibili_vision/
  log_config.py          # root-logger setup, JSONL session file, rotating handler
  errors.py              # VLV error hierarchy with stable error codes (VLV_E…)
  paths.py               # portable PROJECT_ROOT discovery
  i18n.py                # gettext wrapper, language switching
  secret_store.py        # encrypted API-key storage
  diagnostics.py         # "Help → Export diagnostics" zip builder
  cli.py                 # `vlv extract/analyze/diagnostics/gui` entry point
  gpu_watchdog.py        # CUDA preflight + crash-counter for local inference

  tasks/
    cancellation.py      # CancellationToken
    manager.py           # TaskManager — run subprocesses with cancel + timeout

  platform/
    base.py              # PlatformAdapter Protocol, VideoMetadata, capability flags
    registry.py          # URL → adapter routing
    _ytdlp.py            # shared yt-dlp JSON helpers
    bilibili.py          # Bilibili adapter
    youtube.py           # YouTube adapter
    douyin.py            # Douyin adapter
    generic_ytdlp.py     # fallback for any yt-dlp-supported site

  (existing modules — unchanged or lightly refactored)
  bilibili_pipeline.py     extract_bilibili_text.py
  browser_bilibili.py      output_session.py
  transcribe_local.py      analyze_transcript.py
  llm_analyze.py           video_context_builder.py
  serve_gemma4_4bit.py     qwen35_vision_client.py
  frame_vision_gemma.py    vision_deep_pipeline.py
  gui.py + gui_*.py        ffmpeg_utils.py + fsatomic.py
```

## Key abstractions

### 1. `PlatformAdapter` (platform/base.py)

Every video source implements this Protocol:

```python
class PlatformAdapter(Protocol):
    platform_id: str
    capabilities: PlatformCapability
    def detect(self, url: str) -> bool: ...
    def fetch_metadata(self, url, *, cookies=None) -> VideoMetadata: ...
    def fetch_subtitles(self, url, *, cookies=None, lang_preference=None) -> list[SubtitleCue]: ...
    def fetch_enrichment(self, url, *, cookies=None) -> dict: ...
```

Routing lives in `platform/registry.py`. The registry tries each registered
adapter's `detect()` in insertion order; if none match, it falls back to the
`GenericYtdlpAdapter`. Adding a platform = dropping a file in `platform/`
that implements the Protocol and registering it:

```python
# my_platform.py
class MyAdapter:
    platform_id = "myplatform"
    capabilities = PlatformCapability.SUBTITLES | PlatformCapability.COOKIES
    def detect(self, url): return "myplatform.com" in url.lower()
    # …

# On import, e.g. in platform/__init__.py:
from .my_platform import MyAdapter
register_adapter(MyAdapter())
```

### 2. `TaskManager` (tasks/manager.py)

Every long-running child process (yt-dlp, Whisper, local LLM serve) goes
through `TaskManager.run_subprocess()`. The returned `TaskHandle` exposes
`cancel()`, which fires a `CancellationToken`, which in turn kills the
process tree (`taskkill /T` on Windows, `SIGTERM` → `SIGKILL` on POSIX).

Functions that run inside the pool may optionally accept a `token` kwarg;
the manager autodetects that via `inspect.signature` and passes the token
through.

```python
mgr = TaskManager.default()
h = mgr.run_subprocess(
    ["yt-dlp", "-j", url],
    name="fetch_info",
    timeout=30.0,
    on_stdout=lambda line: ...
)
try:
    rc = h.wait(timeout=35.0)
except TaskCancelledError:
    ...
```

### 3. Logging and diagnostics

`log_config.configure_logging()` is idempotent. Every process run produces a
timestamped `out/log/<stamp>_session.jsonl`. The `diagnostics.build_diagnostic_zip()`
helper bundles the five most recent session logs, the rotating `vlv.log`,
environment info, and SHA-256 fingerprints of config files (never their
contents) into a shareable zip.

`get_logger("vlv.myfeature")` returns a configured child logger. Extra fields
passed via `logger.info(msg, extra={"platform": …, "task_id": …})` are
serialised into the JSONL payload.

### 4. Error codes

Every user-visible error inherits from `VLVError` and carries a stable `code`:

| Code         | Class                 | Meaning                          |
|--------------|-----------------------|----------------------------------|
| VLV_E101/102 | NetworkError, RateLimitError | Network failures |
| VLV_E201–203 | LLMError, LLMAuthError, LLMTimeoutError | LLM call failures |
| VLV_E301–303 | GPUError, GPUMemoryError, GPUCrashError | Local GPU issues |
| VLV_E401–403 | ExtractionError, UnsupportedURLError, LoginRequiredError | Platform issues |
| VLV_E501     | ConfigError           | Configuration / secret store |
| VLV_E601     | TaskCancelledError    | Cooperative cancellation |

GUI exception handlers read `.code` and `.user_message` and render a dialog
with a "Copy details" button.

### 5. Secret store

`SecretStore` (`secret_store.py`) encrypts cached API keys under a master
password using AES-GCM (preferred) or an HMAC-SHA256 stream envelope
(stdlib fallback). Legacy plaintext files are auto-loaded and re-encrypted
on next save.

### 6. i18n

Strings wrapped with `_( )` from `bilibili_vision.i18n` go through gettext.
Source catalogues: `src/bilibili_vision/locales/{zh_CN,en_US}/LC_MESSAGES/vlv.po`.
Run `python scripts/compile_locales.py` after editing to regenerate `.mo`
files. `set_language(code)` switches catalogues at runtime.

## How to add a new platform (example: Vimeo)

1. Create `src/bilibili_vision/platform/vimeo.py`:
   ```python
   from .base import PlatformAdapter, PlatformCapability, SubtitleCue, VideoMetadata
   from ._ytdlp import run_ytdlp_info, info_to_base_metadata

   class VimeoAdapter:
       platform_id = "vimeo"
       capabilities = PlatformCapability.COOKIES
       def detect(self, url): return "vimeo.com" in (url or "").lower()
       def fetch_metadata(self, url, *, cookies=None):
           info = run_ytdlp_info(url, cookies=cookies)
           return VideoMetadata(platform=self.platform_id, raw=info, **info_to_base_metadata(info))
       def fetch_subtitles(self, url, *, cookies=None, lang_preference=None): return []
       def fetch_enrichment(self, url, *, cookies=None): return {}
   ```
2. Register it in `platform/__init__.py` and `platform/registry._ensure_defaults`.
3. Add a unit test under `tests/test_platform_registry.py`.
4. Run `python -m pytest` — CI will run the matrix on Windows / macOS / Linux.

## Release flow

Pushing a tag `vX.Y.Z` triggers `.github/workflows/release.yml`:
1. `build-sdist` — produces sdist + wheel.
2. `build-portable` — stages a portable bundle on each OS and archives it.
3. `publish` — creates the GitHub release and uploads every artifact.

`setuptools_scm` writes `src/bilibili_vision/_version.py` from the tag.

## Test tiers

- `pytest -m "not slow and not e2e and not gpu"` — fast unit suite, runs in CI.
- `pytest -m slow` — imports heavy deps (yt-dlp extractor set, torch).
- `pytest -m e2e` — real network / real LLM calls; human-run before release.
- `pytest -m gpu` — requires CUDA; skipped on CPU-only runners.

See `pytest.ini` for the marker definitions.
