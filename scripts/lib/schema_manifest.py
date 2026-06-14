#!/usr/bin/env python3
"""schema_manifest.py — declarative invariant manifest + version reconciler (PR-A2).

R2.1 (finding E-F): the old per-version name-based preflights
(`_assert_tracks_vNN_intact` in scripts/migrate_future_system.py) asserted only a
handful of column/table names and RAISED on mismatch. They fire ONLY when a
migration is actually applied, so a DB that LIES about its `user_version` (claims
v30 but is physically v27) SKIPS every migration and never trips them — leaving
migrations silently un-applied (synthesis E-F, repro-confirmed).

This module replaces that fragile, scattered mechanism with ONE declarative
INVARIANT MANIFEST per schema version (v22-v30) plus a single reconciler engine:

  * The manifest declares, per version, the FULL expected shape: required tables;
    columns (name + affinity + nullability); PK column ordinals; composite UNIQUE
    keys; FK actions (referenced table/cols + on-update/on-delete); index
    definitions (key columns + unique + partial predicate); and views (name +
    normalized SQL).
  * `reconcile_user_version` validates the DB's CLAIMED `user_version` against the
    manifest for that version. If ANY invariant fails, it DOWNGRADES `user_version`
    to the exact highest version whose invariants fully hold (derived low->high),
    so the normal migration walk re-applies whatever the downgrade exposed.
  * Downgrade is SAFE: it only lowers `user_version`; it never drops data. The
    additive migrations are idempotent (`if current_version >= N: skip`).
  * If no lower version fully holds, it FAILS LOUDLY (genuine corruption) rather
    than guessing — preserving the old preflights' guard intent.

ADR-007 (docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md):
the manifest encodes the composite UNIQUE/PK over `project_id` for every central
table (tracks PK `(track_id, project_id)`, `UNIQUE(dispatch_id, project_id)`,
child-table composite PK/FK) so a tenant-broken schema can NEVER validate as
"complete" for its claimed version.

ADR-009 (docs/governance/decisions/ADR-009-schema-first-migrations.md): the
manifest mirrors the ACTUAL semantics of `schemas/migrations/00NN_*.sql` rather
than a hand-typed projection — built from PRAGMA introspection of a freshly
walked DB and pinned by the fresh-DB->v30 self-consistency test. The manifest must
match what each migration produces or the reconciler would oscillate.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


class SchemaReconciliationError(RuntimeError):
    """Raised when a DB fails its claimed version's manifest and no lower version
    fully holds (genuine corruption), or when a downgrade+re-walk did not converge.
    Cite ADR-009 (schema-first): refuse to guess a tenant/version identity."""


# ---------------------------------------------------------------------------
# Declarative invariant types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ColumnInvariant:
    name: str
    affinity: str          # declared SQLite type, upper-cased (TEXT/INTEGER/REAL)
    notnull: bool


@dataclass(frozen=True)
class ForeignKeyInvariant:
    columns: Tuple[str, ...]
    ref_table: str
    ref_columns: Tuple[str, ...]
    on_update: str = "NO ACTION"
    on_delete: str = "NO ACTION"


@dataclass(frozen=True)
class IndexInvariant:
    name: str
    columns: Tuple[str, ...]     # key column names, in order
    unique: bool = False
    partial: bool = False        # has a WHERE predicate
    where: Optional[str] = None  # the partial WHERE predicate text (compared normalized)


@dataclass(frozen=True)
class ViewInvariant:
    name: str
    sql: str                     # raw CREATE VIEW text; compared normalized


@dataclass
class TableInvariant:
    name: str
    columns: Dict[str, ColumnInvariant]
    pk: Tuple[str, ...]
    unique_keys: Tuple[Tuple[str, ...], ...] = ()
    foreign_keys: Tuple[ForeignKeyInvariant, ...] = ()
    indexes: Tuple[IndexInvariant, ...] = ()


@dataclass
class VersionManifest:
    version: int
    tables: Tuple[TableInvariant, ...]
    views: Tuple[ViewInvariant, ...] = ()


@dataclass(frozen=True)
class ReconcileResult:
    reconciled: bool
    claimed: int
    corrected: int
    violations: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Column-group builders (DRY: each version extends the previous shape)
# ---------------------------------------------------------------------------

def _cols(*specs: Tuple[str, str, bool]) -> Dict[str, ColumnInvariant]:
    return {n: ColumnInvariant(n, a.upper(), nn) for (n, a, nn) in specs}


_DISPATCH_COLS_BASE = _cols(
    ("id", "INTEGER", False), ("dispatch_id", "TEXT", True),
    ("project_id", "TEXT", True), ("state", "TEXT", True),
    ("terminal_id", "TEXT", False), ("track", "TEXT", False),
    ("priority", "TEXT", False), ("pr_ref", "TEXT", False),
    ("gate", "TEXT", False), ("attempt_count", "INTEGER", True),
    ("bundle_path", "TEXT", False), ("created_at", "TEXT", True),
    ("updated_at", "TEXT", True), ("expires_after", "TEXT", False),
    ("metadata_json", "TEXT", False), ("operator_approved_at", "TEXT", False),
)
_DISPATCH_COLS_V27 = {
    **_DISPATCH_COLS_BASE,
    **_cols(("output_ref", "TEXT", False), ("output_kind", "TEXT", False)),
}

_DISPATCH_INDEXES = (
    IndexInvariant("idx_dispatch_state", ("state", "updated_at")),
    IndexInvariant("idx_dispatch_terminal", ("terminal_id", "state")),
    IndexInvariant("idx_dispatch_created", ("created_at",)),
    IndexInvariant("idx_dispatches_ready", ("state", "operator_approved_at"),
                   partial=True, where="state = 'proposed' OR state = 'ready'"),
)


def _dispatches(columns: Dict[str, ColumnInvariant]) -> TableInvariant:
    """Dispatches: ADR-007 composite UNIQUE(dispatch_id, project_id), surrogate id PK."""
    return TableInvariant(
        name="dispatches", columns=columns, pk=("id",),
        unique_keys=(("dispatch_id", "project_id"),), indexes=_DISPATCH_INDEXES,
    )


# tracks — single-column PK at v22, composite (track_id, project_id) from v24 (ADR-007)
_TRACKS_COLS_V22 = _cols(
    ("track_id", "TEXT", True), ("title", "TEXT", True),
    ("goal_state", "TEXT", True), ("phase", "TEXT", True),
    ("next_up", "INTEGER", True), ("sort_order", "INTEGER", True),
    ("priority", "TEXT", False), ("requires_operator_promotion", "INTEGER", True),
    ("instruction_template", "TEXT", False), ("context_composer_rules", "TEXT", False),
    ("pr_ref", "TEXT", False), ("trigger_condition", "TEXT", False),
    ("project_id", "TEXT", True), ("created_at", "TEXT", True),
    ("phase_changed_at", "TEXT", False), ("completed_at", "TEXT", False),
    ("metadata_json", "TEXT", False),
)
# v24 rebuild relaxes goal_state to nullable (0024 CREATE TABLE: goal_state TEXT, no NOT NULL)
_TRACKS_COLS_V24 = {**_TRACKS_COLS_V22, "goal_state": ColumnInvariant("goal_state", "TEXT", False)}
_TRACKS_COLS_V27 = {**_TRACKS_COLS_V24, **_cols(("horizon", "TEXT", False))}
_TRACKS_COLS_V28 = {**_TRACKS_COLS_V27, **_cols(("derived_status", "TEXT", False))}
_TRACKS_COLS_V29 = {**_TRACKS_COLS_V28,
                    **_cols(("track_type", "TEXT", True), ("next_action_owner", "TEXT", False))}

# track_phase_history
_TPH_COLS_V22 = _cols(
    ("id", "INTEGER", False), ("track_id", "TEXT", True), ("from_phase", "TEXT", False),
    ("to_phase", "TEXT", True), ("actor", "TEXT", True), ("reason", "TEXT", False),
    ("approval_id", "TEXT", False), ("occurred_at", "TEXT", True),
)
_TPH_COLS_V24 = {**_TPH_COLS_V22, **_cols(("project_id", "TEXT", True))}

# track_dependencies
_TD_COLS_V22 = _cols(
    ("from_track_id", "TEXT", True), ("to_track_id", "TEXT", True),
    ("kind", "TEXT", True), ("derivation_source", "TEXT", True),
    ("confidence", "REAL", True), ("evidence_json", "TEXT", False),
    ("derived_at", "TEXT", True),
)
_TD_COLS_V24 = _cols(
    ("from_track_id", "TEXT", True), ("from_project_id", "TEXT", True),
    ("to_track_id", "TEXT", True), ("to_project_id", "TEXT", True),
    ("kind", "TEXT", True), ("derivation_source", "TEXT", True),
    ("confidence", "REAL", True), ("evidence_json", "TEXT", False),
    ("derived_at", "TEXT", True),
)

# track_open_items
_TOI_COLS_V22 = _cols(
    ("track_id", "TEXT", True), ("oi_id", "TEXT", True), ("link_type", "TEXT", True),
    ("link_source", "TEXT", True), ("linked_at", "TEXT", True),
)
_TOI_COLS_V24 = {**dict(list(_TOI_COLS_V22.items())[:1]),
                 **_cols(("project_id", "TEXT", True)),
                 **dict(list(_TOI_COLS_V22.items())[1:])}
_TOI_COLS_V30 = {**_TOI_COLS_V24,
                 **_cols(("resolved_at", "TEXT", False), ("resolution_reason", "TEXT", False))}


# ---------------------------------------------------------------------------
# Per-table invariant builders (FKs/indexes/PK differ by version)
# ---------------------------------------------------------------------------

def _tracks_v22() -> TableInvariant:
    return TableInvariant(
        name="tracks", columns=_TRACKS_COLS_V22, pk=("track_id",),
        indexes=(
            IndexInvariant("ux_tracks_next_up", ("project_id",), unique=True, partial=True,
                           where="next_up = 1 AND phase = 'queued'"),
            IndexInvariant("idx_tracks_phase_nextup",
                           ("project_id", "phase", "next_up", "sort_order")),
        ),
    )


def _tracks_composite(columns: Dict[str, ColumnInvariant],
                      extra_indexes: Tuple[IndexInvariant, ...] = ()) -> TableInvariant:
    base = (
        IndexInvariant("ux_tracks_next_up_per_project", ("project_id",),
                       unique=True, partial=True, where="next_up = 1 AND phase = 'queued'"),
        IndexInvariant("idx_tracks_project_phase_nextup",
                       ("project_id", "phase", "next_up", "sort_order")),
    )
    return TableInvariant(name="tracks", columns=columns,
                          pk=("track_id", "project_id"), indexes=base + extra_indexes)


_TRACKS_IDX_V27 = (IndexInvariant("idx_tracks_horizon",
                                  ("project_id", "horizon", "sort_order")),)
_TRACKS_IDX_V28 = _TRACKS_IDX_V27 + (
    IndexInvariant("idx_tracks_derived_status", ("project_id", "derived_status")),)
_TRACKS_IDX_V29 = _TRACKS_IDX_V28 + (
    IndexInvariant("idx_tracks_track_type", ("project_id", "track_type")),)


def _tph_v22() -> TableInvariant:
    return TableInvariant(
        name="track_phase_history", columns=_TPH_COLS_V22, pk=("id",),
        foreign_keys=(ForeignKeyInvariant(("track_id",), "tracks", ("track_id",)),),
        indexes=(IndexInvariant("idx_track_phase_history", ("track_id", "occurred_at")),),
    )


def _tph_v24() -> TableInvariant:
    return TableInvariant(
        name="track_phase_history", columns=_TPH_COLS_V24, pk=("id",),
        unique_keys=(("track_id", "project_id", "occurred_at"),),
        foreign_keys=(ForeignKeyInvariant(("track_id", "project_id"), "tracks",
                                          ("track_id", "project_id")),),
        indexes=(IndexInvariant("idx_track_phase_history_track",
                                ("track_id", "project_id", "occurred_at")),),
    )


def _td_v22() -> TableInvariant:
    return TableInvariant(
        name="track_dependencies", columns=_TD_COLS_V22,
        pk=("from_track_id", "to_track_id"),
        foreign_keys=(ForeignKeyInvariant(("from_track_id",), "tracks", ("track_id",)),
                      ForeignKeyInvariant(("to_track_id",), "tracks", ("track_id",))),
        indexes=(IndexInvariant("idx_track_deps_from", ("from_track_id",)),),
    )


def _td_v24() -> TableInvariant:
    return TableInvariant(
        name="track_dependencies", columns=_TD_COLS_V24,
        pk=("from_track_id", "from_project_id", "to_track_id", "to_project_id"),
        foreign_keys=(
            ForeignKeyInvariant(("from_track_id", "from_project_id"), "tracks",
                                ("track_id", "project_id")),
            ForeignKeyInvariant(("to_track_id", "to_project_id"), "tracks",
                                ("track_id", "project_id"))),
        indexes=(IndexInvariant("idx_track_deps_from",
                                ("from_track_id", "from_project_id")),),
    )


def _toi_v22() -> TableInvariant:
    return TableInvariant(
        name="track_open_items", columns=_TOI_COLS_V22,
        pk=("track_id", "oi_id", "link_type"),
        foreign_keys=(ForeignKeyInvariant(("track_id",), "tracks", ("track_id",)),),
    )


def _toi_composite(columns: Dict[str, ColumnInvariant],
                   extra_indexes: Tuple[IndexInvariant, ...] = ()) -> TableInvariant:
    return TableInvariant(
        name="track_open_items", columns=columns,
        pk=("track_id", "project_id", "oi_id", "link_type"),
        foreign_keys=(ForeignKeyInvariant(("track_id", "project_id"), "tracks",
                                          ("track_id", "project_id")),),
        indexes=(IndexInvariant("idx_track_open_items_oi", ("oi_id",)),) + extra_indexes,
    )


# deliverables view (added v27); SQLite stores it verbatim minus IF NOT EXISTS
_DELIVERABLES_VIEW = ViewInvariant("deliverables", """
CREATE VIEW deliverables AS
SELECT
    project_id AS project_id,
    output_ref AS deliverable_ref,
    MIN(output_kind) AS output_kind,
    MIN(track) AS track,
    COUNT(*) AS dispatch_count,
    SUM(CASE WHEN state = 'completed' THEN 1 ELSE 0 END) AS completed_count,
    SUM(CASE WHEN state IN ('active', 'running', 'delivering', 'claimed', 'accepted')
             THEN 1 ELSE 0 END) AS in_flight_count,
    SUM(CASE WHEN state IN ('proposed', 'ready', 'queued')
             THEN 1 ELSE 0 END) AS planned_count,
    CASE
        WHEN SUM(CASE WHEN state = 'completed' THEN 1 ELSE 0 END) = COUNT(*)
            THEN 'done'
        WHEN SUM(CASE WHEN state IN ('active', 'running', 'delivering', 'claimed', 'accepted')
                      THEN 1 ELSE 0 END) > 0
            THEN 'in_progress'
        WHEN SUM(CASE WHEN state IN ('failed', 'failed_delivery', 'dead_letter', 'expired', 'timed_out')
                      THEN 1 ELSE 0 END) = COUNT(*)
            THEN 'failed'
        WHEN SUM(CASE WHEN state = 'ready' THEN 1 ELSE 0 END) > 0
            THEN 'ready'
        ELSE 'proposed'
    END AS derived_status,
    MAX(updated_at) AS last_activity
