"""ADR-028 Phase 4 — judge decisions binding for routine work; human-on-the-last-set.

Gated behind ``VNX_DECISION_JUDGE_ENABLED=1`` (default OFF). When ON, a judge decision is
BINDING for ROUTINE actions — T0 acts on it without re-deciding and reviews only exceptions.
SENSITIVE actions (merge, close track, override a gate, push to main, publish) ALWAYS require
an explicit ``operator_approval`` receipt — the human-on-the-last-set guarantee — EVEN when the
judge is enabled. Default OFF → T0 retains full decision authority (the judge is advisory only,
per Phase 2).

This module is the POLICY. It never executes anything; a caller (the executor / T0) asks
``binding_verdict(...)`` whether it may act on a judge decision, and records an
``operator_approval`` receipt (``record_operator_approval``) when a human clears a sensitive one.
So enabling Phase 4 can hand routine calls to the judge but can NEVER let it merge/close/override
without a human — that gate is unconditional.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_LIB = Path(__file__).resolve().parent
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

_FLAG = "VNX_DECISION_JUDGE_ENABLED"
APPROVAL_LEDGER = "operator_approvals.ndjson"

# Actions that mutate irreversibly or cross a governance boundary — NEVER auto-bound to the
# judge; always require an explicit operator_approval receipt, even when the judge is enabled.
SENSITIVE_ACTIONS = frozenset({
    "merge_pr", "merge", "close_track", "close_objective", "override_gate", "override",
    "push_main", "publish", "release", "tag_push", "delete", "settings_change",
})


def judge_binding_enabled(env: Optional[dict] = None) -> bool:
    """True iff VNX_DECISION_JUDGE_ENABLED=1. Default OFF (T0 keeps full authority)."""
    env = os.environ if env is None else env
    return env.get(_FLAG) == "1"


def is_sensitive(action: str) -> bool:
    """True if ``action`` requires an explicit operator_approval regardless of the judge flag."""
    return str(action or "").strip().lower() in SENSITIVE_ACTIONS


def binding_verdict(
    action: str,
    *,
    has_operator_approval: bool = False,
    env: Optional[dict] = None,
) -> Dict[str, Any]:
    """Decide whether T0 may act on the judge's decision for ``action``.

    Returns ``{binding, requires_operator_approval, reason}``:
    - judge disabled → never binding (T0 decides); sensitive actions still flagged for approval.
    - judge enabled + routine action → binding (T0 may act on the judge call).
    - judge enabled + SENSITIVE action → binding ONLY if ``has_operator_approval`` is True;
      otherwise NOT binding and an operator_approval is required (human-on-the-last-set).
    """
    enabled = judge_binding_enabled(env)
    sensitive = is_sensitive(action)
    if not enabled:
        return {
            "binding": False,
            "requires_operator_approval": sensitive,
            "reason": "judge disabled — T0 decides (Phase 2 advisory only)",
        }
    if sensitive and not has_operator_approval:
        return {
            "binding": False,
            "requires_operator_approval": True,
            "reason": "sensitive action — explicit operator_approval required (human-on-the-last-set)",
        }
    return {
        "binding": True,
        "requires_operator_approval": False,
        "reason": (
            "sensitive action cleared by operator_approval" if sensitive
            else "routine action — judge decision binding"
        ),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir(state_dir: "str | Path | None") -> Path:
    if state_dir is not None:
        return Path(state_dir)
    from vnx_paths import resolve_state_dir  # noqa: PLC0415
    return resolve_state_dir()


def record_operator_approval(
    decision_id: str,
    action: str,
    operator: str,
    *,
    note: Optional[str] = None,
    state_dir: "str | Path | None" = None,
    ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Append an ``operator_approval`` receipt clearing a sensitive judge decision for one
    action. This is the human-on-the-last-set record; ``binding_verdict`` only lets a sensitive
    action bind once such an approval exists."""
    record = {
        "event": "operator_approval",
        "decision_id": decision_id,
        "action": action,
        "operator": operator,
        "note": note,
        "ts": ts or _now_iso(),
    }
    path = _state_dir(state_dir) / APPROVAL_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = path.parent / (APPROVAL_LEDGER + ".lock")
    with sentinel.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        with path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(json.dumps(record, separators=(",", ":"), sort_keys=False) + "\n")
    return record


__all__ = [
    "SENSITIVE_ACTIONS",
    "APPROVAL_LEDGER",
    "judge_binding_enabled",
    "is_sensitive",
    "binding_verdict",
    "record_operator_approval",
]
