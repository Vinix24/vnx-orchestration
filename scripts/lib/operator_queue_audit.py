"""``vnx inbox audit`` (ADR-076 decision 13): per project, how late was each point first shown to the operator.

Two kinds of late: a point first presented more than 48 hours after ``asked_at``, and an open point older than 48
hours that has no ``presented`` line. The audit measures only from the first ledger line (``ledger_since``); a window
that starts before it is ``niet gemeten``, never clean. A project that cannot be read is ``unknown``, never clean.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import operator_queue as oq
    import operator_queue_inbox as inbox
except ImportError:  # vnx-silent-except: imported as scripts.lib.operator_queue_audit
    from scripts.lib import operator_queue as oq
    from scripts.lib import operator_queue_inbox as inbox

LATE_AFTER = timedelta(hours=48)
KIND_SLOW, KIND_NEVER = "eerste_voorlegging_te_laat", "open_zonder_voorlegging"


def parse_since(text: Any) -> int:
    """``7d`` -> 7. Anything else (absent, 0, other unit, malformed) raises ValueError."""
    match = re.fullmatch(r"(\d+)d", text.strip()) if isinstance(text, str) else None
    if not match or int(match.group(1)) < 1:
        raise ValueError("--since is a number of days like 7d")
    return int(match.group(1))


def _now(now: Any) -> datetime:
    return now if isinstance(now, datetime) else (inbox._parse(now) or datetime.now(timezone.utc))


def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600, 1)


def _late_points(lines: List[Dict[str, Any]], now: datetime, start: datetime) -> List[Dict[str, Any]]:
    first: Dict[str, datetime] = {}
    for line in lines:
        if line["kind"] == "presented":
            ts = inbox._parse(line["ts"])
            if ts is not None and (line["point_id"] not in first or ts < first[line["point_id"]]):
                first[line["point_id"]] = ts
    late = []
    for pid, point in oq._fold(lines).items():
        asked = inbox._parse(point["asked_at"])
        if asked is None:
            continue
        if pid in first and first[pid] >= start and first[pid] - asked > LATE_AFTER:
            late.append({"point_id": pid, "kind": KIND_SLOW, "age_h": _hours(first[pid] - asked)})
        elif pid not in first and point["state"] == "open" and now - asked > LATE_AFTER:
            late.append({"point_id": pid, "kind": KIND_NEVER, "age_h": _hours(now - asked)})
    return sorted(late, key=lambda p: (-p["age_h"], p["point_id"]))


def _result(project_id: str, verdict: str, *, late: Optional[list] = None, since: Optional[str] = None,
            niet_gemeten: bool = False, reden: Optional[str] = None) -> Dict[str, Any]:
    status = "unknown" if verdict == "unknown" else ("te_laat" if late else ("niet_gemeten" if niet_gemeten else "schoon"))
    text = {"unknown": f"unknown ({reden})", "te_laat": f"te_laat {len(late or [])}"}.get(status, status.replace("_", " "))
    if status == "te_laat" and niet_gemeten:
        text += " niet gemeten"
    return {"project_id": project_id, "status": status, "verdict": text, "late": late or [], "ledger_since": since,
            "niet_gemeten": niet_gemeten, "reden": reden}


def measure(project_id: str, state_dir: Path, *, dekking: str, reden: Optional[str], now: Any, since_days: int) -> Dict[str, Any]:
    """One project's audit. Anything but a positively read coverage and a strictly read ledger is ``unknown``."""
    if dekking == "niet_gebonden":
        return _result(project_id, "niet_gemeten", niet_gemeten=True, reden=reden)
    if dekking != "gelezen":
        return _result(project_id, "unknown", reden=reden or "coverage not read")
    try:
        lines = oq._ledger_lines(Path(state_dir), project_id)  # the strict reader: a bad line is never skipped
    except oq.QueueUnreadable as exc:
        return _result(project_id, "unknown", reden=str(exc)[:inbox.REDEN_MAX])
    if not lines:
        return _result(project_id, "niet_gemeten", niet_gemeten=True, reden="ledger empty or absent")
    stamps = sorted((inbox._parse(line["ts"]), line["ts"]) for line in lines)
    at = _now(now)
    start = at - timedelta(days=since_days)
    return _result(project_id, "gemeten", late=_late_points(lines, at, start), since=stamps[0][1],
                   niet_gemeten=stamps[0][0] > start)


def audit_project(project_id: str, *, state_dir: Optional[Path], data_root: Optional[Path], now: Any,
                  since_days: int) -> Dict[str, Any]:
    sdir = Path(state_dir) if state_dir is not None else oq.state_dir_for(project_id)
    try:
        shown = inbox.show(project_id, now=now, state_dir=sdir, data_root=data_root)
        dekking, reden = inbox.project_coverage(shown)
    except Exception as exc:  # vnx-silent-except: a project that raises is unknown with the exception type
        dekking, reden = "unknown", f"{type(exc).__name__}: {exc}"
    return measure(project_id, sdir, dekking=dekking, reden=reden, now=now, since_days=since_days)


def audit_all(*, home: Path, registry_path: Optional[Path], now: Any, since_days: int) -> List[Dict[str, Any]]:
    """Same discovery and coverage as ``vnx inbox --all`` (``show_all``): no second discovery."""
    out = []
    for entry in inbox.show_all(home=home, registry_path=registry_path, now=now)["projects"]:
        pid = entry["project_id"]
        out.append(measure(pid, Path(home) / ".vnx-data" / pid / "state", dekking=entry["dekking"],
                           reden=entry["reden"], now=now, since_days=since_days))
    return out


def exit_code(results: List[Dict[str, Any]]) -> int:
    """1 when anything is late, else 3 when anything is unknown or niet gemeten (or nothing was read), else 0."""
    if any(r["late"] for r in results):
        return 1
    return 0 if results and all(r["status"] == "schoon" for r in results) else 3


def render(results: List[Dict[str, Any]]) -> str:
    out = []
    for r in results:
        out.append(f"{r['project_id']}: {r['verdict']}" + (f" (ledger_since {r['ledger_since']})" if r["ledger_since"] else "")
                   + (f" ({r['reden']})" if r["status"] == "niet_gemeten" and r["reden"] else ""))
        out += [f"  {p['point_id']}  {p['kind']}  age {p['age_h']}h" for p in r["late"]]
    return "\n".join(out)
