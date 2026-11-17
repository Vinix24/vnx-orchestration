"""Readers for operator-queue sources 5, 6 and 7 (ADR-076 decisions 2, 3, 5, 7 and 14).

Source 5 is a steering proposal, source 6 a permission proposal (same store, kind in ``PERMISSION_KINDS``),
source 7 a cap-verdict ``mergen-met-OI``. ``vervallen`` is never ``beantwoord``. The PR head and PR state come
from local files and refs only, never the network. A reader opens its source read-only (decision 14).
A reader opens a point on the source's open state and closes it
only on the source's own answer; it writes only on a change, so a second read of the same state writes
nothing. Unreadable is ``unknown`` and closes nothing (ADR-063); an unbound steering store is
``niet_gebonden``, never "0 points read".
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import operator_queue as oq
    import review_cap_verdicts as rcv
    import steering_proposal_store as sps
    from coordination_db import db_path_from_state_dir
    from forge_project_target import project_checkout_path
    from project_root import resolve_central_data_dir
except ImportError:  # vnx-silent-except: imported as scripts.lib.operator_queue_steering_verdict
    from scripts.lib import operator_queue as oq
    from scripts.lib import review_cap_verdicts as rcv
    from scripts.lib import steering_proposal_store as sps
    from scripts.lib.coordination_db import db_path_from_state_dir
    from scripts.lib.forge_project_target import project_checkout_path
    from scripts.lib.project_root import resolve_central_data_dir

BINDING_FILE, BINDING_SCHEMA = "steering_sources.json", "steering_sources/1"
PERMISSION_KINDS = frozenset({"permissie", "bouwer_allowlist", "bouwgewoonte", "alleen_melding"})  # ADR-075
_SHA = re.compile(r"[0-9a-f]{40}")
_GIT_TIMEOUT = 10


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
        elif entry["status"] == "suppressed_hard_stop":
            _close(res, ctx.project_id, point["point_id"], "vervallen", "onderdrukt door een harde stop", "onbekend",
                   ctx.now, ctx.state_dir)
        elif point["revisie"] < current:
            _close(res, ctx.project_id, point["point_id"], "vervallen", f"vervangen door revisie {current}", "onbekend",
                   ctx.now, ctx.state_dir)
    if entry["status"] == "pending" and not any(p["revisie"] == current for p in mine):
        melding = entry["artefact_soort"] == sps.MELDING_KIND  # a point to see, never to approve
        vraag = (f"Melding (harde stop) voor {entry['doelpad']}: gezien?" if melding else
                 f"Steering-voorstel ({entry['artefact_soort']}) voor {entry['doelpad']}: goedkeuren of afwijzen?")
        _open(res, ctx.project_id, ref, current, vraag, "acknowledged in de steering-store" if melding else
              "approved, rejected of applied in de steering-store", entry["revisions"][current - 1]["recorded_at"],
              ctx.now, ctx.state_dir)


def read_steering(project_id: str, *, data_root: Optional[Path] = None, now: Optional[str] = None,
                  state_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Sources 5 and 6: a list of two reader dicts, ``[source 5, source 6]``."""
    results = [_blank(5), _blank(6)]
    src = _root(project_id, data_root) / "state"
    problem = _binding_problem(src, project_id)
    if problem:
        return _fail(results, "niet_gebonden", problem)
    try:
        entries, records = sps.read_proposals(src, project_id)
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


# ---------------------------------------------------------------------------
# Source 7: cap-verdict mergen-met-OI
# ---------------------------------------------------------------------------


def _load_verdicts(src: Path, project_id: str) -> List[Any]:
    path = db_path_from_state_dir(src)
    if not path.is_file():
        raise _Unreadable(f"geen runtime store op {path}")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10.0)  # read-only: never writes the source
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (rcv.TABLE,)).fetchone() is None:
                raise _Unreadable(f"tabel {rcv.TABLE} ontbreekt in {path}")
            cols = ", ".join(f.name for f in fields(rcv.CapVerdict))
            rows = conn.execute(f"SELECT {cols} FROM {rcv.TABLE} WHERE project_id = ? ORDER BY created_at, verdict_id",
                                (project_id,)).fetchall()
        finally:
            conn.close()
        return [rcv.CapVerdict.from_row(r) for r in rows]
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise _Unreadable(f"{rcv.TABLE} onleesbaar: {exc}") from exc


