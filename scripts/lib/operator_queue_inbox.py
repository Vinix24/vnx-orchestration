"""The operator queue view (ADR-076 decisions 5, 10 and 11): what is open, in which order, and when a push is due.

``show`` is always fresh: it runs every reader (``refresh``) and only then reads the open points, so a point whose
source shows the answer is never in the result. ``push_due`` decides and reserves inside one lock of the queue.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

try:
    import operator_queue as oq
    import operator_queue_ask
    import operator_queue_dream_analyzer
    import operator_queue_steering_verdict
    from project_root import resolve_central_data_dir
except ImportError:  # vnx-silent-except: imported as scripts.lib.operator_queue_inbox
    from scripts.lib import operator_queue as oq
    from scripts.lib import operator_queue_ask
    from scripts.lib import operator_queue_dream_analyzer
    from scripts.lib import operator_queue_steering_verdict
    from scripts.lib.project_root import resolve_central_data_dir

_MODULES = (operator_queue_dream_analyzer, operator_queue_steering_verdict, operator_queue_ask)
PUSH_MAX_POINTS = 5
PUSH_PROJECT_GAP_MIN = 30
# Names at the data-home root that are never a project id: SHARED_ROOT_DIRS of scripts/fabric_audit.py, the
# worktree layout of scripts/lib/vnx_worktree.py::_ensure_worktree_layout, and the report/panel stores.
NON_PROJECT_DIRS = frozenset({"state", "events", "locks", "logs", "pids", "database", "unified_reports", "receipts",
                              "profiles", "dispatches", "startup_presets", "business-panels"})


def _parse(value: Any) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):  # vnx-silent-except: absent or malformed is no time
        return None
    return ts if ts.tzinfo is not None else None


def _stamp(now: Any) -> str:
    if isinstance(now, datetime):
        return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return now or oq._now()


def _sdir(project_id: str, state_dir: Optional[Path]) -> Path:
    return Path(state_dir) if state_dir is not None else oq.state_dir_for(project_id)


def _entry(name: str, item: Any) -> Dict[str, Any]:
    if not isinstance(item, dict) or item.get("dekking") not in ("gelezen", "niet_gebonden", "unknown"):
        return {"reader": name, "bron": None, "dekking": "unknown", "reden": "reader result malformed", "unknown": []}
    ids = item.get("unknown")
    return {"reader": name, "bron": item.get("bron"), "dekking": item["dekking"], "reden": item.get("reden"),
            "unknown": [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []}


def refresh(project_id: str, *, data_root: Optional[Path] = None, now: Any = None,
            state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Run every reader. ``sources`` is the coverage per source; ``unknown_ids`` the points this run reported."""
    sources: List[Dict[str, Any]] = []
    for module in _MODULES:
        for name, reader in module.READERS:
            try:
                result = reader(project_id, data_root=data_root, now=now, state_dir=state_dir)
            except Exception as exc:  # a reader that raises is unknown, never a crash of the view
                sources.append({"reader": name, "bron": None, "dekking": "unknown", "reden": type(exc).__name__,
                                "unknown": []})
                continue
            sources += [_entry(name, i) for i in (result if isinstance(result, list) else [result])]
    return {"sources": sources, "unknown_ids": sorted({i for s in sources for i in s["unknown"]})}


def _ledger(project_id: str, sdir: Path) -> List[Dict[str, Any]]:
    return oq._ledger_lines(sdir, project_id)


