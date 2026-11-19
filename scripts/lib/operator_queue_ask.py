"""``vnx inbox ask`` / ``answer``, the ``ref`` grammar and the reader for sources 1, 2 and 8 (ADR-076 decisions 3 to 8).

A point closes only on the state its ``ref`` names, read-only; an unreadable source gives an ``unknown`` line (ADR-063).
"""
from __future__ import annotations
import contextlib
import hashlib
import importlib
import io
import json
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import operator_queue as oq
    import operator_queue_steering_verdict as steering
    import tracks as tracks_lib
    from forge_project_target import project_checkout_path
    from project_root import resolve_central_data_dir
except ImportError:  # vnx-silent-except: imported as scripts.lib.operator_queue_ask
    from scripts.lib import operator_queue as oq
    from scripts.lib import operator_queue_steering_verdict as steering
    from scripts.lib import tracks as tracks_lib
    from scripts.lib.forge_project_target import project_checkout_path
    from scripts.lib.project_root import resolve_central_data_dir

BRON = {"promote": 1, "besluit": 2, "vraag": 8}
FORMS = ("dlv:<id>", "oi:<id>", "track:<id>:<fase>", "config:<sleutel>", "verdict:<id>", "antwoord:<hash>", "pr:<n>")
_ID = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_REF = {
    "dlv": re.compile(rf"^dlv:({_ID})$"), "oi": re.compile(rf"^oi:({_ID})$"),
    "track": re.compile(rf"^track:({_ID}):([a-z]+)$"), "config": re.compile(r"^config:([A-Za-z0-9_.-]+)$"),
    "verdict": re.compile(rf"^verdict:({_ID})$"), "antwoord": re.compile(r"^antwoord:([0-9a-f]{12})$"),
    "pr": re.compile(r"^pr:([1-9][0-9]{0,8})$"),
}
OI_ANSWERED = frozenset({"closed", "done", "deferred", "wontfix"})
MAX_QUOTE = 300
ANSWERABLE_REF_PREFIXES = ("antwoord:", "pr:")  # the only refs `answer` records; the source decides the others

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
        opened_by: str, now: Optional[str] = None, state_dir: Optional[Path] = None,
        ceiling: Optional[int] = None, reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Open a point of source promote (1), besluit (2) or vraag (8); None when it already exists.
    A ``pr:<n>`` point needs ``ceiling`` and ``reason`` (ADR-088 decision 6, see ``_ask_pr``).
    Raises RefRefused (bad ref or bron/ref mismatch) or operator_queue.QueueRefused (secret, bad field)."""
    if bron not in BRON:
        raise RefRefused("bron is one of: " + ", ".join(BRON))
    if bron == "vraag" and ref == "antwoord":
        ref = question_ref(vraag)
    kind, _ = parse_ref(ref)
    if bron == "promote" and kind != "dlv":
        raise RefRefused("bron promote takes only a dlv:<id> ref")
    if kind == "pr":
        if bron != "besluit":
            raise RefRefused("a pr:<n> ref opens only with bron besluit")
        return _ask_pr(project_id, ref=ref, vraag=vraag, sluit_bij=sluit_bij, kopieerblok=kopieerblok,
                       opened_by=opened_by, now=now, state_dir=state_dir, ceiling=ceiling, reason=reason)
    if ceiling is not None or reason is not None:
        raise RefRefused("--ceiling and --reason are only for a pr:<n> ref")
    return oq.open_point(project_id, bron=BRON[bron], ref=ref, vraag=vraag, sluit_bij=sluit_bij,
                         kopieerblok=kopieerblok, opened_by=opened_by, now=now, state_dir=state_dir)

def _pr_points(state: Dict[str, Any], ref: str) -> List[Dict[str, Any]]:
    return sorted((p for p in state["points"].values() if p["bron"] == BRON["besluit"] and p["ref"] == ref),
                  key=lambda p: p["revisie"])

def _ask_pr(project_id: str, *, ref: str, ceiling: Any, reason: Any, now: Optional[str],
            state_dir: Optional[Path], **fields: Any) -> Optional[Dict[str, Any]]:
    """Revision +1 for a ceiling above the latest (or none), lapsing older open ones; the same or lower is refused."""
    if not oq._int(ceiling, 1) or reason not in oq.GRANT_REASONS:
        raise oq.QueueRefused("ask: a pr:<n> ref needs --ceiling (an integer above 0) and --reason, one of: "
                              + ", ".join(sorted(oq.GRANT_REASONS)))
    state = oq.read_state(project_id, state_dir=state_dir)
    if state["status"] != "ok":
        raise oq.QueueUnreadable(state["reason"], state["line"])
    mine = _pr_points(state, ref)
    if mine and mine[-1]["ceiling"] is not None and ceiling <= mine[-1]["ceiling"]:  # an older point has none
        raise oq.QueueRefused(f"ask: ceiling {ceiling} is not above ceiling {mine[-1]['ceiling']} of revision "
                              f"{mine[-1]['revisie']} for {ref}; no new point")
    revision = mine[-1]["revisie"] + 1 if mine else 1
    line = oq.open_point(project_id, bron=BRON["besluit"], ref=ref, revisie=revision, now=now, state_dir=state_dir,
                         extra={"ceiling": ceiling, "reason": reason}, **fields)
    for older in (p for p in mine if p["state"] == "open"):
        oq.close_point(project_id, older["point_id"], uitkomst="vervallen", bewijs=f"replaced by revision {revision}",
                       now=now, state_dir=state_dir)
    return line

def answer(project_id: str, point_id: str, *, quote: str, vastgelegd_door: str, now: Optional[str] = None,
           state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Record Vincent's own words for a chat question or a ``pr:`` point: one ``answer`` line, nothing else
    (decision 6, ADR-088 decision 6). A ``pr:`` point is refused once a higher revision of it exists."""
    state = oq.read_state(project_id, state_dir=state_dir)
    if state["status"] != "ok":
        raise oq.QueueUnreadable(state["reason"], state["line"])
    point = state["points"].get(point_id) or {}
    ref = str(point.get("ref"))
    newer = [p["revisie"] for p in _pr_points(state, ref) if p["revisie"] > point["revisie"]] if ref.startswith("pr:") else []
    for failed, why in ((not point, "the point does not exist"), (point.get("state") != "open", "the point is closed"),
                        (not ref.startswith(ANSWERABLE_REF_PREFIXES), "only an antwoord: or pr: point is answered here; the source decides the others"),
                        (bool(newer), f"revision {max(newer or [0])} of {ref} replaces this point"),
                        (not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE, f"the quote is empty or over {MAX_QUOTE} characters")):
        if failed:
            raise oq.QueueRefused("answer: " + why)
    return oq.append_line(project_id, "answer", {"point_id": point_id, "citaat": quote,
                                                 "vastgelegd_door": vastgelegd_door}, now=now, state_dir=state_dir)

