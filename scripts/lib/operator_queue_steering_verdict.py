"""Readers for operator-queue sources 5 and 6 (ADR-076 decisions 2, 3, 5 and 7).

Source 5 is a steering proposal, source 6 a permission proposal (same store, kind in ``PERMISSION_KINDS``).
A reader opens a point on the source's open state and closes it
only on the source's own answer; it writes only on a change, so a second read of the same state writes
nothing. Unreadable is ``unknown`` and closes nothing (ADR-063); an unbound steering store is
``niet_gebonden``, never "0 points read".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import operator_queue as oq
    import steering_proposal_store as sps
    from project_root import resolve_central_data_dir
except ImportError:  # vnx-silent-except: imported as scripts.lib.operator_queue_steering_verdict
    from scripts.lib import operator_queue as oq
    from scripts.lib import steering_proposal_store as sps
    from scripts.lib.project_root import resolve_central_data_dir

BINDING_FILE, BINDING_SCHEMA = "steering_sources.json", "steering_sources/1"
PERMISSION_KINDS = frozenset({"permissie", "bouwer_allowlist", "bouwgewoonte", "alleen_melding"})  # ADR-075


class _Unreadable(Exception):
    """A source cannot be read; ``args[0]`` is the reason."""


def _blank(bron: int) -> Dict[str, Any]:
    return {"bron": bron, "dekking": "gelezen", "reden": None, "opened": [], "closed": [], "unknown": []}


def _root(project_id: str, data_root: Optional[Path]) -> Path:
    return Path(data_root) if data_root is not None else resolve_central_data_dir(project_id)


def _fail(results: List[Dict[str, Any]], dekking: str, reden: str) -> List[Dict[str, Any]]:
    for res in results:
        res.update(dekking=dekking, reden=reden)
    return results


def _ledger_state(project_id: str, state_dir: Optional[Path]) -> Dict[str, Any]:
    state = oq.read_state(project_id, state_dir=state_dir)
    if state["status"] != "ok":
        raise _Unreadable(f"operator_queue: {state['reason']}")
    return state


def _points_of(state: Dict[str, Any], bron: int, ref: str) -> List[Dict[str, Any]]:
    found = [p for p in state["points"].values() if p["bron"] == bron and p["ref"] == ref]
    return sorted(found, key=lambda p: p["revisie"])


def _binding_problem(state: Path, project_id: str) -> Optional[str]:
    """Why the steering store is not bound (ADR-072 decision 2), or None when it is."""
    try:
        record = json.loads((state / BINDING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):  # vnx-silent-except: no readable binding is the reason, never a pass
        return f"geen leesbaar {BINDING_FILE}"
    if not isinstance(record, dict) or record.get("schema") != BINDING_SCHEMA:
        return f"{BINDING_FILE} heeft niet het schema {BINDING_SCHEMA}"
    if record.get("project_id") != project_id:
        return f"{BINDING_FILE} noemt een ander project"
    dirs = record.get("transcript_dirs")
    if not isinstance(dirs, list) or not dirs or not isinstance(record.get("memory_dir"), str):
        return f"{BINDING_FILE} mist transcript_dirs of memory_dir"
    return None


def _mark_unknown(res: Dict[str, Any], project_id: str, point: Dict[str, Any], reden: str, now: Optional[str],
                  state_dir: Optional[Path]) -> None:
    res["unknown"].append(point["point_id"])
    if point["unknown_reden"] != reden:  # a line only on a change
        oq.append_line(project_id, "unknown", {"point_id": point["point_id"], "reden": reden}, now=now,
                       state_dir=state_dir)


def _close(res: Dict[str, Any], project_id: str, pid: str, uitkomst: str, bewijs: str, door: str,
           now: Optional[str], state_dir: Optional[Path]) -> None:
    if oq.close_point(project_id, pid, uitkomst=uitkomst, bewijs=bewijs, beantwoord_door=door, now=now,
                      state_dir=state_dir):
        res["closed"].append(pid)


def _open(res: Dict[str, Any], project_id: str, ref: str, revisie: int, vraag: str, sluit_bij: str, asked_at: str,
          now: Optional[str], state_dir: Optional[Path]) -> str:
    line = oq.open_point(project_id, bron=res["bron"], ref=ref, revisie=revisie, vraag=vraag[:200], sluit_bij=sluit_bij,
                         opened_by="lezer", asked_at=asked_at, now=now, state_dir=state_dir)
    pid = oq.point_id(project_id, res["bron"], ref, revisie)
    if line:
        res["opened"].append(pid)
    return pid


# ---------------------------------------------------------------------------
# Sources 5 and 6: the steering store
# ---------------------------------------------------------------------------


def _operator_record(records: List[Dict[str, Any]], pid: str, revision: int) -> Optional[Dict[str, Any]]:
    """The latest approved, rejected or applied record bound to ``revision``, or None."""
    mine = [r for r in records if r["proposal_id"] == pid and r["revision"] == revision
            and r["decision"] in sps.OPERATOR_DECISIONS]
    return mine[-1] if mine else None


def _steer_entry(ctx: "_Ctx", res: Dict[str, Any], entry: Dict[str, Any], records: List[Dict[str, Any]]) -> None:
    ref, current = f"steering:{entry['proposal_id']}", len(entry["revisions"])
    mine = _points_of(ctx.queue, res["bron"], ref)
    for point in (p for p in mine if p["state"] == "open"):
        rec = _operator_record(records, entry["proposal_id"], point["revisie"])
        if rec is not None:
            bewijs = f"{rec['decision']} revisie {rec['revision']} op {rec['decided_at']} ({rec['decision_id']})"
            _close(res, ctx.project_id, point["point_id"], "beantwoord", bewijs, rec["decided_by"], ctx.now, ctx.state_dir)
        elif point["revisie"] < current:
            _close(res, ctx.project_id, point["point_id"], "vervallen", f"vervangen door revisie {current}", "onbekend",
                   ctx.now, ctx.state_dir)
    if entry["status"] == "pending" and not any(p["revisie"] == current for p in mine):
        vraag = f"Steering-voorstel ({entry['artefact_soort']}) voor {entry['doelpad']}: goedkeuren of afwijzen?"
        _open(res, ctx.project_id, ref, current, vraag, "approved, rejected of applied in de steering-store",
              entry["revisions"][current - 1]["recorded_at"], ctx.now, ctx.state_dir)


def read_steering(project_id: str, *, data_root: Optional[Path] = None, now: Optional[str] = None,
                  state_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Sources 5 and 6: a list of two reader dicts, ``[source 5, source 6]``."""
    results = [_blank(5), _blank(6)]
    src = _root(project_id, data_root) / "state"
    problem = _binding_problem(src, project_id)
    if problem:
        return _fail(results, "niet_gebonden", problem)
    try:
        entries, records = sps.proposals(src, project_id), sps.decisions(src, project_id)
    except (sps.StoreUnreadable, OSError) as exc:
        return _fail(results, "unknown", f"steering-store onleesbaar: {exc}")
    try:
        ctx = _Ctx(project_id, _ledger_state(project_id, state_dir), now, state_dir)
        for entry in entries:
            _steer_entry(ctx, results[1 if entry["artefact_soort"] in PERMISSION_KINDS else 0], entry, records)
    except (_Unreadable, oq.QueueRefused, oq.QueueUnreadable) as exc:
        return _fail(results, "unknown", str(exc))
    return results


@dataclass
class _Ctx:
    project_id: str
    queue: Dict[str, Any]
    now: Optional[str]
    state_dir: Optional[Path]


READERS = (("steering", read_steering),)  # the cap-verdict reader (source 7) follows in its own delivery
