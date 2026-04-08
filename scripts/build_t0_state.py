#!/usr/bin/env python3
"""Unified T0 state builder — produces a single JSON snapshot of all T0 state.

Replaces 8+ separate startup scripts (generate_t0_brief.sh, reconcile_queue_state.py,
open_items_manager.py digest, runtime_core_cli.py check-terminal x3,
reconcile_terminal_state.py). Called by SessionStart hook.

Usage:
    python3 scripts/build_t0_state.py [--output PATH] [--format {state,brief}]

Output schema: schema_version "2.0" (t0_state.json)
With --format brief: schema 1.0 backward-compat (t0_brief.json format)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Path bootstrap — before importing any lib modules
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_LIB_DIR = _SCRIPT_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from vnx_paths import ensure_env  # noqa: E402

_PATHS = ensure_env()
_STATE_DIR = Path(_PATHS["VNX_STATE_DIR"])
_DISPATCH_DIR = Path(_PATHS["VNX_DISPATCH_DIR"])
_DATA_DIR = Path(_PATHS["VNX_DATA_DIR"])
_PROJECT_ROOT = Path(_PATHS["PROJECT_ROOT"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _safe_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _count_md(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    try:
        return sum(1 for f in directory.iterdir() if f.is_file() and f.suffix == ".md")
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Step 1: Schema init (absorbed from runtime_coordination_init.py)
# ---------------------------------------------------------------------------

def _init_and_check_db(state_dir: Path) -> bool:
    """Idempotent schema init. Returns True if DB is operational."""
    try:
        from coordination_db import init_schema
        init_schema(state_dir)
        return True
    except Exception:
        pass
    # Fallback: check if DB already exists and has tables
    db_path = state_dir / "runtime_coordination.db"
    if not db_path.exists():
        return False
    try:
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            return "terminal_leases" in tables
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Terminal state
# ---------------------------------------------------------------------------

def _build_terminals(state_dir: Path) -> Dict[str, Any]:
    """Terminal state via canonical_state_views + lease DB augmentation."""
    try:
        from canonical_state_views import build_terminal_snapshot, _brief_terminals
        snapshot = build_terminal_snapshot(state_dir)
        terminals: Dict[str, Any] = _brief_terminals(snapshot)
    except Exception:
        terminals = {
            t: {
                "status": "unknown",
                "track": tr,
                "ready": False,
                "current_task": None,
                "last_update": "never",
                "source": "error",
                "status_age_seconds": None,
            }
            for t, tr in [("T1", "A"), ("T2", "B"), ("T3", "C")]
        }

    # Augment with lease state from DB
    try:
        from coordination_db import get_connection, get_lease
        with get_connection(state_dir) as conn:
            for tid in ("T1", "T2", "T3"):
                lease = get_lease(conn, tid)
                terminals.setdefault(tid, {})["lease_state"] = (
                    lease.get("state", "idle") if lease else "idle"
                )
                # Rename current_task -> current_dispatch for schema 2.0
                terminals[tid]["current_dispatch"] = terminals[tid].pop("current_task", None)
    except Exception:
        for tid in ("T1", "T2", "T3"):
            terminals.setdefault(tid, {})["lease_state"] = "idle"
            if "current_task" in terminals.get(tid, {}):
                terminals[tid]["current_dispatch"] = terminals[tid].pop("current_task")

    return terminals


# ---------------------------------------------------------------------------
# Queue counts
# ---------------------------------------------------------------------------

def _build_queues(dispatch_dir: Path, state_dir: Path) -> Dict[str, Any]:
    pending = _count_md(dispatch_dir / "pending")
    active = _count_md(dispatch_dir / "active")
    conflict = _count_md(dispatch_dir / "conflicts")

    completed_last_hour = 0
    receipts_path = state_dir / "t0_receipts.ndjson"
    if receipts_path.exists():
        cutoff = _now_utc() - timedelta(hours=1)
        try:
            for line in receipts_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-500:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                event = e.get("event_type") or e.get("event", "")
                if event not in ("task_complete", "quality_gate_verification"):
                    continue
                ts_raw = e.get("timestamp")
                if not ts_raw:
                    continue
                try:
                    dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt.astimezone(timezone.utc) >= cutoff:
                        completed_last_hour += 1
                except Exception:
                    pass
        except Exception:
            pass

    return {
        "pending_count": pending,
        "active_count": active,
        "completed_last_hour": completed_last_hour,
        "conflict_count": conflict,
    }


# ---------------------------------------------------------------------------
# Track state (from progress_state.yaml)
# ---------------------------------------------------------------------------

def _build_tracks(state_dir: Path) -> Dict[str, Any]:
    progress_path = state_dir / "progress_state.yaml"
    tracks: Dict[str, Any] = {}

    yaml_data: Dict[str, Any] = {}
    try:
        import yaml
        with open(progress_path, encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}
    except Exception:
        pass

    for track_id in ("A", "B", "C"):
        t = (yaml_data.get("tracks") or {}).get(track_id) or {}
        status = str(t.get("status") or "idle").strip()
        active_dispatch_id = t.get("active_dispatch_id")
        last_receipt = t.get("last_receipt") or {}

        if status == "blocked":
            health = "blocked"
        elif status == "working" and active_dispatch_id:
            health = "healthy"
        elif status == "idle":
            health = "healthy"
        else:
            health = "unknown"

        tracks[track_id] = {
            "current_gate": t.get("current_gate"),
            "status": status,
            "active_dispatch_id": active_dispatch_id,
            "last_receipt": last_receipt if isinstance(last_receipt, dict) else {},
            "health": health,
        }

    return tracks


# ---------------------------------------------------------------------------
# PR progress (via QueueReconciler)
# ---------------------------------------------------------------------------

def _build_pr_progress(dispatch_dir: Path, state_dir: Path) -> Dict[str, Any]:
    _empty: Dict[str, Any] = {
        "feature_name": None,
        "total": 0,
        "completed": 0,
        "in_progress": [],
        "completion_pct": 0,
        "has_blocking_drift": False,
    }
    feature_plan = _PROJECT_ROOT / "FEATURE_PLAN.md"
    if not feature_plan.exists():
        return _empty

    try:
        from queue_reconciler import QueueReconciler
        receipts = state_dir / "t0_receipts.ndjson"
        proj = state_dir / "pr_queue_state.json"
        result = QueueReconciler(
            dispatch_dir=dispatch_dir,
            receipts_file=receipts,
            feature_plan=feature_plan,
            projection_file=proj if proj.exists() else None,
        ).reconcile()

        total = len(result.prs)
        completed = sum(1 for p in result.prs if p.state == "completed")
        in_progress = [p.pr_id for p in result.prs if p.state == "active"]
        pct = int(completed * 100 / total) if total > 0 else 0

        return {
            "feature_name": result.feature_name,
            "total": total,
            "completed": completed,
            "in_progress": in_progress,
            "completion_pct": pct,
            "has_blocking_drift": result.has_blocking_drift,
        }
    except Exception:
        return _empty


# ---------------------------------------------------------------------------
# Open items (reads existing digest)
# ---------------------------------------------------------------------------

def _build_open_items(state_dir: Path) -> Dict[str, Any]:
    digest_path = state_dir / "open_items_digest.json"
    data = _safe_json(digest_path) if digest_path.exists() else None
    if not data:
        return {"open_count": 0, "blocker_count": 0, "top_blockers": []}

    summary = data.get("summary") or {}
    return {
        "open_count": int(summary.get("open_count") or 0),
        "blocker_count": int(summary.get("blocker_count") or 0),
        "top_blockers": (data.get("top_blockers") or [])[:3],
    }


# ---------------------------------------------------------------------------
# Active work (scans dispatches/active/)
# ---------------------------------------------------------------------------

def _build_active_work(dispatch_dir: Path) -> List[Dict[str, Any]]:
    active_dir = dispatch_dir / "active"
    if not active_dir.is_dir():
        return []

    items: List[Dict[str, Any]] = []
    try:
        for md_file in sorted(active_dir.glob("*.md")):
            try:
                started_at = datetime.fromtimestamp(
                    md_file.stat().st_mtime, tz=timezone.utc
                ).isoformat().replace("+00:00", "Z")
                dispatch_id = md_file.stem
                track: Optional[str] = None
                gate: Optional[str] = None
                for line in md_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if track is None:
                        m = re.search(r"\[\[TARGET:([^\]]+)\]\]", line)
                        if m:
                            track = m.group(1).strip()
                    if gate is None and re.match(r"^Gate:\s*\S+", line, re.IGNORECASE):
                        gate = line.split(":", 1)[1].strip()
                    if track and gate:
                        break
                items.append({
                    "dispatch_id": dispatch_id,
                    "track": track,
                    "gate": gate,
                    "started_at": started_at,
                })
            except Exception:
                continue
    except Exception:
        pass

    return items[:5]


# ---------------------------------------------------------------------------
# Recent receipts (last N lines from t0_receipts.ndjson)
# ---------------------------------------------------------------------------

def _build_recent_receipts(state_dir: Path, n: int = 3) -> List[Dict[str, Any]]:
    receipts_path = state_dir / "t0_receipts.ndjson"
    if not receipts_path.exists():
        return []

    try:
        # Tail-read without loading entire file
        with open(receipts_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            data = b""
            block = 8192
            while end > 0 and data.count(b"\n") < 100:
                start = max(0, end - block)
                f.seek(start)
                data = f.read(end - start) + data
                end = start
                if start == 0:
                    break

        events: List[Dict[str, Any]] = []
        for line in data.splitlines()[-100:]:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line.decode("utf-8"))
                events.append({
                    "terminal": e.get("terminal"),
                    "status": e.get("status"),
                    "event_type": e.get("event_type") or e.get("event"),
                    "timestamp": e.get("timestamp"),
                    "dispatch_id": e.get("dispatch_id"),
                    "gate": e.get("gate"),
                })
            except Exception:
                continue

        return events[-n:]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Git context
# ---------------------------------------------------------------------------

def _build_git_context() -> Dict[str, Any]:
    def _run(*cmd: str) -> str:
        try:
            return subprocess.check_output(
                list(cmd), cwd=str(_PROJECT_ROOT),
                stderr=subprocess.DEVNULL, text=True, timeout=3,
            ).strip()
        except Exception:
            return ""

    branch = _run("git", "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    log_out = _run("git", "log", "--oneline", "-5")
    commits = [l.strip() for l in log_out.splitlines() if l.strip()] if log_out else []
    status_out = _run("git", "status", "--porcelain")
    uncommitted = bool(status_out.strip())

    return {
        "branch": branch,
        "last_5_commits": commits,
        "uncommitted_changes": uncommitted,
    }


# ---------------------------------------------------------------------------
# System health
# ---------------------------------------------------------------------------

def _build_system_health(state_dir: Path, db_initialized: bool) -> Dict[str, Any]:
    uptime_seconds = 0
    panes_path = state_dir / "panes.json"
    if panes_path.exists():
        try:
            uptime_seconds = int(time.time() - panes_path.stat().st_mtime)
        except Exception:
            pass

    # Degraded if we have neither terminal state nor any receipts
    status = "healthy"
    if (
        not (state_dir / "terminal_state.json").exists()
        and not (state_dir / "t0_receipts.ndjson").exists()
    ):
        status = "degraded"

    return {
        "status": status,
        "db_initialized": db_initialized,
        "uptime_seconds": uptime_seconds,
    }


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_t0_state(
    state_dir: Path,
    dispatch_dir: Path,
) -> Dict[str, Any]:
    """Build the full T0 state document. Never raises — errors produce safe fallbacks."""
    start = time.monotonic()

    # Step 1: Ensure DB schema (absorbed from runtime_coordination_init.py)
    db_ok = _init_and_check_db(state_dir)

    terminals = _build_terminals(state_dir)
    queues = _build_queues(dispatch_dir, state_dir)
    tracks = _build_tracks(state_dir)
    pr_progress = _build_pr_progress(dispatch_dir, state_dir)
    open_items = _build_open_items(state_dir)
    active_work = _build_active_work(dispatch_dir)
    recent_receipts = _build_recent_receipts(state_dir)
    git_context = _build_git_context()
    elapsed = time.monotonic() - start
    system_health = _build_system_health(state_dir, db_ok)

    return {
        "schema_version": "2.0",
        "generated_at": _now_iso(),
        "staleness_seconds": 0,
        "terminals": terminals,
        "queues": queues,
        "tracks": tracks,
        "pr_progress": pr_progress,
        "open_items": open_items,
        "active_work": active_work,
        "recent_receipts": recent_receipts,
        "git_context": git_context,
        "system_health": system_health,
        "_build_seconds": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Brief format adapter (backward-compat t0_brief.json)
# ---------------------------------------------------------------------------

def _state_to_brief(state: Dict[str, Any]) -> Dict[str, Any]:
    """Convert schema 2.0 state to schema 1.0 t0_brief.json format."""
    terminals: Dict[str, Any] = {}
    for tid, info in (state.get("terminals") or {}).items():
        terminals[tid] = {
            "status": info.get("status", "unknown"),
            "track": info.get("track", "?"),
            "ready": bool(info.get("ready", False)),
            "last_update": info.get("last_update", "never"),
            "current_task": info.get("current_dispatch"),
            "source": info.get("source", "t0_state"),
            "status_age_seconds": info.get("status_age_seconds"),
        }

    queues = state.get("queues") or {}
    pr_raw = state.get("pr_progress") or {}
    oi = state.get("open_items") or {}
    sh = state.get("system_health") or {}

    return {
        "timestamp": state.get("generated_at", _now_iso()),
        "version": "1.0",
        "terminals": terminals,
        "queues": {
            "pending": queues.get("pending_count", 0),
            "active": queues.get("active_count", 0),
            "completed_last_hour": queues.get("completed_last_hour", 0),
            "conflicts": queues.get("conflict_count", 0),
        },
        "tracks": state.get("tracks", {}),
        "active_work": state.get("active_work", []),
        "recent_receipts": state.get("recent_receipts", []),
        "blockers": [],
        "open_items_summary": {
            "open_count": oi.get("open_count", 0),
            "blocker_count": oi.get("blocker_count", 0),
            "top_blockers": (oi.get("top_blockers") or [])[:2],
        },
        "pr_progress": {
            "total": pr_raw.get("total", 0),
            "completed": pr_raw.get("completed", 0),
            "in_progress": pr_raw.get("in_progress", []),
            "completion_percentage": pr_raw.get("completion_pct", 0),
        },
        "system_health": {
            "status": sh.get("status", "unknown"),
            "uptime_seconds": sh.get("uptime_seconds", 0),
            "warnings": [],
            "db_initialized": sh.get("db_initialized", False),
        },
    }


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def _write_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp_str, str(path))
    except Exception:
        try:
            os.unlink(tmp_str)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build unified T0 state JSON from all runtime sources."
    )
    parser.add_argument(
        "--output",
        default=str(_STATE_DIR / "t0_state.json"),
        help="Output path (default: .vnx-data/state/t0_state.json)",
    )
    parser.add_argument(
        "--format",
        choices=["state", "brief"],
        default="state",
        help="Output format: 'state' (schema 2.0) or 'brief' (backward-compat 1.0)",
    )
    args = parser.parse_args()

    try:
        state = build_t0_state(_STATE_DIR, _DISPATCH_DIR)
        payload = _state_to_brief(state) if args.format == "brief" else state
        _write_atomic(Path(args.output), payload)
    except Exception:
        pass  # SessionStart hook must never block session

    return 0  # Always exit 0


if __name__ == "__main__":
    sys.exit(main())
