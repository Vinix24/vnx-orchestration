#!/usr/bin/env python3
"""
VNX Dashboard Read Model — Projection layer for the Coding Operator Dashboard.

Implements the read-model views from the Dashboard Contract
(docs/core/140_DASHBOARD_READ_MODEL_CONTRACT.md):

  - ProjectsView (§2.2, §3.1): cross-project overview with attention model
  - SessionView (§2.3): per-project session detail with PR progress
  - TerminalView (§2.4): per-terminal health with heartbeat and output recency
  - OpenItemsView (§2.5): per-project open items with severity summary
  - AggregateOpenItemsView (§2.6, §7): cross-project open item aggregation

Every view response includes a freshness envelope (§3.4) and handles
degraded, stale, missing, and contradictory source states (§5.1).

The dashboard UI queries ONLY these views — never raw files (§6.1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Freshness envelope (§3.4)
# ---------------------------------------------------------------------------

FRESH_THRESHOLD = 60
AGING_THRESHOLD = 300
REGISTRY_AGING_THRESHOLD = 86400  # 24h — registry files change infrequently


@dataclass
class FreshnessEnvelope:
    """Wraps every read-model response with source freshness tracking."""
    view: str
    queried_at: str
    source_freshness: Dict[str, Optional[str]]
    staleness_seconds: float
    degraded: bool
    degraded_reasons: List[str] = field(default_factory=list)
    data: Any = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "view": self.view,
            "queried_at": self.queried_at,
            "source_freshness": self.source_freshness,
            "staleness_seconds": round(self.staleness_seconds, 1),
            "degraded": self.degraded,
            "data": self.data,
        }
        if self.degraded_reasons:
            result["degraded_reasons"] = self.degraded_reasons
        return result


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _file_mtime_iso(path: Path) -> Optional[str]:
    """Return file mtime as ISO string, or None if file doesn't exist."""
    try:
        mt = path.stat().st_mtime
        return datetime.fromtimestamp(mt, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return None


def _staleness(mtime_iso: Optional[str], now: datetime) -> Optional[float]:
    """Return age in seconds from an ISO timestamp, or None."""
    if not mtime_iso:
        return None
    try:
        ts = datetime.fromisoformat(mtime_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds()
    except (ValueError, AttributeError, TypeError):
        return None


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    """Load JSON file, returning None if missing or invalid."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _load_yaml(path: Path) -> Optional[Dict[str, Any]]:
    """Load YAML file, returning None if missing, invalid, or yaml unavailable."""
    if yaml is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except (OSError, Exception):
        return None


def _compute_freshness(
    sources: Dict[str, Optional[str]],
    now: datetime,
    *,
    threshold_overrides: Optional[Dict[str, float]] = None,
) -> tuple[float, bool, List[str]]:
    """Compute max staleness and degraded state from source freshness map.

    Returns (staleness_seconds, degraded, reasons).
    """
    max_staleness = 0.0
    degraded = False
    reasons: List[str] = []
    overrides = threshold_overrides or {}

    for name, mtime in sources.items():
        if mtime is None:
            degraded = True
            reasons.append(f"{name}: unavailable")
            continue
        age = _staleness(mtime, now)
        if age is None:
            degraded = True
            reasons.append(f"{name}: unparseable timestamp")
            continue
        if age > max_staleness:
            max_staleness = age
        threshold = overrides.get(name, AGING_THRESHOLD)
        if age > threshold:
            degraded = True
            reasons.append(f"{name}: stale ({age:.0f}s)")

    return max_staleness, degraded, reasons


# ---------------------------------------------------------------------------
# Project Registry (§3.2)
# ---------------------------------------------------------------------------

DEFAULT_REGISTRY_PATH = Path.home() / ".vnx" / "projects.json"


def load_project_registry(
    registry_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load the project registry. Returns empty registry if missing."""
    path = registry_path or DEFAULT_REGISTRY_PATH
    data = _load_json(path)
    if data and "projects" in data:
        return data
    return {"schema_version": 1, "projects": []}


def register_project(
    name: str,
    project_path: str,
    *,
    vnx_data_dir: str = ".vnx-data",
    registry_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Register a project in the registry. Idempotent — skips if path exists."""
    path = registry_path or DEFAULT_REGISTRY_PATH
    data = load_project_registry(path)
    for proj in data["projects"]:
        if proj["path"] == project_path:
            return proj
    entry = {
        "name": name,
        "path": project_path,
        "vnx_data_dir": vnx_data_dir,
        "registered_at": _now_iso(),
        "active": True,
    }
    data["projects"].append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return entry


# ---------------------------------------------------------------------------
# TerminalView (§2.4)
# ---------------------------------------------------------------------------

class TerminalView:
    """Per-terminal health with heartbeat and output recency from runtime DB."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)

    def get_terminal(self, terminal_id: str) -> FreshnessEnvelope:
        now = _now_utc()
        db_path = self.state_dir / "runtime_coordination.db"
        db_mtime = _file_mtime_iso(db_path)

        ctx_path = self.state_dir / f"context_window_{terminal_id}.json"
        ctx_mtime = _file_mtime_iso(ctx_path)

        sources = {
            "runtime_coordination.db": db_mtime,
            f"context_window_{terminal_id}.json": ctx_mtime,
        }
        staleness, degraded, reasons = _compute_freshness(sources, now)

        terminal_data = self._read_terminal(terminal_id, ctx_path)
        if terminal_data is None:
            degraded = True
            reasons.append(f"terminal {terminal_id}: no data in runtime DB")
            terminal_data = {"terminal_id": terminal_id, "status": "unknown"}

        return FreshnessEnvelope(
            view="TerminalView",
            queried_at=_now_iso(),
            source_freshness=sources,
            staleness_seconds=staleness,
            degraded=degraded,
            degraded_reasons=reasons,
            data=terminal_data,
        )

    def get_all_terminals(self) -> FreshnessEnvelope:
        now = _now_utc()
        db_path = self.state_dir / "runtime_coordination.db"
        db_mtime = _file_mtime_iso(db_path)
        sources = {"runtime_coordination.db": db_mtime}
        staleness, degraded, reasons = _compute_freshness(sources, now)

        terminals = self._read_all_terminals()

        return FreshnessEnvelope(
            view="TerminalView",
            queried_at=_now_iso(),
            source_freshness=sources,
            staleness_seconds=staleness,
            degraded=degraded,
            degraded_reasons=reasons,
            data=terminals,
        )

    def _read_terminal(self, terminal_id: str, ctx_path: Path) -> Optional[Dict[str, Any]]:
        try:
            from runtime_coordination import get_connection
            from worker_state_manager import classify_heartbeat, is_terminal_worker_state
        except ImportError:
            return None

        try:
            with get_connection(self.state_dir) as conn:
                lease = conn.execute(
                    "SELECT * FROM terminal_leases WHERE terminal_id = ?",
                    (terminal_id,),
                ).fetchone()
                if lease is None:
                    return None
                lease = dict(lease)

                worker = conn.execute(
                    "SELECT * FROM worker_states WHERE terminal_id = ?",
                    (terminal_id,),
                ).fetchone()
                worker = dict(worker) if worker else None
        except Exception:
            return None

        now = _now_utc()
        hb_class = classify_heartbeat(lease.get("last_heartbeat_at"), now=now)

        result: Dict[str, Any] = {
            "terminal_id": terminal_id,
            "lease_state": lease["state"],
            "dispatch_id": lease.get("dispatch_id"),
            "heartbeat_classification": hb_class,
            "last_heartbeat_at": lease.get("last_heartbeat_at"),
        }

        if worker:
            result["worker_state"] = worker["state"]
            result["last_output_at"] = worker.get("last_output_at")
            result["state_entered_at"] = worker["state_entered_at"]
            result["stall_count"] = worker["stall_count"]
            result["blocked_reason"] = worker.get("blocked_reason")
            result["is_terminal"] = is_terminal_worker_state(worker["state"])
            # Derive display status from worker state
            result["status"] = worker["state"]
        else:
            result["worker_state"] = None
            result["status"] = "idle" if lease["state"] == "idle" else lease["state"]

        # Context pressure
        ctx_data = _load_json(ctx_path)
        if ctx_data and "remaining_pct" in ctx_data:
            result["context_pressure"] = {
                "remaining_pct": ctx_data["remaining_pct"],
                "warning": ctx_data["remaining_pct"] < 25,
            }

        return result

    def _read_all_terminals(self) -> List[Dict[str, Any]]:
        try:
            from runtime_coordination import get_connection
            from worker_state_manager import classify_heartbeat, is_terminal_worker_state
        except ImportError:
            return []

        try:
            with get_connection(self.state_dir) as conn:
                leases = conn.execute("SELECT * FROM terminal_leases ORDER BY terminal_id").fetchall()
                workers = conn.execute("SELECT * FROM worker_states").fetchall()
        except Exception:
            return []

        worker_map = {dict(w)["terminal_id"]: dict(w) for w in workers}
        now = _now_utc()
        terminals = []

        for lease_row in leases:
            lease = dict(lease_row)
            tid = lease["terminal_id"]
            worker = worker_map.get(tid)
            hb_class = classify_heartbeat(lease.get("last_heartbeat_at"), now=now)

            entry: Dict[str, Any] = {
                "terminal_id": tid,
                "lease_state": lease["state"],
                "dispatch_id": lease.get("dispatch_id"),
                "heartbeat_classification": hb_class,
                "last_heartbeat_at": lease.get("last_heartbeat_at"),
            }
            if worker:
                entry["worker_state"] = worker["state"]
                entry["last_output_at"] = worker.get("last_output_at")
                entry["stall_count"] = worker["stall_count"]
                entry["status"] = worker["state"]
                entry["is_terminal"] = is_terminal_worker_state(worker["state"])
            else:
                entry["worker_state"] = None
                entry["status"] = "idle" if lease["state"] == "idle" else lease["state"]

            terminals.append(entry)

        return terminals


# ---------------------------------------------------------------------------
# Shared open-item helpers
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"blocker": 0, "blocking": 0, "warn": 1, "warning": 1, "info": 2}


def _is_open(item: Dict[str, Any]) -> bool:
    return (item.get("status", "open") == "open"
            and item.get("resolved_at") is None
            and item.get("closed_at") is None)


def _filter_open_items(
    items: List[Dict[str, Any]],
    *,
    severity: Optional[str] = None,
    status: Optional[str] = None,
    include_resolved: bool = False,
) -> List[Dict[str, Any]]:
    if not include_resolved:
        items = [i for i in items if _is_open(i)]
    if severity:
        items = [i for i in items if i.get("severity") == severity]
    if status:
        items = [i for i in items if i.get("status") == status]
    return items


def _sort_by_severity(items: List[Dict[str, Any]]) -> None:
    items.sort(key=lambda x: (
        _SEVERITY_ORDER.get(x.get("severity", "info"), 3),
        x.get("created_at", ""),
    ))


def _enrich_age(items: List[Dict[str, Any]], now: datetime) -> None:
    for item in items:
        created = item.get("created_at") or item.get("detected_at")
        age = _staleness(created, now)
        item["age_seconds"] = round(age, 0) if age is not None else None


def _summarize_open_items(items: List[Dict[str, Any]]) -> Dict[str, int]:
    open_items = [i for i in items if _is_open(i)]
    return {
        "blocker_count": sum(1 for i in open_items if i.get("severity") in ("blocker", "blocking")),
        "warn_count": sum(1 for i in open_items if i.get("severity") in ("warn", "warning")),
        "info_count": sum(1 for i in open_items if i.get("severity") == "info"),
    }


# ---------------------------------------------------------------------------
# OpenItemsView (§2.5)
# ---------------------------------------------------------------------------

class OpenItemsView:
    """Per-project open items with severity summary."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)

    def get_items(
        self,
        *,
        severity: Optional[str] = None,
        status: Optional[str] = None,
        include_resolved: bool = False,
    ) -> FreshnessEnvelope:
        now = _now_utc()
        oi_path = self.state_dir / "open_items.json"
        oi_mtime = _file_mtime_iso(oi_path)
        sources = {"open_items.json": oi_mtime}
        staleness, degraded, reasons = _compute_freshness(sources, now)

        raw = _load_json(oi_path)
        if raw is None:
            degraded = True
            reasons.append("open_items.json: unavailable")
            return FreshnessEnvelope(
                view="OpenItemsView",
                queried_at=_now_iso(),
                source_freshness=sources,
                staleness_seconds=staleness,
                degraded=degraded,
                degraded_reasons=reasons,
                data={"items": [], "summary": {"blocker_count": 0, "warn_count": 0, "info_count": 0}},
            )

        items = _filter_open_items(
            raw.get("items", []),
            severity=severity, status=status,
            include_resolved=include_resolved,
        )
        _sort_by_severity(items)
        _enrich_age(items, now)
        summary = _summarize_open_items(items)

        return FreshnessEnvelope(
            view="OpenItemsView",
            queried_at=_now_iso(),
            source_freshness=sources,
            staleness_seconds=staleness,
            degraded=degraded,
            degraded_reasons=reasons,
            data={"items": items, "summary": summary},
        )


# ---------------------------------------------------------------------------
# AggregateOpenItemsView (§2.6, §7)
# ---------------------------------------------------------------------------

class AggregateOpenItemsView:
    """Cross-project open item aggregation from project registry."""

    def __init__(self, registry_path: Optional[Path] = None) -> None:
        self.registry_path = registry_path or DEFAULT_REGISTRY_PATH

    @staticmethod
    def _collect_project_items(
        proj: Dict[str, Any],
        now: datetime,
        all_items: List[Dict[str, Any]],
        per_project: Dict[str, Dict[str, Any]],
        sources: Dict[str, Optional[str]],
        degraded_reasons: List[str],
    ) -> None:
        proj_path = Path(proj["path"])
        data_dir = proj.get("vnx_data_dir", ".vnx-data")
        state_dir = proj_path / data_dir / "state"
        oi_path = state_dir / "open_items.json"
        source_key = f"{proj['name']}/open_items.json"
        sources[source_key] = _file_mtime_iso(oi_path)

        raw = _load_json(oi_path)
        if raw is None:
            per_project[proj["name"]] = {
                "status": "unavailable",
                "blocker_count": 0, "warn_count": 0, "info_count": 0,
            }
            degraded_reasons.append(f"{proj['name']}: open_items.json unavailable")
            return

        open_items = [i for i in raw.get("items", []) if _is_open(i)]
        _enrich_age(open_items, now)
        for item in open_items:
            item["_project_name"] = proj["name"]
        all_items.extend(open_items)

        per_project[proj["name"]] = {
            "status": "available",
            **_summarize_open_items(open_items),
        }

    def get_aggregate(
        self,
        *,
        project_filter: Optional[str] = None,
    ) -> FreshnessEnvelope:
        now = _now_utc()
        registry = load_project_registry(self.registry_path)
        projects = [p for p in registry.get("projects", []) if p.get("active", True)]

        if project_filter:
            projects = [p for p in projects if p["name"] == project_filter]

        all_items: List[Dict[str, Any]] = []
        per_project: Dict[str, Dict[str, Any]] = {}
        sources: Dict[str, Optional[str]] = {}
        degraded_reasons: List[str] = []

        for proj in projects:
            self._collect_project_items(
                proj, now, all_items, per_project, sources, degraded_reasons,
            )

        _sort_by_severity(all_items)
        staleness, degraded, fresh_reasons = _compute_freshness(sources, now)
        degraded_reasons.extend(fresh_reasons)

        total_summary = {
            "blocker_count": sum(p.get("blocker_count", 0) for p in per_project.values()),
            "warn_count": sum(p.get("warn_count", 0) for p in per_project.values()),
            "info_count": sum(p.get("info_count", 0) for p in per_project.values()),
        }

        return FreshnessEnvelope(
            view="AggregateOpenItemsView",
            queried_at=_now_iso(),
            source_freshness=sources,
            staleness_seconds=staleness,
            degraded=degraded or bool(degraded_reasons),
            degraded_reasons=degraded_reasons,
            data={
                "items": all_items,
                "per_project_subtotals": per_project,
                "total_summary": total_summary,
            },
        )


# ---------------------------------------------------------------------------
# SessionView (§2.3)
# ---------------------------------------------------------------------------


def _latest_terminal_activity(terminal_states: List[Dict[str, Any]]) -> Optional[str]:
    """Return the most recent heartbeat or output timestamp across terminals."""
    last_activity: Optional[str] = None
    for t in terminal_states:
        for ts_field in ("last_heartbeat_at", "last_output_at"):
            val = t.get(ts_field)
            if val and (last_activity is None or val > last_activity):
                last_activity = val
    return last_activity


class SessionView:
    """Per-project session detail with PR progress and terminal summary."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)

    def get_session(self) -> FreshnessEnvelope:
        now = _now_utc()
        queue_path = self.state_dir / "pr_queue_state.json"
        progress_path = self.state_dir / "progress_state.yaml"
        db_path = self.state_dir / "runtime_coordination.db"

        sources = {
            "pr_queue_state.json": _file_mtime_iso(queue_path),
            "progress_state.yaml": _file_mtime_iso(progress_path),
            "runtime_coordination.db": _file_mtime_iso(db_path),
        }
        staleness, degraded, reasons = _compute_freshness(sources, now)

        queue_data = _load_json(queue_path)
        progress_data = _load_yaml(progress_path)

        session: Dict[str, Any] = {}

        # Feature info from queue
        if queue_data:
            session["feature_name"] = queue_data.get("feature")
            prs = queue_data.get("prs", [])
            session["pr_progress"] = [
                {"id": p.get("id"), "title": p.get("title"), "status": p.get("status"),
                 "track": p.get("track"), "gate": p.get("gate")}
                for p in prs
            ]
        else:
            session["feature_name"] = None
            session["pr_progress"] = []
            degraded = True
            reasons.append("pr_queue_state.json: unavailable")

        # Track status from progress
        if progress_data and "tracks" in progress_data:
            tracks = progress_data["tracks"]
            session["track_status"] = {}
            for track_id, track_data in tracks.items():
                session["track_status"][track_id] = {
                    "current_gate": track_data.get("current_gate"),
                    "status": track_data.get("status"),
                    "active_dispatch_id": track_data.get("active_dispatch_id"),
                }
        else:
            session["track_status"] = {}

        # Terminal summary and activity
        tv = TerminalView(self.state_dir)
        terminal_env = tv.get_all_terminals()
        session["terminal_states"] = terminal_env.data if terminal_env.data else []
        session["last_activity"] = _latest_terminal_activity(session["terminal_states"])

        # Open item summary
        oi = OpenItemsView(self.state_dir)
        oi_env = oi.get_items()
        session["open_item_summary"] = oi_env.data.get("summary", {}) if oi_env.data else {}

        return FreshnessEnvelope(
            view="SessionView",
            queried_at=_now_iso(),
            source_freshness=sources,
            staleness_seconds=staleness,
            degraded=degraded,
            degraded_reasons=reasons,
            data=session,
        )


# ---------------------------------------------------------------------------
# ProjectsView (§2.2)
# ---------------------------------------------------------------------------

class ProjectsView:
    """Cross-project overview with attention model."""

    def __init__(self, registry_path: Optional[Path] = None) -> None:
        self.registry_path = registry_path or DEFAULT_REGISTRY_PATH

    def list_projects(self) -> FreshnessEnvelope:
        now = _now_utc()
        reg_mtime = _file_mtime_iso(self.registry_path)
        sources = {"projects.json": reg_mtime}

        registry = load_project_registry(self.registry_path)
        projects = registry.get("projects", [])

        results: List[Dict[str, Any]] = []

        for proj in projects:
            if not proj.get("active", True):
                continue
            proj_path = Path(proj["path"])
            data_dir = proj.get("vnx_data_dir", ".vnx-data")
            state_dir = proj_path / data_dir / "state"

            entry: Dict[str, Any] = {
                "name": proj["name"],
                "path": proj["path"],
                "registered_at": proj.get("registered_at"),
            }

            # Session active check
            session_profile = state_dir / "session_profile.json"
            entry["session_active"] = session_profile.exists()

            # Feature name
            queue = _load_json(state_dir / "pr_queue_state.json")
            entry["active_feature"] = queue.get("feature") if queue else None

            # Open items summary
            oi_raw = _load_json(state_dir / "open_items.json")
            if oi_raw:
                open_items = [i for i in oi_raw.get("items", [])
                              if i.get("status", "open") == "open"
                              and i.get("resolved_at") is None
                              and i.get("closed_at") is None]
                blocker_count = sum(1 for i in open_items if i.get("severity") in ("blocker", "blocking"))
                warn_count = sum(1 for i in open_items if i.get("severity") in ("warn", "warning"))
                entry["open_blocker_count"] = blocker_count
                entry["open_warn_count"] = warn_count
            else:
                entry["open_blocker_count"] = 0
                entry["open_warn_count"] = 0

            # Attention model (§7.3)
            entry["attention_level"] = self._compute_attention(entry)

            results.append(entry)

        staleness, degraded, reasons = _compute_freshness(
            sources, now,
            threshold_overrides={"projects.json": REGISTRY_AGING_THRESHOLD},
        )

        return FreshnessEnvelope(
            view="ProjectsView",
            queried_at=_now_iso(),
            source_freshness=sources,
            staleness_seconds=staleness,
            degraded=degraded,
            degraded_reasons=reasons,
            data=results,
        )

    @staticmethod
    def _compute_attention(proj: Dict[str, Any]) -> str:
        """Compute attention level per §7.3."""
        if proj.get("open_blocker_count", 0) > 0:
            return "critical"
        if proj.get("open_warn_count", 0) > 0:
            return "warning"
        return "clear"
