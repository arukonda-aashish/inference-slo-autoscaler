#!/usr/bin/env bash
# One-time setup on a fresh pod. Run from the repo root on the pod. ~10 min.
set -euo pipefail

echo "--- GPU"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv

echo "--- system packages (prometheus, rsync)"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq prometheus rsync > /dev/null

echo "--- uv"
command -v uv > /dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

echo "--- project, then vLLM into the same venv (not in pyproject: the Mac doesn't need it)"
uv sync --frozen
uv pip install vllm

echo "--- versions (record these in MANIFEST)"
.venv/bin/python -c "import vllm, torch; print('vllm', vllm.__version__, '| torch', torch.__version__, '| cuda', torch.cuda.is_available())"
prometheus --version 2>&1 | head -1

echo "--- model weights, so cold starts measure loading, not downloading"
.venv/bin/python -c "from huggingface_hub import snapshot_download; print(snapshot_download('Qwen/Qwen2.5-0.5B-Instruct'))"
echo "bootstrap done"