FROM dispatches
WHERE output_ref IS NOT NULL
GROUP BY project_id, output_ref
""")

_OI_BLOCKER_IDX = (IndexInvariant("idx_track_oi_active_blockers",
                                  ("track_id", "project_id", "link_type"), partial=True,
                                  where="resolved_at IS NULL"),)


def _build_manifest() -> Dict[int, VersionManifest]:
    base_children_v24 = (_tph_v24(), _td_v24())
    return {
        22: VersionManifest(22, (
            _dispatches(_DISPATCH_COLS_BASE), _tracks_v22(), _tph_v22(),
            _td_v22(), _toi_v22())),
        24: VersionManifest(24, (
            _dispatches(_DISPATCH_COLS_BASE), _tracks_composite(_TRACKS_COLS_V24),
            *base_children_v24, _toi_composite(_TOI_COLS_V24))),
        27: VersionManifest(27, (
            _dispatches(_DISPATCH_COLS_V27),
            _tracks_composite(_TRACKS_COLS_V27, _TRACKS_IDX_V27),
            *base_children_v24, _toi_composite(_TOI_COLS_V24)),
            (_DELIVERABLES_VIEW,)),
        28: VersionManifest(28, (
            _dispatches(_DISPATCH_COLS_V27),
            _tracks_composite(_TRACKS_COLS_V28, _TRACKS_IDX_V28),
            *base_children_v24, _toi_composite(_TOI_COLS_V24)),
            (_DELIVERABLES_VIEW,)),
        29: VersionManifest(29, (
            _dispatches(_DISPATCH_COLS_V27),
            _tracks_composite(_TRACKS_COLS_V29, _TRACKS_IDX_V29),
            *base_children_v24, _toi_composite(_TOI_COLS_V24)),
            (_DELIVERABLES_VIEW,)),
        30: VersionManifest(30, (
            _dispatches(_DISPATCH_COLS_V27),
            _tracks_composite(_TRACKS_COLS_V29, _TRACKS_IDX_V29),
            *base_children_v24, _toi_composite(_TOI_COLS_V30, _OI_BLOCKER_IDX)),
            (_DELIVERABLES_VIEW,)),
    }


SCHEMA_MANIFEST: Dict[int, VersionManifest] = _build_manifest()
TERMINAL_VERSION: int = max(SCHEMA_MANIFEST)
MIN_VERSION: int = min(SCHEMA_MANIFEST)


# ---------------------------------------------------------------------------
# Live-DB introspection helpers
# ---------------------------------------------------------------------------

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _actual_columns(conn: sqlite3.Connection, table: str) -> Dict[str, Tuple[str, bool]]:
    """name -> (affinity_upper, notnull) for *table*."""
    return {r[1]: ((r[2] or "").upper(), bool(r[3]))
            for r in conn.execute(f"PRAGMA table_info('{table}')")}


def _actual_pk(conn: sqlite3.Connection, table: str) -> Tuple[str, ...]:
    rows = [r for r in conn.execute(f"PRAGMA table_info('{table}')") if r[5] > 0]
    return tuple(r[1] for r in sorted(rows, key=lambda r: r[5]))


def _actual_foreign_keys(conn: sqlite3.Connection, table: str):
    groups: Dict[int, Dict] = {}
    for r in conn.execute(f"PRAGMA foreign_key_list('{table}')"):
        fk_id, seq, ref_table, frm, to, on_upd, on_del = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
        g = groups.setdefault(fk_id, {"ref": ref_table, "pairs": [],
                                      "on_update": on_upd, "on_delete": on_del})
        g["pairs"].append((seq, frm, to))
    out = set()
    for g in groups.values():
        pairs = sorted(g["pairs"], key=lambda p: p[0])
        out.add((tuple(p[1] for p in pairs), g["ref"], tuple(p[2] for p in pairs),
                 g["on_update"], g["on_delete"]))
    return out


def _actual_indexes(conn: sqlite3.Connection, table: str) -> Dict[str, Tuple]:
    """name -> (unique, partial, keycols_tuple) for explicit + auto indexes."""
    out: Dict[str, Tuple] = {}
    for idx in conn.execute(f"PRAGMA index_list('{table}')"):
        name, unique, partial = idx[1], bool(idx[2]), bool(idx[4]) if len(idx) > 4 else False
        keycols = tuple(r[2] for r in conn.execute(f"PRAGMA index_xinfo('{name}')")
                        if r[5] == 1)
        out[name] = (unique, partial, keycols)
    return out


def _actual_unique_keys(conn: sqlite3.Connection, table: str):
    """Column-sets (frozenset) of every unique index on *table* (PK + explicit UNIQUE)."""
    keys = set()
    for unique, _partial, keycols in _actual_indexes(conn, table).values():
        if unique and keycols:
            keys.add(frozenset(keycols))
    return keys


def _normalize_sql(sql: Optional[str]) -> str:
    s = re.sub(r"\s+", " ", sql or "").strip().rstrip(";").strip().lower()
    return s.replace("if not exists ", "")


def _normalize_predicate(pred: Optional[str]) -> str:
    """Collapse a partial-index WHERE predicate to a comparable canonical form."""
    return re.sub(r"\s+", " ", pred or "").strip().rstrip(";").strip().lower()


def _index_where_predicate(conn: sqlite3.Connection, index_name: str) -> Optional[str]:
    """The raw WHERE predicate text of a partial index (None when absent/non-partial).

    PRAGMA index_xinfo does NOT expose the predicate, so it is read from the
    verbatim CREATE INDEX statement in sqlite_master: everything after the first
    WHERE keyword (a CREATE INDEX has no WHERE other than the partial predicate)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index_name,)
    ).fetchone()
    if not row or not row[0]:
        return None
    m = re.search(r"(?i)\bWHERE\b", row[0])
    return row[0][m.end():] if m else None