def _answer_ts(project_id: str, pid: str, state_dir: Optional[Path]) -> Optional[str]:
    return next((r["ts"] for r in oq.answers(project_id, state_dir=state_dir) if r.get("point_id") == pid), None)

class _Source:
    def __init__(self, project_id: str, sdir: Path, *, now: Optional[str] = None,
                 state_dir: Optional[Path] = None) -> None:
        self.project_id, self.sdir, self.now, self.state_dir, self._conn = project_id, sdir, now, state_dir, None
        self.cap_read: Optional[Dict[str, Any]] = None

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

    def visited_phases(self, track_id: str) -> set:
        rows = self.conn().execute("SELECT to_phase FROM track_phase_history WHERE track_id = ?", (track_id,)).fetchall()
        return {r["to_phase"] for r in rows}

    def config_audit_after(self, key: str, asked_at: str) -> Optional[str]:
        """The ``changed_at`` of a ``project_config_audit`` row after ``asked_at`` for a key that is in ``project_config``."""
        conn = self.conn()
        if conn.execute("SELECT 1 FROM project_config WHERE project_id = ? AND config_key = ?",
                        (self.project_id, key)).fetchone() is None:
            return None
        rows = conn.execute("SELECT changed_at FROM project_config_audit WHERE project_id = ? AND config_key = ? "
                            "ORDER BY id", (self.project_id, key)).fetchall()
        asked = _moment(asked_at)
        return next((r["changed_at"] for r in rows if _moment(r["changed_at"]) > asked), None)

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

