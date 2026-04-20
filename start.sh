#!/usr/bin/env bash
# VLV launcher — macOS / Linux counterpart of START.bat.
# Picks the first working Python in: ./venv/bin, ./python_embed/bin, $PATH.
set -u

here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"

for candidate in \
    "$here/venv/bin/python" \
    "$here/python_embed/bin/python" \
    "$here/python_embed/bin/python3" \
    python3 \
    python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        exec "$candidate" -u "$here/run_gui.py" "$@"
    fi
done

echo "ERROR: no Python interpreter found. Run prepare script or install Python 3.11+." >&2
exit 1