# ---------------------------------------------------------------------------
# Validators (each returns a list of human-readable violation strings)
# ---------------------------------------------------------------------------

def _validate_columns(actual: Dict[str, Tuple[str, bool]], tbl: TableInvariant) -> List[str]:
    out: List[str] = []
    for name, spec in tbl.columns.items():
        if name not in actual:
            out.append(f"{tbl.name}: missing column '{name}'")
            continue
        affinity, notnull = actual[name]
        if affinity != spec.affinity:
            out.append(f"{tbl.name}.{name}: affinity {affinity!r} != {spec.affinity!r}")
        if notnull != spec.notnull:
            out.append(f"{tbl.name}.{name}: nullability notnull={notnull} != {spec.notnull}")
    return out


def _validate_indexes(conn: sqlite3.Connection, tbl: TableInvariant) -> List[str]:
    actual = _actual_indexes(conn, tbl.name)
    out: List[str] = []
    for idx in tbl.indexes:
        got = actual.get(idx.name)
        if got is None:
            out.append(f"{tbl.name}: missing index '{idx.name}'")
            continue
        unique, partial, keycols = got
        if keycols != idx.columns:
            out.append(f"{tbl.name}.{idx.name}: key cols {keycols} != {idx.columns}")
        if unique != idx.unique:
            out.append(f"{tbl.name}.{idx.name}: unique={unique} != {idx.unique}")
        if partial != idx.partial:
            out.append(f"{tbl.name}.{idx.name}: partial={partial} != {idx.partial}")
        elif idx.partial and idx.where is not None:
            # A2-N1: an index with the right columns but a DIFFERENT WHERE predicate is
            # NOT the declared invariant — the boolean partial flag alone would accept
            # a mis-predicated index, so the actual predicate text must also match.
            actual_pred = _normalize_predicate(_index_where_predicate(conn, idx.name))
            want_pred = _normalize_predicate(idx.where)
            if actual_pred != want_pred:
                out.append(f"{tbl.name}.{idx.name}: partial predicate "
                           f"{actual_pred!r} != {want_pred!r}")
    return out