def _moment(text: Any) -> datetime:
    """An ISO instant as an aware datetime; raises ValueError for anything else (an unreadable source)."""
    when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=datetime.now().astimezone().tzinfo)

_RANK = {"queued": 0, "active": 1, "done": 2}

def _eval_track(src: _Source, track_id: str, phase: str, point: Dict[str, Any]) -> tuple:
    now = src.track_phase(track_id)
    if now is None:
        return ("unknown", f"track {track_id} is not readable")
    visited = src.visited_phases(track_id) | {now, "queued"}
    if now == "done":
        if phase in visited:
            return ("closed", "beantwoord", f"tracks.phase=done, {phase} visited", "onbekend")
        return ("closed", "vervallen", f"tracks.phase=done without {phase}", "onbekend")
    reached = now == phase if "parked" in (phase, now) else _RANK.get(now, -1) >= _RANK.get(phase, 99)
    return ("closed", "beantwoord", f"tracks.phase={now}", "onbekend") if reached else _OPEN

def _eval_config(src: _Source, key: str, point: Dict[str, Any]) -> tuple:
    changed = src.config_audit_after(key, point["asked_at"])
    return _OPEN if changed is None else ("closed", "beantwoord", f"project_config_audit.changed_at={changed}", "onbekend")

def _eval_verdict(src: _Source, verdict_id: str, point: Dict[str, Any]) -> tuple:
    """The match of source 7 (``read_cap_verdict``), imported: its point for the same ``verdict:`` ref decides."""
    if src.cap_read is None:
        src.cap_read = steering.read_cap_verdict(src.project_id, data_root=src.sdir.parent, now=src.now,
                                                 state_dir=src.state_dir)
    if src.cap_read["dekking"] != "gelezen":
        return ("unknown", f"source 7 unreadable: {src.cap_read['reden']}")
    ledger = oq.read_state(src.project_id, state_dir=src.state_dir)
    if ledger["status"] != "ok":
        return ("unknown", ledger["reason"])
    mine = [p for p in ledger["points"].values() if p["bron"] == 7 and p["ref"] == point["ref"]]
    if not mine:
        return ("unknown", f"source 7 has no point for {point['ref']}")
    state = mine[-1]["state"]
    if state == "open":
        return _OPEN
    return ("closed", state, f"bron 7: {mine[-1]['bewijs']}", "onbekend")

def gh_pr_state(project_id: str, pr: int) -> str:
    """``OPEN``, ``MERGED`` or ``CLOSED`` from ``gh pr view`` in the project's checkout; raises OSError naming why
    for anything else (no checkout, gh fails or times out, an unknown state): unreadable is never a state."""
    repo = project_checkout_path(project_id)
    if repo is None:
        raise OSError(f"no checkout registered for {project_id}")
    try:
        done = subprocess.run(["gh", "pr", "view", str(pr), "--json", "state", "--jq", ".state"], cwd=str(repo),
                              capture_output=True, text=True, timeout=20)
    except subprocess.SubprocessError as exc:
        raise OSError(f"gh pr view {pr}: {type(exc).__name__}") from exc
    state = done.stdout.strip()
    if done.returncode != 0 or state not in ("OPEN", "MERGED", "CLOSED"):
        raise OSError(f"gh pr view {pr}: exit {done.returncode}, state {state[:40]!r}")
    return state

