"""plan_scope.py — the one plan-scope classification of a track (P1).

Track plan-gate-alleen-voor-architectuur, operator decision 2026-10-17. A track
is ``architecture`` (it carries a plan-gate, ``OI-PLAN-<track>``) or ``light``
(it does not). The value lives in ``tracks.metadata_json`` as ``plan_scope``,
next to ``lane_hint``; ``plan_scope_rule`` names the automatic rule that set it,
or the marker ``tracks.CREATED_SCOPE_RULE`` when the scope was decided at
creation. No migration and no new column.

How the value moves:

- ``vnx horizon add --plan-scope architecture|light`` sets it at birth.
- It goes UP by itself, never down: the fourth deliverable on a light track
  (``deliverable add``), or a build dispatch at the door whose paths fall in an
  architecture class (``dispatch_rule``). Going up seeds ``OI-PLAN-<track>``
  unless the track already has a plan-gate record, in the same transaction as
  the scope: both commit or neither does.
- It goes DOWN only by hand: ``vnx horizon set-plan-scope <track> light
  --reason "..."``. That never closes or attests ``OI-PLAN``.
- A stored architecture track with no OI-PLAN row of any status gets one on the
  next door pass or ``set-plan-scope ... architecture``.

Every change writes one ``track_plan_scope_set`` line to track_events.ndjson
(``tracks.set_plan_scope``; a scope set at birth by ``tracks.create_track``).
P1 refuses nothing: it writes, seeds and says so.

Old tracks carry no ``plan_scope``. Reading one gives ``architecture`` when it
has an open ``OI-PLAN`` and ``light`` when it has none; reading writes nothing.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import track_reconciler
import tracks as tracks_lib

ARCHITECTURE = "architecture"
LIGHT = "light"
PLAN_SCOPES = (ARCHITECTURE, LIGHT)
SCOPE_KEY = "plan_scope"
RULE_KEY = "plan_scope_rule"

# The deliverable count at which a light track becomes architecture.
DELIVERABLE_LIMIT = 4

PLAN_OI_PREFIX = "OI-PLAN-"

# Where a reading came from.
SOURCE_STORED = "stored"            # plan_scope is in metadata_json
SOURCE_TRANSITION = "transition"    # an old track, read from its open OI-PLAN (or none)
SOURCE_UNREADABLE = "unreadable"    # a value or metadata_json that does not parse: architecture

# The state of OI-PLAN-<track> after an upward move.
SEEDED = "seeded"
ALREADY_OPEN = "already-open"
ALREADY_RESOLVED = "already-resolved"
UNSUPPORTED = "unsupported"


def plan_blocker_oi(track_id: str) -> str:
    return f"{PLAN_OI_PREFIX}{track_id}"


@dataclass(frozen=True)
class ScopeReading:
    """A track's plan scope as read, and where the value came from."""
    scope: str
    source: str
    rule: Optional[str] = None
    raw: Any = None

    def describe(self) -> str:
        """One line for ``objective show``."""
        if self.source == SOURCE_TRANSITION:
            basis = "an open" if self.scope == ARCHITECTURE else "no open"
            return f"{self.scope} (not stored; read from {basis} OI-PLAN)"
        if self.source == SOURCE_UNREADABLE:
            return f"{self.scope} (stored value {self.raw!r} unreadable; read as architecture)"
        if self.rule == tracks_lib.CREATED_SCOPE_RULE:
            return f"{self.scope} (set at creation)"
        if self.rule:
            return f"{self.scope} (rule: {self.rule})"
        return f"{self.scope} (set by hand)"

    def tag(self) -> str:
        """The short tag for ``objective list``."""
        return "arch" if self.scope == ARCHITECTURE else "light"


def read_scope(metadata_json: Optional[str], *, open_plan_blocker: bool) -> ScopeReading:
    """Read ``plan_scope`` from a track's metadata_json. Pure: writes nothing.

    No key: the transition rule, architecture with an open OI-PLAN, light
    without. A value that is not architecture or light, or metadata_json that
    does not parse, reads as architecture (closed on doubt).
    """
    try:
        meta = json.loads(metadata_json or "{}")
    except (TypeError, ValueError):
        return ScopeReading(ARCHITECTURE, SOURCE_UNREADABLE, raw=metadata_json)
    if not isinstance(meta, dict):
        return ScopeReading(ARCHITECTURE, SOURCE_UNREADABLE, raw=metadata_json)
    if SCOPE_KEY not in meta:
        return ScopeReading(ARCHITECTURE if open_plan_blocker else LIGHT, SOURCE_TRANSITION)
    value = meta[SCOPE_KEY]
    if value not in PLAN_SCOPES:
        return ScopeReading(ARCHITECTURE, SOURCE_UNREADABLE, raw=value)
    rule = meta.get(RULE_KEY)
    return ScopeReading(value, SOURCE_STORED, rule=rule if isinstance(rule, str) and rule else None)


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(row[1] == col for row in conn.execute(f"PRAGMA table_info('{table}')"))