def validate_table(conn: sqlite3.Connection, tbl: TableInvariant) -> List[str]:
    if not _table_exists(conn, tbl.name):
        return [f"missing table '{tbl.name}'"]
    out = _validate_columns(_actual_columns(conn, tbl.name), tbl)
    actual_pk = _actual_pk(conn, tbl.name)
    if actual_pk != tbl.pk:
        out.append(f"{tbl.name}: PK {actual_pk} != {tbl.pk}")
    actual_uk = _actual_unique_keys(conn, tbl.name)
    for key in tbl.unique_keys:
        if frozenset(key) not in actual_uk:
            out.append(f"{tbl.name}: missing UNIQUE{key}")
    actual_fks = _actual_foreign_keys(conn, tbl.name)
    for fk in tbl.foreign_keys:
        want = (fk.columns, fk.ref_table, fk.ref_columns, fk.on_update, fk.on_delete)
        if want not in actual_fks:
            out.append(f"{tbl.name}: missing FK {fk.columns}->{fk.ref_table}{fk.ref_columns}")
    out += _validate_indexes(conn, tbl)
    return out


def validate_view(conn: sqlite3.Connection, view: ViewInvariant) -> List[str]:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (view.name,)
    ).fetchone()
    if row is None:
        return [f"missing view '{view.name}'"]
    if _normalize_sql(row[0]) != _normalize_sql(view.sql):
        return [f"view '{view.name}': normalized SQL mismatch"]
    return []


