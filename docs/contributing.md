# Contributing to VLV

Short, friendly guide. VLV is maintained as a personal + small-circle tool;
contributions that keep the portable philosophy (zero-SDK, stdlib-first where
possible, Windows + macOS + Linux) are most welcome.

## Dev loop

```bash
git clone <repo>
cd bilibili-transcript-oneclick-vision
python -m pip install -r requirements.lock -r requirements.txt
python -m pip install -e .[dev]

# Fast unit suite (no network, no GPU, no heavy imports):
python -m pytest -m "not slow and not e2e and not gpu"

# Launch the GUI:
python run_gui.py
# or
python -m bilibili_vision.cli gui
```

## Code conventions

- **stdlib-first**. Before pulling a new dependency, check whether `urllib`,
  `subprocess`, `hashlib`, `json`, or `concurrent.futures` already does the
  job. The cloud-LLM dispatcher in `llm_analyze.py` is the reference: zero
  SDKs, `urllib.request` only.
- **No circular imports via the GUI**. Any module importable from tests must
  not transitively import `tkinter`, `gui.py`, or any `gui_*.py`.
- **Platform adapters must not block the event loop**. They are called from
  background threads via `TaskManager`; a tight `subprocess.run` is fine, a
  GUI `messagebox` is not.
- **Errors go through `VLVError` subclasses** so the GUI can render a stable
  error code and the "Copy details" button has useful content.
- **No `print` in `src/bilibili_vision/`**. Use `get_logger("vlv.<area>")`.
  (Legacy modules that still use `print` are migrating incrementally.)

## Adding a platform adapter

Follow the recipe in `docs/architecture.md` §"How to add a new platform".
TL;DR:

1. Drop a new file under `src/bilibili_vision/platform/`.
2. Implement the `PlatformAdapter` Protocol.
3. Register it in `platform/__init__.py` and `platform/registry.py`.
4. Add a test in `tests/test_platform_registry.py`.

## Adding a string to translate

```python
from bilibili_vision.i18n import gettext as _

label = _("Extract")
```

Then edit both `locales/zh_CN/LC_MESSAGES/vlv.po` and
`locales/en_US/LC_MESSAGES/vlv.po` to add the new entry, and run:

```bash
python scripts/compile_locales.py
```

## Pull request checklist

- [ ] `pytest -m "not slow and not e2e and not gpu"` green on your machine.
- [ ] If you touched a platform adapter, ran a real extract against a
      public video on that platform.
- [ ] If you touched the GUI, launched `python run_gui.py` and confirmed
      no regressions in the smoke path (URL → Extract → Analyze).
- [ ] If you added new user-visible strings, updated both `.po` catalogues
      and re-ran `scripts/compile_locales.py`.
- [ ] Commit message explains **why**, not **what**.

## Release process

1. Bump your git working tree to a clean state.
2. `git tag vX.Y.Z && git push --tags`.
3. `.github/workflows/release.yml` builds the sdist/wheel and three
   portable bundles (Windows zip, macOS tar.gz, Linux tar.gz) and drafts
   a GitHub release.
4. Edit the release notes and publish.

## Known limitations (v1.0)

- Mac has no CUDA path — local Gemma / Qwen unavailable. Cloud LLMs only.
- Douyin adapter depends on yt-dlp's extractor; anti-bot pushes sometimes
  break it until yt-dlp releases a fix.
- The GUI split from `gui.py` into `pages/*.py` is in progress; some
  screens still live in the monolith file.
