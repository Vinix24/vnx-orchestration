"""failure_classification.py — classify dispatch failures so a receipt carries
distinguishable failure_reason and failure_class fields instead of a sentinel
"(no error captured)" log line.

OI-866 (blocker): the fabric currently writes no error/error_detail/failure_reason
field into the receipt when a dispatch fails. A 401 auth rejection, an empty
completion, a model error, and a timeout all produce the same silent receipt.
This module gives every failure path a structured classification so an operator
can tell them apart without tailing log files.

Categories
----------
auth_rejected    — HTTP 401/403 from the provider endpoint (proxy, API, etc.)
empty_completion — call succeeded (HTTP 200) but the model returned zero text
timeout          — call exceeded its deadline
model_error      — the model/provider itself returned an error (5xx, rate-limit, etc.)
unknown          — everything else; should be rare after OI-866
"""

from __future__ import annotations

from typing import Any, Dict, Optional


FAILURE_CLASSES = frozenset({
    "auth_rejected",
    "empty_completion",
    "timeout",
    "model_error",
    "unknown",
})


def classify_failure(
    *,
    status: str,
    error: Optional[str] = None,
    completion_text: Optional[str] = None,
    timed_out: bool = False,
    provider: str = "",
    sub_provider: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    returncode: Optional[int] = None,
) -> Dict[str, Optional[str]]:
    """Classify a dispatch failure into a structured {failure_class, failure_reason} dict.

    Callers that already know certain facts (e.g. a spawn function caught a
    BrokenPipeError) should pass them in ``error``.  The function gives priority
    to explicit signals (timeout flag, auth keywords) before falling back to
    pattern matching on the raw error string.

    Returns a dict with two keys; both are ``None`` when the status is
    "success" (callers should skip classification in that case).  A status of
    "timeout" produces ``failure_class="timeout"`` regardless of other signals
    so the receipt is unambiguous.
    """
    if status == "success":
        return {"failure_class": None, "failure_reason": None}

    # ── timeout ──────────────────────────────────────────────────────────
    if status == "timeout" or timed_out:
        reason = _build_reason("deadline exceeded", duration_seconds, provider, sub_provider)
        return {"failure_class": "timeout", "failure_reason": reason}

    # ── empty completion (call succeeded, no text) ───────────────────────
    completion = (completion_text or "").strip()
    if not completion and error is None:
        # _fail_loud_on_empty_success already downgrades a blank success to
        # failure with an error message.  If we reach here without an error,
        # it means the spawn itself reported success with no text and no
        # guard caught it — classify it explicitly so it is never silent.
        return {
            "failure_class": "empty_completion",
            "failure_reason": (
                f"provider={provider} returned an empty completion with "
                f"returncode={returncode} (no error captured by spawn)"
            ),
        }

    error_lower = (error or "").lower()

    # ── auth rejection ───────────────────────────────────────────────────
    auth_keywords = (
        "authentication", "auth",
        "credentials", "credential",
        "apikey", "api key", "api_key",
        "unauthorized", "forbidden",
        "401", "403",
    )
    if any(kw in error_lower for kw in auth_keywords):
        source = _error_source(provider, sub_provider)
        return {
            "failure_class": "auth_rejected",
            "failure_reason": f"{source}: {error}",
        }

    # ── model / provider error ──────────────────────────────────────────
    model_keywords = (
        "rate limit", "rate_limit", "ratelimit",
        "overloaded", "capacity",
        "server error", "internal error",
        "500", "502", "503", "504",
        "bad gateway", "service unavailable",
    )
    if any(kw in error_lower for kw in model_keywords):
        return {"failure_class": "model_error", "failure_reason": error}

    # ── error present but no keyword match ───────────────────────────────
    if error:
        return {"failure_class": "unknown", "failure_reason": error}

    # ── status is failure with no error at all ───────────────────────────
    return {
        "failure_class": "unknown",
        "failure_reason": f"provider={provider} returncode={returncode} completion_len={len(completion)} (no error captured)",
    }


def _build_reason(
    base: str,
    duration_seconds: Optional[float],
    provider: str,
    sub_provider: Optional[str],
) -> str:
    """Build a human-readable failure_reason string with timing context."""
    parts = [base]
    src = _error_source(provider, sub_provider)
    if src:
        parts.append(f"(source: {src})")
    if duration_seconds is not None:
        parts.append(f"[{duration_seconds:.1f}s]")
    return " ".join(parts)


def _error_source(provider: str, sub_provider: Optional[str]) -> str:
    """Return a compact endpoint label for the error source."""
    provider_norm = (provider or "").lower().strip()
    sub_norm = (sub_provider or "").lower().strip()

    if provider_norm in ("litellm", "litellm:deepseek", "litellm:zai", "litellm:moonshot"):
        return f"litellm-proxy/{sub_norm}" if sub_norm else "litellm-proxy"
    if provider_norm in ("deepseek-harness", "deepseek_harness"):
        return "deepseek-api"
    if provider_norm in ("glm-harness", "glm_harness"):
        return "openrouter/z-ai"
    if provider_norm == "codex":
        return "openai-api"
    if provider_norm == "kimi":
        return "kimi-cli"
    if provider_norm == "gemini":
        return "google-api"
    if provider_norm in ("claude", "anthropic"):
        return "anthropic-api"
    return provider_norm if provider_norm else "unknown"