def validate_db_at_version(conn: sqlite3.Connection, version: int) -> List[str]:
    """Return the list of invariant violations for *version* (empty == fully holds).

    Lenient on EXTRA columns/indexes/FKs (a later-version DB may carry more) but
    STRICT on PK ordinals (so a composite-PK v24+ DB never validates as v22) and on
    the declared nullability/affinity of every required column."""
    manifest = SCHEMA_MANIFEST[version]
    out: List[str] = []
    for tbl in manifest.tables:
        out += validate_table(conn, tbl)
    for view in manifest.views:
        out += validate_view(conn, view)
    return out


# ---------------------------------------------------------------------------
# Reconciler engine
# ---------------------------------------------------------------------------

def derive_correct_version(conn: sqlite3.Connection, max_version: int) -> Optional[int]:
    """Highest manifest version <= *max_version* whose invariants FULLY hold.

    Checked low->high; the last fully-satisfied version wins (R2.1). Returns None
    when not even the lowest manifest version holds (genuine corruption)."""
    best: Optional[int] = None
    for v in sorted(SCHEMA_MANIFEST):
        if v > max_version:
            break
        if not validate_db_at_version(conn, v):
            best = v
    return best


def _effective_manifest_version(claimed: int) -> Optional[int]:
    """Manifest version to validate a *claimed* user_version against (highest <= claimed)."""
    candidates = [v for v in SCHEMA_MANIFEST if v <= claimed]
    return max(candidates) if candidates else None


