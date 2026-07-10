"""ADR-028 Phase 2 — decision-judge SHADOW mode (the safety valve).

Gated behind ``VNX_DECISION_JUDGE_SHADOW=1`` (default OFF). When ON, a judge produces an
ADVISORY decision at each governance decision point; it is written to a SEPARATE
``decision_advisory.ndjson`` ledger that T0 READS BUT NEVER ACTS ON — T0 decides itself. A
comparator then logs where the judge and the human (T0) diverge to
``decision_divergence.ndjson``. This validates the judge against the human at ZERO risk
before any decision authority shifts (that shift is Phases 3-4, separately operator-gated).

Zero-risk guarantees:
- Default OFF; every function is a no-op when the flag is unset.
- Advisories + divergences live in their OWN ledgers, NOT the governed ``t0_receipts.ndjson``,
  so nothing downstream (receipt processor, gates) is affected by shadow output.
- The comparator only LOGS; it never changes a decision.
- Every entry point is fail-open: a shadow error MUST NOT break T0's real decision.

Rollback: unset the flag; the advisory ledgers are inert observability.

The judge is pluggable (``judge`` param). The default is the deterministic rule-based
decision (no LLM, zero cost); the real ephemeral LLM judge (Phase 3+) injects its own.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_LIB = Path(__file__).resolve().parent
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

ADVISORY_LEDGER = "decision_advisory.ndjson"
DIVERGENCE_LEDGER = "decision_divergence.ndjson"
_FLAG = "VNX_DECISION_JUDGE_SHADOW"


def shadow_enabled(env: Optional[dict] = None) -> bool:
    """True iff VNX_DECISION_JUDGE_SHADOW=1. Default OFF (the safety valve is opt-in)."""
    env = os.environ if env is None else env
    return env.get(_FLAG) == "1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir(state_dir: "str | Path | None") -> Path:
    if state_dir is not None:
        return Path(state_dir)
    from vnx_paths import resolve_state_dir  # noqa: PLC0415
    return resolve_state_dir()


def _default_judge(context: Dict[str, Any], question: str):
    """Deterministic rule-based judge (no LLM, no cost). The real ephemeral LLM judge is
    injected via the ``judge`` param; this keeps shadow mode zero-cost by default."""
    from llm_decision_router import _rule_based_decision  # noqa: PLC0415
    return _rule_based_decision(context, question)


def _atomic_append(path: Path, record: Dict[str, Any], lock_name: str) -> None:
    """fcntl-locked atomic NDJSON append (same contract as append_receipt/shadow_logger)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = path.parent / lock_name
    with sentinel.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        with path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(json.dumps(record, separators=(",", ":"), sort_keys=False) + "\n")


def _advisory_dict(result: Any) -> Dict[str, Any]:
    if hasattr(result, "to_dict"):
        return result.to_dict()
    if isinstance(result, dict):
        return result
    return {"action": getattr(result, "action", None)}


def record_advisory(
    decision_id: str,
    context: Dict[str, Any],
    question: str,
    *,
    judge: Optional[Callable[[Dict[str, Any], str], Any]] = None,
    state_dir: "str | Path | None" = None,
    ts: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """When shadow is ON, run the judge and append a ``decision_advisory`` entry. T0 must
    READ BUT NOT ACT on it. Returns the advisory dict (action/reasoning/confidence/…), or
    None when shadow is OFF or on any error (fail-open — never breaks the real path)."""
    if not shadow_enabled():
        return None
    try:
        result = (judge or _default_judge)(context, question)
        advisory = _advisory_dict(result)
        record = {
            "event": "decision_advisory",
            "decision_id": decision_id,
            "question": question,
            "advisory": advisory,
            "ts": ts or _now_iso(),
            "note": "SHADOW advisory — T0 does not act on this (ADR-028 Phase 2)",
        }
        _atomic_append(_state_dir(state_dir) / ADVISORY_LEDGER, record, ADVISORY_LEDGER + ".lock")
        return advisory
    except Exception:  # noqa: BLE001 — shadow must never break the real decision path
        return None


def compare_and_log(
    decision_id: str,
    actual_action: Optional[str],
    advisory: Optional[Dict[str, Any]],
    *,
    state_dir: "str | Path | None" = None,
    ts: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Comparator: log whether T0's actual decision agrees with the judge's advisory, to
    ``decision_divergence.ndjson``. Logs ONLY — never changes a decision. No-op when shadow
    is OFF or ``advisory`` is None. Returns the divergence record, or None (fail-open)."""
    if not shadow_enabled() or advisory is None:
        return None
    try:
        advisory_action = (
            advisory.get("action") if isinstance(advisory, dict)
            else getattr(advisory, "action", None)
        )
        record = {
            "event": "decision_divergence",
            "decision_id": decision_id,
            "actual_action": actual_action,
            "advisory_action": advisory_action,
            "agree": actual_action == advisory_action,
            "advisory_confidence": (
                advisory.get("confidence") if isinstance(advisory, dict) else None
            ),
            "ts": ts or _now_iso(),
        }
        _atomic_append(
            _state_dir(state_dir) / DIVERGENCE_LEDGER, record, DIVERGENCE_LEDGER + ".lock"
        )
        return record
    except Exception:  # noqa: BLE001 — comparator is observational; never break the caller
        return None


def divergence_summary(state_dir: "str | Path | None" = None) -> Dict[str, Any]:
    """Read the divergence ledger and return {total, agree, disagree, agree_rate}. This is
    the data that justifies (or blocks) turning on Phases 3-4. Empty/absent ledger -> zeros."""
    path = _state_dir(state_dir) / DIVERGENCE_LEDGER
    total = agree = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            if rec.get("agree") is True:
                agree += 1
    except OSError:
        pass
    return {
        "total": total,
        "agree": agree,
        "disagree": total - agree,
        "agree_rate": (agree / total) if total else None,
    }


__all__ = [
    "ADVISORY_LEDGER",
    "DIVERGENCE_LEDGER",
    "shadow_enabled",
    "record_advisory",
    "compare_and_log",
    "divergence_summary",
]
