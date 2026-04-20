#!/usr/bin/env bash
# Linux counterpart of SERVE_GEMMA4_4BIT.bat. Requires a CUDA GPU.
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"

venv="$here/venv_gemma4"
python="$venv/bin/python"

if [ ! -x "$python" ]; then
    echo "Creating $venv..."
    python3 -m venv "$venv"
    "$python" -m pip install --upgrade pip
    echo "Install CUDA PyTorch first, e.g.:"
    echo "  $python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121"
    "$python" -m pip install -r "$here/requirements-gemma4-4bit.txt"
fi

model_dir="$here/models/Gemma-4-31B-it-abliterated"
if [ ! -f "$model_dir/config.json" ]; then
    echo "ERROR: Model not found at $model_dir" >&2
    exit 1
fi

echo "Starting server on http://127.0.0.1:18090"
export PYTHONPATH="$here/src"
exec "$python" -u -m bilibili_vision.serve_gemma4_4bit \
    --model "$model_dir" \
    --host 127.0.0.1 --port 18090 \
    --listen-model-id gemma-4-31b-4bit \
    --max-model-len 8192 \
    --default-temperature 0 \
    --default-top-p 0.82 \
    --repetition-penalty 1.22 \
    --no-repeat-ngram 6
