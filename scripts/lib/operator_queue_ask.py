"""``vnx inbox ask`` / ``answer``, the ``ref`` grammar and the reader for sources 1, 2 and 8 (ADR-076 decisions 3 to 8).

A point closes only on the state its ``ref`` names, read-only; an unreadable source gives an ``unknown`` line (ADR-063).
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import operator_queue as oq
    import tracks as tracks_lib
    from project_root import resolve_central_data_dir
except ImportError:  # vnx-silent-except: imported as scripts.lib.operator_queue_ask
    from scripts.lib import operator_queue as oq
    from scripts.lib import tracks as tracks_lib
    from scripts.lib.project_root import resolve_central_data_dir

BRON = {"promote": 1, "besluit": 2, "vraag": 8}
FORMS = ("dlv:<id>", "oi:<id>", "track:<id>:<fase>", "config:<sleutel>", "verdict:<id>", "antwoord:<hash>")
_ID = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_REF = {
    "dlv": re.compile(rf"^dlv:({_ID})$"), "oi": re.compile(rf"^oi:({_ID})$"),
    "track": re.compile(rf"^track:({_ID}):([a-z]+)$"), "config": re.compile(r"^config:([A-Za-z0-9_.-]+)$"),
    "verdict": re.compile(rf"^verdict:({_ID})$"), "antwoord": re.compile(r"^antwoord:([0-9a-f]{12})$"),
}
OI_ANSWERED = frozenset({"closed", "done", "deferred", "wontfix"})
MAX_QUOTE = 300

class RefRefused(ValueError):
    """The ref is not one of the six forms of ADR-076 decision 4."""

def parse_ref(text: Any) -> Tuple[str, Tuple[str, ...]]:
    """``(kind, parts)`` of a ref; raises RefRefused naming the six forms for anything else."""
    if isinstance(text, str):
        for kind, pattern in _REF.items():
            found = pattern.match(text)
            if found:
                if kind == "track" and found.group(2) not in tracks_lib.VALID_PHASES:
                    break
                return kind, found.groups()
    raise RefRefused("ref is not one of: " + ", ".join(FORMS))

def normalise_question(vraag: str) -> str:
    """Lower case, whitespace collapsed to one space, nothing else (ADR-076 decision 3)."""
    return " ".join(vraag.lower().split())

def question_ref(vraag: str) -> str:
    return "antwoord:" + hashlib.sha256(normalise_question(vraag).encode("utf-8")).hexdigest()[:12]

def ask(project_id: str, *, bron: str, ref: str, vraag: str, sluit_bij: str, kopieerblok: str = "",
        opened_by: str, now: Optional[str] = None, state_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Open a point of source promote (1), besluit (2) or vraag (8); None when it already exists.
    Raises RefRefused (bad ref or bron/ref mismatch) or operator_queue.QueueRefused (secret, bad field)."""
    if bron not in BRON:
        raise RefRefused("bron is one of: " + ", ".join(BRON))
    if bron == "vraag" and ref == "antwoord":
        ref = question_ref(vraag)
    kind, _ = parse_ref(ref)
    if bron == "promote" and kind != "dlv":
        raise RefRefused("bron promote takes only a dlv:<id> ref")
    return oq.open_point(project_id, bron=BRON[bron], ref=ref, vraag=vraag, sluit_bij=sluit_bij,
                         kopieerblok=kopieerblok, opened_by=opened_by, now=now, state_dir=state_dir)

def answer(project_id: str, point_id: str, *, quote: str, vastgelegd_door: str, now: Optional[str] = None,
           state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Record Vincent's own words for a chat question: one ``answer`` line, nothing else (decision 6)."""
    state = oq.read_state(project_id, state_dir=state_dir)
    if state["status"] != "ok":
        raise oq.QueueUnreadable(state["reason"], state["line"])
    point = state["points"].get(point_id) or {}
    for failed, why in ((not point, "the point does not exist"), (point.get("state") != "open", "the point is closed"),
                        (not str(point.get("ref")).startswith("antwoord:"), "only an antwoord: point is answered here; the source decides the others"),
                        (not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE, f"the quote is empty or over {MAX_QUOTE} characters")):
        if failed:
            raise oq.QueueRefused("answer: " + why)
    return oq.append_line(project_id, "answer", {"point_id": point_id, "citaat": quote,
                                                 "vastgelegd_door": vastgelegd_door}, now=now, state_dir=state_dir)

def _answer_ts(project_id: str, pid: str, state_dir: Optional[Path]) -> Optional[str]:
    path = (Path(state_dir) if state_dir is not None else oq.state_dir_for(project_id)) / oq.LEDGER_FILENAME
    recs = (json.loads(raw) for raw in path.read_text(encoding="utf-8").split("\n") if f'"{pid}"' in raw)
    return next((r["ts"] for r in recs if r.get("kind") == "answer" and r.get("point_id") == pid), None)

class _Source:
    def __init__(self, project_id: str, sdir: Path) -> None:
        self.project_id, self.sdir, self._conn = project_id, sdir, None

    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            db = self.sdir / tracks_lib.DB_FILENAME
            if not db.is_file():
                raise OSError(f"{tracks_lib.DB_FILENAME} is missing")
            self._conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10.0)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def track_phase(self, track_id: Optional[str]) -> Optional[str]:
        row = self.conn().execute("SELECT phase FROM tracks WHERE track_id = ? AND project_id = ?",
                                  (track_id, self.project_id)).fetchone()
        return row["phase"] if row else None

    def dispatch(self, dispatch_id: str) -> Optional[sqlite3.Row]:
        return self.conn().execute("SELECT * FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
                                   (dispatch_id, self.project_id)).fetchone()

    def promoted_by(self, dispatch_id: str) -> str:
        try:
            row = self.conn().execute(
                "SELECT metadata_json FROM coordination_events WHERE event_type = 'deliverable_promoted' "
                "AND entity_id = ? AND project_id = ? ORDER BY occurred_at DESC LIMIT 1",
                (dispatch_id, self.project_id)).fetchone()
            who = json.loads(row["metadata_json"] or "{}").get("actor_class") if row else None
        except (sqlite3.Error, ValueError, AttributeError):  # vnx-silent-except: the actor is display, the state is the evidence
            return "onbekend"
        return who if isinstance(who, str) and who.strip() else "onbekend"

