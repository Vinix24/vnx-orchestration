"""ADR-028 Phase 3 — decision judge fast-path.

Gated behind ``VNX_DECISION_FAST_PATH=1`` (default OFF). When ON, a deterministic classifier
short-circuits the TRIVIAL, unambiguous governance decisions (a clean receipt needs no
action) so they never spend a judge/LLM call; everything with any nuance (failures,
silences, escalations, unknown states) falls through to the normal backend (the judge, or
the rule-based fallback). Rollback: unset the flag → every decision goes to the backend.

Conservative by construction: the fast-path only handles the clearly-nothing-to-do case. It
never decides a failure, an escalation, or an ambiguous state — those are exactly what the
judge should weigh. So enabling it can only REMOVE obvious no-ops from the judge's load, never
make a substantive call the judge would have made differently.
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, Optional

# Receipt statuses that are unambiguously "nothing to do".
_CLEAN_STATUSES = frozenset({"ok", "done", "passed", "pass", "success", "succeeded", "complete", "completed"})


def fast_path_enabled(env: Optional[dict] = None) -> bool:
    """True iff VNX_DECISION_FAST_PATH=1. Default OFF (activation is the operator's knob)."""
    env = os.environ if env is None else env
    return env.get("VNX_DECISION_FAST_PATH") == "1"


def classify(context: Dict[str, Any], question: str):
    """Return a ``DecisionResult`` for a TRIVIAL, unambiguous decision, or ``None`` to route
    to the judge/backend. Only the clearly-nothing-to-do case is handled here."""
    from llm_decision_router import DecisionResult  # noqa: PLC0415 — avoid import cycle at module load

    receipt = context.get("receipt")
    if not isinstance(receipt, dict):
        return None
    status = str(receipt.get("status", "")).strip().lower()
    if status not in _CLEAN_STATUSES:
        return None

    # Any pending signal → defer to the judge. Coerce the silence value ROBUSTLY (a numeric
    # string like "1200" must still count). A value that is NOT a valid finite duration —
    # bool (True/False coerce to 1.0/0.0), NaN, ±inf, or an unparseable string — is treated as
    # a signal (defer), never as clean. The fast-path only fires when it is SURE nothing pends.
    silence_raw = context.get("terminal_silence_seconds", 0)
    if isinstance(silence_raw, bool):
        silent = True  # a bool is not a valid duration → defer
    else:
        try:
            s = float(silence_raw)
            silent = (not math.isfinite(s)) or s > 900  # NaN/inf → invalid → defer
        except (TypeError, ValueError):
            silent = True  # cannot determine → not trivial → defer to the judge
    escalated = any(
        bool(context.get(k))
        for k in ("escalate", "needs_review", "needs_human", "error", "failure", "incident")
    )
    if silent or escalated:
        return None

    return DecisionResult(
        action="skip",
        reasoning="fast-path: receipt clean, no pending signal — no action required",
        confidence=1.0,
        backend_used="fast-path",
        latency_ms=0,
    )


__all__ = ["fast_path_enabled", "classify"]
