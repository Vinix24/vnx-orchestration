"""spawn.py — Local Gemma e4b via MLX runtime with Ollama fallback.

Routes to mlx-lm primary; if MLX fails or unavailable, falls back to Ollama HTTP.
ADR-005: receipt is emitted by _dispatch_local_gemma in provider_dispatch.py,
which follows the same governed pattern as codex/gemini/kimi.
cost_usd = 0.0 always (local inference, no API cost).

BILLING SAFETY: subprocess only. No Anthropic SDK, no LiteLLM, no direct API calls.
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

_LIB_DIR = str(Path(__file__).resolve().parents[2])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

DEFAULT_MLX_MODEL = "mlx-community/gemma-3-4b-it-4bit"
DEFAULT_OLLAMA_MODEL = "gemma3:4b"
_DEFAULT_DEADLINE = 300
_DEFAULT_MAX_TOKENS = 2048

# Approximate chars-per-token for local token estimation (no real tokenizer available)
_CHARS_PER_TOKEN = 4


@dataclass
class LocalGemmaSpawnResult:
    """Return value from spawn_local_gemma(); carries spawn outcome to the caller."""

    returncode: int
    completion_text: str
    runtime_used: str       # "mlx" or "ollama"
    duration_seconds: float
    timed_out: bool
    error: Optional[str]
    token_usage: Optional[Dict[str, Any]]
    model_used: str

    def frontmatter_fields(self) -> Dict[str, Any]:
        usage = self.token_usage or {}
        return {
            "provider": "local-gemma",
            "sub_provider": "none",
            "exit_code": self.returncode,
            "token_usage": {
                "input": int(usage.get("input", 0) or 0),
                "output": int(usage.get("output", 0) or 0),
                "cache_read": 0,
            },
            "cost_usd": 0.0,
            "runtime": self.runtime_used,
        }


def _estimate_token_count(text: str) -> int:
    """Estimate token count via char division (no real tokenizer; local models don't report tokens)."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _run_ollama_fallback(
    prompt: str,
    model: str,
    timeout: float,
) -> tuple:
    """Run inference via Ollama CLI as fallback.

    Returns (output_text: str, success: bool, error: Optional[str]).
    """
    try:
        result = subprocess.run(
            ["ollama", "run", model, prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "", False, f"ollama timed out after {timeout}s"
    except FileNotFoundError:
        return "", False, "ollama binary not found (install: https://ollama.com)"
    except OSError as exc:
        return "", False, f"ollama launch failed: {exc}"

    if result.returncode != 0:
        err = (result.stderr or "").strip() or f"exit code {result.returncode}"
        return "", False, f"ollama failed: {err}"

    output = (result.stdout or "").strip()
    if not output:
        return "", False, "ollama returned empty output"

    return output, True, None


def spawn_local_gemma(
    *,
    instruction: str,
    model: str = DEFAULT_MLX_MODEL,
    role: Optional[str] = None,
    deadline_seconds: int = _DEFAULT_DEADLINE,
    dispatch_id: str,
    project_id: str = "vnx-dev",
) -> LocalGemmaSpawnResult:
    """Spawn local Gemma inference via MLX (primary) or Ollama (fallback).

    Primary: mlx_lm.generate (Apple Silicon, no API cost).
    Fallback: ollama run gemma3:4b (when MLX unavailable or fails).
    Returns LocalGemmaSpawnResult. Receipt emitted by caller (_dispatch_local_gemma).
    """
    from providers.local_gemma.runtime_mlx import mlx_available, run_mlx  # noqa: PLC0415

    t_start = time.monotonic()
    output = ""
    runtime_used = "mlx"
    error: Optional[str] = None
    status_ok = False

    # --- Primary: MLX ---
    if mlx_available():
        output, mlx_success, mlx_err = run_mlx(
            model,
            instruction,
            max_tokens=_DEFAULT_MAX_TOKENS,
            timeout=float(deadline_seconds),
        )
        if mlx_success:
            status_ok = True
        else:
            error = f"MLX primary failed: {mlx_err}; trying Ollama fallback"
            runtime_used = "ollama"
    else:
        error = "MLX unavailable (mlx-lm not installed or non-Apple-Silicon); using Ollama fallback"
        runtime_used = "ollama"

    # --- Fallback: Ollama ---
    if not status_ok:
        elapsed = time.monotonic() - t_start
        fallback_timeout = max(30.0, float(deadline_seconds) - elapsed)
        output, ollama_success, ollama_err = _run_ollama_fallback(
            instruction,
            DEFAULT_OLLAMA_MODEL,
            fallback_timeout,
        )
        if ollama_success:
            status_ok = True
        else:
            if error:
                error = f"{error}; Ollama fallback failed: {ollama_err}"
            else:
                error = f"Ollama fallback failed: {ollama_err}"

    duration = time.monotonic() - t_start
    returncode = 0 if status_ok else 1
    token_usage = {
        "input": _estimate_token_count(instruction),
        "output": _estimate_token_count(output) if output else 0,
    }

    # Preserve MLX failure reason as a warning even on successful Ollama fallback
    # so callers can log the runtime degradation. Only clear on clean MLX success.
    result_error = error if (not status_ok or (status_ok and runtime_used == "ollama" and error)) else None
    return LocalGemmaSpawnResult(
        returncode=returncode,
        completion_text=output,
        runtime_used=runtime_used,
        duration_seconds=round(duration, 3),
        timed_out=False,
        error=result_error,
        token_usage=token_usage,
        model_used=model,
    )
