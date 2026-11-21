"""deliverable_predecessor_gate — is every hard predecessor of a fire satisfied? (ADR-096 §3-4, §12-14, B2).

Only a ``pr_merged`` receipt (``doc``/``post``: a T0 ``accept``) satisfies; what cannot be read is ``predecessor_unknown``.
A deliverable shipped in parts satisfies once closed with its PR list and every listed PR merged from ``dispatch/P[-part]`` (OI-3054)."""
from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import coordination_db
from blast_radius import _round_base as deliverable_of
from open_outcomes import DecisionLogUnreadable, read_outcome_decisions_strict

MODE_KEY = "VNX_DELIVERABLE_PREDECESSOR_MODE"
MODES = ("off", "warn", "refuse")
LEDGER = "deliverable_predecessor.ndjson"
REASON_UNMERGED, REASON_RETIRED, REASON_UNKNOWN = "deliverable_predecessor_unmerged", "predecessor_retired", "predecessor_unknown"
RETIRED_STATES = frozenset({"cancelled", "superseded", "retired", "withdrawn"})
DECISION_KINDS = frozenset({"doc", "post"})
CLOSED_STATES = frozenset({"completed", "expired"})  # the two endings `vnx deliverable close` writes pr_ref with
Edge = namedtuple("Edge", "predecessor kind state output_kind pr_list", defaults=(None,))  # state None: no row
Finding = namedtuple("Finding", "predecessor hard reason detail", defaults=(None,))
_PR_NUMBER = re.compile(r"[0-9]+")


def parse_pr_list(raw: object) -> Optional[Tuple[int, ...]]:
    """``"#3118,#3117"`` -> ``(3118, 3117)``; None for anything else (not a string, empty, a non-number, zero)."""
    if not isinstance(raw, str):
        return None
    out = [t.strip().removeprefix("#") for t in raw.split(",")]
    if not all(_PR_NUMBER.fullmatch(t) and int(t) > 0 for t in out):
        return None
    return tuple(int(t) for t in out)


def judge_parts(e: Edge, by_pr: Dict[int, List[object]]) -> Tuple[Optional[str], Optional[str]]:
    """(None, None) when every PR the close listed merged from ``dispatch/P`` or ``dispatch/P-<part>``, else (reason, detail)."""
    numbers = parse_pr_list(e.pr_list)
    if numbers is None:
        return REASON_UNKNOWN, f"closing PR list unreadable: {e.pr_list!r}"
    want = f"dispatch/{e.predecessor}"
    foreign = [n for n in numbers if by_pr.get(n) and not all(
        isinstance(b, str) and deliverable_of(b) == want for b in by_pr[n])]
    if foreign:
        return REASON_UNKNOWN, f"PR #{foreign[0]} merged from a branch outside {want}"
    open_prs = [n for n in numbers if n not in by_pr]
    if open_prs:
        return REASON_UNMERGED, "PR " + ", ".join(f"#{n}" for n in open_prs) + " not merged"
    return None, None


def judge(edges: Iterable[Edge], merged: Optional[Set[str]], accepted: Optional[Set[str]],
          by_pr: Optional[Dict[int, List[object]]] = None) -> Tuple[Finding, ...]:
    """One finding per unsatisfied edge; ``merged``/``accepted`` of None means that ledger was unreadable."""
    out = []
    for e in edges:
        detail = None
        by_decision = e.output_kind in DECISION_KINDS
        kind_known = by_decision or e.output_kind in (None, "pr")  # None is the repo default, pr
        proof = accepted if by_decision else merged
        if e.state in RETIRED_STATES:
            reason = REASON_RETIRED  # ADR-096 §4: whatever the proof says
        elif e.state not in coordination_db.DISPATCH_STATES or not kind_known or proof is None:
            reason = REASON_UNKNOWN  # no row (None), an unknown status or kind, an unreadable ledger
        elif e.predecessor in proof:
            continue  # only now does positive proof decide
        elif not by_decision and e.state in CLOSED_STATES and e.pr_list is not None:
            reason, detail = judge_parts(e, by_pr) if by_pr is not None else (REASON_UNKNOWN, "receipts unreadable")
            if reason is None:
                continue  # every listed PR is a merged part of this deliverable
        else:
            reason = REASON_UNMERGED
        out.append(Finding(e.predecessor, e.kind != "soft", reason, detail))  # an unknown kind counts as hard
    return tuple(out)


