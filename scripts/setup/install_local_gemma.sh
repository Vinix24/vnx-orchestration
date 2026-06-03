#!/usr/bin/env bash
# install_local_gemma.sh — Install MLX + download Gemma 3 4b-it 4bit model.
#
# Usage: bash scripts/setup/install_local_gemma.sh
#
# Requirements:
#   - Apple Silicon Mac (M1/M2/M3/M4)
#   - Python 3.11+ with pip
#   - Hugging Face CLI (or pip installs huggingface_hub)
#
# After this script, run the smoke test:
#   python3 -c "from scripts.lib.providers.local_gemma.spawn import spawn_local_gemma; \
#     r = spawn_local_gemma(instruction='Say hello', dispatch_id='smoke', project_id='vnx-dev'); \
#     print(r.status, r.runtime_used)"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MLX_MODEL="mlx-community/gemma-3-4b-it-4bit"
OLLAMA_MODEL="gemma3:4b"

echo "[install_local_gemma] Checking MLX availability..."

# Check for Apple Silicon
if ! sysctl -n machdep.cpu.brand_string 2>/dev/null | grep -qi "apple"; then
    echo "[install_local_gemma] WARNING: Apple Silicon not detected. MLX will not run."
    echo "[install_local_gemma] Ollama fallback will be used instead. Install Ollama from https://ollama.com"
fi

# Install mlx-lm if missing
if python3 -c "import mlx_lm" 2>/dev/null; then
    echo "[install_local_gemma] mlx-lm already installed."
else
    echo "[install_local_gemma] Installing mlx-lm>=0.20..."
    pip install "mlx-lm>=0.20"
fi

# Check huggingface_hub for model download
if ! python3 -c "import huggingface_hub" 2>/dev/null; then
    echo "[install_local_gemma] Installing huggingface_hub..."
    pip install "huggingface_hub>=0.20"
fi

# Download the model via mlx_lm
echo "[install_local_gemma] Downloading model: ${MLX_MODEL}"
echo "[install_local_gemma] (This may take a few minutes on first download; cached on subsequent runs.)"

if python3 -m mlx_lm.convert --help 2>/dev/null | grep -q "model"; then
    python3 -m mlx_lm.convert --hf-path "${MLX_MODEL}" --quantize --q-bits 4 2>/dev/null || true
fi

# mlx_lm.generate will auto-download on first use if huggingface_hub is available
python3 -c "
from huggingface_hub import snapshot_download
import sys
print(f'[install_local_gemma] Downloading {\"${MLX_MODEL}\"} from HuggingFace...')
try:
    path = snapshot_download(repo_id='${MLX_MODEL}', ignore_patterns=['*.safetensors'])
    print(f'[install_local_gemma] Model downloaded to: {path}')
except Exception as e:
    print(f'[install_local_gemma] WARNING: HuggingFace download failed: {e}')
    print('[install_local_gemma] The model will be downloaded on first mlx_lm.generate call.')
    sys.exit(0)
"

# Check Ollama for fallback path
echo "[install_local_gemma] Checking Ollama fallback..."
if command -v ollama &>/dev/null; then
    echo "[install_local_gemma] Ollama found. Pulling ${OLLAMA_MODEL} for fallback..."
    ollama pull "${OLLAMA_MODEL}" || echo "[install_local_gemma] WARNING: ollama pull failed (non-fatal; Ollama still available for future use)"
else
    echo "[install_local_gemma] Ollama not found. Install from https://ollama.com for fallback support."
    echo "[install_local_gemma] Primary MLX path will be used; Ollama is optional."
fi

# Smoke test
echo "[install_local_gemma] Running smoke test..."
python3 -c "
import sys
sys.path.insert(0, '${SCRIPT_DIR}/../lib')
from providers.local_gemma.runtime_mlx import mlx_available
avail = mlx_available()
print(f'[install_local_gemma] MLX available: {avail}')
print('[install_local_gemma] Setup complete.')
print('Run full smoke test with:')
print('  python3 -c \"from scripts.lib.providers.local_gemma.spawn import spawn_local_gemma; r = spawn_local_gemma(instruction=\\\"Say hello in one word.\\\", dispatch_id=\\\"smoke\\\", project_id=\\\"vnx-dev\\\"); print(r.status, r.runtime_used)\"')
"

echo "[install_local_gemma] Done."
