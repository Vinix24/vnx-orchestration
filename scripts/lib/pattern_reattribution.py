#!/usr/bin/env python3
"""pattern_reattribution.py — fix ``project_id`` on mis-attributed pattern rows.

D3b (learning-loop-sluiten): ``success_patterns`` and ``antipatterns`` rows carry
a ``project_id`` stamp plus ``source_dispatch_ids`` (the dispatches the pattern
was learned from). Some rows were stamped with a ``project_id`` that does not
match the project that actually ran their source dispatches — the per-project
injection selector (``intelligence_sources/proven_pattern.py`` /
``failure_prevention.py`` ``_query_central``) reads ``WHERE project_id = ?``,
so a mis-attributed row is offered to the wrong project (or to no project at
all) forever.

D3c (a follow-up PR) will deduplicate on ``(project_id, pattern_type, title)``,
so this reattribution must land first — deduping before the project_id is
correct would merge rows that belong to different tenants.

Ground truth (ADR-068 besluit 2): per dispatch_id, a vote over its receipt
copies under ``<root>/<project>/state/``. A copy votes for its store, its body
``project_id`` and the ``/.vnx-data/<project>/`` root of its report path; a
sentinel or an unknown project is no vote. P wins with two or more votes and
more than all others together. The fail-closed row categories (besluit 3) are
in :func:`decide_row`; only ``moves`` acts, and ``--apply`` refuses (exit 3) a
move into another DB file: a transfer (``qi_transfer_apply.py``, besluit 13-17).

Safe to move: ``id`` is ``INTEGER PRIMARY KEY AUTOINCREMENT`` on both tables,
so it is already globally unique across every project_id: the composite
unique index ``(project_id, id)`` can never be violated by changing
``project_id`` alone (ADR-007).

CLI:
  python3 scripts/lib/pattern_reattribution.py --dry-run [--db PATH] [--report PATH]
  python3 scripts/lib/pattern_reattribution.py --apply --db PATH [--report PATH]

``--apply`` requires an explicit ``--db`` (never resolves ~/.vnx-data by
default) so an operator cannot mutate the live store by omission. Idempotent:
re-running after ``--apply`` finds every moved row's resolved project already
equal to its (now-updated) ``project_id``, so nothing moves twice.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ledger_schema_version import iter_receipts
from pattern_dedup import _table_exists

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

STATUS_POSITIVE_OWN = "positive_own"
STATUS_STORE_ONLY = "store_only"
STATUS_MOVES = "moves"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_ABSENT = "absent"
STATUS_UNATTRIBUTED = "unattributed"

ALL_STATUSES = (
    STATUS_POSITIVE_OWN, STATUS_STORE_ONLY, STATUS_MOVES,
    STATUS_AMBIGUOUS, STATUS_ABSENT, STATUS_UNATTRIBUTED,
)

# ADR-068 besluit 2: placeholders that are never a vote and never an id.
_SENTINELS = frozenset({"", "unknown", "none", "null"})
_REPORT_ROOT_RE = re.compile(r"/\.vnx-data/([^/]+)/")

# Tables this reattribution scans, and the pattern_id prefix each row's id
# maps to in the junction tables (matches the convention in
# intelligence_sources/_common.py::_stable_item_id and
# intelligence_sources/_recording.py::_stamp_source_dispatch_id).
TABLES: Tuple[str, ...] = ("success_patterns", "antipatterns")
_PATTERN_ID_PREFIX: Dict[str, str] = {
    "success_patterns": "intel_sp_",
    "antipatterns": "intel_ap_",
}

# Junction tables that carry a per-row project_id keyed by pattern_id; must be
# kept consistent with a moved row's new project_id in the same transaction.
_JUNCTION_TABLES: Tuple[str, ...] = (
    "dispatch_pattern_offered",
    "pattern_usage",
    "pattern_injection_outcome",
)


# ---------------------------------------------------------------------------
# source_dispatch_ids parsing and signal votes (ADR-068 besluit 2, 3g, 11)
# ---------------------------------------------------------------------------

def _is_sentinel(value: object) -> bool:
    return not isinstance(value, str) or value.strip().lower() in _SENTINELS


def _project_vote(value: object, known_projects: Set[str]) -> Optional[str]:
    if _is_sentinel(value) or value.strip() not in known_projects:
        return None
    return value.strip()


def _parse_source_dispatch_ids(raw: object, known_projects: Set[str]) -> Optional[List[str]]:
    """The row's ids, or ``None`` (``unattributed``) unless it is a JSON list of strings.

    One unreadable element could name another project, so the rest never
    decides alone. Sentinels are no ids; a known-project prefix is stripped.
    """
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list) or not all(isinstance(e, str) for e in parsed):
        return None
    ids: List[str] = []
    for element in parsed:
        prefix, sep, rest = element.strip().partition(":")
        sid = rest.strip() if sep and prefix in known_projects else element.strip()
        if not _is_sentinel(sid) and sid not in ids:
            ids.append(sid)
    return ids or None


# ---------------------------------------------------------------------------
# Receipt-store discovery + dispatch_id -> project_id index
# ---------------------------------------------------------------------------

def discover_receipt_stores(vnx_data_root: Path) -> Dict[str, Path]:
    """Map project_id -> its ``t0_receipts.ndjson`` path under ``vnx_data_root``.

    Canonical layout is ``<vnx_data_root>/<project_id>/state/t0_receipts.ndjson``
    (ADR-007, same layout ``project_root.resolve_central_data_dir`` returns).
    A project directory with no receipt store yet is simply absent from the
    result — not an error.
    """
    stores: Dict[str, Path] = {}
    if not vnx_data_root.is_dir():
        return stores
    for state_dir in sorted(vnx_data_root.glob("*/state")):
        receipts_path = state_dir / "t0_receipts.ndjson"
        if receipts_path.is_file():
            stores[state_dir.parent.name] = receipts_path
    return stores


def build_dispatch_project_index(stores: Dict[str, Path], known_projects: Set[str]) -> Dict[str, Dict[str, int]]:
    """dispatch_id -> {project: votes} over every receipt copy (besluit 2).

    Built via :func:`ledger_schema_version.iter_receipts` (torn-tail-safe,
    schema-version aware, never writes). A copy is one store's lines for an
    id: one vote for the store, one per distinct known project in a body or
    report path. More lines stay one copy, so one signal never has two votes.
    """
    copies: Dict[str, Dict[str, Set[Tuple[str, Optional[str]]]]] = {}
    for store, path in stores.items():
        for receipt in iter_receipts(path):
            dispatch_id = receipt.get("dispatch_id")
            if _is_sentinel(dispatch_id):
                continue
            named = copies.setdefault(dispatch_id.strip(), {}).setdefault(
                store, {("store", _project_vote(store, known_projects))})
            named.add(("body", _project_vote(receipt.get("project_id"), known_projects)))
            for value in (receipt.get("report_path"), receipt.get("report_file")):
                root = _REPORT_ROOT_RE.search(value) if isinstance(value, str) else None
                named.add(("path", _project_vote(root.group(1), known_projects) if root else None))
    return {
        dispatch_id: dict(Counter(p for named in per_store.values() for _, p in named if p))
        for dispatch_id, per_store in copies.items()
    }


def vote_winner(tally: Dict[str, int]) -> Optional[str]:
    """The project with at least two votes and more than all others together."""
    total = sum(tally.values())
    return next((p for p, n in tally.items() if n >= 2 and n > total - n), None)


@dataclass
class AttributionIndex:
    votes: Dict[str, Dict[str, int]]  # dispatch_id -> {project: votes}
    qi_databases: Dict[str, Path]  # project -> its own quality_intelligence.db
    known_projects: Set[str]
    stores: Dict[str, Path]


def build_attribution_index(vnx_data_root: Path) -> AttributionIndex:
    """Everything a row decision reads, from ``<root>/<project>/state/`` only."""
    dirs = sorted(vnx_data_root.glob("*/state")) if vnx_data_root.is_dir() else []
    known = {d.parent.name for d in dirs if d.is_dir() and not _is_sentinel(d.parent.name)}
    dbs = {d.parent.name: d / "quality_intelligence.db" for d in dirs if d.parent.name in known}
    stores = discover_receipt_stores(vnx_data_root)
    qi_databases = {p: db for p, db in dbs.items() if db.is_file()}
    return AttributionIndex(build_dispatch_project_index(stores, known), qi_databases, known, stores)


def _same_file(a: Path, b: Path) -> bool:
    """True only when both paths are provably one file (fail closed)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Per-row decision