_OPEN = ("open",)
def _eval_dlv(src: _Source, dispatch_id: str) -> tuple:
    row = src.dispatch(dispatch_id)
    phase = src.track_phase(row["track"] if row is not None and "track" in row.keys() else None)
    if row is not None:
        state = row["state"]
        if not isinstance(state, str) or not state.strip():
            return ("unknown", "dispatches.state is empty")
        if state != "proposed":
            return ("closed", "beantwoord", f"dispatches.state={state}", src.promoted_by(dispatch_id))
    if phase == "done":
        return ("closed", "vervallen", "tracks.phase=done", "onbekend")
    return _OPEN if row is not None else ("unknown", "dispatch row is gone and its track is not done")

def _eval_oi(src: _Source, oi_id: str) -> tuple:
    import open_items_manager  # lazy: it resolves paths at import
    items = open_items_manager.read_items(src.sdir).get("items")
    item = next((i for i in items if isinstance(i, dict) and i.get("id") == oi_id), None) if isinstance(items, list) else None
    if item is None:
        return ("unknown", f"{oi_id} is not readable in open_items.json")
    return ("closed", "beantwoord", f"{oi_id}.status={item['status']}", "onbekend") if item.get("status") in OI_ANSWERED else _OPEN

def _evaluate(src: _Source, project_id: str, point: Dict[str, Any], state_dir: Optional[Path]) -> tuple:
    try:
        kind, parts = parse_ref(point["ref"])
    except RefRefused:
        return ("unknown", "ref is not readable")
    try:
        if kind == "antwoord":
            ts = _answer_ts(project_id, point["point_id"], state_dir)
            return _OPEN if ts is None else ("closed", "beantwoord", ts, "t0_namens_operator")
        evaluator = {"dlv": _eval_dlv, "oi": _eval_oi}.get(kind)
        if evaluator:
            return evaluator(src, *parts)
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:  # vnx-silent-except: an unreadable source is an unknown line
        return ("unknown", f"source unreadable ({type(exc).__name__}: {exc})")
    return _OPEN  # verdict:, track: and config: close none here

def read_asked(project_id: str, *, data_root: Optional[Path] = None, now: Optional[str] = None,
               state_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """One reader dict per source (1, 2, 8); Opens nothing."""
    out = {b: {"bron": b, "dekking": "gelezen", "reden": None, "opened": [], "closed": [], "unknown": []}
           for b in BRON.values()}
    state = oq.read_state(project_id, state_dir=state_dir)
    if state["status"] != "ok":
        for result in out.values():
            result.update(dekking="unknown", reden=state["reason"])
        return list(out.values())
    root = Path(data_root) if data_root is not None else resolve_central_data_dir(project_id)
    src = _Source(project_id, root / "state")
    try:
        for point in state["points"].values():
            if point["state"] == "open" and point["bron"] in out:
                _apply(src, project_id, point, out[point["bron"]], now, state_dir)
    finally:
        src.close()
    return list(out.values())

def _apply(src: _Source, project_id: str, point: Dict[str, Any], result: Dict[str, Any], now: Optional[str],
           state_dir: Optional[Path]) -> None:
    pid = point["point_id"]
    verdict = _evaluate(src, project_id, point, state_dir)
    if verdict[0] == "closed":
        oq.close_point(project_id, pid, uitkomst=verdict[1], bewijs=verdict[2], beantwoord_door=verdict[3],
                       now=now, state_dir=state_dir)
        result["closed"].append(pid)
        return
    if verdict[0] == "unknown":
        if point["unknown_reden"] != verdict[1]:
            oq.append_line(project_id, "unknown", {"point_id": pid, "reden": verdict[1]}, now=now, state_dir=state_dir)
        result["unknown"].append(pid)
        result.update(dekking="unknown", reden=result["reden"] or verdict[1])

READERS = (("asked", read_asked),)