def _eval_pr(src: _Source, number: str, point: Dict[str, Any]) -> tuple:
    """ADR-088 decision 6: an answer closes as beantwoord; a higher revision or a merged/closed PR as vervallen."""
    ts = _answer_ts(src.project_id, point["point_id"], src.state_dir)
    if ts is not None:
        return ("closed", "beantwoord", ts, "t0_namens_operator")
    ledger = oq.read_state(src.project_id, state_dir=src.state_dir)
    if ledger["status"] != "ok":
        return ("unknown", ledger["reason"])
    newer = [p["revisie"] for p in _pr_points(ledger, point["ref"]) if p["revisie"] > point["revisie"]]
    if newer:
        return ("closed", "vervallen", f"replaced by revision {max(newer)}", "onbekend")
    state = gh_pr_state(src.project_id, int(number))
    return _OPEN if state == "OPEN" else ("closed", "vervallen", f"gh pr view {number}: state={state}", "onbekend")

def _evaluate(src: _Source, project_id: str, point: Dict[str, Any], state_dir: Optional[Path]) -> tuple:
    try:
        kind, parts = parse_ref(point["ref"])
    except RefRefused:
        return ("unknown", "ref is not readable")
    try:
        if kind == "antwoord":
            ts = _answer_ts(project_id, point["point_id"], state_dir)
            return _OPEN if ts is None else ("closed", "beantwoord", ts, "t0_namens_operator")
        evaluator = {"dlv": _eval_dlv, "oi": _eval_oi, "track": _eval_track, "config": _eval_config,
                     "verdict": _eval_verdict, "pr": _eval_pr}.get(kind)
        if evaluator in (_eval_track, _eval_config, _eval_verdict, _eval_pr):
            return evaluator(src, *parts, point)
        if evaluator:
            return evaluator(src, *parts)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, oq.QueueRefused, oq.QueueUnreadable) as exc:  # vnx-silent-except: an unreadable source is an unknown line
        return ("unknown", f"source unreadable ({type(exc).__name__}: {exc})")
    return _OPEN

def _planning_cli() -> Any:
    """``planning_cli`` is imported here, not at module level: it edits ``sys.path`` and loads the track layer on import."""
    try:
        return importlib.import_module("planning_cli")
    except ImportError:  # vnx-silent-except: imported as scripts.lib.operator_queue_ask
        return importlib.import_module("scripts.planning_cli")

def promotion_refusal(src: _Source, point: Dict[str, Any]) -> Optional[str]:
    """Why a promote of this source-1 point is refused today, or None; read-only, never promotes (ADR-056, ADR-076 decision 7)."""
    try:
        kind, parts = parse_ref(point["ref"])
        row = src.dispatch(parts[0]) if kind == "dlv" else None
        if row is None:
            return "onleesbaar: the dispatch row is not readable"
        track_id = row["track"] if "track" in row.keys() else None
        cli, said = _planning_cli(), io.StringIO()
        with contextlib.redirect_stderr(said):
            blocked = bool(track_id) and cli._track_blocks_promotion(src.sdir, src.project_id, parts[0], track_id)
        if blocked:
            return said.getvalue().strip() or f"track {track_id!r} blocks promotion"
        return cli._promotion_refusal(src.sdir, src.project_id, row, track_id)[0]
    except Exception as exc:  # vnx-silent-except: an unreadable gate is a stated reason, never "nothing blocks"
        return f"onleesbaar: {type(exc).__name__}: {exc}"

def read_asked(project_id: str, *, data_root: Optional[Path] = None, now: Optional[str] = None,
               state_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """One reader dict per source (1, 2, 8); Opens nothing."""
    out = {b: {"bron": b, "dekking": "gelezen", "reden": None, "opened": [], "closed": [], "unknown": []}
           for b in BRON.values()}
    out[1]["weigerreden"] = {}
    state = oq.read_state(project_id, state_dir=state_dir)
    if state["status"] != "ok":
        for result in out.values():
            result.update(dekking="unknown", reden=state["reason"])
        return list(out.values())
    root = Path(data_root) if data_root is not None else resolve_central_data_dir(project_id)
    src = _Source(project_id, root / "state", now=now, state_dir=state_dir)
    try:
        for point in state["points"].values():
            if point["state"] == "open" and point["bron"] in out:
                _apply(src, project_id, point, out[point["bron"]], now, state_dir)
                if point["bron"] == 1 and point["point_id"] not in out[1]["closed"]:
                    out[1]["weigerreden"][point["point_id"]] = promotion_refusal(src, point)
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