# ---------------------------------------------------------------------------

@dataclass
class ReattributionDecision:
    table: str
    row_id: int
    title: str
    old_project_id: str
    status: str
    new_project_id: str  # == old_project_id unless status == STATUS_MOVES
    source_ids: List[str] = field(default_factory=list)
    resolved_projects: List[str] = field(default_factory=list)
    destination_db: Optional[str] = None  # set only when status == STATUS_MOVES


def decide_row(
    *,
    table: str,
    row_id: int,
    title: str,
    project_id: str,
    source_dispatch_ids_raw: Optional[str],
    dispatch_index: AttributionIndex,
    db_path: Path,
) -> ReattributionDecision:
    """Apply the ADR-068 besluit 3 categories (a)-(g) to one pattern row. Never guesses."""
    source_ids = _parse_source_dispatch_ids(source_dispatch_ids_raw, dispatch_index.known_projects)
    with_receipt = [sid for sid in source_ids or [] if sid in dispatch_index.votes]
    winners = sorted({w for w in (vote_winner(dispatch_index.votes[s]) for s in with_receipt) if w})
    destination = winners[0] if len(winners) == 1 else None
    own_db = dispatch_index.qi_databases.get(destination) if destination else None
    if source_ids is None:  # (g) no ids, only sentinels, not a JSON list of strings
        status = STATUS_UNATTRIBUTED
    elif not with_receipt:  # (a, f) an id without a receipt never counts
        status = STATUS_ABSENT
    elif not winners:  # (e) one vote or a tie on every id: never verifies, never moves
        status = STATUS_STORE_ONLY
    elif own_db is None:  # (d) winners disagree, or the one winner has no own DB file
        status = STATUS_AMBIGUOUS
    elif destination == project_id and _same_file(own_db, db_path):  # (b) P's file, label P
        status = STATUS_POSITIVE_OWN
    else:  # (c) the consumer of P cannot see the row: wrong label, wrong file, or both
        status = STATUS_MOVES
    moves = status == STATUS_MOVES
    return ReattributionDecision(
        table, row_id, title, project_id, status,
        destination if moves else project_id, source_ids or [], winners,
        str(own_db) if moves else None,
    )


