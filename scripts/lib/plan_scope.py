"""plan_scope.py — the one plan-scope classification of a track (P1).

Track plan-gate-alleen-voor-architectuur, operator decision 2026-10-17. A track
is ``architecture`` (it carries a plan-gate, ``OI-PLAN-<track>``) or ``light``
(it does not). The value lives in ``tracks.metadata_json`` as ``plan_scope``,
next to ``lane_hint``; ``plan_scope_rule`` names the automatic rule that set it.
No migration and no new column.

How the value moves:

- ``vnx horizon add --plan-scope architecture|light`` sets it at birth.
- It goes UP by itself, never down: the fourth deliverable on a light track
  (``deliverable add``), or a build dispatch at the door whose paths fall in an
  architecture class (``dispatch_rule``). Going up seeds ``OI-PLAN-<track>``
  unless the track already has a plan-gate record.
- It goes DOWN only by hand: ``vnx horizon set-plan-scope <track> light
  --reason "..."``. That never closes or attests ``OI-PLAN``.

Every change writes one ``track_plan_scope_set`` line to track_events.ndjson
(``tracks.set_plan_scope``). P1 refuses nothing: it writes, seeds and says so.

Old tracks carry no ``plan_scope``. Reading one gives ``architecture`` when it
has an open ``OI-PLAN`` and ``light`` when it has none; reading writes nothing.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

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


def _plan_blocker_rows(state_dir: Path, track_id: str, project_id: str) -> list[dict]:
    """Every OI-PLAN-<track> 'blocks' row, resolved or not. A store without the
    open-items table has none; one without resolved_at reads every row as open."""
    conn = _connect(state_dir)
    try:
        if not _has_table(conn, "track_open_items"):
            return []
        resolved = "resolved_at" if _has_col(conn, "track_open_items", "resolved_at") else "NULL"
        rows = conn.execute(
            f"SELECT oi_id, {resolved} AS resolved_at FROM track_open_items "
            "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = 'blocks'",
            (track_id, project_id, plan_blocker_oi(track_id)),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def has_open_plan_blocker(state_dir: Path, track_id: str, project_id: str) -> bool:
    """True when OI-PLAN-<track> is linked and not resolved. Read-only."""
    return any(row.get("resolved_at") is None for row in _plan_blocker_rows(state_dir, track_id, project_id))


def read_track_scope(
    state_dir: Path, track_id: str, project_id: str, *, track: Optional[dict] = None,
) -> Optional[ScopeReading]:
    """The scope of one track in the store, or None when the track does not exist."""
    if track is None:
        track = tracks_lib.get_track(state_dir, track_id, project_id)
    if track is None:
        return None
    return read_scope(
        track.get("metadata_json"),
        open_plan_blocker=has_open_plan_blocker(state_dir, track_id, project_id),
    )


def _seed_unless_recorded(state_dir: Path, track_id: str, project_id: str) -> str:
    """Seed OI-PLAN-<track> for a track that just became architecture.

    A track that already has an OI-PLAN row keeps it as it is: an open row is
    the gate still to run, a resolved row is a gate already passed or attested.
    Seeding over a resolved row would reopen a passed gate, which no upward
    move may do.
    """
    rows = _plan_blocker_rows(state_dir, track_id, project_id)
    if any(row.get("resolved_at") is None for row in rows):
        return ALREADY_OPEN
    if rows:
        return ALREADY_RESOLVED
    return SEEDED if seed_plan_blocker(state_dir, track_id, project_id) else UNSUPPORTED


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
        return f"plan-scope: {self.track_id} {self.old} -> {self.new} ({self.why}){note}"


def raise_to_architecture(
    state_dir: Path, track_id: str, project_id: str, *, rule: str, actor: str = "system",
) -> Optional[ScopeChange]:
    """Move a light track up to architecture by ``rule``; None when it is not light.

    Only upward: a track already architecture (stored, read from an open
    OI-PLAN, or unreadable) is left alone. Never refuses anything.
    """
    track = tracks_lib.get_track(state_dir, track_id, project_id)
    if track is None:
        return None
    reading = read_track_scope(state_dir, track_id, project_id, track=track)
    if reading.scope != LIGHT:
        return None
    tracks_lib.set_plan_scope(
        state_dir, track_id, project_id, ARCHITECTURE,
        old_scope=LIGHT, reason=rule, rule=rule, actor=actor,
    )
    gate = _seed_unless_recorded(state_dir, track_id, project_id)
    return ScopeChange(track_id, LIGHT, ARCHITECTURE, f"rule: {rule}", gate)


def set_by_hand(
    state_dir: Path, track_id: str, project_id: str, scope: str, *, reason: str,
) -> ScopeChange:
    """The operator's ``set-plan-scope``: either direction, always with a reason.

    Down to light writes the value and leaves OI-PLAN as it is: closing or
    attesting it stays with the plan-gate or the operator. Up to architecture
    seeds OI-PLAN the same way an automatic move does.

    Raises ValueError on an empty reason or an unknown scope, and
    tracks.TrackNotFoundError when the track does not exist.
    """
    if not (reason or "").strip():
        raise ValueError("set-plan-scope requires --reason: say why the scope changes")
    if scope not in PLAN_SCOPES:
        raise ValueError(f"unknown plan scope {scope!r}; use {' or '.join(PLAN_SCOPES)}")
    reading = read_track_scope(state_dir, track_id, project_id)
    if reading is None:
        raise tracks_lib.TrackNotFoundError(f"Track not found: ({track_id!r}, {project_id!r})")
    tracks_lib.set_plan_scope(
        state_dir, track_id, project_id, scope,
        old_scope=reading.scope, reason=reason, rule=None, actor="operator",
    )
    gate = _seed_unless_recorded(state_dir, track_id, project_id) if scope == ARCHITECTURE else None
    return ScopeChange(track_id, reading.scope, scope, f"by hand: {reason.strip()}", gate)


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

    No paths at all counts as architecture: a dispatch that names nothing cannot
    be judged, and nothing may escape the classification by naming nothing.
    Then the path categories (schema, security, gate-merge, store-resolution),
    then ``derive_variant`` giving coding-strict.
    """
    # Lazy, so importing this module (planning_cli does) pulls in no router.
    import smart_router

    paths = [str(p).strip() for p in (dispatch_paths or []) if p and str(p).strip()]
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
