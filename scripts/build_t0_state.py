#!/usr/bin/env python3
"""Unified T0 state builder — produces a single JSON snapshot of all T0 state.

Replaces 8+ separate startup scripts (generate_t0_brief.sh, reconcile_queue_state.py,
open_items_manager.py digest, runtime_core_cli.py check-terminal x3,
reconcile_terminal_state.py). Called by SessionStart hook.

Usage:
    python3 scripts/build_t0_state.py [--output PATH] [--format {state,brief}]

Output schema: schema_version "2.2" (t0_state.json)
With --format brief: schema 1.0 backward-compat (t0_brief.json format)

Schema 2.2 changes (fabric-freshness):
  - Additive, backward-compatible: a top-level ``track_freshness`` marker
    {reconciled, tracks, drifted, drifted_tracks, reason, seconds, store}.
    Before projecting, the builder runs the advisory track reconciler against
    the central tracks SSOT and records declared-vs-derived drift here, so the
    SessionStart snapshot carries its own freshness. A compact form
    {reconciled, drifted, tracks, reason} is also surfaced in t0_index.json
    (the always-loaded cold-start file). Consumers that predate 2.2 ignore the
    new key (no removals/renames).

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
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path bootstrap — before importing any lib modules
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_LIB_DIR = _SCRIPT_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from vnx_paths import ensure_env, project_id_from_state_dir  # noqa: E402
try:
    from vnx_paths import resolve_central_data_dir  # noqa: E402
except ImportError:
    resolve_central_data_dir = None  # type: ignore[assignment]

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

try:
    import shadow_verifier as _shadow_verifier  # noqa: E402
    import shadow_logger as _shadow_logger  # noqa: E402
except ImportError:
    _shadow_verifier = None  # type: ignore[assignment]
    _shadow_logger = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _safe_json(path: Path) -> Optional[Dict[str, Any]]:
    """Load JSON from path. Returns None on OSError (absent file).
    Raises json.JSONDecodeError if the file exists but contains malformed JSON (R7.3)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    data = json.loads(text)  # propagates JSONDecodeError to caller
    return data if isinstance(data, dict) else None


def _central_state_dir_for(state_dir: Path) -> Optional[Path]:
    """Return the central state dir for the current project_id, derived from state_dir.

    Phase 6 P3: resolves project_id from the explicit ``state_dir`` first
    (central hierarchy or nearby ``.vnx-project-id``), and falls back to
    ambient ``VNX_PROJECT_ID`` only when state_dir itself is not attributable.

    Returns None when:
    - VNX_USE_CENTRAL_DB != '1' (explicit opt-in required until P5 cutover)
    - resolve_central_data_dir is unavailable
    - no project_id can be derived from state_dir and no ambient project_id exists
    - central state dir does not exist on filesystem
    - central == primary (P5 cutover guard — skip double-read)
    """
    if os.environ.get("VNX_USE_CENTRAL_DB") != "1":
        return None
    if resolve_central_data_dir is None:
        return None
    project_id = project_id_from_state_dir(state_dir) or os.environ.get("VNX_PROJECT_ID", "").strip()
    if not project_id:
        return None
    try:
        central_base = resolve_central_data_dir(project_id)
        central_state = central_base / "state"
        if not central_state.exists():
            return None
        if central_state.resolve() == state_dir.resolve():
            return None
        return central_state
    except Exception:
        return None


def _central_qi_db_for_project(project_id: str) -> Optional[Path]:
    """Return central quality_intelligence.db path for a project_id, or None."""
    if not project_id or resolve_central_data_dir is None:
        return None
    try:
        db_path = resolve_central_data_dir(project_id) / "state" / "quality_intelligence.db"
        return db_path if db_path.exists() else None
    except Exception:
        return None


def _shadow_log(cmp: Any, project_id: str, read_site: str) -> None:
    """Write comparison divergences to shadow ledger if any."""
    if _shadow_logger is None or not cmp.divergences:
        return
    _shadow_logger.write_comparison_result(cmp, project_id, read_site)


