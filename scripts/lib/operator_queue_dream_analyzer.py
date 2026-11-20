"""Operator-queue readers for source 3 (dream reviews) and source 4 (analyzer proposals), ADR-076.

Each reader opens a point while its source says open, closes it on the source state with that state as
``bewijs`` and writes an ``unknown`` line when the source cannot be read (ADR-063). It writes only on a
change, so running it twice on one source state adds no line (decision 3). Sources are opened read-only
(decision 14). Refs: ``dream:<review file stem>``, ``edit:<edit id>``, ``archival:<pattern id>``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import operator_queue as oq
    import ochtendronde_routing as rt
    from dream_liveness import read_events, read_reviews, _parse_ts
    from project_root import resolve_central_data_dir
except ImportError:  # vnx-silent-except: imported as scripts.lib.operator_queue_dream_analyzer
    from scripts.lib import operator_queue as oq
    from scripts.lib import ochtendronde_routing as rt
    from scripts.lib.dream_liveness import read_events, read_reviews, _parse_ts
    from scripts.lib.project_root import resolve_central_data_dir

BRON_DREAM, BRON_ANALYZER = 3, 4
EFFECT_PREFIX = "effect:"  # D3d: opened by suggested_edit_remeasure, not by a pending line
REVIEW_SUFFIX = "-pending-review"
_REVIEW_EVENTS = ("dream_cycle_approved", "dream_cycle_rejected")
_TIME_KEYS = ("rejected_at", "decided_at", "accepted_at", "applied_at")
_Verdict = Tuple[str, ...]  # ("open", vraag, asked_at) | ("closed", bewijs, door) | ("unknown", reden)


class _Run:
    """One reader run: the project, the dir of the ledger, the clock and what was written."""

    def __init__(self, project_id: str, bron: int, state_dir: Optional[Path], now: Any) -> None:
        self.project_id, self.bron, self.state_dir = project_id, bron, state_dir
        self.now = _as_datetime(now)
        self.stamp = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.problems: List[str] = []
        self.out: Dict[str, List[str]] = {"opened": [], "closed": [], "unknown": []}
        state = oq.read_state(project_id, state_dir=state_dir)
        self.broken = state["status"] != "ok"
        if self.broken:
            self.problems.append("operator_queue onleesbaar: " + str(state["reason"]))
        self.points = [p for p in state["points"].values() if p["bron"] == bron]

    def latest(self, ref: str) -> Optional[dict]:
        found = [p for p in self.points if p["ref"] == ref]
        return max(found, key=lambda p: p["revisie"]) if found else None

    def open_refs(self) -> List[str]:
        return sorted({p["ref"] for p in self.points if self.latest(p["ref"])["state"] == "open"})

    def apply(self, ref: str, verdict: _Verdict) -> None:
        if self.broken:
            return
        latest, kind = self.latest(ref), verdict[0]
        try:
            self._write(ref, latest, kind, verdict)
        except oq.QueueRefused as exc:
            self.problems.append(f"{ref}: {exc}")

    def _write(self, ref: str, latest: Optional[dict], kind: str, verdict: _Verdict) -> None:
        is_open = latest is not None and latest["state"] == "open"
        if kind == "open":
            if is_open:
                return
            line = oq.open_point(self.project_id, bron=self.bron, ref=ref, revisie=(latest["revisie"] + 1 if latest else 1),
                                 vraag=verdict[1][:200], sluit_bij=verdict[3], opened_by="lezer", asked_at=verdict[2],
                                 now=self.stamp, state_dir=self.state_dir)
            if line:
                self.out["opened"].append(line["point_id"])
        elif not is_open:
            return
        elif kind == "closed":
            line = oq.close_point(self.project_id, latest["point_id"], uitkomst="beantwoord", bewijs=verdict[1],
                                  beantwoord_door=verdict[2], now=self.stamp, state_dir=self.state_dir)
            if line:
                self.out["closed"].append(latest["point_id"])
        elif latest["unknown_reden"] != verdict[1]:
            oq.append_line(self.project_id, "unknown", {"point_id": latest["point_id"], "reden": verdict[1]},
                           now=self.stamp, state_dir=self.state_dir)
            self.out["unknown"].append(latest["point_id"])

    def result(self) -> Dict[str, Any]:
        return {"bron": self.bron, "dekking": "unknown" if self.problems else "gelezen", "reden": "; ".join(self.problems) or None,
                "opened": self.out["opened"], "closed": self.out["closed"], "unknown": self.out["unknown"]}


def _as_datetime(now: Any) -> datetime:
    if isinstance(now, datetime):
        return now.astimezone(timezone.utc)
    return _parse_ts(now) or datetime.now(timezone.utc)


def _root(project_id: str, data_root: Optional[Path]) -> Path:
    return Path(data_root) if data_root is not None else resolve_central_data_dir(project_id)


def _read_json(path: Path) -> Tuple[Any, Optional[str]]:
    """(data, None), or (None, reason): a missing or unparsable file is never an empty source."""
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"geen {path.name}"
    except (OSError, ValueError):
        return None, f"{path.name} onleesbaar"


# --- source 3: dream reviews -------------------------------------------------------------------------------

def _review_event(events: List[dict], cycle_id: str) -> Optional[Tuple[str, str]]:
    """(event id, actor) of the newest approve or reject event of the cycle; None when there is none."""
    hits = [e for e in events if e.get("cycle_id") == cycle_id and e.get("event_type") in _REVIEW_EVENTS]
    if not hits:
        return None
    ev = hits[-1]
    eid = ev.get("event_id") or ev.get("id") or f"{ev['event_type']}:{cycle_id}@{ev.get('timestamp')}"
    actor = ev.get("actor") or ev.get("decided_by")
    return str(eid), actor if isinstance(actor, str) and actor.strip() else "onbekend"


def _dream_verdict(root: Path, events: List[dict], ref: str) -> _Verdict:
    """The verdict for an open point whose review is no longer listed as pending."""
    data, why = _read_json(root / "state" / "dream" / f"{ref.split(':', 1)[1]}.json")
    cycle = ref.split(":", 1)[1].removesuffix(REVIEW_SUFFIX)
    event = _review_event(events, cycle)
    if why is not None and why.startswith("geen"):
        return ("closed", event[0], event[1]) if event else ("unknown", "bestand weg")
    if not isinstance(data, dict):
        return ("unknown", "review-bestand onleesbaar")
    if data.get("requires_operator_review") is not False:
        return ("unknown", "review zegt niet false (veld ontbreekt of is onbekend)")
    if event is None:
        return ("unknown", "review is false maar er is geen dream_cycle_approved of dream_cycle_rejected event")
    return ("closed", event[0], event[1])


def _pending_verdicts(reviews: dict) -> Dict[str, _Verdict]:
    out: Dict[str, _Verdict] = {}
    for item in reviews["pending_reviews"]:
        cycle = item.get("cycle_id")
        if not isinstance(cycle, str) or not cycle.strip():
            continue
        days = int((item["age_seconds"] or 0) // 86400)
        out[f"dream:{cycle}{REVIEW_SUFFIX}"] = (
            "open", f"Dream-review van cycle {cycle} wacht al {days} dagen op de operator.",
            item.get("created_at") or None, "goedkeuren of afwijzen van de dream-review")
    for name in reviews["unreadable_paths"]:
        out[f"dream:{name.removesuffix('.json')}"] = ("unknown", "review-bestand onleesbaar")
    return out


def read_dream(project_id: str, *, data_root: Optional[Path] = None, now: Any = None,
               state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Source 3: a review with ``requires_operator_review`` true is a point; it closes on the false plus the event."""
    run, root = _Run(project_id, BRON_DREAM, state_dir, now), _root(project_id, data_root)
    events = read_events(root / "events" / "dream")
    reviews = read_reviews(root, project_id, events=events or [], now=run.now)
    run.problems += [] if reviews["available"] else ["dream-map niet te lezen"]
    run.problems += [f"{reviews['unreadable_count']} review-bestand(en) onleesbaar"] if reviews["unreadable_count"] else []
    run.problems += ["geen dream-events te lezen"] if events is None else []
    verdicts = _pending_verdicts(reviews) if reviews["available"] else {}
    for ref, verdict in verdicts.items():
        run.apply(ref, verdict)
    for ref in run.open_refs():
        if ref not in verdicts:
            run.apply(ref, _dream_verdict(root, events or [], ref) if reviews["available"] else ("unknown", "dream-map niet te lezen"))
    return run.result()


