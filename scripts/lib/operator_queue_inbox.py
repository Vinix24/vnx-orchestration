"""The operator queue view (ADR-076 decisions 5, 10 and 11): what is open, in which order, and when a push is due.

``show`` is always fresh: it runs every reader (``refresh``) and only then reads the open points, so a point whose
source shows the answer is never in the result. ``push_due`` decides and reserves inside one lock of the queue.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

try:
    import operator_queue as oq
    import operator_queue_ask
    import operator_queue_dream_analyzer
    import operator_queue_steering_verdict
    from project_root import resolve_central_data_dir
    from dashboard_read_model import _load_json as _registry_json
    from fabric_audit import SHARED_ROOT_DIRS
except ImportError:  # vnx-silent-except: imported as scripts.lib.operator_queue_inbox
    from scripts.lib import operator_queue as oq
    from scripts.lib import operator_queue_ask
    from scripts.lib import operator_queue_dream_analyzer
    from scripts.lib import operator_queue_steering_verdict
    from scripts.lib.project_root import resolve_central_data_dir
    from scripts.lib.dashboard_read_model import _load_json as _registry_json
    from scripts.fabric_audit import SHARED_ROOT_DIRS

_MODULES = (operator_queue_dream_analyzer, operator_queue_steering_verdict, operator_queue_ask)
PUSH_MAX_POINTS = 5
PUSH_PROJECT_GAP_MIN = 30


_parse = oq.parse_ts


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


_presented, _order = oq.presented_counts, oq.order_points


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
    state = oq.rebuild_state(project_id, state_dir=sdir)  # the readers may have appended nothing: keep the summary current
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
    return 4.0


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


# Folders under <home>/.vnx-data/ that are not a project. Sources: SHARED_ROOT_DIRS (state, events, locks) from
# scripts/fabric_audit.py; the subdirs that vnx_worktree._ensure_worktree_layout creates (logs, pids, database,
# unified_reports, receipts, profiles, dispatches, startup_presets; state and locks are already above);
# business-panels (a panel store, not a project). Names starting with "_" are skipped separately.
NON_PROJECT_DIRS = frozenset(SHARED_ROOT_DIRS) | {
    "logs", "pids", "database", "unified_reports", "receipts", "profiles", "dispatches", "startup_presets",
    "business-panels"}


def _registry_projects(registry_path: Path) -> tuple:
    """(registry coverage, [project names]). Absent, unreadable or malformed is `unknown` with its reason."""
    try:
        present = registry_path.exists()
    except OSError as exc:  # vnx-silent-except: recorded as unknown registry coverage with the error as reason
        return {"dekking": "unknown", "reden": f"{registry_path}: registry unreadable: {type(exc).__name__}: {exc}"}, []
    if not present:
        return {"dekking": "unknown", "reden": f"{registry_path}: registry file absent"}, []
    data = _registry_json(registry_path)  # the loader load_project_registry uses; None = unreadable or invalid JSON
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list):
        return {"dekking": "unknown", "reden": f"{registry_path}: registry unreadable or has no projects list"}, []
    names = [p["name"] for p in projects if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"]]
    return {"dekking": "gelezen", "reden": None}, names


def _list_store(base: Path) -> tuple:
    """(store coverage, {name: None | reason}). A name maps to a reason when its own check failed; a failed
    listing is `unknown` with the reason and no names, never an empty successful listing."""
    try:
        if not base.is_dir():
            return {"dekking": "gelezen", "reden": None}, {}
        entries = list(base.iterdir())
    except OSError as exc:  # vnx-silent-except: recorded as unknown store coverage with the error as reason
        return {"dekking": "unknown", "reden": f"{base}: {type(exc).__name__}: {exc}"}, {}
    found: Dict[str, Optional[str]] = {}
    for entry in entries:
        try:
            if entry.is_dir():
                found[entry.name] = None
        except OSError as exc:  # vnx-silent-except: this entry is listed as its own unknown project with the reason
            found[entry.name] = f"{entry}: {type(exc).__name__}: {exc}"
    return {"dekking": "gelezen", "reden": None}, found


def discover_projects(*, home: Path, registry_path: Optional[Path] = None) -> Dict[str, Any]:
    """``{registry, store: {dekking, reden}, projects: [{project_id, in_store, in_registry, reden}]}``, sorted.

    ``in_store`` is None when the store (or this one entry) could not be read: unknown, never "not in the store"."""
    home = Path(home)
    registry, names = _registry_projects(Path(registry_path) if registry_path is not None else home / ".vnx" / "projects.json")
    store, stored = _list_store(home / ".vnx-data")
    keep = lambda n: not n.startswith("_") and n not in NON_PROJECT_DIRS  # skip non-project folders
    stored = {n: r for n, r in stored.items() if keep(n)}
    names = [n for n in names if keep(n)]
    unreadable = store["dekking"] != "gelezen"
    projects = []
    for n in sorted(set(stored) | set(names)):
        if unreadable:
            in_store, reden = None, f"store listing unreadable: {store['reden']}"
        elif n in stored:
            in_store, reden = (True, None) if stored[n] is None else (None, stored[n])
        else:
            in_store, reden = False, None
        projects.append({"project_id": n, "in_store": in_store, "in_registry": n in names, "reden": reden})
    return {"registry": registry, "store": store, "projects": projects}


REDEN_MAX = 200


def unknown_sources(coverage: Any) -> List[tuple]:
    """[(name, reden)] for every source that is not positively read: `unknown`, malformed, or a coverage that is
    not a list. `niet_gebonden` is not unknown. The name is the source's bron, else its reader name."""
    if not isinstance(coverage, list):
        return [("coverage", "coverage malformed")]
    out = []
    for s in coverage:
        if not isinstance(s, dict) or s.get("dekking") not in ("gelezen", "niet_gebonden"):
            src = s if isinstance(s, dict) else {}
            name = src.get("bron") if src.get("bron") is not None else src.get("reader") or "?"
            out.append((str(name), src.get("reden") or ("dekking unknown" if src.get("dekking") == "unknown"
                                                        else "source entry malformed")))
    return out


