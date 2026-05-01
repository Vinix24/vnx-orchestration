#!/usr/bin/env python3
"""Unified T0 state builder — produces a single JSON snapshot of all T0 state.

Replaces 8+ separate startup scripts (generate_t0_brief.sh, reconcile_queue_state.py,
open_items_manager.py digest, runtime_core_cli.py check-terminal x3,
reconcile_terminal_state.py). Called by SessionStart hook.

Usage:
    python3 scripts/build_t0_state.py [--output PATH] [--format {state,brief}]

Output schema: schema_version "2.1" (t0_state.json)
With --format brief: schema 1.0 backward-compat (t0_brief.json format)

Schema 2.1 changes (W4E / OI-1199):
  - feature_state union-merges register-canonical aggregation with the
    FEATURE_PLAN.md fallback fields (current_pr/next_task/assigned_track/
    assigned_role/completion_pct/total_prs/completed_prs/feature_name) so
    consumers see a single stable shape regardless of register population.
  - feature_state aggregation accepts events identified by any single ID
    (dispatch_id OR pr_number OR feature_id), matching the writer
    contract in dispatch_register.append_event. Events with no
    identifying fields are still dropped.

Index/detail split (Sprint 4a):
  - t0_index.json: cheap always-loaded index (≤50 fields, ≤5KB)
  - t0_detail/<section>.json: full per-section files loaded on-demand
  - t0_state.json: DEPRECATED — kept for backward-compat; future consumers
    should read t0_index.json (orientation) + t0_detail/*.json (on-demand).
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

# Register events reader — used by _build_register_events and _build_feature_state
try:
    from dispatch_register import read_events as _dr_read_events
except ImportError:
    _dr_read_events = None

try:
    from pr_queue_state import build_pr_queue_state as _build_pqs
except ImportError:
    _build_pqs = None


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
# Feature state — register-canonical aggregation with FEATURE_PLAN.md fallback
# ---------------------------------------------------------------------------

def _read_register_events(state_dir: Optional[Path] = None) -> list[dict]:
    """Read all register events, honoring state_dir for test isolation."""
    if _dr_read_events is None:
        return []
    try:
        return _dr_read_events(state_dir=state_dir) or []
    except Exception:
        return []


_EVENT_TO_STATUS: Dict[str, str] = {
    "dispatch_completed": "completed",
    "dispatch_failed": "failed",
    "gate_failed": "failed",
    "dispatch_promoted": "active",
    "dispatch_started": "active",
    "gate_requested": "active",
    "gate_passed": "active",
    "dispatch_created": "queued",
    "pr_opened": "active",
    "pr_merged": "completed",
}


# Keys contributed by the FEATURE_PLAN.md fallback. The register-canonical path
# union-merges these into its own output so consumers see one stable shape
# regardless of whether dispatch_register.ndjson has been populated yet
# (W4E / OI-1199).
_FEATURE_PLAN_KEYS: tuple[str, ...] = (
    "feature_name",
    "current_pr",
    "next_task",
    "assigned_track",
    "assigned_role",
    "completion_pct",
    "total_prs",
    "completed_prs",
)


def _build_feature_state(state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Build feature_state from dispatch_register.ndjson (register-canonical).

    Aggregation contract:
    - Group events by dispatch_id when present; events lacking a dispatch_id
      but identified by pr_number or feature_id are aggregated directly into
      the PR/feature rollups (mirrors dispatch_register.append_event, which
      requires only one of dispatch_id/pr_number/feature_id).
    - Per-dispatch status: latest-event-wins (recency).
    - Per-PR/feature: most-recently-active source (dispatch record or
      dispatch-less event) wins.
    - Events with no identifying field at all are dropped.
    - FEATURE_PLAN.md fields (current_pr/next_task/assigned_track/
      assigned_role/completion_pct/total_prs/completed_prs/feature_name)
      are union-merged into the result so the schema is stable across the
      empty-register and populated-register code paths.

    Schema (schema_version 2.1):
      source: "dispatch_register" | "feature_plan_md" (primary origin)
      feature_plan_status: status reported by FEATURE_PLAN.md parser
        ("planned" | "in_progress" | "completed") — only present when
        register is populated; the top-level "status" key is reserved for
        the FEATURE_PLAN.md fallback to preserve backward compatibility
        with consumers that read it from the empty-register path.
      dispatches/pr_status/feature_status/register_event_count: only
        present when register is populated.
      current_pr/next_task/assigned_track/assigned_role/completion_pct/
        total_prs/completed_prs/feature_name: always present.

    Refs: synthesis 2026-04-28 §D Sprint 3 split 3/3, codex findings
    PR #276 r1+r2; W4E / OI-1199 schema split + any-ID filter.
    """
    register_events = _read_register_events(state_dir=state_dir)
    feature_plan_part = _build_feature_state_from_feature_plan()
    if not register_events:
        return feature_plan_part

    by_dispatch: Dict[str, list] = {}
    dispatchless_events: list[dict] = []
    for ev in register_events:
        did = (ev.get("dispatch_id") or "").strip()
        pr_number = ev.get("pr_number")
        feature_id = (ev.get("feature_id") or "").strip()
        if did:
            by_dispatch.setdefault(did, []).append(ev)
        elif pr_number is not None or feature_id:
            dispatchless_events.append(ev)
        # else: event lacks any identifying field — drop it.

    dispatch_records: Dict[str, Any] = {}
    for did, events in by_dispatch.items():
        events_sorted = sorted(events, key=lambda e: e.get("timestamp", ""))
        latest = events_sorted[-1]
        latest_event = latest.get("event", "")
        status = _EVENT_TO_STATUS.get(latest_event, "unknown")
        pr_number = next(
            (e.get("pr_number") for e in events if e.get("pr_number") is not None), None
        )
        feature_id = next((e.get("feature_id") for e in events if e.get("feature_id")), "")
        dispatch_records[did] = {
            "status": status,
            "latest_event": latest_event,
            "latest_event_ts": latest.get("timestamp", ""),
            "pr_number": pr_number,
            "feature_id": feature_id,
            "event_count": len(events),
        }

    by_pr: Dict[str, Any] = {}
    by_feature: Dict[str, Any] = {}
    for did, rec in dispatch_records.items():
        if rec["pr_number"] is not None:
            pr_key = str(rec["pr_number"])
            existing = by_pr.get(pr_key)
            if existing is None or rec["latest_event_ts"] > existing["latest_event_ts"]:
                by_pr[pr_key] = rec
        if rec["feature_id"]:
            f_key = rec["feature_id"]
            existing = by_feature.get(f_key)
            if existing is None or rec["latest_event_ts"] > existing["latest_event_ts"]:
                by_feature[f_key] = rec

    # Roll up dispatch-less events (pr_number-only or feature_id-only).
    # These come from writers that record PR-level lifecycle (pr_opened,
    # pr_merged) without an originating dispatch_id.
    for ev in dispatchless_events:
        latest_event = ev.get("event", "")
        ts = ev.get("timestamp", "")
        synthetic = {
            "status": _EVENT_TO_STATUS.get(latest_event, "unknown"),
            "latest_event": latest_event,
            "latest_event_ts": ts,
            "pr_number": ev.get("pr_number"),
            "feature_id": (ev.get("feature_id") or "").strip(),
            "event_count": 1,
            "dispatch_id": None,
        }
        if synthetic["pr_number"] is not None:
            pr_key = str(synthetic["pr_number"])
            existing = by_pr.get(pr_key)
            if existing is None or ts > existing["latest_event_ts"]:
                by_pr[pr_key] = synthetic
        if synthetic["feature_id"]:
            f_key = synthetic["feature_id"]
            existing = by_feature.get(f_key)
            if existing is None or ts > existing["latest_event_ts"]:
                by_feature[f_key] = synthetic

    # Union-merge: start with FEATURE_PLAN.md fields, then overlay register
    # aggregation. The FEATURE_PLAN "status" field is preserved as
    # "feature_plan_status" because the top-level key isn't currently used
    # in the register-canonical path and we don't want to introduce a name
    # collision that would change consumer behavior unexpectedly.
    merged: Dict[str, Any] = {}
    for key in _FEATURE_PLAN_KEYS:
        merged[key] = feature_plan_part.get(key)
    merged["feature_plan_status"] = feature_plan_part.get("status")
    merged["source"] = "dispatch_register"
    merged["dispatches"] = dispatch_records
    merged["pr_status"] = by_pr
    merged["feature_status"] = by_feature
    merged["register_event_count"] = len(register_events)
    return merged