def plan_gate_supported(conn: sqlite3.Connection) -> bool:
    """True when the DB can both BLOCK and later CLEAR a plan-gate: track_open_items
    with resolved_at (clearable) + tracks.derived_status (the promote-lock's signal).

    resolved_at (migration 0030) is required at SEED time too — without it the
    reconciler counts any 'blocks' row forever, so seeding would create a blocker
    this same gate could never resolve (a permanently stuck track)."""
    return (
        _has_table(conn, "track_open_items")
        and _has_col(conn, "track_open_items", "resolved_at")
        and _has_col(conn, "tracks", "derived_status")
    )


def _connect(state_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(Path(state_dir) / tracks_lib.DB_FILENAME), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def seed_plan_blocker(
    state_dir: Path, track_id: str, project_id: str, reason: Optional[str] = None
) -> bool:
    """Seed the synthetic OI-PLAN-<track> blocker so the track is born plan-gated.

    The one write path to the OI-PLAN row: ``objective add``, ``plan-gate seed``,
    ``plan-gate reblock`` and an upward plan-scope move all come through here.

    Idempotent (INSERT OR IGNORE on the track_open_items PK). Returns True once the
    blocker exists and the track reconciles to blocked; False on a DB that predates
    the plan-gate schema (graceful no-op).

    ``reason`` turns this into the RE-BLOCK form (plan-gate reblock): the existing
    resolved row's ``resolved_at`` is cleared in the SAME UPDATE that records the
    reversal ``resolution_reason``, so the history that the gate was once lifted
    is preserved on the row instead of being silently wiped.
    """
    conn = _connect(state_dir)
    try:
        # plan_gate_supported requires resolved_at (0030), which implies project_id
        # (0024) — both are present here by migration ordering.
        if not plan_gate_supported(conn):
            return False
        oi = plan_blocker_oi(track_id)
        conn.execute(
            "INSERT OR IGNORE INTO track_open_items "
            "(track_id, project_id, oi_id, link_type, link_source) "
            "VALUES (?, ?, ?, 'blocks', 'manual')",
            (track_id, project_id, oi),
        )
        # Re-seed must RE-block: clear resolved_at so a track whose plan gate
        # previously passed is gated again (e.g. a mid-flight plan change). Without
        # this, INSERT OR IGNORE silently no-ops on the resolved row.
        if reason is None:
            conn.execute(
                "UPDATE track_open_items SET resolved_at = NULL "
                "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = 'blocks'",
                (track_id, project_id, oi),
            )
        else:
            conn.execute(
                "UPDATE track_open_items SET resolved_at = NULL, resolution_reason = ? "
                "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = 'blocks'",
                (reason, track_id, project_id, oi),
            )
        conn.commit()
    finally:
        conn.close()
    track_reconciler.reconcile_track(state_dir, track_id, project_id)
    return True


def _plan_rows_in(conn: sqlite3.Connection, track_id: str, project_id: str) -> list[dict]:
    """Every OI-PLAN-<track> 'blocks' row, resolved or not. A store without the
    open-items table has none; one without resolved_at reads every row as open."""
    if not _has_table(conn, "track_open_items"):
        return []
    resolved = "resolved_at" if _has_col(conn, "track_open_items", "resolved_at") else "NULL"
    rows = conn.execute(
        f"SELECT oi_id, {resolved} AS resolved_at FROM track_open_items "
        "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = 'blocks'",
        (track_id, project_id, plan_blocker_oi(track_id)),
    ).fetchall()
    return [dict(row) for row in rows]


def _any_open(rows: list[dict]) -> bool:
    return any(row.get("resolved_at") is None for row in rows)


@contextmanager
def _write_transaction(state_dir: Path) -> Iterator[sqlite3.Connection]:
    """One IMMEDIATE transaction on the track store: everything in it commits, or nothing."""
    conn = _connect(state_dir)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _locked_reading(
    conn: sqlite3.Connection, track_id: str, project_id: str,
) -> tuple[Optional[ScopeReading], list[dict]]:
    """The scope and the OI-PLAN rows as they are under the caller's write lock.
    (None, []) when the track does not exist."""
    row = conn.execute(
        "SELECT metadata_json FROM tracks WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchone()
    if row is None:
        return None, []
    rows = _plan_rows_in(conn, track_id, project_id)
    return read_scope(row["metadata_json"], open_plan_blocker=_any_open(rows)), rows


def _seed_in(conn: sqlite3.Connection, track_id: str, project_id: str, rows: list[dict]) -> str:
    """Seed OI-PLAN-<track> inside the caller's transaction, unless a row exists.

    A track that already has an OI-PLAN row keeps it as it is: an open row is
    the gate still to run, a resolved row is a gate already passed or attested.
    Seeding over a resolved row would reopen a passed gate, which no upward
    move may do. ``rows`` were read under the same lock, so none can appear
    between that read and this insert.
    """
    if _any_open(rows):
        return ALREADY_OPEN
    if rows:
        return ALREADY_RESOLVED
    if not plan_gate_supported(conn):
        return UNSUPPORTED
    conn.execute(
        "INSERT INTO track_open_items (track_id, project_id, oi_id, link_type, link_source) "
        "VALUES (?, ?, ?, 'blocks', 'manual')",
        (track_id, project_id, plan_blocker_oi(track_id)),
    )
    return SEEDED


_GATE_NOTES = {
    SEEDED: "{oi} seeded; the plan-gate runs before this track is built",
    ALREADY_OPEN: "{oi} was already open",
    ALREADY_RESOLVED: "{oi} is already resolved, so it is not reopened",
    UNSUPPORTED: "this store has no plan-gate schema, so {oi} is not seeded",
}


@dataclass(frozen=True)
class ScopeChange:
    """What one write did: the old and new scope and the OI-PLAN state after it."""
    track_id: str
    old: str
    new: str
    why: str
    gate: Optional[str] = None

    def line(self) -> str:
        note = ""
        if self.gate:
            note = "; " + _GATE_NOTES[self.gate].format(oi=plan_blocker_oi(self.track_id))
        move = f"stays {self.new}" if self.old == self.new else f"{self.old} -> {self.new}"
        return f"plan-scope: {self.track_id} {move} ({self.why}){note}"


def _has_open_plan_blocker(state_dir: Path, project_id: str, track_id: str) -> bool:
    """Whether OI-PLAN-<track> is open in the store as it stands after the commit."""
    conn = _connect(state_dir)
    try:
        return _any_open(_plan_rows_in(conn, track_id, project_id))
    finally:
        conn.close()


def _reconciled(
    state_dir: Path, project_id: str, track_id: str, change: Optional[ScopeChange],
) -> Optional[ScopeChange]:
    """Re-derive the track's status after the commit when OI-PLAN is open.

    derived_status is a projection of the committed rows (the promote-lock reads
    it), so it is computed once they are durable, as ``seed_plan_blocker`` does.
    It must re-derive whenever the store has an open OI-PLAN — newly seeded,
    already open, or a retry that changed nothing — because a failed reconcile
    (the rows committed, the projection did not) is repaired by no other path,
    and until it is, ``deliverable promote`` would read a stale status and could
    promote while the blocker is open. A reconcile that raises is not swallowed:
    it propagates so the command reports it and exits non-zero, and a later
    repeat repairs the projection.
    """
    if _has_open_plan_blocker(state_dir, project_id, track_id):
        track_reconciler.reconcile_track(state_dir, track_id, project_id)
    return change


def _recover_in(
    conn: sqlite3.Connection, track_id: str, project_id: str,
    reading: ScopeReading, rows: list[dict],
) -> Optional[ScopeChange]:
    """Seed OI-PLAN for a stored architecture track that has no OI-PLAN row at all.

    ``horizon add --plan-scope architecture`` writes the track and seeds its
    blocker in two steps, and a store can gain the plan-gate schema after a
    track was set architecture: both can leave a stored architecture track
    without a blocker. A row of any status means the gate was seeded once and
    is left as it is. Only a stored value counts: a track read as architecture
    from an open OI-PLAN has one, and an unreadable value is architecture by
    doubt, not by a decision anyone made.
    """
    if reading.source != SOURCE_STORED or reading.scope != ARCHITECTURE or rows:
        return None
    if _seed_in(conn, track_id, project_id, rows) != SEEDED:
        return None
    return ScopeChange(track_id, ARCHITECTURE, ARCHITECTURE, "stored, without an OI-PLAN row", SEEDED)


def raise_to_architecture(
    state_dir: Path, track_id: str, project_id: str, *, rule: str, actor: str = "system",
) -> Optional[ScopeChange]:
    """Move a light track up to architecture by ``rule``; None when nothing was written.

    Reading the scope, seeding ``OI-PLAN-<track>`` and writing the scope run in
    one IMMEDIATE transaction on runtime_coordination.db, where both rows live:
    a seed that fails rolls the raise back, so a track is never architecture by
    rule without its blocker, and the next attempt finds it light and tries
    again. The scope is read under the write lock, so a value written by
    another process in the meantime is what this decides on.

    Only upward: a track already architecture (stored, read from an open
    OI-PLAN, or unreadable) keeps its scope. A stored architecture track with
    no OI-PLAN row of any status gets one (``_recover_in``). Never refuses
    anything.
    """
    with _write_transaction(state_dir) as conn:
        reading, rows = _locked_reading(conn, track_id, project_id)
        if reading is None:
            return None
        if reading.scope != LIGHT:
            change = _recover_in(conn, track_id, project_id, reading, rows)
        else:
            gate = _seed_in(conn, track_id, project_id, rows)
            tracks_lib.set_plan_scope(
                state_dir, track_id, project_id, ARCHITECTURE,
                old_scope=LIGHT, reason=rule, rule=rule, actor=actor, conn=conn,
            )
            change = ScopeChange(track_id, LIGHT, ARCHITECTURE, f"rule: {rule}", gate)
    return _reconciled(state_dir, project_id, track_id, change)


def ensure_plan_blocker(state_dir: Path, track_id: str, project_id: str) -> Optional[ScopeChange]:
    """The door's pass on a build dispatch that names no architecture rule.

    The scope stays as it is; only a stored architecture track without any
    OI-PLAN row gets one seeded (``_recover_in``). None when nothing was written.
    """
    with _write_transaction(state_dir) as conn:
        reading, rows = _locked_reading(conn, track_id, project_id)
        change = None if reading is None else _recover_in(conn, track_id, project_id, reading, rows)
    return _reconciled(state_dir, project_id, track_id, change)


def set_by_hand(
    state_dir: Path, track_id: str, project_id: str, scope: str, *, reason: str,
) -> ScopeChange:
    """The operator's ``set-plan-scope``: either direction, always with a reason.

    Down to light writes the value and leaves OI-PLAN as it is: closing or
    attesting it stays with the plan-gate or the operator. Up to architecture,
    or architecture again, seeds OI-PLAN when the track has no OI-PLAN row of
    any status, in the same transaction as the scope, as an automatic move does.

    Raises ValueError on an empty reason or an unknown scope, and
    tracks.TrackNotFoundError when the track does not exist.
    """
    if not (reason or "").strip():
        raise ValueError("set-plan-scope requires --reason: say why the scope changes")
    if scope not in PLAN_SCOPES:
        raise ValueError(f"unknown plan scope {scope!r}; use {' or '.join(PLAN_SCOPES)}")
    with _write_transaction(state_dir) as conn:
        reading, rows = _locked_reading(conn, track_id, project_id)
        if reading is None:
            raise tracks_lib.TrackNotFoundError(f"Track not found: ({track_id!r}, {project_id!r})")
        gate = _seed_in(conn, track_id, project_id, rows) if scope == ARCHITECTURE else None
        tracks_lib.set_plan_scope(
            state_dir, track_id, project_id, scope,
            old_scope=reading.scope, reason=reason, rule=None, actor="operator", conn=conn,
        )
        change = ScopeChange(track_id, reading.scope, scope, f"by hand: {reason.strip()}", gate)
    return _reconciled(state_dir, project_id, track_id, change)


def deliverable_rule(deliverable_count: int) -> Optional[str]:
    """The size rule: a track with DELIVERABLE_LIMIT or more deliverables is architecture."""
    if deliverable_count >= DELIVERABLE_LIMIT:
        return f"{deliverable_count} deliverables (limit {DELIVERABLE_LIMIT})"
    return None


def dispatch_rule(
    dispatch_paths: Optional[Sequence[str]],
    *,
    task_class: Optional[str] = None,
    irreversible: bool = False,
) -> Optional[str]:
    """The rule a build dispatch triggers on its track, or None for a light change.

    Every path is normalised first (``smart_router.normalize_dispatch_path``),
    so both checks below and the rule's text read the path the repo names.
    No paths at all counts as architecture: a dispatch that names nothing cannot
    be judged, and nothing may escape the classification by naming nothing.
    Then the path categories (outside-repo, schema, security, gate-merge,
    store-resolution), then ``derive_variant`` giving coding-strict.
    """
    # Lazy, so importing this module (planning_cli does) pulls in no router.
    import smart_router

    normalized = (smart_router.normalize_dispatch_path(p) for p in (dispatch_paths or []) if p)
    paths = [p for p in normalized if p]
    if not paths:
        return "build dispatch without dispatch_paths (closed on doubt)"
    for path in paths:
        category = smart_router.plan_scope_path_category(path)
        if category:
            return f"{category} path {path}"
    derived = smart_router.derive_variant(paths, task_class=task_class, irreversible=irreversible)
    if derived.variant == "coding-strict":
        return f"variant coding-strict ({derived.reason})"
    return None