def read_edges(db_path: Path, project_id: str, deliverable: str) -> Tuple[Edge, ...]:
    """Active rows (no ``removed_at``) of ``deliverable``; raises ``sqlite3.Error`` on a DB it cannot read."""
    if not db_path.exists():
        return ()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'deliverable_dependencies'").fetchone():
            return ()
        has_pr_ref = any(r[1] == "pr_ref" for r in conn.execute("PRAGMA table_info(dispatches)"))
        return tuple(Edge(*r) for r in conn.execute(
            "SELECT dd.depends_on_id, dd.kind, d.state, d.output_kind, " + ("d.pr_ref" if has_pr_ref else "NULL") +
            " FROM deliverable_dependencies dd "
            "LEFT JOIN dispatches d ON d.project_id = dd.project_id AND d.dispatch_id = dd.depends_on_id "
            "WHERE dd.project_id = ? AND dd.deliverable_id = ? AND dd.removed_at IS NULL ORDER BY 1",
            (project_id, deliverable)))
    finally:
        conn.close()


def read_merged(receipts: Path, project_id: str, wanted: Set[str],
                by_pr: Optional[Dict[int, List[object]]] = None) -> Optional[Set[str]]:
    """The ``wanted`` ids with a qualifying ``pr_merged`` receipt; None when the ledger or a pr_merged line is unreadable.

    ``by_pr`` (filled in place): each of its keys, a PR number, gets the ``branch`` of every merged receipt of this project."""
    found: Set[str] = set()
    try:
        with open(receipts, "rb") as fh:
            for raw in fh:
                if b"pr_merged" not in raw or not raw.endswith(b"\n"):
                    continue  # no candidate line, or a writer mid-append
                try:
                    rec = json.loads(raw)
                except ValueError:
                    return None  # a broken pr_merged line may be the one we look for
                if not isinstance(rec, dict):
                    continue  # not a receipt
                num = rec.get("pr_number")
                if (by_pr is not None and type(num) is int and num in by_pr and
                        (rec.get("event_type"), rec.get("conclusion"), rec.get("project_id")) == ("pr_merged", "merged", project_id)):
                    by_pr[num].append(rec.get("branch"))
                did = rec.get("dispatch_id")
                base = deliverable_of(did) if isinstance(did, str) else None
                if base in wanted and (rec.get("event_type"), rec.get("conclusion"), rec.get("project_id"),
                                       rec.get("branch")) == ("pr_merged", "merged", project_id, f"dispatch/{base}"):
                    found.add(base)
    except OSError:
        return None
    return found


def assess(dispatch_id: str, project_id: str, state_dir: Path) -> Tuple[Finding, ...]:
    """Every unsatisfied predecessor of the fire ``dispatch_id`` (its round suffix taken off)."""
    try:
        edges = read_edges(state_dir / "runtime_coordination.db", project_id, deliverable_of(dispatch_id))
    except sqlite3.Error as exc:
        return (Finding(f"(dependencies unreadable: {type(exc).__name__})", True, REASON_UNKNOWN),)
    if not edges:
        return ()
    log, accepted = state_dir / "t0_decision_log.jsonl", set()
    if log.exists() and any(e.output_kind in DECISION_KINDS for e in edges):  # a missing log holds no accept
        try:
            accepted = {d for d, r in read_outcome_decisions_strict(log, project_id).items() if r.get("decision") == "accept"}
        except (OSError, DecisionLogUnreadable):
            accepted = None
    by_pr: Dict[int, List[object]] = {n: [] for e in edges for n in (parse_pr_list(e.pr_list) or ())}
    merged = read_merged(state_dir / "t0_receipts.ndjson", project_id, {e.predecessor for e in edges}, by_pr)
    return judge(edges, merged, accepted, {n: b for n, b in by_pr.items() if b} if merged is not None else None)


def record(state_dir: Path, project_id: str, dispatch_id: str, findings: Iterable[Finding], *, mode: str,
           outcome: str, override_reason: str = "") -> None:
    """One line per hard finding, key (project_id, ts, dispatch_id, seq), in one flocked append; raises OSError."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    lines = [{"project_id": project_id, "ts": ts, "dispatch_id": dispatch_id, "seq": seq,
              "candidate": deliverable_of(dispatch_id), "predecessor": f.predecessor, "reason": f.reason,
              "mode": mode, "outcome": outcome, "override_reason": override_reason or None}
             for seq, f in enumerate(f for f in findings if f.hard)]
    append_lines(state_dir / LEDGER, lines)


def append_lines(path: Path, lines: Iterable[dict]) -> None:
    """Append ``lines`` as NDJSON in one flocked write, looping on a short write; raises OSError (or ValueError on NaN)."""
    payload = memoryview("".join(json.dumps(x, sort_keys=True, allow_nan=False) + "\n" for x in lines).encode())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        while payload:
            written = os.write(fd, payload)
            if written <= 0:
                raise OSError(f"short write to {path.name}")
            payload = payload[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