def _build_feature_state_from_feature_plan() -> Dict[str, Any]:
    """FEATURE_PLAN.md parser — fallback when register is empty."""
    _empty: Dict[str, Any] = {
        "source": "feature_plan_md",
        "feature_name": None,
        "current_pr": None,
        "next_task": None,
        "assigned_track": None,
        "assigned_role": None,
        "completion_pct": 0,
        "total_prs": 0,
        "completed_prs": 0,
        "status": "planned",
    }
    feature_plan = _PROJECT_ROOT / "FEATURE_PLAN.md"
    if not feature_plan.exists():
        return _empty
    try:
        from feature_state_machine import parse_feature_plan
        state = parse_feature_plan(feature_plan)
        result = state.as_dict()
        result["source"] = "feature_plan_md"
        return result
    except Exception:
        return _empty


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

        blocked = [p.pr_id for p in result.prs if p.state == "blocked"]
        return {
            "feature_name": result.feature_name,
            "total": total,
            "completed": completed,
            "in_progress": in_progress,
            "completion_pct": pct,
            "has_blocking_drift": result.has_blocking_drift,
            "blocked": blocked,
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
# Quality digest (reads t0_quality_digest.json)
# ---------------------------------------------------------------------------

def _build_quality_digest(state_dir: Path) -> Dict[str, Any]:
    digest_path = state_dir / "t0_quality_digest.json"
    if not digest_path.exists():
        return {
            "operational_defects": 0,
            "prompt_tuning_items": 0,
            "governance_health_items": 0,
            "total_items": 0,
            "critical_high_count": 0,
            "generated_at": None,
        }
    data = _safe_json(digest_path)
    if not data:
        return {
            "operational_defects": 0,
            "prompt_tuning_items": 0,
            "governance_health_items": 0,
            "total_items": 0,
            "critical_high_count": 0,
            "generated_at": None,
        }
    summary = data.get("summary") or {}
    sections = summary.get("sections") or {}
    return {
        "operational_defects": int(sections.get("operational_defects") or 0),
        "prompt_tuning_items": int(sections.get("prompt_config_tuning") or 0),
        "governance_health_items": int(sections.get("governance_health") or 0),
        "total_items": int(summary.get("total_recommendations") or 0),
        "critical_high_count": int(summary.get("critical_or_high_count") or 0),
        "generated_at": data.get("run_at"),
    }


# ---------------------------------------------------------------------------
# Dispatch insights (from DispatchParameterTracker)
# ---------------------------------------------------------------------------

def _build_dispatch_insights(state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Return top 5 dispatch insights when >= 20 experiments exist."""
    _empty: Dict[str, Any] = {"available": False, "insights": [], "experiment_count": 0}
    actual_state_dir = state_dir if state_dir else _STATE_DIR
    try:
        from dispatch_parameter_tracker import DispatchParameterTracker
        tracker = DispatchParameterTracker(state_dir=actual_state_dir)
        stats = tracker.stats()
        if not stats.get("insights_available"):
            return {**_empty, "experiment_count": stats.get("completed", 0)}
        top = tracker.top_insights_for_t0(n=5)
        return {
            "available": True,
            "insights": top,
            "experiment_count": stats.get("completed", 0),
            "avg_cqs": stats.get("avg_cqs"),
            "success_rate": stats.get("success_rate"),
        }
    except Exception:
        return _empty


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
        raw_lines = receipts_path.read_bytes().splitlines()

        events: List[Dict[str, Any]] = []
        for raw_line in raw_lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                e = json.loads(line.decode("utf-8"))
                if e.get("event_type") == "state_mutation":
                    continue
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

        return events[-100:][-n:]
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
# Register events (dispatch_register.ndjson reader)
# ---------------------------------------------------------------------------

def _build_register_events(state_dir: Optional[Path] = None, limit: int = 50) -> list[dict]:
    """Last N register events (raw; for debugging)."""
    if _dr_read_events is None:
        return []
    try:
        events = _dr_read_events(state_dir=state_dir)
        return events[-limit:] if events else []
    except Exception:
        return []


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
    feature_state = _build_feature_state(state_dir=state_dir)
    open_items = _build_open_items(state_dir)
    quality_digest = _build_quality_digest(state_dir)
    dispatch_insights = _build_dispatch_insights(state_dir=state_dir)
    active_work = _build_active_work(dispatch_dir)
    recent_receipts = _build_recent_receipts(state_dir)
    register_events = _build_register_events(state_dir=state_dir)
    git_context = _build_git_context()
    pr_queue: Dict[str, Any] = {
        "schema": "pr_queue/1.0",
        "timestamp": _now_iso(),
        "open_prs": [],
        "merged_today": [],
        "queued_features": [],
    }
    if _build_pqs is not None:
        try:
            pr_queue = _build_pqs(state_dir)
        except Exception:
            pass
    elapsed = time.monotonic() - start
    system_health = _build_system_health(state_dir, db_ok)

    return {
        "schema_version": "2.1",
        "generated_at": _now_iso(),
        "staleness_seconds": 0,
        "terminals": terminals,
        "queues": queues,
        "tracks": tracks,
        "pr_progress": pr_progress,
        "feature_state": feature_state,
        "open_items": open_items,
        "quality_digest": quality_digest,
        "dispatch_insights": dispatch_insights,
        "active_work": active_work,
        "recent_receipts": recent_receipts,
        "dispatch_register_events": register_events,
        "git_context": git_context,
        "system_health": system_health,
        "pr_queue": pr_queue,
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
    active_work = state.get("active_work") or []

    blockers = (oi.get("top_blockers") or [])[:3]
    next_gates = [
        item["gate"]
        for item in active_work
        if item.get("gate")
    ]

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
        "active_work": active_work,
        "recent_receipts": state.get("recent_receipts", []),
        "blockers": blockers,
        "next_gates": next_gates,
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
            "blocked": pr_raw.get("blocked", []),
        },
        "system_health": {
            "status": sh.get("status", "unknown"),
            "uptime_seconds": sh.get("uptime_seconds", 0),
            "warnings": [],
            "db_initialized": sh.get("db_initialized", False),
        },
    }


# ---------------------------------------------------------------------------
# Index / detail split (Sprint 4a)
# ---------------------------------------------------------------------------

# Maps state-dict key → detail file stem (t0_detail/<stem>.json)
_DETAIL_SECTION_MAP: Dict[str, str] = {
    "feature_state": "feature_state",
    "quality_digest": "quality_digest",
    "open_items": "open_items",
    "dispatch_register_events": "dispatch_register",
    "active_chains": "active_chains",
    "intelligence": "intelligence",
}


def _build_t0_index(state: Dict[str, Any]) -> Dict[str, Any]:
    """Build the cheap, always-loaded index from full state dict.

    Guaranteed ≤50 top-level keys and ≤5KB serialized. Suitable for
    cold-start orientation without loading any heavy section data.
    """
    queues = state.get("queues") or {}
    open_items = state.get("open_items") or {}
    active_work = state.get("active_work") or []
    git_ctx = state.get("git_context") or {}

    last_commits: List[str] = git_ctx.get("last_5_commits") or []
    raw_head = last_commits[0].split()[0] if last_commits else ""

    return {
        "schema": "t0_index/1.0",
        "timestamp": state.get("generated_at", ""),
        "git_branch": git_ctx.get("branch", ""),
        "git_head": raw_head[:7],
        "terminals": {
            tid: {
                "status": t.get("status", ""),
                "lease_expires": t.get("lease_expires_at"),
            }
            for tid, t in (state.get("terminals") or {}).items()
        },
        "queue": {
            "pending": queues.get("pending_count", 0),
            "active": queues.get("active_count", 0),
            "open_prs": len((state.get("pr_progress") or {}).get("in_progress", [])),
            "blocking_open_items": open_items.get("blocker_count", 0),
        },
        "active_dispatches": [d.get("dispatch_id", "") for d in active_work],
        "recent_receipts": (state.get("recent_receipts") or [])[-3:],
        "health": state.get("system_health") or {},
        "last_rebuild_seconds": state.get("_build_seconds"),
    }


def _write_detail_files(state: Dict[str, Any], detail_dir: Path) -> Dict[str, str]:
    """Write per-section detail files atomically; return manifest of written paths."""
    detail_dir.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, str] = {}
    for state_key, file_stem in _DETAIL_SECTION_MAP.items():
        if state_key not in state:
            continue
        section_path = detail_dir / f"{file_stem}.json"
        fd, tmp_str = tempfile.mkstemp(
            prefix=section_path.name + ".tmp.", dir=str(detail_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state[state_key], fh, indent=2, default=str)
            os.replace(tmp_str, str(section_path))
            manifest[state_key] = str(section_path)
        except Exception:
            try:
                os.unlink(tmp_str)
            except Exception:
                pass
    return manifest


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
        "--format",
        choices=["state", "brief"],
        default="state",
        help="Output format: 'state' (schema 2.0) or 'brief' (backward-compat 1.0)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Output path (default: t0_state.json for --format state, "
            "t0_brief.json for --format brief)"
        ),
    )
    args = parser.parse_args()

    if args.output is None:
        if args.format == "brief":
            args.output = str(_STATE_DIR / "t0_brief.json")
        else:
            args.output = str(_STATE_DIR / "t0_state.json")

    output_path = Path(args.output)
    elapsed = 0.0
    _build_succeeded = False
    try:
        t_start = time.monotonic()
        state = build_t0_state(_STATE_DIR, _DISPATCH_DIR)
        payload = _state_to_brief(state) if args.format == "brief" else state
        _write_atomic(output_path, payload)
        # Write cheap index — always loaded for cold-start orientation (Sprint 4a)
        try:
            _write_atomic(_STATE_DIR / "t0_index.json", _build_t0_index(state))
        except Exception:
            pass  # best-effort — must not block SessionStart
        # Write per-section detail files — loaded on-demand (Sprint 4a)
        try:
            _write_detail_files(state, _STATE_DIR / "t0_detail")
        except Exception:
            pass  # best-effort — must not block SessionStart
        # Write pr_queue_state.json — replaces hand-maintained PR_QUEUE.md (Phase 2.1)
        try:
            pqs = state.get("pr_queue")
            if pqs:
                _write_atomic(_STATE_DIR / "pr_queue_state.json", pqs)
        except Exception:
            pass  # best-effort — must not block SessionStart
        # Regenerate t0_brief.json alongside t0_state.json — orchestration helpers
        # (receipt_processor_v4, intelligence_ack, t0_intelligence_aggregator) read
        # t0_brief.json directly and must stay in sync with the new state.
        try:
            brief_path = _STATE_DIR / "t0_brief.json"
            _write_atomic(brief_path, _state_to_brief(state))
        except Exception:
            pass  # best-effort — must not block SessionStart
        # Write human-readable cold-start orientation doc (Sprint 4b)
        try:
            sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
            from build_project_status import write_project_status
            write_project_status(_STATE_DIR)
        except Exception:
            pass  # best-effort
        # Regenerate FEATURE_PLAN.md from canonical state sources (Phase 2.2)
        try:
            from build_feature_plan import write_feature_plan
            write_feature_plan(_PROJECT_ROOT / "FEATURE_PLAN.md", state_dir=_STATE_DIR)
        except Exception:
            pass  # best-effort — must not block SessionStart
        elapsed = time.monotonic() - t_start
        _build_succeeded = True
    except Exception:
        pass  # SessionStart hook must never block session

    if _build_succeeded:
        try:
            if str(_LIB_DIR) not in sys.path:
                sys.path.insert(0, str(_LIB_DIR))
            from state_mutation import emit_state_mutation
            size_bytes = output_path.stat().st_size if output_path.exists() else 0
            emit_state_mutation(
                output_path.name,
                trigger="auto_rebuild",
                rebuild_seconds=elapsed,
                size_bytes=size_bytes,
            )
        except Exception:
            pass

    try:
        from health_beacon import HealthBeacon
        HealthBeacon(
            _DATA_DIR,
            "t0_state_builder",
            expected_interval_seconds=1800,
        ).heartbeat(
            status="ok" if _build_succeeded else "fail",
            details={
                "format": args.format,
                "output": str(output_path),
                "elapsed_seconds": round(elapsed, 3),
            },
        )
    except Exception:
        pass

    return 0  # Always exit 0


if __name__ == "__main__":
    sys.exit(main())
