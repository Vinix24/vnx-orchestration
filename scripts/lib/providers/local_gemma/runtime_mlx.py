"""runtime_mlx.py — MLX-specific inference runner for local Gemma models.

Wraps mlx_lm.generate as a subprocess call (Apple Silicon optimized).

BILLING SAFETY: subprocess only. No Anthropic SDK, no direct API calls.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS = 2048
_DEFAULT_TEMP = 0.7


def run_mlx(
    model: str,
    prompt: str,
    *,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temp: float = _DEFAULT_TEMP,
    timeout: float = 120.0,
) -> tuple:
    """Run inference via mlx_lm.generate subprocess.

    Returns (output_text: str, success: bool, error: Optional[str]).
    success=True iff exit code 0 and non-empty output.
    Raises nothing — all failures surface as (empty, False, error_str).
    """
    cmd = [
        sys.executable, "-m", "mlx_lm.generate",
        "--model", model,
        "--prompt", prompt,
        "--max-tokens", str(max_tokens),
        "--temp", str(temp),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "", False, f"mlx_lm.generate timed out after {timeout}s"
    except FileNotFoundError as exc:
        return "", False, f"mlx_lm not found (install: pip install mlx-lm): {exc}"
    except OSError as exc:
        return "", False, f"mlx_lm.generate OS error: {exc}"

    if result.returncode != 0:
        err = (result.stderr or "").strip() or f"exit code {result.returncode}"
        return "", False, f"mlx_lm.generate failed: {err}"

    output = (result.stdout or "").strip()
    if not output:
        return "", False, "mlx_lm.generate returned empty output"

    return output, True, None


def mlx_available() -> bool:
    """Return True if mlx_lm is importable (Apple Silicon + mlx-lm installed)."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import mlx_lm"],
            capture_output=True,
            timeout=10.0,
        )
        return result.returncode == 0
    except Exception as e:  # noqa: BLE001
        logger.debug("mlx not available: %s: %s", type(e).__name__, e)
        return False
