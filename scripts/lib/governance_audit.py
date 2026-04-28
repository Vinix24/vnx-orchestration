#!/usr/bin/env python3
"""VNX Governance Audit Trail — F51-PR3.

Append-only NDJSON log of all governance enforcement decisions.
Written to: $VNX_STATE_DIR/governance_audit.ndjson

Schema per line:
    {
        "timestamp":    "ISO8601",
        "event_type":   "enforcement_check" | "gate_result" | "dispatch_decision",
        "check_name":   str | null,
        "level":        int | null,
        "passed":       bool,
        "message":      str,
        "context_hash": "sha256[:16] of context dict" | null,
        "override":     str | null,
        "operator":     str | null,
        "feature":      str | null,
        "pr_number":    int | null,
        "dispatch_id":  str | null
    }
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]

_legacy_warned: bool = False


def _audit_path() -> Path:
    data_dir = Path(os.environ.get("VNX_DATA_DIR", str(_REPO_ROOT / ".vnx-data")))
    return data_dir / "state" / "governance_audit.ndjson"


def _legacy_audit_path() -> Path:
    data_dir = Path(os.environ.get("VNX_DATA_DIR", str(_REPO_ROOT / ".vnx-data")))
    return data_dir / "events" / "governance_audit.ndjson"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _context_hash(context: dict) -> str:
    serialized = json.dumps(context, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


def _read_path(path: Path) -> List[Dict[str, Any]]:
    """Read all valid NDJSON entries from path. Returns [] if missing or unreadable."""
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _append(record: Dict[str, Any]) -> None:
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------


def log_enforcement(
    check_name: str,
    level: int,
    result: bool,
    context: dict,
    override: Optional[str] = None,
    message: str = "",
    dispatch_id: Optional[str] = None,
) -> None:
    """Append a governance enforcement decision to the audit trail.

    Args:
        check_name:  Name of the governance check (e.g. "gate_before_next_feature").
        level:       Enforcement level (0=off, 1=advisory, 2=soft_mandatory, 3=hard_mandatory).
        result:      True if check passed.
        context:     Context dict passed to the check (used for hash + field extraction).
        override:    Override reason string if a soft-mandatory check was bypassed.
        message:     Human-readable outcome message from the check.
        dispatch_id: Dispatch that triggered this enforcement check (for traceability).
    """
    effective_dispatch_id = (
        dispatch_id
        or context.get("dispatch_id")
        or None
    )
    _append({
        "timestamp": _now_utc(),
        "event_type": "enforcement_check",
        "check_name": check_name,
        "level": level,
        "passed": result,
        "message": message,
        "context_hash": _context_hash(context),
        "override": override,
        "operator": os.environ.get("VNX_OPERATOR") or None,
        "feature": context.get("feature") or None,
        "pr_number": context.get("pr_number") or None,
        "dispatch_id": effective_dispatch_id,
    })


def log_gate_result(
    gate: str,
    pr_number: Optional[int],
    status: str,
    findings_count: int,
    dispatch_id: Optional[str] = None,
) -> None:
    """Log a review gate execution result (codex/gemini) to the audit trail.

    Args:
        gate:           Gate name (e.g. "codex_gate", "gemini_review").
        pr_number:      GitHub PR number, or None if unavailable.
        status:         Outcome string (e.g. "triggered", "passed", "failed").
        findings_count: Number of findings returned by the gate.
        dispatch_id:    Dispatch that triggered this gate (for traceability).
    """
    passed = status in ("triggered", "passed", "ok", "success")
    _append({
        "timestamp": _now_utc(),
        "event_type": "gate_result",
        "check_name": gate,
        "level": None,
        "passed": passed,
        "message": f"Gate {gate} {status} (findings: {findings_count})",
        "context_hash": None,
        "override": None,
        "operator": os.environ.get("VNX_OPERATOR") or None,
        "feature": None,
        "pr_number": pr_number,
        "dispatch_id": dispatch_id,
    })


def log_dispatch_decision(
    action: str,
    dispatch_id: str,
    reasoning: str,
    pr_number: Optional[int] = None,
) -> None:
    """Log a T0 dispatch accept/reject/block decision to the audit trail.

    Args:
        action:      Decision action: "accepted", "blocked", "rejected", "dispatched".
        dispatch_id: Dispatch file stem (e.g. "f51-pr3-t1-20260413T120000").
        reasoning:   Human-readable explanation of the decision.
        pr_number:   GitHub PR number associated with this dispatch, or None.
    """
    _append({
        "timestamp": _now_utc(),
        "event_type": "dispatch_decision",
        "check_name": None,
        "level": None,
        "passed": action not in ("blocked", "rejected"),
        "message": reasoning,
        "context_hash": None,
        "override": None,
        "operator": os.environ.get("VNX_OPERATOR") or None,
        "feature": None,
        "pr_number": pr_number,
        "dispatch_id": dispatch_id,
        "action": action,
    })


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def get_recent(limit: int = 50) -> List[Dict[str, Any]]:
    """Return the last `limit` entries from the governance audit trail (newest first).

    Also reads legacy events/governance_audit.ndjson if present, merging both
    sources during the upgrade window. Logs a one-time warning on first legacy read.
    """
    global _legacy_warned

    state_entries = _read_path(_audit_path())
    legacy_entries = _read_path(_legacy_audit_path())

    if legacy_entries and not _legacy_warned:
        logging.getLogger(__name__).warning(
            "governance_audit: reading legacy events/governance_audit.ndjson — "
            "run scripts/migrate_governance_audit_path.py to consolidate"
        )
        _legacy_warned = True

    all_entries = state_entries + legacy_entries
    all_entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return all_entries[:limit]


def get_overrides(days: int = 7) -> List[Dict[str, Any]]:
    """Return entries with a non-null override field from the last `days` days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    overrides: List[Dict[str, Any]] = []
    for record in _read_path(_audit_path()) + _read_path(_legacy_audit_path()):
        if record.get("override") is not None and record.get("timestamp", "") >= cutoff:
            overrides.append(record)
    overrides.sort(key=lambda e: e.get("timestamp", ""))
    return overrides