def reconcile_user_version(conn: sqlite3.Connection) -> ReconcileResult:
    """Validate the claimed user_version against the manifest; downgrade on mismatch.

    If the DB satisfies the manifest for its claimed version -> no-op. Otherwise
    downgrade `user_version` to the highest version that fully holds, so the
    numbered walk re-applies the exposed migrations. If no lower version holds,
    raise SchemaReconciliationError (corruption; never guess — ADR-009)."""
    claimed = conn.execute("PRAGMA user_version").fetchone()[0]
    effective = _effective_manifest_version(claimed)
    if effective is None:
        return ReconcileResult(False, claimed, claimed)        # pre-track DB; walk handles it
    violations = validate_db_at_version(conn, effective)
    if not violations:
        return ReconcileResult(False, claimed, claimed)        # claim consistent with shape
    correct = derive_correct_version(conn, max_version=claimed)
    if correct is None or correct >= claimed:
        raise SchemaReconciliationError(
            f"user_version={claimed} fails its v{effective} invariant manifest "
            f"({violations[:3]}) and no lower fully-satisfied version exists "
            f"(derived={correct}); refusing to guess — genuine schema corruption "
            "(R2.1; ADR-009 schema-first; ADR-007 tenant invariants).")
    conn.execute(f"PRAGMA user_version = {correct}")
    return ReconcileResult(True, claimed, correct, tuple(violations))


# ---------------------------------------------------------------------------
# Manifest query helpers (used by the manifest-backed migration preflights)
# ---------------------------------------------------------------------------

def table_at(version: int, table: str) -> Optional[TableInvariant]:
    for tbl in SCHEMA_MANIFEST[version].tables:
        if tbl.name == table:
            return tbl
    return None


def table_pk_at(version: int, table: str) -> Tuple[str, ...]:
    tbl = table_at(version, table)
    return tbl.pk if tbl else ()


def columns_introduced_at(version: int, table: str) -> Tuple[str, ...]:
    """Column names present in *table* at *version* but not at the prior manifest
    version (the additive delta that migration introduces). Order preserved."""
    versions = sorted(SCHEMA_MANIFEST)
    if version not in versions:
        return ()
    idx = versions.index(version)
    cur = table_at(version, table)
    if cur is None:
        return ()
    if idx == 0:
        return tuple(cur.columns)
    prev = table_at(versions[idx - 1], table)
    prev_names = set(prev.columns) if prev else set()
    return tuple(n for n in cur.columns if n not in prev_names)
