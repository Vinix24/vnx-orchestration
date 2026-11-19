"""Morning round agenda view: the day snapshot joined with the live inbox status (ADR-095, D2c).

Builds the ``morning_agenda/1`` object that ``vnx morning-round agenda --json`` prints and Mission Control reads.
Read-only. Anything it cannot read raises ``AgendaUnreadable`` with the reason; it never returns an empty agenda.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import operator_queue as oq
from operator_queue_ask import ANSWERABLE_REF_PREFIXES
from ochtendronde_agenda import _load_snapshot, _snapshot_path, decision_status

SCHEMA = "morning_agenda/1"
TOP_KEYS = ("schema", "project_id", "day", "snapshot_written_at", "points")
POINT_KEYS = ("point_id", "project_id", "source", "ref", "question", "advice", "classification",
              "age_seconds", "status", "decide_command")
SOURCES = {1: "promote", 2: "decision", 3: "dream", 4: "analyzer", 5: "steering", 6: "permission",
           7: "cap_verdict", 8: "question"}
STATUSES = ("open", "decided")


class AgendaUnreadable(ValueError):
    """No agenda can be shown: no snapshot, an unreadable snapshot or ledger, or a point the ledger lacks."""


def decide_command(source: str, point_id: str, ref: str) -> Optional[str]:
    """The command that carries out the decision, as the source dictates; None where the source has no command."""
    if source in ("decision", "question"):  # `inbox answer` refuses every other ref: the source decides those
        return f'vnx inbox answer {point_id} --quote "..."' if ref.startswith(ANSWERABLE_REF_PREFIXES) else None
    bare = ref.split(":", 1)[-1]  # the snapshot ref is "<form>:<id>" (dlv:<id>, edit:<n>, dream:<id>); the CLIs take the id
    if source == "promote":
        return f"vnx deliverable promote {bare}"
    if source == "dream":
        return f"vnx dream review {bare} --approve"  # without --approve/--reject the command waits on stdin
    if source == "analyzer":
        return f"python3 scripts/apply_suggested_edits.py accept {bare}"
    return None


def _age(asked_at: Any, now: datetime) -> Optional[int]:
    try:
        moment = datetime.fromisoformat(str(asked_at).strip().replace("Z", "+00:00"))
        return max(0, int((now - moment).total_seconds()))
    except ValueError:  # vnx-silent-except: an unparsable ask time shows as an unknown age (null), the point still shows
        return None


def build_agenda(project_id: str, day: str, *, state_dir: Path, now: datetime) -> Dict[str, Any]:
    status = decision_status(project_id, day, state_dir=state_dir)
    if status.snapshot != "ok":
        raise AgendaUnreadable(f"no usable snapshot for {project_id} on {day}: {status.reason}")
    if status.ledger != "ok":
        raise AgendaUnreadable(f"inbox ledger unreadable in {Path(state_dir) / oq.LEDGER_FILENAME}: {status.reason}")
    snapshot = _load_snapshot(_snapshot_path(state_dir, day), project_id, day)
    written = json.loads(_snapshot_path(state_dir, day).read_text(encoding="utf-8")).get("taken_at")
    state = oq.read_state(project_id, state_dir=state_dir)["points"]
    points: List[Dict[str, Any]] = []
    for item in snapshot:
        pid = item["point_id"]
        decided = status.points.get(pid)
        if decided not in STATUSES:
            raise AgendaUnreadable(f"point {pid} has status {decided!r} in the inbox ledger of {project_id}")
        ledger_point = state.get(pid)
        if ledger_point is None:
            raise AgendaUnreadable(f"point {pid} is missing from the inbox ledger of {project_id} on the second read")
        source = SOURCES.get(item.get("bron"), "unknown")
        points.append({
            "point_id": pid, "project_id": project_id, "source": source, "ref": item.get("ref"),
            "question": item.get("vraag"), "advice": item.get("advies") or None,
            "classification": item.get("klasse") or None,
            "age_seconds": _age(ledger_point.get("asked_at"), now), "status": decided,
            "decide_command": decide_command(source, pid, str(item.get("ref"))),
        })
    return {"schema": SCHEMA, "project_id": project_id, "day": day, "snapshot_written_at": written,
            "points": points}


def render_text(agenda: Dict[str, Any]) -> str:
    lines = [f"morning agenda {agenda['project_id']} {agenda['day']} (snapshot {agenda['snapshot_written_at']})"]
    for p in agenda["points"]:
        age = "?" if p["age_seconds"] is None else f"{p['age_seconds']}s"
        line = (f"{p['point_id']} {p['source']} {p['classification'] or 'unclassified'} {age} "
                f"{p['status']}: {p['question']}")
        lines.append(line + (f" | advice: {p['advice']}" if p["advice"] else ""))
    return "\n".join(lines)
