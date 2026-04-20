#!/usr/bin/env bash
# Download faster-whisper model files under ./models/whisper/.
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"

for candidate in "$here/venv/bin/python" "$here/python_embed/bin/python" python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        export PYTHONPATH="$here/src"
        exec "$candidate" -u -m bilibili_vision.download_whisper_models "$@"
    fi
done

echo "ERROR: no Python interpreter found." >&2
exit 1