def project_coverage(data: Mapping[str, Any]) -> tuple:
    """(dekking, reden) of one project from show(): `gelezen` only when the queue is ok AND no source is unknown."""
    parts = [] if data.get("status") == "ok" else [data.get("reason") or f"queue status {data.get('status')!r}"]
    parts += [f"bron {name}: {reden}" for name, reden in unknown_sources(data.get("coverage"))]
    return ("unknown", "; ".join(parts)[:REDEN_MAX]) if parts else ("gelezen", None)


def _project_entry(home: Path, found: Dict[str, Any], limit: int, now: Any) -> Dict[str, Any]:
    pid = found["project_id"]
    out = {"project_id": pid, "dekking": "unknown", "reden": None, "open_total": None, "points": [], "more": 0,
           "sources": []}
    root = home / ".vnx-data" / pid
    if found["in_store"] is None:
        return {**out, "reden": found.get("reden") or "store state unknown"}
    if not found["in_store"]:
        return {**out, "dekking": "niet_gebonden", "reden": "registry only: no store under .vnx-data"}
    try:
        if not (root / "state").is_dir():
            return {**out, "reden": "geen state/"}
        data = show(pid, limit=limit, now=now, state_dir=root / "state", data_root=root)
    except Exception as exc:  # vnx-silent-except: one project that raises is unknown with the exception type, not a crash of the fleet
        return {**out, "reden": f"{type(exc).__name__}: {exc}"}
    dekking, reden = project_coverage(data)
    total = data["open_total"] if dekking == "gelezen" else None  # unknown never counts as zero (decision 11)
    return {**out, "dekking": dekking, "reden": reden, "open_total": total, "points": data["points"],
            "more": data["more"], "sources": data["coverage"]}


def show_all(*, home: Path, registry_path: Optional[Path] = None, limit: int = 5, now: Any = None) -> Dict[str, Any]:
    """Decision 11: one coverage per project. `unknown` never counts as zero (open_total is None)."""
    found = discover_projects(home=home, registry_path=registry_path)
    return {"registry": found["registry"], "store": found["store"],
            "projects": [_project_entry(Path(home), f, limit, now) for f in found["projects"]]}