# --- source 4: analyzer proposals --------------------------------------------------------------------------

# Decision statuses the writers set (apply_suggested_edits.py cmd_accept, cmd_reject; learning_loop.py:2236). Exact match:
# stale, superseded, "unknown" and case/whitespace variants are no decision.
_DECISION_STATUSES = frozenset({"accepted", "rejected", "applied", "approved", "dismissed"})


def _no_decision(entry: dict) -> Optional[str]:
    """None when ``entry`` records a decision; else the reason it does not. The one rule every close path uses."""
    status = entry.get("status")
    if not isinstance(status, str) or not status.strip():
        return f"status={status!r} ontbreekt of is geen tekst"
    return None if status in _DECISION_STATUSES else f"status={status!r} is geen besluit"


def _decided(entry: dict) -> _Verdict:
    status, why = entry.get("status"), _no_decision(entry)
    if why is not None:
        return ("unknown", why)
    when = next((f"{k}={entry[k]}" for k in _TIME_KEYS if isinstance(entry.get(k), str) and entry[k].strip()), "")
    return ("closed", f"status={status}" + (f" {when}" if when else ""), "onbekend")


def _list_verdicts(items: Any, key: str, prefix: str, label: str) -> Dict[str, _Verdict]:
    out: Dict[str, _Verdict] = {}
    for entry in items:
        ident = entry.get(key) if isinstance(entry, dict) else None
        if isinstance(ident, bool) or not isinstance(ident, (int, str)) or not str(ident).strip():
            continue
        title = str(entry.get("category") or entry.get("title") or "voorstel")
        out[f"{prefix}:{ident}"] = (("open", f"Analyzer-voorstel {label}{ident} ({title[:60]}) wacht op accepteren of afwijzen.",
                                     None, "accepteren of afwijzen van het analyzer-voorstel")
                                    if entry.get("status") == "pending" else _decided(entry))
    return out


