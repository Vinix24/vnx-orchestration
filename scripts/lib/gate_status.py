"""Canonical gate result status interpretation (CFX-3).

Single source of truth for "is this gate result a pass?" across closure
verification, postmerge audit summaries, and any other consumer of files
under ``${VNX_STATE_DIR}/review_gates/results/`` (resolved via
``scripts/lib/vnx_paths``).

Schema drift fixed here:
- writers populate ``status`` with values from one canonical set
- readers call :func:`is_pass` instead of comparing fields ad hoc
- legacy ``verdict`` field is honored as fallback when ``status`` is null
"""
from __future__ import annotations

import warnings
from typing import Any, Dict, Tuple

PASS_STATES = frozenset({"approve", "completed", "pass", "passed"})
FAIL_STATES = frozenset({"failed", "errored", "fail", "blocked"})
INCOMPLETE_STATES = frozenset({"pending", "running", "queued", "requested"})

ALL_KNOWN_STATES = PASS_STATES | FAIL_STATES | INCOMPLETE_STATES | frozenset({"not_executable"})


def _coerce_status(result: Dict[str, Any]) -> Tuple[str, bool]:
    """Return (status, used_legacy_verdict_fallback).

    Uses ``status`` when present and non-empty. Falls back to legacy
    ``verdict`` for old files written before CFX-3 — this is graceful
    migration, not a permanent contract.
    """
    status = result.get("status")
    if isinstance(status, str) and status:
        return status.lower(), False
    verdict = result.get("verdict")
    if isinstance(verdict, str) and verdict:
        warnings.warn(
            "gate_status: result file uses legacy 'verdict' field; "
            "writers should populate 'status' (CFX-3 migration).",
            DeprecationWarning,
            stacklevel=3,
        )
        return verdict.lower(), True
    return "", False


def is_pass(result: Dict[str, Any]) -> Tuple[bool, str]:
    """Return ``(passed, reason)`` for a gate result dict.

    ``reason`` always explains the decision so callers can surface it.
    Pass requires: canonical pass status AND zero blocking findings AND
    ``blocking_count`` is zero or absent.
    """
    status, _legacy = _coerce_status(result)
    blocking_findings = result.get("blocking_findings") or []
    blocking_len = len(blocking_findings) if isinstance(blocking_findings, list) else 0
    blocking_count = result.get("blocking_count")
    if not isinstance(blocking_count, int):
        blocking_count = None

    if status in PASS_STATES and blocking_len == 0 and blocking_count in (0, None):
        return True, "passed"
    if status in FAIL_STATES:
        return False, f"status: {status}"
    if blocking_len > 0:
        return False, f"{blocking_len} blocking finding(s)"
    if blocking_count is not None and blocking_count > 0:
        return False, f"blocking_count: {blocking_count}"
    if status in INCOMPLETE_STATES:
        return False, f"incomplete: {status}"
    if status == "not_executable":
        return False, "status: not_executable"
    if not status:
        return False, "no status or verdict field"
    return False, f"unknown status: {status}"


def is_terminal(result: Dict[str, Any]) -> bool:
    """True when the result represents a decided pass/fail (not in-flight).

    Used by closure verifier to decide whether to enforce report_path on
    a result (pass/fail must carry evidence; in-flight states must not).
    ``not_executable`` is treated as terminal because the gate has been
    finally classified even though no execution happened.
    """
    status, _ = _coerce_status(result)
    return status in PASS_STATES or status in FAIL_STATES or status == "not_executable"


def canonical_status(result: Dict[str, Any]) -> str:
    """Return the canonical status string for a result, "" if unknown.

    Honors legacy ``verdict`` fallback. Always lowercased.
    """
    status, _ = _coerce_status(result)
    return status