def _read_ndjson(path: Path) -> Optional[List[Dict[str, Any]]]:
    """The object lines of an ndjson file; None when the file cannot be read or any non-empty line is torn or not an
    object (a damaged ledger is unreadable, never a shorter history). An empty file is an empty list."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):  # vnx-silent-except: unreadable is None, which the caller turns into unknown
        return None
    rows = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError:  # vnx-silent-except: a torn line makes the whole ledger None (unknown), never a shorter history
            return None
        if not isinstance(row, dict):
            return None
        rows.append(row)
    return rows


def default_pr_state_of(src: Path) -> Callable[[int], Optional[str]]:
    """``merged`` when the door's ``pr_merged`` receipt names the PR, ``open`` when the receipt ledger does not,
    None when the ledger cannot be read. A PR closed without a merge shows in no local receipt."""
    def pr_state_of(pr: int) -> Optional[str]:
        rows = _read_ndjson(Path(src) / "t0_receipts.ndjson")
        if rows is None:
            return None
        return "merged" if any(r.get("event_type") == "pr_merged" and r.get("pr_number") == pr for r in rows) else "open"
    return pr_state_of


def default_head_of(src: Path, repo: Optional[Path] = None) -> Callable[[int], Optional[str]]:
    """The head of a PR from ``refs/remotes/origin/<branch>`` in ``repo`` (``git rev-parse``, no fetch).
    The branch is the latest one the review rounds ledger records for the PR; None when any link is missing,
    also when no ``repo`` is known (the project is not in the registry)."""
    def head_of(pr: int) -> Optional[str]:
        rows = _read_ndjson(Path(src) / "review_gates" / "rounds.ndjson")
        if rows is None:
            return None
        branches = [r["branch"] for r in rows if r.get("pr_number") == pr and isinstance(r.get("branch"), str) and r["branch"]]
        try:
            done = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                                   f"refs/remotes/origin/{branches[-1]}"], capture_output=True, text=True,
                                  timeout=_GIT_TIMEOUT) if branches and repo is not None else None
        except (OSError, subprocess.SubprocessError):  # vnx-silent-except: no head is None, never a match
            return None
        out = done.stdout.strip() if done is not None and done.returncode == 0 else ""
        return out if _SHA.fullmatch(out) else None
    return head_of


def _bewijs_of(approval: Dict[str, Any]) -> str:
    keep = ("ts", "event", "action", "verdict_id", "pr_number", "head_sha", "operator")
    return "operator_approvals.ndjson: " + json.dumps({k: approval.get(k) for k in keep}, sort_keys=True)


def _terminal(v: Any, verdicts: List[Any], pr_state: Optional[str]) -> Optional[str]:
    """The evidence that makes the verdict moot (``vervallen``), or None."""
    newer = [w for w in verdicts if w.pr_number == v.pr_number and w.created_at > v.created_at]
    if newer:
        return f"nieuwer verdict {newer[-1].verdict_id} op PR {v.pr_number}"
    return f"PR {v.pr_number} is {pr_state}" if pr_state in ("merged", "closed") else None


def _cap_point(ctx: _Ctx, res: Dict[str, Any], src: Path, v: Any, verdicts: List[Any],
               head_of: Callable, pr_state_of: Callable) -> None:
    ref = f"verdict:{v.verdict_id}"
    mine = _points_of(ctx.queue, 7, ref)
    point = mine[-1] if mine else None
    if point is not None and point["state"] != "open":
        return
    pr_state, head = pr_state_of(v.pr_number), head_of(v.pr_number)
    moot = _terminal(v, verdicts, pr_state)
    if moot is not None:
        if point is not None:
            _close(res, ctx.project_id, point["point_id"], "vervallen", moot, "onbekend", ctx.now, ctx.state_dir)
        return
    if point is None:
        point = {"unknown_reden": None, "point_id": _open(
            res, ctx.project_id, ref, 1, f"PR {v.pr_number}: mergen met open items goedkeuren (verdict {v.verdict_id})?",
            f"een akkoordregel voor verdict {v.verdict_id}, PR {v.pr_number}, kop {v.head_sha}", v.created_at,
            ctx.now, ctx.state_dir)}
    reden = _doubt(v, pr_state, head)
    if reden:
        _mark_unknown(res, ctx.project_id, point, reden, ctx.now, ctx.state_dir)
        return
    approval = rcv.merge_approval(src, v)
    if approval is not None:
        _close(res, ctx.project_id, point["point_id"], "beantwoord", _bewijs_of(approval), "onbekend", ctx.now,
               ctx.state_dir)


def _doubt(v: Any, pr_state: Optional[str], head: Optional[str]) -> Optional[str]:
    """Why the verdict's head or PR state cannot be trusted (the point stays open), or None."""
    if pr_state is None:
        return "pr_staat_onleesbaar"
    if pr_state != "open":
        return f"pr_staat_onbekend: {pr_state}"
    if head is None:
        return "kop_onleesbaar"
    if head.strip().lower() != v.head_sha.strip().lower():
        return "kop_verschoven"  # an approval on an old head does not count
    return None


def read_cap_verdict(project_id: str, *, data_root: Optional[Path] = None, now: Optional[str] = None,
                     state_dir: Optional[Path] = None, head_of: Optional[Callable[[int], Optional[str]]] = None,
                     pr_state_of: Optional[Callable[[int], Optional[str]]] = None,
                     repo: Optional[Path] = None) -> Dict[str, Any]:
    """Source 7. ``head_of(pr)`` and ``pr_state_of(pr)`` default to ``default_head_of`` and ``default_pr_state_of``."""
    res, src = _blank(7), _root(project_id, data_root) / "state"
    head_of = head_of or default_head_of(src, repo if repo is not None else project_checkout_path(project_id))
    pr_state_of = pr_state_of or default_pr_state_of(src)
    try:
        verdicts = _load_verdicts(src, project_id)
        ctx = _Ctx(project_id, _ledger_state(project_id, state_dir), now, state_dir)
        for v in (w for w in verdicts if w.outcome == rcv.MERGEN_MET_OI):
            _cap_point(ctx, res, src, v, verdicts, head_of, pr_state_of)
    except (_Unreadable, oq.QueueRefused, oq.QueueUnreadable) as exc:
        res.update(dekking="unknown", reden=str(exc))
    return res


READERS = (("steering", read_steering), ("cap_verdict", read_cap_verdict))