def _presented(lines: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for line in lines:
        if line["kind"] == "presented":
            counts[line["point_id"]] = counts.get(line["point_id"], 0) + 1
    return counts


def _order(points: List[Dict[str, Any]], counts: Mapping[str, int]) -> List[Dict[str, Any]]:
    """Decision 10: never presented first, then fewest presented, then oldest asked_at, ties on point_id."""
    return sorted(points, key=lambda p: (counts.get(p["point_id"], 0), _parse(p["asked_at"]), p["point_id"]))


def _age(seconds: int) -> str:
    days, rest = divmod(max(seconds, 0), 86400)
    return f"{days}d {rest // 3600}h" if days else f"{rest // 3600}h {rest % 3600 // 60}m"


def _view(point: Dict[str, Any], counts: Mapping[str, int], blocks: Mapping[str, str], unknown_ids: set,
          bad_sources: set, now: datetime) -> Dict[str, Any]:
    pid = point["point_id"]
    seconds = int((now - _parse(point["asked_at"])).total_seconds())
    # The fresh run decides: the stored reason shows only for a point this run reported, or whose source is
    # still unknown (the dream and analyzer readers report a repeated reason once, not again).
    shown = point["unknown_reden"] if point["unknown_reden"] and (pid in unknown_ids or point["bron"] in bad_sources) else None
    return {"point_id": pid, "bron": point["bron"], "ref": point["ref"], "vraag": point["vraag"],
            "asked_at": point["asked_at"], "age_seconds": seconds, "age": _age(seconds),
            "presented": counts.get(pid, 0), "kopieerblok": blocks.get(pid, ""), "unknown_reden": shown}


def show(project_id: str, *, limit: int = 5, now: Any = None, state_dir: Optional[Path] = None,
         data_root: Optional[Path] = None) -> Dict[str, Any]:
    """Always fresh. ``{project_id, status, reason, coverage, open_total, points, more}``; closed points never."""
    sdir, stamp = _sdir(project_id, state_dir), _stamp(now)
    run = refresh(project_id, data_root=data_root, now=stamp, state_dir=sdir)
    out: Dict[str, Any] = {"project_id": project_id, "status": "ok", "reason": None, "coverage": run["sources"],
                           "open_total": 0, "points": [], "more": 0}
    state = oq.read_state(project_id, state_dir=sdir)
    if state["status"] != "ok":
        return {**out, "status": "unknown", "reason": state["reason"], "open_total": None}
    try:
        lines = _ledger(project_id, sdir)
    except oq.QueueUnreadable as exc:
        return {**out, "status": "unknown", "reason": str(exc), "open_total": None}
    blocks = {line["point_id"]: line.get("kopieerblok", "") for line in lines if line["kind"] == "opened"}
    opened = [p for p in state["points"].values() if p["state"] == "open"]
    bad = {s["bron"] for s in run["sources"] if s["dekking"] == "unknown" and s["bron"] is not None}
    ranked = _order(opened, _presented(lines))
    now_dt = _parse(stamp)
    counts, unknown_ids = _presented(lines), set(run["unknown_ids"])
    out["open_total"], out["more"] = len(ranked), max(len(ranked) - max(limit, 0), 0)
    out["points"] = [_view(p, counts, blocks, unknown_ids, bad, now_dt) for p in ranked[:max(limit, 0)]]
    return out


def present(project_id: str, point_ids: List[str], *, kanaal: str, sessie: str, now: Any = None,
            state_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """One ``presented`` line per shown point; raises QueueRefused (unknown channel or point) or QueueUnreadable."""
    stamp = _stamp(now)
    return [oq.append_line(project_id, "presented", {"point_id": pid, "kanaal": kanaal, "sessie": sessie},
                           now=stamp, state_dir=state_dir) for pid in point_ids]


def _interval_h() -> float:
    try:
        value = float(os.environ.get("VNX_INBOX_PUSH_INTERVAL_H", "4"))
    except ValueError:  # vnx-silent-except: a bad value is the default
        return 4.0
    return value if value > 0 else 4.0


def _chooser(now: datetime):
    def choose(state: Dict[str, Any]) -> List[str]:
        last: Dict[str, datetime] = {}
        newest: Optional[datetime] = None
        for line in state["lines"]:
            ts = _parse(line["ts"])
            if line["kind"] != "push_reserved" or ts is None:
                continue
            newest = ts if newest is None or ts > newest else newest
            for pid in line["point_ids"]:
                last[pid] = ts if pid not in last or ts > last[pid] else last[pid]
        if newest is not None and (now - newest).total_seconds() < PUSH_PROJECT_GAP_MIN * 60:
            return []
        gap = _interval_h() * 3600
        due = [p for p in state["points"].values() if p["state"] == "open"
               and (p["point_id"] not in last or (now - last[p["point_id"]]).total_seconds() >= gap)]
        return [p["point_id"] for p in _order(due, _presented(state["lines"]))[:PUSH_MAX_POINTS]]
    return choose


def push_due(project_id: str, *, sessie: str, now: Any = None, state_dir: Optional[Path] = None,
             data_root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Decision 10: the points a push is due for, after one ``push_reserved`` line was fsynced under the queue lock.
    A second call sees the reservation and returns nothing. QueueUnreadable (nothing written) on a bad ledger."""
    sdir, stamp = _sdir(project_id, state_dir), _stamp(now)
    refresh(project_id, data_root=data_root, now=stamp, state_dir=sdir)  # a closed point is never pushed
    ids = oq.reserve(project_id, _chooser(_parse(stamp)), sessie=sessie, now=stamp, state_dir=sdir)
    if not ids:
        return []
    state = oq.read_state(project_id, state_dir=sdir)
    blocks = {line["point_id"]: line.get("kopieerblok", "") for line in _ledger(project_id, sdir) if line["kind"] == "opened"}
    counts = _presented(_ledger(project_id, sdir))
    return [_view(state["points"][pid], counts, blocks, set(), set(), _parse(stamp)) for pid in ids]


def _registry_projects(registry_path: Optional[Path]) -> tuple:
    """(names, reason): the project names in ``~/.vnx/projects.json``; reason is set when the file is unreadable."""
    try:
        from dashboard_read_model import DEFAULT_REGISTRY_PATH, load_project_registry
    except ImportError:  # vnx-silent-except: imported as scripts.lib.operator_queue_inbox
        from scripts.lib.dashboard_read_model import DEFAULT_REGISTRY_PATH, load_project_registry
    path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    if not path.exists():
        return [], None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ok = isinstance(raw, dict) and isinstance(raw.get("projects"), list)
    except (OSError, ValueError):  # vnx-silent-except: reported as the reason below
        ok = False
    if not ok:
        return [], f"{path.name} onleesbaar of zonder projects-lijst"
    names = [e.get("name") for e in load_project_registry(path)["projects"] if isinstance(e, dict)]
    return [n for n in names if isinstance(n, str) and n.strip()], None


def discover(home: Path, registry_path: Optional[Path] = None) -> Dict[str, Any]:
    """Every directory under ``<home>/.vnx-data`` (not ``_``-prefixed, not in NON_PROJECT_DIRS) plus every registry project."""
    root, found = Path(home) / ".vnx-data", set()
    if root.is_dir():
        found = {d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(("_", "."))
                 and d.name not in NON_PROJECT_DIRS}
    names, reason = _registry_projects(registry_path)
    return {"projects": sorted(found | set(names)), "registry": {"dekking": "unknown" if reason else "gelezen", "reden": reason}}


def _coverage(view: Dict[str, Any]) -> Dict[str, Any]:
    bad = [s for s in view["coverage"] if s["dekking"] == "unknown"]
    if view["status"] != "ok":
        return {"dekking": "unknown", "reden": view["reason"]}
    if bad:
        return {"dekking": "unknown", "reden": "; ".join(f"bron {s['bron']}: {s['reden']}" for s in bad)}
    if any(s["dekking"] == "niet_gebonden" for s in view["coverage"]):
        return {"dekking": "niet_gebonden", "reden": None}
    return {"dekking": "gelezen", "reden": None}


def show_all(*, home: Optional[Path] = None, registry_path: Optional[Path] = None, limit: int = 5,
             now: Any = None) -> Dict[str, Any]:
    """Fleet view: ``{registry, projects: [{project_id, dekking, reden, open_total, points, more, sources}]}``."""
    home = Path(home) if home is not None else Path.home()
    found = discover(home, registry_path)
    projects = []
    for pid in found["projects"]:
        root = home / ".vnx-data" / pid
        if not (root / "state").is_dir():  # nothing to read, and a read must not create a store
            projects.append({"project_id": pid, "dekking": "unknown", "reden": "geen state/", "open_total": None,
                             "points": [], "more": 0, "sources": []})
            continue
        view = show(pid, limit=limit, now=now, state_dir=root / "state", data_root=root)
        projects.append({"project_id": pid, **_coverage(view), "open_total": view["open_total"],
                         "points": view["points"], "more": view["more"], "sources": view["coverage"]})
    return {"registry": found["registry"], "projects": projects}
