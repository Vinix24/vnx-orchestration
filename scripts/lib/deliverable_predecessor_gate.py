"""deliverable_predecessor_gate — is every hard predecessor of a fire satisfied? (ADR-096 §3-4, §12-14, B2).

Only a ``pr_merged`` receipt (``doc``/``post``: a T0 ``accept``) satisfies; what cannot be read is ``predecessor_unknown``."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Set, Tuple

import coordination_db
from blast_radius import _round_base as deliverable_of
from open_outcomes import DecisionLogUnreadable, read_outcome_decisions_strict

MODE_KEY = "VNX_DELIVERABLE_PREDECESSOR_MODE"
MODES = ("off", "warn", "refuse")
LEDGER = "deliverable_predecessor.ndjson"
REASON_UNMERGED, REASON_RETIRED, REASON_UNKNOWN = "deliverable_predecessor_unmerged", "predecessor_retired", "predecessor_unknown"
RETIRED_STATES = frozenset({"cancelled", "superseded", "retired", "withdrawn"})
DECISION_KINDS = frozenset({"doc", "post"})
Edge = namedtuple("Edge", "predecessor kind state output_kind")  # state None: the predecessor has no row
Finding = namedtuple("Finding", "predecessor hard reason")


def judge(edges: Iterable[Edge], merged: Optional[Set[str]], accepted: Optional[Set[str]]) -> Tuple[Finding, ...]:
    """One finding per unsatisfied edge; ``merged``/``accepted`` of None means that ledger was unreadable."""
    out = []
    for e in edges:
        by_decision = e.output_kind in DECISION_KINDS
        kind_known = by_decision or e.output_kind in (None, "pr")  # None is the repo default, pr
        proof = accepted if by_decision else merged
        if e.state in RETIRED_STATES:
            reason = REASON_RETIRED  # ADR-096 §4: whatever the proof says
        elif e.state not in coordination_db.DISPATCH_STATES or not kind_known or proof is None:
            reason = REASON_UNKNOWN  # no row (None), an unknown status or kind, an unreadable ledger
        elif e.predecessor in proof:
            continue  # only now does positive proof decide
        else:
            reason = REASON_UNMERGED
        out.append(Finding(e.predecessor, e.kind != "soft", reason))  # an unknown kind counts as hard
    return tuple(out)


def read_edges(db_path: Path, project_id: str, deliverable: str) -> Tuple[Edge, ...]:
    """Active rows (no ``removed_at``) of ``deliverable``; raises ``sqlite3.Error`` on a DB it cannot read."""
    if not db_path.exists():
        return ()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'deliverable_dependencies'").fetchone():
            return ()
        return tuple(Edge(*r) for r in conn.execute(
            "SELECT dd.depends_on_id, dd.kind, d.state, d.output_kind FROM deliverable_dependencies dd "
            "LEFT JOIN dispatches d ON d.project_id = dd.project_id AND d.dispatch_id = dd.depends_on_id "
            "WHERE dd.project_id = ? AND dd.deliverable_id = ? AND dd.removed_at IS NULL ORDER BY 1",
            (project_id, deliverable)))
    finally:
        conn.close()


def read_merged(receipts: Path, project_id: str, wanted: Set[str]) -> Optional[Set[str]]:
    """The ``wanted`` ids with a qualifying ``pr_merged`` receipt; None when the ledger or a pr_merged line is unreadable."""
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
                did = rec.get("dispatch_id") if isinstance(rec, dict) else None
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
    return judge(edges, read_merged(state_dir / "t0_receipts.ndjson", project_id, {e.predecessor for e in edges}), accepted)


def record(state_dir: Path, project_id: str, dispatch_id: str, findings: Iterable[Finding], *, mode: str,
           outcome: str, override_reason: str = "") -> None:
    """One line per hard finding, key (project_id, ts, dispatch_id, seq), in one flocked append; raises OSError."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    lines = [{"project_id": project_id, "ts": ts, "dispatch_id": dispatch_id, "seq": seq,
              "candidate": deliverable_of(dispatch_id), "predecessor": f.predecessor, "reason": f.reason,
              "mode": mode, "outcome": outcome, "override_reason": override_reason or None}
             for seq, f in enumerate(f for f in findings if f.hard)]
    payload = memoryview("".join(json.dumps(x, sort_keys=True, allow_nan=False) + "\n" for x in lines).encode())
    fd = os.open(state_dir / LEDGER, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        while payload:
            written = os.write(fd, payload)
            if written <= 0:
                raise OSError(f"short write to {LEDGER}")
            payload = payload[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
