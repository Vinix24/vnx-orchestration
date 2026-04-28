"""PR queue state builder — replaces hand-maintained PR_QUEUE.md.

Generates pr_queue_state.json from dispatch_register.ndjson + gh pr list.
Schema: pr_queue/1.0
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_gh(*args: str) -> Optional[Any]:
    """Run gh CLI; return parsed JSON or None on failure (best-effort)."""
    try:
        result = subprocess.run(
            ["gh"] + list(args),
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception:
        return None


def _extract_ci_status(checks: List[Dict[str, Any]]) -> str:
    """Derive overall CI status from gh statusCheckRollup entries."""
    if not checks:
        return "unknown"
    statuses = {c.get("state", "").upper() for c in checks}
    if "FAILURE" in statuses or "ERROR" in statuses:
        return "fail"
    if "PENDING" in statuses or "IN_PROGRESS" in statuses:
        return "pending"
    if statuses and all(s == "SUCCESS" for s in statuses):
        return "pass"
    return "unknown"


def _get_open_prs() -> List[Dict[str, Any]]:
    """Fetch open PRs via gh CLI. Returns [] on any failure."""
    data = _run_gh(
        "pr", "list",
        "--state", "open",
        "--json", "number,title,headRefName,isDraft,statusCheckRollup",
        "--limit", "50",
    )
    if not data or not isinstance(data, list):
        return []
    results = []
    for pr in data:
        ci_status = _extract_ci_status(pr.get("statusCheckRollup") or [])
        results.append({
            "number": pr.get("number"),
            "title": pr.get("title", ""),
            "branch": pr.get("headRefName", ""),
            "state": "draft" if pr.get("isDraft") else "active",
            "ci_status": ci_status,
        })
    return results


def _get_merged_today() -> List[Dict[str, Any]]:
    """Fetch PRs merged today via gh CLI. Returns [] on any failure."""
    data = _run_gh(
        "pr", "list",
        "--state", "merged",
        "--json", "number,title,headRefName,mergedAt",
        "--limit", "30",
    )
    if not data or not isinstance(data, list):
        return []
    today = datetime.now(timezone.utc).date().isoformat()
    results = []
    for pr in data:
        merged_at = pr.get("mergedAt", "")
        if merged_at and merged_at[:10] == today:
            results.append({
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "branch": pr.get("headRefName", ""),
                "merged_at": merged_at,
            })
    return results


def _build_gates_map(
    register_events: List[Dict[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    """Build per-PR {gates_passed, blocked_on} from register events."""
    pr_map: Dict[int, Dict[str, Any]] = {}
    for ev in register_events:
        pr_number = ev.get("pr_number")
        if pr_number is None:
            continue
        entry = pr_map.setdefault(pr_number, {"gates_passed": [], "blocked_on": []})
        event = ev.get("event", "")
        gate = ev.get("gate", "")
        if event == "gate_passed" and gate and gate not in entry["gates_passed"]:
            entry["gates_passed"].append(gate)
        elif event == "gate_failed" and gate:
            if gate not in entry["blocked_on"]:
                entry["blocked_on"].append(gate)
            if gate in entry["gates_passed"]:
                entry["gates_passed"].remove(gate)
    return pr_map


def _build_queued_features(
    register_events: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Derive queued features — dispatches not yet in a terminal state."""
    terminal_events = {"dispatch_completed", "dispatch_failed", "pr_merged"}
    by_dispatch: Dict[str, Dict[str, Any]] = {}
    for ev in register_events:
        did = ev.get("dispatch_id", "").strip()
        if not did:
            continue
        entry = by_dispatch.setdefault(did, {
            "dispatch_id": did,
            "feature_id": "",
            "status": "unknown",
            "latest_ts": "",
        })
        ts = ev.get("timestamp", "")
        if ts > entry["latest_ts"]:
            entry["latest_ts"] = ts
            entry["status"] = ev.get("event", "unknown")
        if ev.get("feature_id"):
            entry["feature_id"] = ev["feature_id"]

    return [
        {"dispatch_id": e["dispatch_id"], "feature_id": e["feature_id"], "status": e["status"]}
        for e in by_dispatch.values()
        if e["status"] not in terminal_events
    ]


def build_pr_queue_state(
    state_dir: Path,
    register_events: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build pr_queue_state dict. Never raises — gh failures produce empty arrays."""
    if register_events is None:
        try:
            scripts_lib = str(_REPO_ROOT / "scripts" / "lib")
            if scripts_lib not in sys.path:
                sys.path.insert(0, scripts_lib)
            from dispatch_register import read_events  # noqa: PLC0415
            register_events = read_events(state_dir=state_dir) or []
        except Exception:
            register_events = []

    gates_map = _build_gates_map(register_events)
    open_prs_raw = _get_open_prs()

    open_prs: List[Dict[str, Any]] = []
    for pr in open_prs_raw:
        pr_num = pr["number"]
        gate_info = gates_map.get(pr_num, {"gates_passed": [], "blocked_on": []})
        open_prs.append({
            "number": pr_num,
            "title": pr["title"],
            "branch": pr["branch"],
            "state": pr["state"],
            "ci_status": pr["ci_status"],
            "gates_passed": gate_info["gates_passed"],
            "blocked_on": gate_info["blocked_on"],
        })

    return {
        "schema": "pr_queue/1.0",
        "timestamp": _now_iso(),
        "open_prs": open_prs,
        "merged_today": _get_merged_today(),
        "queued_features": _build_queued_features(register_events),
    }


def write_pr_queue_state(
    state_dir: Path,
    register_events: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    """Build and atomically write pr_queue_state.json; return output path."""
    state = build_pr_queue_state(state_dir, register_events=register_events)
    out = state_dir / "pr_queue_state.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix="pr_queue_state.json.tmp.", dir=str(state_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, separators=(",", ":"))
        os.replace(tmp_str, str(out))
    except Exception:
        try:
            os.unlink(tmp_str)
        except Exception:
            pass
        raise
    return out
