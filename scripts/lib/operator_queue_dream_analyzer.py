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
    from dream_liveness import read_events, read_reviews, _parse_ts
    from project_root import resolve_central_data_dir
except ImportError:  # vnx-silent-except: imported as scripts.lib.operator_queue_dream_analyzer
    from scripts.lib import operator_queue as oq
    from scripts.lib.dream_liveness import read_events, read_reviews, _parse_ts
    from scripts.lib.project_root import resolve_central_data_dir

BRON_DREAM, BRON_ANALYZER = 3, 4
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


def read_analyzer(project_id: str, *, data_root: Optional[Path] = None, now: Any = None,
                  state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Source 4: pending edits and pending archival lines; a missing ``pending_edits.json`` is ``unknown``, not 0."""
    run, sdir = _Run(project_id, BRON_ANALYZER, state_dir, now), _root(project_id, data_root) / "state"
    edits = _load_list(run, sdir / "pending_edits.json", "edits", True)
    archival = _load_list(run, sdir / "pending_archival.json", "pending_archival", False)
    history, hist_why = _read_json(sdir / "edit_history.json")
    verdicts = {**_list_verdicts(edits or [], "id", "edit", "#"), **_list_verdicts(archival or [], "pattern_id", "archival", "")}
    for ref, verdict in verdicts.items():
        run.apply(ref, verdict)
    for ref in run.open_refs():
        if ref in verdicts:
            continue
        if ref.startswith("edit:"):
            run.apply(ref, _history_verdict(history, hist_why, ref) if edits is not None
                      else ("unknown", "pending_edits.json niet te lezen"))
        else:
            run.apply(ref, ("unknown", "regel weg uit pending_archival.json" if archival is not None
                            else "pending_archival.json niet te lezen"))
    return run.result()


READERS: Tuple[Tuple[str, Callable[..., Dict[str, Any]]], ...] = (("dream", read_dream), ("analyzer", read_analyzer))