def _history_verdict(history: Any, hist_why: Optional[str], ref: str) -> _Verdict:
    if hist_why is not None and not hist_why.startswith("geen"):
        return ("unknown", "edit weg zonder edit_history: " + hist_why)
    ident = ref.split(":", 1)[1]
    for entry in (history if isinstance(history, list) else []):
        if isinstance(entry, dict) and str(entry.get("id")) == ident:
            why = _no_decision(entry)
            if why is not None:
                return ("unknown", f"edit_history.json: id={ident} {why}")
            return ("closed", f"edit_history.json: id={ident} status={entry.get('status')} applied_at={entry.get('applied_at')}", "onbekend")
    return ("unknown", "edit weg zonder edit_history")


def _load_list(run: _Run, path: Path, key: str, required: bool) -> Optional[list]:
    data, why = _read_json(path)
    if why is not None and not (why.startswith("geen") and not required):
        run.problems.append(why)
    if why is not None:
        return None
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        run.problems.append(f"{path.name} heeft geen lijst {key}")
        return None
    return items


def _entry_map(items: Any, key: str, prefix: str) -> Dict[str, dict]:
    """ref -> source entry, the same refs ``_list_verdicts`` builds (entries it skips are skipped here too)."""
    out: Dict[str, dict] = {}
    for entry in items or []:
        ident = entry.get(key) if isinstance(entry, dict) else None
        if not (isinstance(ident, bool) or not isinstance(ident, (int, str)) or not str(ident).strip()):
            out[f"{prefix}:{ident}"] = entry
    return out


def _missing_verdict(src: dict, ref: str) -> _Verdict:
    """The verdict for an open point whose source line is no longer in the pending list."""
    if ref.startswith("edit:"):
        return (_history_verdict(src["history"], src["hist_why"], ref) if src["edits"] is not None
                else ("unknown", "pending_edits.json niet te lezen"))
    return ("unknown", "regel weg uit pending_archival.json" if src["archival"] is not None
            else "pending_archival.json niet te lezen")


def _sweep(run: _Run, verdicts: Dict[str, _Verdict], src: dict, prefix: str = "") -> None:
    """Open points of ``run`` (those under ``prefix``) whose source line is gone get the history verdict.
    Without a prefix a ``project:`` ref is another project's routed point; only its owner's sweep updates it."""
    for ref in run.open_refs():
        if ref[len(prefix):].startswith(EFFECT_PREFIX):  # an effect point has no pending line: its closer is the operator
            continue
        if ref.startswith(prefix) and (prefix or not ref.startswith(rt.ROUTED_PREFIX)) and ref[len(prefix):] not in verdicts:
            run.apply(ref, _missing_verdict(src, ref[len(prefix):]))


