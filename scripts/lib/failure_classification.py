"""failure_classification.py — classify dispatch failures so a receipt carries
distinguishable failure_reason and failure_class fields instead of a sentinel
"(no error captured)" log line.

OI-866 (blocker): the fabric currently writes no error/error_detail/failure_reason
field into the receipt when a dispatch fails. A 401 auth rejection, an empty
completion, a model error, and a timeout all produce the same silent receipt.
This module gives every failure path a structured classification so an operator
can tell them apart without tailing log files.

Taxonomy ownership (dispatch 20260816-p10b-provider-observability): THIS module
owns the receipt-facing failure taxonomy — the ``failure_class`` /
``failure_reason`` pair stamped on a dispatch receipt. ``failure_classifier.py``
carries a DIFFERENT, non-overlapping taxonomy (delivery-code → retryable
mapping for the tmux lane's release_on_delivery_failure) and references this
module as the owner of the receipt-facing concern; it must not re-derive or
duplicate these categories.

Categories
----------
auth_rejected    — HTTP 401/403 from the provider endpoint (proxy, API, etc.)
empty_completion — call succeeded (HTTP 200) but the model returned zero text
timeout          — call exceeded its deadline
model_error      — the model/provider itself returned an error (5xx, rate-limit, etc.)
credit_exhausted — provider/gateway balance or credits ran out (HTTP 402 and
                    variants); dispatch 20260816-p9p10-failure-reason-root
unknown          — everything else; should be rare after OI-866

dispatch 20260816-p9p10-failure-reason-root: the glm-harness and
deepseek-harness lanes route through the `claude` CLI, so a provider-side API
error (e.g. "API Error: 402 Insufficient Balance") arrives as ordinary
assistant TEXT in ``completion_text`` — the spawn never populates ``error``
for that case. classify_failure previously only pattern-matched ``error``,
so any failure with a real, readable completion and no separate ``error``
fell through to the contentless ``"(no error captured)"`` fallback even
though the reason was sitting right there on disk. classify_failure now also
scans ``completion_text`` (only when ``error`` is empty — a caller-supplied
``error`` stays authoritative) and, failing a specific pattern match, surfaces
a bounded snippet of the completion itself instead of just its length.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional


FAILURE_CLASSES = frozenset({
    "auth_rejected",
    "empty_completion",
    "timeout",
    "model_error",
    "credit_exhausted",
    "unknown",
})

# Credit/balance exhaustion — matched on phrases actually observed in the
# ledger (measured 2026-08-16, dispatch 20260816-p9p10-failure-reason-root):
# deepseek-harness prints a direct "API Error: 402 Insufficient Balance";
# glm-harness's OpenRouter gateway wraps the same signal inside a 500
# "litellm.acompletion" envelope ("...requires more credits...",
# "openrouter_credits" in the error metadata). Provider error text is not a
# stable contract — a future variant that does not match one of these phrases
# falls through to `unknown` below and must be added here explicitly once
# observed, never silently absorbed back into a contentless placeholder.
CREDIT_EXHAUSTED_KEYWORDS = (
    "insufficient balance",
    "insufficient_balance",
    "insufficient credits",
    "requires more credits",
    "add more credits",
    "openrouter_credits",
)

# Provider gateways (observed: glm-harness's litellm/OpenRouter proxy) can wrap
# the real one-line reason inside a multi-KB nested JSON blob carrying dozens
# of duplicated retry attempts. Prefer the first quoted "message" field (the
# provider's own sentence) when present.
_JSON_MESSAGE_RE = re.compile(r'"message"\s*:\s*"([^"]*)"')
_MAX_DETAIL_LEN = 300

# OI-1333: a completion that fails every keyword match above (credit/auth/
# model_error) AND is this short cannot possibly carry the report contract —
# report_body_contract.py requires >= 50 non-whitespace chars for the Summary
# heading alone, before any of the other three required headings. Below this
# floor, a non-report reply reads as a bare, uninformative fragment rather
# than a legitimate short answer, so it belongs in `empty_completion`
# (escalation: retry_same_tier) rather than `unknown` (no_climb).
#
# Measured 2026-08-18 against t0_receipts.ndjson (schema_version=2 dispatch
# receipts) plus the on-disk report bodies each receipt's report_path points
# to — 504 successful completions, 69 real (non-placeholder) failed ones:
#   SUCCESS completions : n=504, min=262 chars (smallest legitimate report)
#   FAILURE completions : n=69,  smallest genuinely-empty fragment observed
#                          is 17 chars ("Request timed out", the measured
#                          OI-1333 case, dispatch 20260817-d1592r3-pro);
#                          the smallest short-but-NAMED unmatched error text
#                          observed is 34 chars ("API Error: Content block
#                          not found") — informative enough to stay `unknown`
#                          per this module's own "never silently absorb a
#                          named-but-unmatched error" policy (see module
#                          docstring). Nothing was observed in [18, 33].
# 25 sits in that unobserved gap: well clear of the 17-char measured floor,
# well clear of the 34-char named-error floor, and an order of magnitude
# below the smallest real success (262).
_NEAR_EMPTY_COMPLETION_CHARS = 25


def _extract_detail(text: Optional[str], max_len: int = _MAX_DETAIL_LEN) -> str:
    """Pull a bounded, human-readable detail out of raw error/completion text.

    Falls back to a truncated, whitespace-collapsed prefix of the raw text
    when no JSON "message" field is present, so a receipt's failure_reason
    never carries a multi-KB blob.
    """
    text = text or ""
    match = _JSON_MESSAGE_RE.search(text)
    detail = match.group(1) if match else text
    detail = " ".join(detail.split())
    if len(detail) > max_len:
        detail = detail[:max_len].rstrip() + "…"
    return detail or "(empty)"


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

    # A caller-supplied `error` stays authoritative and completion_text is
    # never consulted for it. Only when `error` is empty does the provider's
    # failure text live solely in the completion (see module docstring) —
    # scan that instead so it is never silently discarded.
    signal_text = error_lower if error else completion.lower()

    # ── credit / balance exhaustion ────────────────────────────────────────
    # Checked before model_keywords so glm-harness's outer "500" wrapper never
    # hides the real, actionable 402-credits cause nested inside it.
    if any(kw in signal_text for kw in CREDIT_EXHAUSTED_KEYWORDS):
        source = _error_source(provider, sub_provider)
        detail = _extract_detail(error if error else completion)
        return {
            "failure_class": "credit_exhausted",
            "failure_reason": f"{source}: credit/balance exhausted — {detail}",
        }

    # ── auth rejection ───────────────────────────────────────────────────
    auth_keywords = (
        "authentication", "auth",
        "credentials", "credential",
        "apikey", "api key", "api_key",
        "unauthorized", "forbidden",
        "401", "403",
    )
    if any(kw in signal_text for kw in auth_keywords):
        source = _error_source(provider, sub_provider)
        detail = error if error else _extract_detail(completion)
        return {
            "failure_class": "auth_rejected",
            "failure_reason": f"{source}: {detail}",
        }

    # ── model / provider error ──────────────────────────────────────────
    model_keywords = (
        "rate limit", "rate_limit", "ratelimit",
        "overloaded", "capacity",
        "server error", "internal error",
        "500", "502", "503", "504",
        "bad gateway", "service unavailable",
    )
    if any(kw in signal_text for kw in model_keywords):
        detail = error if error else _extract_detail(completion)
        return {"failure_class": "model_error", "failure_reason": detail}

    # ── error present but no keyword match ───────────────────────────────
    if error:
        return {"failure_class": "unknown", "failure_reason": error}

    # ── error absent, completion non-empty, too short to be a report ─────
    # (OI-1333) A completion under the floor is not meaningfully different
    # from an empty one — see _NEAR_EMPTY_COMPLETION_CHARS above for the
    # measurement backing this boundary.
    if len(completion) < _NEAR_EMPTY_COMPLETION_CHARS:
        return {
            "failure_class": "empty_completion",
            "failure_reason": (
                f"provider={provider} returned a near-empty completion "
                f"({len(completion)} chars, below the "
                f"{_NEAR_EMPTY_COMPLETION_CHARS}-char floor a report needs) "
                f"with returncode={returncode}: {_extract_detail(completion)}"
            ),
        }

    # ── error absent but completion is non-empty and long enough to be
    # informative — surface the completion itself instead of a contentless
    # "(no error captured)" placeholder; the provider's own words are on
    # disk, even if they match no known pattern above.
    return {
        "failure_class": "unknown",
        "failure_reason": f"provider={provider} returncode={returncode}: {_extract_detail(completion)}",
    }


def classify_failure_safe(
    *,
    status: str,
    error: Optional[str] = None,
    completion_text: Optional[str] = None,
    timed_out: bool = False,
    provider: str = "",
    sub_provider: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    returncode: Optional[int] = None,
    dispatch_id: str = "",
) -> Dict[str, Optional[str]]:
    """classify_failure(), guaranteed to never raise and never leave a
    non-success receipt with a completely empty failure_reason.

    Both receipt-emit call sites (envelope_govern._govern,
    provider_dispatch._emit_governance) previously wrapped classify_failure()
    in their own best-effort try/except and, on an exception, left
    failure_reason/failure_class at their None default — the exact
    "completely empty field" gap dispatch 20260816-p9p10-failure-reason-root
    measured on the ledger (100 receipts, all pre-OI-866; none since). Routing
    both call sites through this single safety net means a future
    classify_failure regression can no longer reintroduce that gap silently.
    """
    if status == "success":
        return {"failure_class": None, "failure_reason": None}
    try:
        return classify_failure(
            status=status,
            error=error,
            completion_text=completion_text,
            timed_out=timed_out,
            provider=provider,
            sub_provider=sub_provider,
            duration_seconds=duration_seconds,
            returncode=returncode,
        )
    except Exception as exc:  # noqa: BLE001 — this IS the safety net; must never raise
        return {
            "failure_class": "unknown",
            "failure_reason": f"failure classification raised {exc!r} (dispatch={dispatch_id})",
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