def scan_database(
    db_path: Path, dispatch_index: AttributionIndex,
) -> List[ReattributionDecision]:
    """Compute the reattribution decision for every row in every scanned table.

    Read-only: never mutates ``db_path``. Callers that want to apply moves
    pass this function's output to :func:`apply_decisions` against a
    (possibly different, e.g. explicit) DB path.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        decisions: List[ReattributionDecision] = []
        for table in TABLES:
            if not _table_exists(conn, table):
                continue
            rows = conn.execute(
                f"SELECT id, title, project_id, source_dispatch_ids "
                f"FROM {table} ORDER BY id"
            ).fetchall()
            for row in rows:
                decisions.append(
                    decide_row(
                        table=table,
                        row_id=row["id"],
                        title=row["title"] or "",
                        project_id=row["project_id"],
                        source_dispatch_ids_raw=row["source_dispatch_ids"],
                        dispatch_index=dispatch_index,
                        db_path=db_path,
                    )
                )
        return decisions
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Apply (mutating) — moved rows only, one transaction
# ---------------------------------------------------------------------------

def _reattribute_dispatch_pattern_offered(
    conn: sqlite3.Connection, pattern_id: str, old_pid: str, new_pid: str,
) -> None:
    """Move every ``dispatch_pattern_offered`` row for ``pattern_id``/``old_pid``.

    Per-row UPDATE, never INSERT-then-DELETE: a store whose real PRIMARY KEY
    predates ADR-007 (``(dispatch_id, pattern_id)`` — v17's original
    ``_migrate_v17``, no ``project_id``) allows only one row per
    ``(dispatch_id, pattern_id)`` in the first place, so an ``INSERT OR
    IGNORE`` of the "new" row collides with the very row it is meant to
    replace, is silently ignored, and the DELETE that follows then erases
    the only copy — the offer vanishes. Checking for a same-dispatch target
    row under ``new_pid`` first and UPDATEing in place is correct on that
    schema (there can never be a same-key sibling to collide with) and on a
    fresh bootstrap whose PRIMARY KEY does include ``project_id``
    (``intelligence_sources/_recording.py``), where a genuine collision is
    possible and is merged (keep the later ``offered_at``) instead of
    silently dropped.
    """
    if not _table_exists(conn, "dispatch_pattern_offered"):
        return
    old_rows = conn.execute(
        "SELECT dispatch_id, offered_at FROM dispatch_pattern_offered "
        "WHERE pattern_id = ? AND project_id = ?",
        (pattern_id, old_pid),
    ).fetchall()
    for row in old_rows:
        dispatch_id = row["dispatch_id"]
        target = conn.execute(
            "SELECT offered_at FROM dispatch_pattern_offered "
            "WHERE dispatch_id = ? AND pattern_id = ? AND project_id = ?",
            (dispatch_id, pattern_id, new_pid),
        ).fetchone()
        if target is None:
            conn.execute(
                "UPDATE dispatch_pattern_offered SET project_id = ? "
                "WHERE dispatch_id = ? AND pattern_id = ? AND project_id = ?",
                (new_pid, dispatch_id, pattern_id, old_pid),
            )
            continue
        # Genuine collision (only reachable on a PRIMARY KEY that includes
        # project_id): both projects already recorded an offer for this
        # exact (dispatch_id, pattern_id). Merge — keep the later
        # offered_at — instead of silently dropping the old row.
        if row["offered_at"] > target["offered_at"]:
            conn.execute(
                "UPDATE dispatch_pattern_offered SET offered_at = ? "
                "WHERE dispatch_id = ? AND pattern_id = ? AND project_id = ?",
                (row["offered_at"], dispatch_id, pattern_id, new_pid),
            )
        conn.execute(
            "DELETE FROM dispatch_pattern_offered "
            "WHERE dispatch_id = ? AND pattern_id = ? AND project_id = ?",
            (dispatch_id, pattern_id, old_pid),
        )


def _reattribute_pattern_usage(
    conn: sqlite3.Connection, pattern_id: str, old_pid: str, new_pid: str,
) -> None:
    if not _table_exists(conn, "pattern_usage"):
        return
    old_row = conn.execute(
        "SELECT used_count, ignored_count, success_count, failure_count, "
        "       confidence, last_used, last_offered "
        "FROM   pattern_usage WHERE pattern_id = ? AND project_id = ?",
        (pattern_id, old_pid),
    ).fetchone()
    if old_row is None:
        return
    new_row = conn.execute(
        "SELECT 1 FROM pattern_usage WHERE pattern_id = ? AND project_id = ?",
        (pattern_id, new_pid),
    ).fetchone()
    if new_row is None:
        conn.execute(
            "UPDATE pattern_usage SET project_id = ? WHERE pattern_id = ? AND project_id = ?",
            (new_pid, pattern_id, old_pid),
        )
        return
    # Target already has a row (rare: both projects independently offered the
    # same pattern_id) — merge counters instead of violating ux_pattern_usage_pid,
    # same fold pattern_dedup._merge_pattern_usage uses across duplicate ids.
    conn.execute(
        """
        UPDATE pattern_usage
        SET    used_count    = used_count    + ?,
               ignored_count = ignored_count + ?,
               success_count = success_count + ?,
               failure_count = failure_count + ?,
               confidence    = MAX(confidence, ?),
               last_used     = COALESCE(MAX(last_used, ?), last_used, ?),
               last_offered  = COALESCE(MAX(last_offered, ?), last_offered, ?),
               updated_at    = CURRENT_TIMESTAMP
        WHERE  pattern_id = ? AND project_id = ?
        """,
        (
            int(old_row["used_count"] or 0),
            int(old_row["ignored_count"] or 0),
            int(old_row["success_count"] or 0),
            int(old_row["failure_count"] or 0),
            float(old_row["confidence"] or 0.0),
            old_row["last_used"], old_row["last_used"],
            old_row["last_offered"], old_row["last_offered"],
            pattern_id, new_pid,
        ),
    )
    conn.execute(
        "DELETE FROM pattern_usage WHERE pattern_id = ? AND project_id = ?",
        (pattern_id, old_pid),
    )


def _reattribute_pattern_injection_outcome(
    conn: sqlite3.Connection, pattern_id: str, old_pid: str, new_pid: str,
) -> None:
    """Move every ``pattern_injection_outcome`` row for ``pattern_id``/``old_pid``.

    Same per-row UPDATE-or-merge shape as
    :func:`_reattribute_dispatch_pattern_offered`, for the same reason: an
    ``INSERT OR IGNORE`` of the "new" row can collide with a UNIQUE/PRIMARY
    KEY that does not include ``project_id`` and be silently ignored, after
    which the DELETE erases the only copy. Targeting by the table's real
    ``id`` PRIMARY KEY (present on every known schema variant — v28's
    ``_migrate_v28`` and every test fixture) keeps the UPDATE/DELETE
    unambiguous regardless of what the table's UNIQUE constraint covers.
    """
    if not _table_exists(conn, "pattern_injection_outcome"):
        return
    old_rows = conn.execute(
        "SELECT id, dispatch_id, created_at FROM pattern_injection_outcome "
        "WHERE pattern_id = ? AND project_id = ?",
        (pattern_id, old_pid),
    ).fetchall()
    for row in old_rows:
        dispatch_id = row["dispatch_id"]
        target = conn.execute(
            "SELECT id, created_at FROM pattern_injection_outcome "
            "WHERE dispatch_id = ? AND pattern_id = ? AND project_id = ?",
            (dispatch_id, pattern_id, new_pid),
        ).fetchone()
        if target is None:
            conn.execute(
                "UPDATE pattern_injection_outcome SET project_id = ? WHERE id = ?",
                (new_pid, row["id"]),
            )
            continue
        # Genuine collision (only reachable when the UNIQUE constraint does
        # not span project_id): both projects already recorded an outcome
        # for this exact (dispatch_id, pattern_id). Keep the row with the
        # later created_at, drop the other — never a silent, unconditional
        # drop of the old side.
        if row["created_at"] > target["created_at"]:
            conn.execute(
                "DELETE FROM pattern_injection_outcome WHERE id = ?",
                (target["id"],),
            )
            conn.execute(
                "UPDATE pattern_injection_outcome SET project_id = ? WHERE id = ?",
                (new_pid, row["id"]),
            )
        else:
            conn.execute(
                "DELETE FROM pattern_injection_outcome WHERE id = ?",
                (row["id"],),
            )


def _reattribute_junction_rows(
    conn: sqlite3.Connection, table: str, row_id: int, old_pid: str, new_pid: str,
) -> None:
    prefix = _PATTERN_ID_PREFIX.get(table)
    if prefix is None:
        return
    pattern_id = f"{prefix}{row_id}"
    _reattribute_dispatch_pattern_offered(conn, pattern_id, old_pid, new_pid)
    _reattribute_pattern_usage(conn, pattern_id, old_pid, new_pid)
    _reattribute_pattern_injection_outcome(conn, pattern_id, old_pid, new_pid)


class CrossDatabaseMove(RuntimeError):
    """A ``moves`` decision into another DB file: a transfer, never a relabel."""


def apply_decisions(
    db_path: Path, decisions: List[ReattributionDecision],
) -> Dict[str, int]:
    """Relabel, in one transaction, every ``moves`` row inside ``db_path``.

    Raises :class:`CrossDatabaseMove` before any write when a destination is
    not provably ``db_path`` itself. Idempotent: a relabelled row reads back
    as ``positive_own``, so it is never moved twice.
    """
    to_move = [d for d in decisions if d.status == STATUS_MOVES]
    foreign = [
        d for d in to_move
        if not d.destination_db or not _same_file(Path(d.destination_db), db_path)
    ]
    if foreign:
        named = ", ".join(f"{d.table}:{d.row_id}->{d.new_project_id}" for d in foreign[:10])
        raise CrossDatabaseMove(
            f"refusing to apply to {db_path}: {len(foreign)} row(s) belong in another project's "
            f"DB file ({named}); a relabel here reaches no consumer. Transfer them with "
            "scripts/lib/qi_transfer_apply.py (ADR-068 besluit 13-17). Nothing was written."
        )
    if not to_move:
        return {"rows_moved": 0}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        for d in to_move:
            conn.execute(
                f"UPDATE {d.table} SET project_id = ? WHERE id = ?",
                (d.new_project_id, d.row_id),
            )
            _reattribute_junction_rows(
                conn, d.table, d.row_id, d.old_project_id, d.new_project_id,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _emit_reattribution_receipts(db_path, to_move)
    return {"rows_moved": len(to_move)}


def _emit_reattribution_receipts(
    db_path: Path, moved: List[ReattributionDecision],
) -> None:
    """Append one ``state_mutation`` receipt per moved row (ADR-005, best-effort).

    A reattribution changes the ``project_id`` that drives injection
    selection with no other canonical record of the transition. Reuses the
    existing ``state_mutation`` receipt mechanism
    (:func:`state_mutation.emit_state_mutation`, already used by
    ``build_t0_state.py`` for state-file rewrites) rather than inventing a
    second audit path. Never raises: ``emit_state_mutation`` itself already
    swallows errors, and the DB write above has already committed by the
    time this runs, so a receipt failure must not be mistaken for the move
    having failed.
    """
    try:
        from state_mutation import emit_state_mutation
    except Exception:
        return
    for d in moved:
        try:
            emit_state_mutation(
                db_path.name,
                trigger="pattern_reattribution",
                section=f"{d.table}:{d.row_id}:{d.old_project_id}->{d.new_project_id}",
            )
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(
    decisions: List[ReattributionDecision], *, mode: str, db_path: Path,
    stores: Dict[str, Path],
) -> Dict[str, object]:
    counts: Dict[str, int] = {status: 0 for status in ALL_STATUSES}
    rows: List[Dict[str, object]] = []
    for d in decisions:
        counts[d.status] = counts.get(d.status, 0) + 1
        rows.append(
            {
                "table": d.table,
                "id": d.row_id,
                "title": d.title,
                "old_project_id": d.old_project_id,
                "new_project_id": d.new_project_id,
                "status": d.status,
                "source_dispatch_ids": d.source_ids,
                "resolved_projects": d.resolved_projects,
                "destination_db": d.destination_db,
            }
        )
    return {
        "mode": mode,
        "db_path": str(db_path),
        "stores_scanned": sorted(stores.keys()),
        "counts": counts,
        "total_rows": len(decisions),
        "rows": rows,
    }


def format_report_text(report: Dict[str, object]) -> str:
    lines = [
        f"[pattern_reattribution] mode={report['mode']} db={report['db_path']}",
        f"[pattern_reattribution] stores scanned: {', '.join(report['stores_scanned']) or '(none)'}",
        f"[pattern_reattribution] total rows: {report['total_rows']}",
    ]
    counts = report["counts"]
    for status in ALL_STATUSES:
        lines.append(f"  {status}: {counts.get(status, 0)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pattern_reattribution",
        description=(
            "Reattribute success_patterns/antipatterns rows whose project_id "
            "does not match the project their source dispatches actually ran in."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help=(
            "Path to quality_intelligence.db. Required (and must be explicit, "
            "never the resolved default) with --apply."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report reattribution decisions without mutating the database.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Relabel moves rows inside --db; refuses any move into another DB file.",
    )
    parser.add_argument(
        "--vnx-data-root",
        type=Path,
        default=None,
        help="Root containing <project_id>/state/t0_receipts.ndjson stores (default: ~/.vnx-data).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write the JSON report to this path in addition to stdout.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if args.apply and args.db is None:
        print(
            "[pattern_reattribution] --apply requires an explicit --db path "
            "(refusing to guess the live store)",
            file=sys.stderr,
        )
        return 2

    if args.db is None:
        from project_root import resolve_state_dir
        args.db = resolve_state_dir() / "quality_intelligence.db"

    if not args.db.exists():
        print(f"[pattern_reattribution] DB not found: {args.db}", file=sys.stderr)
        return 2

    vnx_data_root = args.vnx_data_root or (Path.home() / ".vnx-data")
    dispatch_index = build_attribution_index(vnx_data_root)
    decisions = scan_database(args.db, dispatch_index)

    if args.apply:
        try:
            apply_decisions(args.db, decisions)
        except CrossDatabaseMove as exc:
            print(f"[pattern_reattribution] {exc}", file=sys.stderr)
            return 3
        # Recompute so the report reflects the post-apply state (moved rows
        # now read back as positive_own), matching what a second dry-run would show.
        decisions = scan_database(args.db, dispatch_index)

    mode = "apply" if args.apply else "dry-run"
    report = build_report(decisions, mode=mode, db_path=args.db, stores=dispatch_index.stores)
    print(format_report_text(report))
    if args.report is not None:
        args.report.write_text(json.dumps(report, indent=2))
        print(f"[pattern_reattribution] report written: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