def _fleet_verdict(owner: str, verdict: _Verdict) -> _Verdict:
    return (verdict[0], f"[{owner}] {verdict[1]}") + verdict[2:] if verdict[0] == "open" else verdict


def _route_new(run: _Run, fleet: _Run, routing: Any, ref: str, verdict: _Verdict, entry: dict, extra: dict) -> None:
    """A source line with no point yet: classify it (ADR-095 decision 5) and open it where it belongs, or report why not."""
    index, pid = routing.index, run.project_id
    cls = (index.classify(pid, ref, entry) if index is not None else
           rt.classify(pid, ref, entry.get("target"), rt.fingerprint_of(entry), {}, {"register": routing.problems[0]}))
    extra["classification"][ref] = cls.kind
    if cls.kind == rt.UNKNOWN:
        extra["unclassified"].append({"ref": ref, "reden": cls.reason})
    elif cls.kind == rt.PROJECT:
        run.apply(ref, verdict)
    elif cls.owner == pid:  # the elected owner opens it; the fleet project's own ledger keeps the plain ref
        (run.apply(ref, verdict) if pid == rt.FLEET_PROJECT else fleet.apply(rt.routed_ref(pid, ref), _fleet_verdict(pid, verdict)))
    else:
        extra["routed_elsewhere"].append({"ref": ref, "owner": cls.owner})


def _route_analyzer(run: _Run, verdicts: Dict[str, _Verdict], entries: Dict[str, dict], src: dict, routing: Any) -> Dict[str, Any]:
    fleet = _Run(rt.FLEET_PROJECT, BRON_ANALYZER, routing.fleet_state_dir, run.now)
    prefix = f"project:{run.project_id}/"
    extra: Dict[str, Any] = {"classification": {}, "unclassified": [], "routed_elsewhere": []}
    for ref, verdict in verdicts.items():
        if fleet.latest(prefix + ref) is not None:  # a point routed earlier keeps its ledger, closing on the source state
            fleet.apply(prefix + ref, verdict)
        elif run.latest(ref) is not None or verdict[0] != "open":
            run.apply(ref, verdict)
        else:
            _route_new(run, fleet, routing, ref, verdict, entries.get(ref, {}), extra)
    _sweep(run, verdicts, src)
    _sweep(fleet, verdicts, src, prefix)
    run.problems += routing.problems + [f"vnx-dev: {p}" for p in fleet.problems]
    return {**extra, "routed": fleet.out}


def read_analyzer(project_id: str, *, data_root: Optional[Path] = None, now: Any = None,
                  state_dir: Optional[Path] = None, routing: Any = None) -> Dict[str, Any]:
    """Source 4: pending edits and pending archival lines; a missing ``pending_edits.json`` is ``unknown``, not 0.

    With ``routing`` (ochtendronde_routing.Routing) a new fleet point opens in the vnx-dev ledger as
    ``project:<owner>/<ref>`` instead of here, and closes on this project's source state; read-only on this store.
    """
    run, sdir = _Run(project_id, BRON_ANALYZER, state_dir, now), _root(project_id, data_root) / "state"
    edits = _load_list(run, sdir / "pending_edits.json", "edits", True)
    archival = _load_list(run, sdir / "pending_archival.json", "pending_archival", False)
    history, hist_why = _read_json(sdir / "edit_history.json")
    src = {"edits": edits, "archival": archival, "history": history, "hist_why": hist_why}
    verdicts = {**_list_verdicts(edits or [], "id", "edit", "#"), **_list_verdicts(archival or [], "pattern_id", "archival", "")}
    if routing is not None:
        entries = {**_entry_map(edits, "id", "edit"), **_entry_map(archival, "pattern_id", "archival")}
        extra = _route_analyzer(run, verdicts, entries, src, routing)
        return {**run.result(), **extra}
    for ref, verdict in verdicts.items():
        run.apply(ref, verdict)
    _sweep(run, verdicts, src)
    return run.result()


READERS: Tuple[Tuple[str, Callable[..., Dict[str, Any]]], ...] = (("dream", read_dream), ("analyzer", read_analyzer))