def _count_md(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    try:
        return sum(1 for f in directory.iterdir() if f.is_file() and f.suffix == ".md")
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# DB health classification (R6.1)
# ---------------------------------------------------------------------------

_PREMIGRATION_OPS_MSGS: frozenset = frozenset({"no such table", "no such column"})


def _classify_db_error(exc: Exception) -> str:
    """Classify a sqlite3 exception for health signaling.

    Returns 'premigration' for expected missing-table/column (fallback ok),
    'degraded' for SQLITE_BUSY/LOCKED or other transient OperationalError,
    'failed' for malformed / disk-error DatabaseError.
    """
    msg = str(exc).lower()
    if any(pat in msg for pat in _PREMIGRATION_OPS_MSGS):
        return "premigration"
    if isinstance(exc, sqlite3.OperationalError):
        return "degraded"
    return "failed"


def _probe_db_health(db_path: Path) -> str:
    """Quick read-only probe to classify a SQLite file's accessibility (R6.1).

    Returns 'healthy' when absent (pre-migration) or when PRAGMA integrity_check passes.
    Returns 'degraded' for SQLITE_BUSY/LOCKED; 'failed' for malformed / disk errors.
    Non-pre-migration OperationalErrors are NOT silently swallowed as legacy fallbacks.
    Uses PRAGMA integrity_check so page-level corruption and garbage files are detected —
    SELECT 1 is computed purely in-memory and cannot detect a malformed file.
    """
    if not db_path.exists():
        return "healthy"
    try:
        conn = sqlite3.connect(str(db_path), timeout=1)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                return "failed"
        finally:
            conn.close()
        return "healthy"
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        return _classify_db_error(exc)


# ---------------------------------------------------------------------------
# Staleness helper (R6.4) — consumers call this at read time
# ---------------------------------------------------------------------------


def compute_staleness_seconds(
    state: Dict[str, Any],
    now: Optional[datetime] = None,
) -> float:
    """Compute real staleness from a state dict's generated_at timestamp (R6.4).

    Consumers MUST call this at read time rather than relying on any persisted
    staleness_seconds field (which was always written as 0 at build time).
    Returns 0.0 if generated_at is absent or unparseable.
    """
    ts_raw = str(state.get("generated_at") or "").strip()
    if not ts_raw:
        return 0.0
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        actual_now = now if now is not None else _now_utc()
        return max(0.0, (actual_now - ts).total_seconds())
    except (ValueError, AttributeError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Step 1: Schema init (absorbed from runtime_coordination_init.py)
# ---------------------------------------------------------------------------

def _init_and_check_db(state_dir: Path) -> bool:
    """Idempotent schema init. Returns True if DB is operational."""
    try:
        from coordination_db import db_path_from_state_dir, init_schema
        init_schema(state_dir)
        # PR-6.5d: auto-apply any pending numbered migrations (e.g. 0020 pool tables)
        # before downstream readers touch the DB. Best-effort: a migration error
        # must not block SessionStart, but surface at WARNING for operator visibility.
        try:
            from migrations.auto_apply import auto_apply
            auto_apply(db_path_from_state_dir(state_dir))
        except Exception as e:
            log.warning("migration auto_apply failed: %s", e)
        return True
    except (ImportError, OSError, sqlite3.OperationalError) as e:
        log.debug("coordination_db init failed, falling back: %s", e)
    # Fallback: check if DB already exists and has tables
    db_path = state_dir / "runtime_coordination.db"
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(str(db_path)) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "terminal_leases" not in tables:
                return False
    except Exception:
        return False
    # PR-6.5d fallback: DB exists but coordination_db init failed (e.g. locked).
    # Still attempt auto_apply with the known path so pending migrations run.
    try:
        from migrations.auto_apply import auto_apply
        auto_apply(db_path)
    except Exception as e:
        log.warning("migration auto_apply (fallback path) failed: %s", e)
    return True


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
    # Phase 6 P3: prefer central receipts when available (derived from state_dir)
    _central = _central_state_dir_for(state_dir)
    receipts_path = (_central if _central is not None else state_dir) / "t0_receipts.ndjson"
    # Wave 2a: filter central receipts by project_id to prevent cross-project contamination.
    _receipts_project_id = (
        (project_id_from_state_dir(state_dir) or os.environ.get("VNX_PROJECT_ID", "").strip())
        if _central is not None
        else None
    )
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
                # Skip receipts from other projects; receipts without project_id pass (backward compat).
                if _receipts_project_id:
                    receipt_pid = (e.get("project_id") or "").strip()
                    if receipt_pid and receipt_pid != _receipts_project_id:
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
                except ValueError as e:
                    log.debug("Malformed receipt timestamp: %s", e)
        except OSError as e:
            log.debug("Could not read receipts file %s: %s", receipts_path, e)

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
    except ImportError as e:
        log.debug("yaml not installed, skipping progress_state.yaml: %s", e)
    else:
        try:
            with open(progress_path, encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            log.debug("Failed to load progress_state.yaml: %s", e)

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
# Canonical tracks (DB-backed, tenant-scoped) — R3.2 / ADR-007
# ---------------------------------------------------------------------------
#
# ADR-007 binding: the canonical `tracks` / `track_open_items` tables are
# multi-tenant, keyed by composite PRIMARY KEY (track_id, project_id). EVERY
# read here MUST carry a `WHERE project_id = ?` predicate. A tenant-less SELECT
# would merge rows across tenants — two tenants sharing a track_id would
# overwrite/collide (codex F11 / opus #11). On an unavailable identity we return
# a documented DEGRADED fallback (no rows + flag) rather than risk a
# cross-tenant leak; we never SELECT canonical track rows without the predicate.

_CANONICAL_TRACKS_SQL = (
    "SELECT track_id, title, phase, next_up, sort_order, priority, "
    "pr_ref, phase_changed_at, completed_at "
    "FROM tracks WHERE project_id = ? "
    "ORDER BY next_up DESC, sort_order ASC, track_id ASC"
)

_CANONICAL_TRACK_OI_SQL = (
    "SELECT track_id, oi_id, link_type, link_source, linked_at "
    "FROM track_open_items WHERE project_id = ? "
    "ORDER BY linked_at DESC"
)


def _tracks_db_degraded(
    reason: str, *, health: str, tenant_unavailable: bool
) -> Dict[str, Any]:
    """Documented DEGRADED fallback for canonical tracks (R3.2).

    NEVER carries track rows — this guarantees no cross-tenant leak when the
    identity is unavailable or the DB is locked/malformed.
    """
    return {
        "available": False,
        "health": health,
        "tenant_unavailable": tenant_unavailable,
        "reason": reason,
        "project_id": None,
        "tracks": [],
        "open_items": [],
    }


def _query_canonical_scoped(db_path: Path, sql: str, project_id: str) -> List[Dict[str, Any]]:
    """Run a tenant-scoped read-only query; project_id is ALWAYS bound (ADR-007)."""
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, (project_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _build_tracks_from_db(state_dir: Path, project_id: str) -> Dict[str, Any]:
    """Tenant-scoped canonical tracks/track_open_items reader (R3.2, ADR-007).

    Returns ``available=True`` with tenant-scoped rows when the identity
    resolves and the DB is healthy. On UNAVAILABLE identity (no derivable
    project_id) → DEGRADED fallback (no rows, ``tenant_unavailable=True``),
    never another tenant's rows. On a locked/malformed DB → degraded/failed
    (no rows). A pre-migration DB (no ``tracks`` table yet) is a healthy empty
    result, not a tenant problem.
    """
    pid = (project_id or "").strip()
    if not pid:
        return _tracks_db_degraded(
            "tenant_identity_unavailable", health="degraded", tenant_unavailable=True
        )

    db_path = state_dir / "runtime_coordination.db"
    healthy_empty = {
        "available": True, "health": "healthy", "tenant_unavailable": False,
        "reason": None, "project_id": pid, "tracks": [], "open_items": [],
    }
    if not db_path.exists():
        return healthy_empty
    try:
        tracks = _query_canonical_scoped(db_path, _CANONICAL_TRACKS_SQL, pid)
        open_items = _query_canonical_scoped(db_path, _CANONICAL_TRACK_OI_SQL, pid)
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        cls = _classify_db_error(exc)
        if cls == "premigration":
            return {**healthy_empty, "reason": "premigration"}
        return _tracks_db_degraded(f"db_{cls}", health=cls, tenant_unavailable=False)

    return {**healthy_empty, "tracks": tracks, "open_items": open_items}


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

# Wave 1 shadow — _collect_open_items is the 3-state dispatcher; the original
# _build_open_items logic lives in _collect_open_items_per_project.

_OPEN_ITEMS_SQL_TEMPLATE = "open_items_digest.json"


def _collect_open_items_per_project(project_id: str, state_dir: Path) -> Dict[str, Any]:
    digest_path = state_dir / "open_items_digest.json"
    try:
        data = _safe_json(digest_path) if digest_path.exists() else None
    except json.JSONDecodeError:
        data = None
    if not data:
        return {"open_count": 0, "blocker_count": 0, "top_blockers": []}
    summary = data.get("summary") or {}
    return {
        "open_count": int(summary.get("open_count") or 0),
        "blocker_count": int(summary.get("blocker_count") or 0),
        "top_blockers": (data.get("top_blockers") or [])[:3],
    }


def _collect_open_items_central(project_id: str) -> Dict[str, Any]:
    if not project_id or resolve_central_data_dir is None:
        return {"open_count": 0, "blocker_count": 0, "top_blockers": []}
    try:
        central_state = resolve_central_data_dir(project_id) / "state"
        return _collect_open_items_per_project(project_id, central_state)
    except Exception:
        return {"open_count": 0, "blocker_count": 0, "top_blockers": []}


def _collect_open_items(project_id: str, state_dir: Path) -> Dict[str, Any]:
    flag = os.environ.get("VNX_USE_CENTRAL_DB", "")
    if flag == "":
        return _collect_open_items_per_project(project_id, state_dir)
    if flag == "1":
        return _collect_open_items_central(project_id)
    # flag == "shadow": read both, compare via metric 4; metric 1 skipped because
    # open_items_digest.json rows carry no project_id field to scope-check against.
    legacy_result = _collect_open_items_per_project(project_id, state_dir)
    central_result = _collect_open_items_central(project_id)
    if _shadow_verifier is not None:
        cmp = _shadow_verifier.compare(
            [legacy_result],
            [central_result],
            project_id=project_id,
            read_site="build_t0_state._collect_open_items",
            sql_template=_OPEN_ITEMS_SQL_TEMPLATE,
            metric_id=4,
            table="open_items",
        )
        _shadow_log(cmp, project_id, "build_t0_state._collect_open_items")
    return legacy_result


# ---------------------------------------------------------------------------
# Quality digest (reads t0_quality_digest.json)
# ---------------------------------------------------------------------------

def _build_quality_digest(state_dir: Path) -> Dict[str, Any]:
    _empty: Dict[str, Any] = {
        "operational_defects": 0,
        "prompt_tuning_items": 0,
        "governance_health_items": 0,
        "total_items": 0,
        "critical_high_count": 0,
        "generated_at": None,
    }
    digest_path = state_dir / "t0_quality_digest.json"
    if not digest_path.exists():
        return _empty
    try:
        data = _safe_json(digest_path)
    except json.JSONDecodeError:
        return _empty
    if not data:
        return _empty
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
# Recent dispatches (direct query of dispatch_metadata) — Wave 1 shadow read
# ---------------------------------------------------------------------------

_RECENT_DISPATCHES_SQL = (
    "SELECT dispatch_id, terminal, track, role, gate, priority, pr_id, "
    "dispatched_at, completed_at, outcome_status, provider, model "
    "FROM dispatch_metadata "
    "ORDER BY dispatched_at DESC "
    "LIMIT 50"
)

_RECENT_DISPATCHES_CENTRAL_SQL = (
    "SELECT dispatch_id, terminal, track, role, gate, priority, pr_id, "
    "dispatched_at, completed_at, outcome_status, provider, model "
    "FROM dispatch_metadata "
    "WHERE project_id = ? "
    "ORDER BY dispatched_at DESC "
    "LIMIT 50"
)

_RECENT_DISPATCHES_SQL_NO_PROVIDER = (
    "SELECT dispatch_id, terminal, track, role, gate, priority, pr_id, "
    "dispatched_at, completed_at, outcome_status "
    "FROM dispatch_metadata "
    "ORDER BY dispatched_at DESC "
    "LIMIT 50"
)

_RECENT_DISPATCHES_CENTRAL_SQL_NO_PROVIDER = (
    "SELECT dispatch_id, terminal, track, role, gate, priority, pr_id, "
    "dispatched_at, completed_at, outcome_status "
    "FROM dispatch_metadata "
    "WHERE project_id = ? "
    "ORDER BY dispatched_at DESC "
    "LIMIT 50"
)


def _query_qi_db(db_path: Path, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Execute a read-only query against a quality_intelligence.db and return dicts."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def _dm_available_columns(db_path: Path) -> frozenset:
    """Return frozenset of column names in dispatch_metadata (empty if DB absent/error)."""
    if not db_path.exists():
        return frozenset()
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            rows = conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()
            return frozenset(r[1] for r in rows)
        finally:
            conn.close()
    except Exception:
        return frozenset()


def _collect_recent_dispatches_per_project(
    project_id: str, state_dir: Path
) -> List[Dict[str, Any]]:
    db_path = state_dir / "quality_intelligence.db"
    cols = _dm_available_columns(db_path)
    provider_part = ", provider" if "provider" in cols else ""
    model_part = ", model" if "model" in cols else ""
    sql = (
        "SELECT dispatch_id, terminal, track, role, gate, priority, pr_id, "
        f"dispatched_at, completed_at, outcome_status{provider_part}{model_part} "
        "FROM dispatch_metadata "
        "ORDER BY dispatched_at DESC "
        "LIMIT 50"
    )
    return _query_qi_db(db_path, sql)


def _collect_recent_dispatches_central(project_id: str) -> List[Dict[str, Any]]:
    db_path = _central_qi_db_for_project(project_id)
    if db_path is None:
        return []
    cols = _dm_available_columns(db_path)
    provider_part = ", provider" if "provider" in cols else ""
    model_part = ", model" if "model" in cols else ""
    sql = (
        "SELECT dispatch_id, terminal, track, role, gate, priority, pr_id, "
        f"dispatched_at, completed_at, outcome_status{provider_part}{model_part} "
        "FROM dispatch_metadata "
        "WHERE project_id = ? "
        "ORDER BY dispatched_at DESC "
        "LIMIT 50"
    )
    return _query_qi_db(db_path, sql, (project_id,))


def _collect_recent_dispatches(
    project_id: str, state_dir: Path
) -> List[Dict[str, Any]]:
    flag = os.environ.get("VNX_USE_CENTRAL_DB", "")
    if flag == "":
        return _collect_recent_dispatches_per_project(project_id, state_dir)
    if flag == "1":
        return _collect_recent_dispatches_central(project_id)
    # flag == "shadow"
    legacy_result = _collect_recent_dispatches_per_project(project_id, state_dir)
    central_result = _collect_recent_dispatches_central(project_id)
    if _shadow_verifier is not None:
        cmp = _shadow_verifier.compare(
            legacy_result,
            central_result,
            project_id=project_id,
            read_site="build_t0_state._collect_recent_dispatches",
            sql_template=_RECENT_DISPATCHES_SQL,
            metric_id=4,
            table="dispatch_metadata",
        )
        _shadow_log(cmp, project_id, "build_t0_state._collect_recent_dispatches")
    return legacy_result


# ---------------------------------------------------------------------------
# Intelligence brief (success_patterns + antipatterns) — Wave 1 shadow read
# ---------------------------------------------------------------------------

_INTELLIGENCE_BRIEF_SQL = (
    "SELECT id, pattern_type, category, title, description, "
    "confidence_score "
    "FROM success_patterns "
    "ORDER BY confidence_score DESC "
    "LIMIT 10"
)

_INTELLIGENCE_BRIEF_CENTRAL_SQL = (
    "SELECT id, pattern_type, category, title, description, "
    "confidence_score "
    "FROM success_patterns "
    "WHERE project_id = ? "
    "ORDER BY confidence_score DESC "
    "LIMIT 10"
)


def _collect_intelligence_brief_per_project(
    project_id: str, state_dir: Path
) -> List[Dict[str, Any]]:
    return _query_qi_db(state_dir / "quality_intelligence.db", _INTELLIGENCE_BRIEF_SQL)


def _collect_intelligence_brief_central(project_id: str) -> List[Dict[str, Any]]:
    db_path = _central_qi_db_for_project(project_id)
    if db_path is None:
        return []
    return _query_qi_db(db_path, _INTELLIGENCE_BRIEF_CENTRAL_SQL, (project_id,))


def _collect_intelligence_brief(
    project_id: str, state_dir: Path
) -> List[Dict[str, Any]]:
    flag = os.environ.get("VNX_USE_CENTRAL_DB", "")
    if flag == "":
        return _collect_intelligence_brief_per_project(project_id, state_dir)
    if flag == "1":
        return _collect_intelligence_brief_central(project_id)
    # flag == "shadow"
    legacy_result = _collect_intelligence_brief_per_project(project_id, state_dir)
    central_result = _collect_intelligence_brief_central(project_id)
    if _shadow_verifier is not None:
        cmp = _shadow_verifier.compare(
            legacy_result,
            central_result,
            project_id=project_id,
            read_site="build_t0_state._collect_intelligence_brief",
            sql_template=_INTELLIGENCE_BRIEF_SQL,
            metric_id=3,
        )
        _shadow_log(cmp, project_id, "build_t0_state._collect_intelligence_brief")
    return legacy_result


# ---------------------------------------------------------------------------
# Dispatch insights (from DispatchParameterTracker) — Wave 1 shadow-wrapped
# ---------------------------------------------------------------------------

# Wave 1 shadow — _collect_dispatch_insights is the 3-state dispatcher; the
# original _build_dispatch_insights logic is in _collect_dispatch_insights_per_project.

_DISPATCH_INSIGHTS_SQL_TEMPLATE = "dispatch_experiments"


def _collect_dispatch_insights_per_project(
    project_id: str, state_dir: Optional[Path] = None
) -> Dict[str, Any]:
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


def _collect_dispatch_insights_central(project_id: str) -> Dict[str, Any]:
    if not project_id or resolve_central_data_dir is None:
        return {"available": False, "insights": [], "experiment_count": 0}
    try:
        central_state = resolve_central_data_dir(project_id) / "state"
        return _collect_dispatch_insights_per_project(project_id, central_state)
    except Exception:
        return {"available": False, "insights": [], "experiment_count": 0}


def _collect_dispatch_insights(
    project_id: str, state_dir: Optional[Path] = None
) -> Dict[str, Any]:
    flag = os.environ.get("VNX_USE_CENTRAL_DB", "")
    if flag == "":
        return _collect_dispatch_insights_per_project(project_id, state_dir)
    if flag == "1":
        return _collect_dispatch_insights_central(project_id)
    # flag == "shadow"
    legacy_result = _collect_dispatch_insights_per_project(project_id, state_dir)
    central_result = _collect_dispatch_insights_central(project_id)
    if _shadow_verifier is not None:
        cmp = _shadow_verifier.compare(
            [legacy_result],
            [central_result],
            project_id=project_id,
            read_site="build_t0_state._collect_dispatch_insights",
            sql_template=_DISPATCH_INSIGHTS_SQL_TEMPLATE,
            metric_id=4,
            table="dispatch_experiments",
        )
        _shadow_log(cmp, project_id, "build_t0_state._collect_dispatch_insights")
    return legacy_result


# ---------------------------------------------------------------------------
# Active work (scans dispatches/active/) — R6.2 + R6.3
# ---------------------------------------------------------------------------


def _read_dir_dispatch(subdir: Path) -> tuple[Dict[str, Any], bool]:
    """Read one directory-form active dispatch (active/<id>/manifest.json).

    Returns (item, has_error). R6.2: corrupt manifest yields a placeholder
    with artifact_error so the dispatch is NOT silently dropped.
    """
    dispatch_id = subdir.name
    try:
        started_at: Optional[str] = datetime.fromtimestamp(
            subdir.stat().st_mtime, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
    except OSError:
        started_at = None
    try:
        data = _safe_json(subdir / "manifest.json")
        if data is None:
            # Missing file or non-object JSON — flag as artifact error (Warning 3)
            return {
                "dispatch_id": dispatch_id,
                "track": None, "gate": None, "started_at": started_at,
                "artifact_error": "manifest missing or non-object",
            }, True
        return {
            "dispatch_id": dispatch_id,
            "track": data.get("track"),
            "gate": data.get("gate"),
            "started_at": started_at,
        }, False
    except json.JSONDecodeError as exc:
        return {
            "dispatch_id": dispatch_id,
            "track": None, "gate": None, "started_at": started_at,
            "artifact_error": str(exc),
        }, True


def _read_md_dispatch(md_file: Path) -> tuple[Dict[str, Any], bool]:
    """Read one legacy .md active dispatch. Returns (item, has_error)."""
    dispatch_id = md_file.stem
    try:
        started_at = datetime.fromtimestamp(
            md_file.stat().st_mtime, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
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
        return {
            "dispatch_id": dispatch_id,
            "track": track, "gate": gate, "started_at": started_at,
        }, False
    except Exception as exc:
        return {
            "dispatch_id": dispatch_id,
            "track": None, "gate": None, "started_at": None,
            "artifact_error": str(exc),
        }, True


def _build_active_work(
    dispatch_dir: Path,
) -> tuple[List[Dict[str, Any]], bool]:
    """Scan active dispatches from directory and legacy .md forms.

    Returns (items, has_artifact_errors). R6.2+R6.3.
    """
    active_dir = dispatch_dir / "active"
    if not active_dir.is_dir():
        return [], False

    items: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    has_errors = False

    # Directory-form: active/<dispatch_id>/manifest.json
    try:
        for subdir in sorted(d for d in active_dir.iterdir() if d.is_dir()):
            item, err = _read_dir_dispatch(subdir)
            items.append(item)
            if err:
                has_errors = True
            seen_ids.add(subdir.name)
    except OSError as exc:
        log.debug("Failed to enumerate active dispatch dirs in %s: %s", active_dir, exc)
        has_errors = True  # R6.2: unreadable dir is a failure, not a silent skip

    # Legacy .md form — de-dup against directory form (R6.3)
    try:
        for md_file in sorted(active_dir.glob("*.md")):
            if md_file.stem in seen_ids:
                continue
            item, err = _read_md_dispatch(md_file)
            items.append(item)
            if err:
                has_errors = True
            seen_ids.add(md_file.stem)
    except OSError as exc:
        log.debug("Failed to enumerate active dispatches in %s: %s", active_dir, exc)
        has_errors = True  # R6.2: unreadable dir is a failure, not a silent skip

    return items[:5], has_errors


# ---------------------------------------------------------------------------
# Recent receipts (last N lines from t0_receipts.ndjson)
# ---------------------------------------------------------------------------

_STATUS_PRIORITY: Dict[str, int] = {
    "done": 0, "success": 0, "completed": 0, "pass": 0,
    "failed": 1, "failure": 1, "timeout": 1, "blocked": 1,
    "running": 2, "queued": 2, "requested": 2,
    "not_configured": 3, "not_executable": 3,
    "unknown": 9, "": 9, None: 9,  # type: ignore[misc]
}


def _infer_next_action(r: Dict[str, Any]) -> str:
    s = (r.get("status") or "").lower()
    if s in ("done", "success"):
        return "review" if r.get("pr_id") or r.get("pr") else "merge_ready"
    if s in ("failed", "failure"):
        return "fix_needed"
    if s in ("requested", "queued", "running"):
        return "wait"
    return "verify"


def _build_recent_receipts(
    state_dir: Path,
    project_id: str = "",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Top-N most recent receipts (any status), with T0-view filter.

    Filters by project_id when provided; accepts receipts with no project_id
    for backward compat. Returns T0-relevant fields only — token_usage and
    contract_hash are intentionally excluded to keep the snapshot lean.
    """
    # Phase 6 P3: prefer central receipts when available
    _central = _central_state_dir_for(state_dir)
    receipts_path = (_central if _central is not None else state_dir) / "t0_receipts.ndjson"
    if not receipts_path.exists():
        return []

    try:
        lines = receipts_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []

    best_by_dispatch: Dict[str, Dict[str, Any]] = {}
    no_dispatch_recs: List[Dict[str, Any]] = []

    for line in lines[-2000:]:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        # Skip internal bookkeeping events — T0 wants worker completion signals
        if r.get("event_type") == "state_mutation":
            continue
        # Project-id filter: accept matching project OR no project_id (backward compat)
        pid = (r.get("project_id") or "").strip()
        if project_id and pid and pid != project_id:
            continue
        view: Dict[str, Any] = {
            "timestamp": r.get("timestamp"),
            "dispatch_id": r.get("dispatch_id"),
            "event_type": r.get("event_type") or r.get("event"),
            "status": r.get("status"),
            "terminal": r.get("terminal"),
            "commit_hash": r.get("commit_hash") or r.get("commit"),
            "pr_id": r.get("pr_id") or r.get("pr"),
            "report_evidence_path": r.get("report_path") or r.get("evidence"),
            "next_action": _infer_next_action(r),
        }
        did = (view.get("dispatch_id") or "").strip()
        if not did or did in ("?", "unknown"):
            no_dispatch_recs.append(view)
            continue
        cur_best = best_by_dispatch.get(did)
        if cur_best is None:
            best_by_dispatch[did] = view
            continue
        new_prio = _STATUS_PRIORITY.get(view.get("status"), 9)
        cur_prio = _STATUS_PRIORITY.get(cur_best.get("status"), 9)
        if new_prio < cur_prio:
            best_by_dispatch[did] = view
        elif new_prio == cur_prio:
            new_terminal_known = bool(view.get("terminal") and view.get("terminal") != "unknown")
            cur_terminal_known = bool(cur_best.get("terminal") and cur_best.get("terminal") != "unknown")
            if new_terminal_known and not cur_terminal_known:
                best_by_dispatch[did] = view

    recs = list(best_by_dispatch.values()) + no_dispatch_recs
    return sorted(recs, key=lambda x: str(x.get("timestamp") or ""), reverse=True)[:limit]


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

def _build_system_health(
    state_dir: Path,
    db_initialized: bool,
    *,
    build_degraded: bool = False,
    db_health: str = "healthy",
) -> Dict[str, Any]:
    uptime_seconds = 0
    panes_path = state_dir / "panes.json"
    if panes_path.exists():
        try:
            uptime_seconds = int(time.time() - panes_path.stat().st_mtime)
        except OSError as e:
            log.debug("Could not stat panes.json for uptime: %s", e)

    # R6.1: DB health (locked/malformed) takes precedence over other signals.
    # R6.2: artifact errors (corrupt manifest) flag build as degraded.
    if db_health in ("failed", "degraded"):
        status = db_health
    elif build_degraded:
        status = "degraded"
    elif (
        not (state_dir / "terminal_state.json").exists()
        and not (state_dir / "t0_receipts.ndjson").exists()
    ):
        status = "degraded"
    else:
        status = "healthy"

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
# Strategic state (Phase 2 W-state-5: surface strategy/ folder under t0_state)
# ---------------------------------------------------------------------------


def _resolve_strategy_dir(state_dir: Path) -> Path:
    """Resolve strategy/ folder. ``<data>/strategy`` by convention.

    When ``state_dir`` follows the conventional ``<data>/state`` layout, the
    strategy folder lives next to it under ``<data>/strategy``. We prefer the
    sibling-of-state path so tests passing an arbitrary tmp ``state_dir`` see a
    co-located strategy/ rather than the global ``_DATA_DIR`` fallback.
    """
    candidate = state_dir.parent / "strategy"
    if candidate.exists():
        return candidate
    return _DATA_DIR / "strategy"


def _decision_to_light_dict(d: Any) -> Dict[str, Any]:
    rationale = getattr(d, "rationale", "") or ""
    return {
        "decision_id": getattr(d, "decision_id", ""),
        "scope": getattr(d, "scope", ""),
        "ts": getattr(d, "ts", ""),
        "rationale": rationale[:200],
    }


def _decision_to_full_dict(d: Any) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "decision_id": getattr(d, "decision_id", ""),
        "scope": getattr(d, "scope", ""),
        "ts": getattr(d, "ts", ""),
        "rationale": getattr(d, "rationale", "") or "",
    }
    supersedes = getattr(d, "supersedes", None)
    if supersedes:
        rec["supersedes"] = supersedes
    evidence_path = getattr(d, "evidence_path", None)
    if evidence_path:
        rec["evidence_path"] = evidence_path
    return rec


def _doc_entry_to_dict(e: Any) -> Dict[str, Any]:
    return {
        "id": getattr(e, "id", ""),
        "path": getattr(e, "path", ""),
        "version": getattr(e, "version", ""),
        "status": getattr(e, "status", ""),
        "supersedes": getattr(e, "supersedes", None),
        "title": getattr(e, "title", ""),
    }


def _strategy_unavailable(reason: str) -> Dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "current_focus": None,
        "next_actionable_wave_id": None,
        "recent_decisions": [],
        "available_indexes": [],
    }


def _build_strategic_state(
    state_dir: Path, *, strategy_dir: Optional[Path] = None
) -> Dict[str, Any]:
    """Light strategic-state surface for ``t0_state.json.strategic_state``.

    Defensive: a missing strategy/ folder, malformed roadmap.yaml, or any
    unexpected error yields ``available=false`` with no crash. Budget: must
    not regress total build_t0_state runtime by more than 200ms (W-state-5).

    Returned shape (when ``available=true``):
      - ``current_focus``: ``{wave_id, title, phase_id}`` or None
      - ``next_actionable_wave_id``: wave_id of the first ready wave, or None
      - ``recent_decisions``: up to 5 entries, oldest-first, with truncated
        ``rationale`` (≤200 chars) for cheap ingest
      - ``available_indexes``: presence summary for prd/adr indexes
    """
    target = strategy_dir if strategy_dir is not None else _resolve_strategy_dir(state_dir)
    if not target.exists() or not target.is_dir():
        return _strategy_unavailable("strategy/ folder not found")

    try:
        from strategy.loaders import load_strategy_for_boot
        from strategy.roadmap import next_actionable_wave
    except Exception as e:
        return _strategy_unavailable(f"strategy module import failed: {type(e).__name__}")

    try:
        loaded = load_strategy_for_boot(target, decisions_n=5)
    except Exception as e:
        return _strategy_unavailable(f"load failed: {type(e).__name__}")

    roadmap = loaded.get("roadmap")
    next_wave_id: Optional[str] = None
    current_focus: Optional[Dict[str, Any]] = None
    if roadmap is not None:
        try:
            nw = next_actionable_wave(roadmap)
        except Exception:
            nw = None
        if nw is not None:
            next_wave_id = nw.wave_id
            current_focus = {
                "wave_id": nw.wave_id,
                "title": nw.title,
                "phase_id": nw.phase_id,
            }
        else:
            for w in (roadmap.waves or []):
                if w.status == "in_progress":
                    current_focus = {
                        "wave_id": w.wave_id,
                        "title": w.title,
                        "phase_id": w.phase_id,
                    }
                    break

    recent_decisions_light = [
        _decision_to_light_dict(d) for d in (loaded.get("decisions") or [])
    ]

    available_indexes: List[Dict[str, Any]] = []
    prd = loaded.get("prd_index") or []
    adr = loaded.get("adr_index") or []
    if prd:
        available_indexes.append({"name": "prd_index", "count": len(prd)})
    if adr:
        available_indexes.append({"name": "adr_index", "count": len(adr)})

    return {
        "available": True,
        "strategy_dir": str(target),
        "current_focus": current_focus,
        "next_actionable_wave_id": next_wave_id,
        "recent_decisions": recent_decisions_light,
        "available_indexes": available_indexes,
    }


def _build_strategic_state_heavy(
    state_dir: Path, *, strategy_dir: Optional[Path] = None
) -> Dict[str, Any]:
    """Heavy strategic-state surface for ``t0_detail/strategic_state.json``.

    Includes the full roadmap, last 20 decisions, and full prd/adr index
    payloads. Defensive: same crash semantics as the light builder.
    """
    target = strategy_dir if strategy_dir is not None else _resolve_strategy_dir(state_dir)
    if not target.exists() or not target.is_dir():
        return {"available": False, "reason": "strategy/ folder not found"}

    try:
        from strategy.loaders import load_strategy_for_boot
        from strategy.roadmap import roadmap_to_dict
    except Exception as e:
        return {
            "available": False,
            "reason": f"strategy module import failed: {type(e).__name__}",
        }

    try:
        loaded = load_strategy_for_boot(target, decisions_n=20)
    except Exception as e:
        return {"available": False, "reason": f"load failed: {type(e).__name__}"}

    roadmap = loaded.get("roadmap")
    roadmap_dict: Optional[Dict[str, Any]] = None
    if roadmap is not None:
        try:
            roadmap_dict = roadmap_to_dict(roadmap)
        except Exception:
            roadmap_dict = None

    decisions_full = [
        _decision_to_full_dict(d) for d in (loaded.get("decisions") or [])
    ]
    prd_index = [_doc_entry_to_dict(e) for e in (loaded.get("prd_index") or [])]
    adr_index = [_doc_entry_to_dict(e) for e in (loaded.get("adr_index") or [])]

    return {
        "available": True,
        "strategy_dir": str(target),
        "roadmap": roadmap_dict,
        "decisions": decisions_full,
        "prd_index": prd_index,
        "adr_index": adr_index,
    }


# ---------------------------------------------------------------------------
# PR queue section helper (extracted for R7.1 — keeps build_t0_state ≤70 lines)
# ---------------------------------------------------------------------------

def _pytest_db_isolation_guard(state_dir: Path) -> None:
    """Refuse to read/mutate a DB when running under pytest without explicit isolation.

    Active whenever pytest is loaded (collection OR execution), so an
    import-time module-level call is also guarded. Two conditions must hold:
    1. VNX_DATA_DIR_EXPLICIT=1 must be set.
    2. The resolved state_dir must be under tempfile.gettempdir() and NOT
       under ~/.vnx-data.

    The second check prevents tests from passing the flag while still resolving
    to the real canonical data location — a false sense of isolation.

    Production is unaffected: pytest is never in sys.modules outside a test run.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") is None and "pytest" not in sys.modules:
        return
    if os.environ.get("VNX_DATA_DIR_EXPLICIT") != "1":
        raise RuntimeError(
            "[TEST ISOLATION GUARD] build_t0_state() called under pytest "
            "without VNX_DATA_DIR_EXPLICIT=1. This would read/mutate the live database. "
            "Ensure the _fsr_migration_module_isolation fixture is active (tests/conftest.py), "
            "or set VNX_DATA_DIR_EXPLICIT=1 and VNX_DATA_DIR=<tmp_path> in your test."
        )
    # Flag is set — validate the resolved state_dir is actually temp-owned.
    resolved = state_dir.resolve()
    tmp_root = Path(tempfile.gettempdir()).resolve()
    canonical = (Path.home() / ".vnx-data").resolve()
    _sep = os.sep
    under_tmp = (
        str(resolved) == str(tmp_root)
        or str(resolved).startswith(str(tmp_root) + _sep)
    )
    under_canonical = (
        str(resolved) == str(canonical)
        or str(resolved).startswith(str(canonical) + _sep)
    )
    if under_canonical or not under_tmp:
        raise RuntimeError(
            f"[TEST ISOLATION GUARD] VNX_DATA_DIR_EXPLICIT=1 is set but state_dir "
            f"'{resolved}' is NOT under the system temp directory ('{tmp_root}'). "
            "Setting the flag while pointing at the canonical ~/.vnx-data location is unsafe. "
            "Ensure state_dir is a pytest-managed tmp_path."
        )

def _build_pr_queue_section(state_dir: Path) -> Dict[str, Any]:
    """Build pr_queue section. Best-effort — never raises."""
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
        except Exception as e:
            log.warning("pr_queue_state build failed (best-effort): %s", e)
    return pr_queue


# ---------------------------------------------------------------------------
# Track freshness (fabric-freshness): reconcile derived_status before projecting
# ---------------------------------------------------------------------------

def _resolve_tracks_store(state_dir: Path, project_id: str) -> Path:
    """Resolve the store that actually holds the tracks for ``project_id``.

    The tracks/objectives SSOT is the central per-project store
    (``~/.vnx-data/<project>/state``). The repo-local ``.vnx-data/state`` is a
    degraded fallback that frequently has no ``tracks`` table at all. The
    ambient ``_STATE_DIR`` only points at central when ``VNX_DATA_DIR`` is set,
    so resolve central explicitly (env-independent) and fall back to
    ``state_dir`` when central cannot be resolved or has no DB yet.
    """
    if resolve_central_data_dir is not None and project_id:
        try:
            central_state = resolve_central_data_dir(project_id) / "state"
            if (central_state / "runtime_coordination.db").exists():
                return central_state
        except Exception as exc:
            log.debug("central tracks-store resolution failed: %s", exc)
    return state_dir


def _reconcile_tracks_fresh(
    state_dir: Path, project_id: str, *, store: Optional[Path] = None
) -> Dict[str, Any]:
    """Reconcile track ``derived_status`` BEFORE projection and return a
    freshness marker. Advisory-only and never raises.

    build_t0_state runs at EVERY SessionStart. Without this, a track whose PR
    merged (but whose declared phase was never advanced) stays silently stale
    until a human writes a handoff. Running the advisory reconciler here
    recomputes derived_status from dispatch/event state and surfaces any
    declared-vs-derived drift in the projection itself — so the SessionStart
    snapshot carries its own freshness instead of a hand-written handoff.

    Targets the CENTRAL tracks SSOT (see ``_resolve_tracks_store``): the reconcile
    therefore performs a per-track ``UPDATE tracks.derived_status`` (tenant-scoped,
    ADR-007-safe) on the central multi-tenant DB on every SessionStart. The
    writes use WAL + a 10s busy-timeout (``track_reconciler._get_conn``), so
    concurrent session starts serialize safely and contention degrades to an
    ``error:OperationalError`` marker rather than corrupting state. The pass is
    idempotent, so a partial batch (some tracks written, then a failure) is
    self-healing — the next SessionStart re-reconciles.

    Precondition: the ``derived_status`` column (migration 0028) is applied
    out-of-band by ``migrate_future_system.py`` — NOT by ``_init_and_check_db``
    (no ``apply_0028.py`` runner). On a store that has not run it, the reconcile
    degrades to a ``precondition_unmet`` marker (safe, never raises).
    """
    marker: Dict[str, Any] = {
        "reconciled": False,
        "tracks": 0,
        "drifted": 0,
        "drifted_tracks": [],
        "reason": None,
        "seconds": 0.0,
        "store": None,
    }
    pid = (project_id or "").strip()
    if not pid:
        marker["reason"] = "no_project_id"
        return marker

    # Use the caller-resolved store when given (guarantees coherence with the
    # canonical projection); otherwise resolve it here.
    store = store if store is not None else _resolve_tracks_store(state_dir, pid)
    marker["store"] = str(store)
    t0 = time.monotonic()
    try:
        if str(_LIB_DIR) not in sys.path:
            sys.path.insert(0, str(_LIB_DIR))
        from track_reconciler import reconcile_all_tracks

        results = reconcile_all_tracks(store, pid)
    except RuntimeError as exc:
        # derived_status column absent (migration 0028 not applied to this
        # store). Not an error — reconcile is N/A here.
        marker["reason"] = "precondition_unmet"
        log.debug("track reconcile skipped (precondition): %s", exc)
        return marker
    except Exception as exc:  # never break the hot path
        marker["reason"] = f"error:{type(exc).__name__}"
        log.warning("track reconcile failed (projection continues): %s", exc)
        return marker

    drifted = [r for r in results if r.get("drifted")]
    marker.update(
        reconciled=True,
        tracks=len(results),
        drifted=len(drifted),
        drifted_tracks=[
            {
                "track_id": r.get("track_id"),
                "declared_phase": r.get("declared_phase"),
                "derived_status": r.get("derived_status"),
            }
            for r in drifted
        ],
        seconds=round(time.monotonic() - t0, 3),
    )
    return marker


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_t0_state(
    state_dir: Path,
    dispatch_dir: Path,
) -> Dict[str, Any]:
    """Build the full T0 state document. Never raises — errors produce safe fallbacks.

    Raises RuntimeError under pytest when VNX_DATA_DIR_EXPLICIT=1 is not set
    (test isolation guard, R8.6 / PR-0).
    """
    _pytest_db_isolation_guard(state_dir)
    start = time.monotonic()

    project_id = (
        project_id_from_state_dir(state_dir)
        or os.environ.get("VNX_PROJECT_ID", "").strip()
    )
    db_ok = _init_and_check_db(state_dir)
    # R6.1: probe quality_intelligence.db; classify locked/malformed (not premigration)
    db_health = _probe_db_health(state_dir / "quality_intelligence.db")

    # Fabric-freshness: resolve the tracks store ONCE (the central per-project
    # SSOT, with local fallback) and use it for BOTH the reconcile AND the
    # canonical projection, so the snapshot is internally coherent — the
    # freshness marker and canonical_tracks never reflect different stores.
    # (Migration 0028 / the derived_status column is applied out-of-band by
    # migrate_future_system.py, NOT by _init_and_check_db; a store without it
    # degrades to a precondition_unmet marker.)
    tracks_store = _resolve_tracks_store(state_dir, project_id)
    track_freshness = _reconcile_tracks_fresh(state_dir, project_id, store=tracks_store)

    terminals = _build_terminals(state_dir)
    queues = _build_queues(dispatch_dir, state_dir)
    tracks = _build_tracks(state_dir)
    canonical_tracks = _build_tracks_from_db(tracks_store, project_id)  # R3.2/ADR-007: tenant-scoped (central SSOT)
    pr_progress = _build_pr_progress(dispatch_dir, state_dir)
    feature_state = _build_feature_state(state_dir=state_dir)
    open_items = _collect_open_items(project_id, state_dir)
    quality_digest = _build_quality_digest(state_dir)
    dispatch_insights = _collect_dispatch_insights(project_id, state_dir=state_dir)
    recent_dispatches = _collect_recent_dispatches(project_id, state_dir)
    intelligence_brief = _collect_intelligence_brief(project_id, state_dir)
    active_work, active_errors = _build_active_work(dispatch_dir)  # R6.2: track errors
    recent_receipts = _build_recent_receipts(state_dir, project_id=project_id, limit=20)
    register_events = _build_register_events(state_dir=state_dir)
    git_context = _build_git_context()
    pr_queue = _build_pr_queue_section(state_dir)  # R7.1: extracted helper
    strategic_state = _build_strategic_state(state_dir)
    strategic_state_heavy = _build_strategic_state_heavy(state_dir)
    elapsed = time.monotonic() - start
    system_health = _build_system_health(
        state_dir, db_ok, build_degraded=active_errors, db_health=db_health
    )

    return {
        "schema_version": "2.2",
        "generated_at": _now_iso(),
        # staleness_seconds removed (R6.4): call compute_staleness_seconds() at read time
        "terminals": terminals,
        "queues": queues,
        "tracks": tracks,
        "track_freshness": track_freshness,
        "canonical_tracks": canonical_tracks,
        "pr_progress": pr_progress,
        "feature_state": feature_state,
        "open_items": open_items,
        "quality_digest": quality_digest,
        "dispatch_insights": dispatch_insights,
        "recent_dispatches": recent_dispatches,
        "intelligence_brief": intelligence_brief,
        "active_work": active_work,
        "recent_receipts": recent_receipts,
        "dispatch_register_events": register_events,
        "git_context": git_context,
        "system_health": system_health,
        "pr_queue": pr_queue,
        "strategic_state": strategic_state,
        "_strategic_state_heavy": strategic_state_heavy,
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
    # Phase 2 W-state-5: heavy strategic_state lives in a private state key
    # (``_strategic_state_heavy``) so it is excluded from t0_state.json/the
    # brief output but still mirrored to t0_detail/strategic_state.json.
    "_strategic_state_heavy": "strategic_state",
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
        "track_freshness": _track_freshness_summary(state.get("track_freshness")),
        "last_rebuild_seconds": state.get("_build_seconds"),
    }


def _track_freshness_summary(tf: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact track-freshness marker for the always-loaded index (F3).

    The full marker (with per-track drift detail) lives in the state doc; the
    index carries only the cheap orientation fields so a cold-start session sees
    whether the projection is fresh and how many tracks drifted.
    """
    tf = tf or {}
    return {
        "reconciled": bool(tf.get("reconciled", False)),
        "drifted": tf.get("drifted", 0),
        "tracks": tf.get("tracks", 0),
        "reason": tf.get("reason"),
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
            except OSError as e:
                log.debug("Could not remove temp file %s during detail write cleanup: %s", tmp_str, e)
    return manifest


# ---------------------------------------------------------------------------
# GC: retention sweep for t0_detail snapshots (W-UX-4)
# ---------------------------------------------------------------------------

def _gc_t0_detail(detail_dir: Path) -> int:
    """Prune t0_detail/*.json files older than VNX_T0_DETAIL_RETENTION_DAYS days.

    Env-var VNX_T0_DETAIL_RETENTION_DAYS (default 14). Set to 0 to disable GC.
    Only touches *.json files inside detail_dir — never t0_state.json or t0_index.json.
    Idempotent: a second call after files are already pruned deletes nothing more.
    Returns the count of files deleted.
    """
    try:
        retention_days = int(os.environ.get("VNX_T0_DETAIL_RETENTION_DAYS", "14"))
    except (ValueError, TypeError):
        retention_days = 14

    if retention_days == 0:
        return 0

    if not detail_dir.is_dir():
        return 0

    cutoff = time.time() - retention_days * 86400
    deleted = 0
    try:
        for p in detail_dir.glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    deleted += 1
            except OSError as e:
                log.debug("GC: could not remove stale detail file %s: %s", p, e)
    except OSError as e:
        log.debug("GC: could not enumerate detail dir %s: %s", detail_dir, e)
    return deleted


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
        except OSError as e:
            log.debug("Could not remove temp file %s during atomic write cleanup: %s", tmp_str, e)
        raise


# ---------------------------------------------------------------------------
# CLI helpers (extracted for R7.1 — keeps main() ≤70 lines)
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build unified T0 state JSON from all runtime sources."
    )
    parser.add_argument(
        "--format", choices=["state", "brief"], default="state",
        help="Output format: 'state' (schema 2.0) or 'brief' (backward-compat 1.0)",
    )
    parser.add_argument(
        "--output", default=None,
        help=(
            "Output path (default: t0_state.json for --format state, "
            "t0_brief.json for --format brief)"
        ),
    )
    return parser.parse_args()


def _resolve_output_path(args: argparse.Namespace) -> Path:
    if args.output is not None:
        return Path(args.output)
    if args.format == "brief":
        return _STATE_DIR / "t0_brief.json"
    return _STATE_DIR / "t0_state.json"


def _write_all_state_outputs(
    state: Dict[str, Any], state_dir: Path, strategic_heavy: Any,
    *,
    project_root: Optional[Path] = None,
    skip_paths: Optional[set] = None,
) -> bool:
    """Write secondary outputs (index, detail, brief, status). All best-effort.

    Returns True if any core state-file write failed (caller should mark build degraded).
    Optional helper modules (build_project_status, build_feature_plan) are not
    counted as write failures when unavailable — only atomic state-file writes are.

    project_root: project root for FEATURE_PLAN.md resolution (defaults to _PROJECT_ROOT).
                  Tests pass an isolated tmp path to prevent touching the real file (Error 2).
    skip_paths: resolved Path objects to skip (prevents double-writing the primary output
                when --format brief is used; caller passes {output_path.resolve()}).
    """
    write_failed = False
    try:
        _write_atomic(state_dir / "t0_index.json", _build_t0_index(state))
    except Exception as exc:
        log.warning("Failed to write t0_index.json: %s", exc)
        write_failed = True
    try:
        if strategic_heavy is not None:
            state["_strategic_state_heavy"] = strategic_heavy
        _write_detail_files(state, state_dir / "t0_detail")
    except Exception as exc:
        log.warning("Failed to write t0_detail/ files: %s", exc)
        write_failed = True
    finally:
        state.pop("_strategic_state_heavy", None)
    try:
        _gc_t0_detail(state_dir / "t0_detail")
    except Exception as exc:
        log.debug("GC of t0_detail failed (non-critical): %s", exc)
    try:
        pqs = state.get("pr_queue")
        if pqs:
            _write_atomic(state_dir / "pr_queue_state.json", pqs)
    except Exception as exc:
        log.warning("Failed to write pr_queue_state.json: %s", exc)
        write_failed = True
    # Warning 4: guard against double-write when output_path == brief_path
    brief_path = state_dir / "t0_brief.json"
    if skip_paths is None or brief_path.resolve() not in skip_paths:
        try:
            _write_atomic(brief_path, _state_to_brief(state))
        except Exception as exc:
            log.warning("Failed to write t0_brief.json: %s", exc)
            write_failed = True
    effective_root = project_root if project_root is not None else _PROJECT_ROOT
    try:
        sys.path.insert(0, str(effective_root / "scripts"))
        from build_project_status import write_project_status
        write_project_status(state_dir)
    except Exception as exc:
        log.debug("build_project_status unavailable or failed (non-critical): %s", exc)
    # Error 2: derive FEATURE_PLAN.md path from effective_root so tests can isolate it
    try:
        from build_feature_plan import write_feature_plan
        write_feature_plan(effective_root / "FEATURE_PLAN.md", state_dir=state_dir)
    except Exception as exc:
        log.debug("build_feature_plan unavailable or failed (non-critical): %s", exc)
    return write_failed


def _emit_build_signal(output_path: Path, elapsed: float) -> None:
    """Emit state mutation event (best-effort)."""
    try:
        if str(_LIB_DIR) not in sys.path:
            sys.path.insert(0, str(_LIB_DIR))
        from state_mutation import emit_state_mutation
        size_bytes = output_path.stat().st_size if output_path.exists() else 0
        emit_state_mutation(
            output_path.name, trigger="auto_rebuild",
            rebuild_seconds=elapsed, size_bytes=size_bytes,
        )
    except Exception as e:
        log.warning("emit_state_mutation failed (non-critical): %s", e)


def _emit_health_beacon(
    output_path: Path, fmt: str, elapsed: float, succeeded: bool,
    track_freshness: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit HealthBeacon heartbeat (best-effort).

    Carries a compact track-freshness summary so reconcile health (e.g.
    ``reconciled=False`` with an ``error:*`` reason) is observable on the
    beacon, not only inside the projection. A benign skip
    (``no_project_id``/``precondition_unmet``) is reported but does NOT flip the
    beacon to ``fail`` — only a real build/write failure does.
    """
    details: Dict[str, Any] = {
        "format": fmt,
        "output": str(output_path),
        "elapsed_seconds": round(elapsed, 3),
    }
    if track_freshness is not None:
        details["track_freshness"] = {
            "reconciled": bool(track_freshness.get("reconciled", False)),
            "drifted": track_freshness.get("drifted", 0),
            "reason": track_freshness.get("reason"),
        }
    try:
        from health_beacon import HealthBeacon
        HealthBeacon(
            _DATA_DIR, "t0_state_builder", expected_interval_seconds=1800,
        ).heartbeat(
            status="ok" if succeeded else "fail",
            details=details,
        )
    except Exception as e:
        log.debug("HealthBeacon heartbeat failed (non-critical): %s", e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    args = _parse_args()
    output_path = _resolve_output_path(args)

    elapsed = 0.0
    _build_succeeded = False
    write_failed = False
    state: Optional[Dict[str, Any]] = None

    try:
        t_start = time.monotonic()
        state = build_t0_state(_STATE_DIR, _DISPATCH_DIR)
        _strategic_heavy = state.pop("_strategic_state_heavy", None)
        payload = _state_to_brief(state) if args.format == "brief" else state
        _write_atomic(output_path, payload)
        write_failed = _write_all_state_outputs(
            state, _STATE_DIR, _strategic_heavy,
            project_root=_PROJECT_ROOT,
            skip_paths={output_path.resolve()},
        )
        # Error 1: patch health + re-persist BEFORE the beacon fires so on-disk state is honest
        if write_failed:
            sh = state.get("system_health") or {}
            if sh.get("status") not in ("degraded", "failed"):
                state["system_health"] = {**sh, "status": "degraded"}
            try:
                re_payload = _state_to_brief(state) if args.format == "brief" else state
                _write_atomic(output_path, re_payload)
            except Exception as exc:
                log.warning("Failed to re-persist patched state after write errors: %s", exc)
            brief_path = _STATE_DIR / "t0_brief.json"
            if brief_path.resolve() != output_path.resolve():
                try:
                    _write_atomic(brief_path, _state_to_brief(state))
                except Exception as exc:
                    log.warning("Failed to re-write brief with degraded status: %s", exc)
        elapsed = time.monotonic() - t_start
        _build_succeeded = True
    except Exception as exc:
        log.warning("build_t0_state failed (SessionStart continues): %s", exc)

    if _build_succeeded:
        _emit_build_signal(output_path, elapsed)

    # Error 1: beacon status reflects both build success AND write health
    _emit_health_beacon(
        output_path, args.format, elapsed,
        _build_succeeded and not write_failed,
        track_freshness=(state or {}).get("track_freshness") if state else None,
    )

    if _build_succeeded and state is not None:
        sh_status = (state.get("system_health") or {}).get("status", "healthy")
        if sh_status in ("degraded", "failed"):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
