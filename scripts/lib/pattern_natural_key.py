#!/usr/bin/env python3
"""pattern_natural_key.py — collapse duplicate patterns, then enforce the natural key.

``success_patterns`` and ``antipatterns`` carry an ADR-007 unique index on
``(project_id, id)``. ``id`` is an autoincrement, so that index never stops a
project from holding the same pattern twice. This migration gives both tables
the natural key ``UNIQUE(project_id, pattern_type, title)``, in this order:

1. Count before: rows per table, duplicate groups, NULL/empty key rows and
   references to pattern ids that do not exist.
2. Merge every duplicate group with the rules in ``pattern_upsert``: counter
   summed, ``first_seen`` minimum, last-seen maximum, id lists as a
   de-duplicated union, ``confidence_score`` weighted by the counter, every
   other column from the row with the highest counter (ties: lowest id). The
   lowest id survives, so the oldest ``intel_sp_N`` / ``intel_ap_N`` reference
   stays valid and only the younger ones move.
3. Remap every reference to a merged id onto the survivor:
   ``dispatch_pattern_offered``, ``pattern_usage``,
   ``pattern_injection_outcome`` (``intel_sp_N`` / ``intel_ap_N``),
   ``dream_pattern_archives`` (``original_table`` + ``original_pattern_id``)
   and a ``"pattern_id"`` inside ``pattern_data``.
4. Create the natural-key unique index.
5. Count after, including the dangling-reference check.

Rows with a NULL or empty ``project_id``, ``pattern_type`` or ``title`` are
listed in the report and never merged or deleted.

The whole apply runs in one ``BEGIN IMMEDIATE`` transaction and is idempotent:
a second run finds no groups and an existing index and changes nothing. The
default is a read-only dry run; nothing is written without ``apply=True``, and
the caller always names the DB path — no path is resolved from the environment.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import pattern_upsert as pu
    from pattern_dedup import _merge_source_dispatch_ids, _redirect_dispatch_pattern_offered
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import pattern_upsert as pu
    from pattern_dedup import _merge_source_dispatch_ids, _redirect_dispatch_pattern_offered

REF_TABLES = ("dispatch_pattern_offered", "pattern_usage", "pattern_injection_outcome")
_KEY_COLS = ", ".join(pu.NATURAL_KEY)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _keyed(conn: sqlite3.Connection, spec: pu.TableSpec) -> bool:
    return _table_exists(conn, spec.table) and set(pu.NATURAL_KEY) <= set(
        pu.table_columns(conn, spec.table)
    )


def _blank(col: str) -> str:
    return f"({col} IS NULL OR TRIM({col}) = '')"


_ANY_BLANK = " OR ".join(_blank(c) for c in pu.NATURAL_KEY)


def blank_key_rows(conn: sqlite3.Connection, spec: pu.TableSpec) -> List[int]:
    return [r[0] for r in conn.execute(
        f"SELECT id FROM {spec.table} WHERE {_ANY_BLANK} ORDER BY id"
    ).fetchall()]


def duplicate_groups(conn: sqlite3.Connection, spec: pu.TableSpec) -> List[List[int]]:
    """Id lists (ascending) of every natural-key group with more than one row.

    Rows with a blank key part are excluded: they are reported, not merged.
    """
    rows = conn.execute(
        f"SELECT group_concat(id) FROM (SELECT id, {_KEY_COLS} FROM {spec.table} "
        f"WHERE NOT ({_ANY_BLANK}) ORDER BY id) "
        f"GROUP BY {_KEY_COLS} HAVING COUNT(*) > 1 ORDER BY MIN(id)"
    ).fetchall()
    return [sorted(int(x) for x in r[0].split(",")) for r in rows]


def blank_key_collisions(conn: sqlite3.Connection, spec: pu.TableSpec) -> List[List[int]]:
    """Groups of blank-key rows that would still block the unique index."""
    rows = conn.execute(
        f"SELECT group_concat(id) FROM {spec.table} WHERE {_ANY_BLANK} "
        f"AND project_id IS NOT NULL AND pattern_type IS NOT NULL AND title IS NOT NULL "
        f"GROUP BY {_KEY_COLS} HAVING COUNT(*) > 1"
    ).fetchall()
    return [sorted(int(x) for x in r[0].split(",")) for r in rows]


def _ref_id(value: Any, prefix: str) -> Optional[int]:
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    try:
        return int(value[len(prefix):])
    except ValueError:
        return None


def _pattern_data_ref(raw: Any) -> Optional[str]:
    try:
        data = json.loads(raw) if raw else None
    except (json.JSONDecodeError, TypeError):
        return None
    ref = data.get("pattern_id") if isinstance(data, dict) else None
    return ref if isinstance(ref, str) else None


def dangling_references(conn: sqlite3.Connection) -> Dict[str, int]:
    """Count references to an ``intel_sp_N`` / ``intel_ap_N`` / archived id that
    has no row. Keys: referencing table (or ``<table>.pattern_data``)."""
    live = {s.ref_prefix: set() for s in pu.SPECS}
    by_table = {s.table: set() for s in pu.SPECS}
    for spec in pu.SPECS:
        if _table_exists(conn, spec.table):
            ids = {r[0] for r in conn.execute(f"SELECT id FROM {spec.table}")}
            live[spec.ref_prefix] = ids
            by_table[spec.table] = ids

    def missing(value: Any) -> bool:
        for prefix, ids in live.items():
            rid = _ref_id(value, prefix)
            if rid is not None:
                return rid not in ids
        return False

    out: Dict[str, int] = {}
    for table in REF_TABLES:
        if _table_exists(conn, table):
            out[table] = sum(
                1 for (pid,) in conn.execute(f"SELECT pattern_id FROM {table}") if missing(pid)
            )
    if _table_exists(conn, "dream_pattern_archives"):
        out["dream_pattern_archives"] = sum(
            1 for tbl, oid in conn.execute(
                "SELECT original_table, original_pattern_id FROM dream_pattern_archives"
            )
            if tbl in by_table and oid not in by_table[tbl]
        )
    for spec in pu.SPECS:
        if _table_exists(conn, spec.table):
            out[f"{spec.table}.pattern_data"] = sum(
                1 for (raw,) in conn.execute(f"SELECT pattern_data FROM {spec.table}")
                if missing(_pattern_data_ref(raw))
            )
    return out


def measure(conn: sqlite3.Connection) -> Dict[str, Any]:
    tables: Dict[str, Any] = {}
    for spec in pu.SPECS:
        if not _keyed(conn, spec):
            tables[spec.table] = {"present": False}
            continue
        groups = duplicate_groups(conn, spec)
        tables[spec.table] = {
            "present": True,
            "rows": conn.execute(f"SELECT COUNT(*) FROM {spec.table}").fetchone()[0],
            "duplicate_groups": len(groups),
            "duplicate_rows": sum(len(g) for g in groups),
            "blank_key_rows": blank_key_rows(conn, spec),
            "blank_key_collisions": blank_key_collisions(conn, spec),
            "natural_key_index": pu.has_natural_key_index(conn, spec.table),
        }
    dangling = dangling_references(conn)
    return {"tables": tables, "dangling_references": dangling,
            "dangling_total": sum(dangling.values())}


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merged_values(spec: pu.TableSpec, rows: List[dict]) -> dict:
    """Merge a duplicate group (rows ordered by id) into the survivor's values."""
    c = spec.counter
    best = max(rows, key=lambda r: (int(r.get(c) or 0), -int(r["id"])))
    out = {k: v for k, v in best.items() if k != "id"}
    for key in pu.NATURAL_KEY:
        out[key] = rows[0][key]
    out[c] = sum(int(r.get(c) or 0) for r in rows)
    first, last = None, None
    for r in rows:
        first = pu.ts_min(first, r.get("first_seen"))
        last = pu.ts_max(last, r.get(spec.last_seen))
    out["first_seen"], out[spec.last_seen] = first, last
    for col in spec.id_lists:
        if col in rows[0]:
            if all(r.get(col) is None for r in rows):
                out[col] = None
            else:
                out[col] = _merge_source_dispatch_ids([r.get(col) for r in rows])
    if spec.has_confidence and "confidence_score" in rows[0]:
        conf, n = None, 0
        for r in rows:
            rn = int(r.get(c) or 0)
            conf = r.get("confidence_score") if conf is None else pu.weighted_confidence(
                conf, n, r.get("confidence_score"), rn
            )
            n += rn
        out["confidence_score"] = conf
    return out


def _fold_pattern_usage(conn: sqlite3.Connection, old: str, new: str) -> int:
    """Move pattern_usage rows from ``old`` to ``new``, per project_id."""
    if not _table_exists(conn, "pattern_usage"):
        return 0
    conn.row_factory = sqlite3.Row
    moved = 0
    for dup in conn.execute(
        "SELECT * FROM pattern_usage WHERE pattern_id = ?", (old,)
    ).fetchall():
        canon = conn.execute(
            "SELECT * FROM pattern_usage WHERE pattern_id = ? AND project_id = ?",
            (new, dup["project_id"]),
        ).fetchone()
        if canon is None:
            conn.execute(
                "UPDATE pattern_usage SET pattern_id = ? WHERE pattern_id = ? AND project_id = ?",
                (new, old, dup["project_id"]),
            )
        else:
            conn.execute(
                "UPDATE pattern_usage SET used_count = ?, ignored_count = ?, "
                "success_count = ?, failure_count = ?, confidence = ?, last_used = ?, "
                "last_offered = ?, created_at = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE pattern_id = ? AND project_id = ?",
                (
                    int(canon["used_count"] or 0) + int(dup["used_count"] or 0),
                    int(canon["ignored_count"] or 0) + int(dup["ignored_count"] or 0),
                    int(canon["success_count"] or 0) + int(dup["success_count"] or 0),
                    int(canon["failure_count"] or 0) + int(dup["failure_count"] or 0),
                    max(float(canon["confidence"] or 0.0), float(dup["confidence"] or 0.0)),
                    pu.ts_max(canon["last_used"], dup["last_used"]),
                    pu.ts_max(canon["last_offered"], dup["last_offered"]),
                    pu.ts_min(canon["created_at"], dup["created_at"]),
                    new, dup["project_id"],
                ),
            )
            conn.execute(
                "DELETE FROM pattern_usage WHERE pattern_id = ? AND project_id = ?",
                (old, dup["project_id"]),
            )
        moved += 1
    return moved


def _fold_injection_outcome(conn: sqlite3.Connection, old: str, new: str) -> int:
    """Move pattern_injection_outcome rows; a (project, dispatch) that already has
    an outcome for ``new`` keeps it, with ``used`` as the maximum of both."""
    if not _table_exists(conn, "pattern_injection_outcome"):
        return 0
    moved = 0
    for rid, project_id, dispatch_id, used in conn.execute(
        "SELECT id, project_id, dispatch_id, used FROM pattern_injection_outcome "
        "WHERE pattern_id = ?", (old,)
    ).fetchall():
        canon = conn.execute(
            "SELECT id FROM pattern_injection_outcome WHERE project_id = ? "
            "AND dispatch_id = ? AND pattern_id = ?",
            (project_id, dispatch_id, new),
        ).fetchone()
        if canon is None:
            conn.execute(
                "UPDATE pattern_injection_outcome SET pattern_id = ? WHERE id = ?", (new, rid)
            )
        else:
            conn.execute(
                "UPDATE pattern_injection_outcome SET used = MAX(used, ?) WHERE id = ?",
                (int(used or 0), canon[0]),
            )
            conn.execute("DELETE FROM pattern_injection_outcome WHERE id = ?", (rid,))
        moved += 1
    return moved


def _remap_pattern_data(conn: sqlite3.Connection, old: str, new: str) -> int:
    moved = 0
    for spec in pu.SPECS:
        if not _table_exists(conn, spec.table):
            continue
        for rid, raw in conn.execute(
            f"SELECT id, pattern_data FROM {spec.table} WHERE pattern_data LIKE ?",
            (f"%{old}%",),
        ).fetchall():
            if _pattern_data_ref(raw) != old:
                continue
            data = json.loads(raw)
            data["pattern_id"] = new
            conn.execute(
                f"UPDATE {spec.table} SET pattern_data = ? WHERE id = ?", (json.dumps(data), rid)
            )
            moved += 1
    return moved


def remap_references(conn: sqlite3.Connection, spec: pu.TableSpec, dup_id: int,
                     survivor_id: int) -> Dict[str, int]:
    old, new = f"{spec.ref_prefix}{dup_id}", f"{spec.ref_prefix}{survivor_id}"
    counts: Dict[str, int] = {}
    if _table_exists(conn, "dispatch_pattern_offered"):
        counts["dispatch_pattern_offered"] = conn.execute(
            "SELECT COUNT(*) FROM dispatch_pattern_offered WHERE pattern_id = ?", (old,)
        ).fetchone()[0]
        _redirect_dispatch_pattern_offered(conn, old, new)
    counts["pattern_usage"] = _fold_pattern_usage(conn, old, new)
    counts["pattern_injection_outcome"] = _fold_injection_outcome(conn, old, new)
    if _table_exists(conn, "dream_pattern_archives"):
        counts["dream_pattern_archives"] = conn.execute(
            "UPDATE dream_pattern_archives SET original_pattern_id = ? "
            "WHERE original_table = ? AND original_pattern_id = ?",
            (survivor_id, spec.table, dup_id),
        ).rowcount
    counts["pattern_data"] = _remap_pattern_data(conn, old, new)
    return counts


def merge_group(conn: sqlite3.Connection, spec: pu.TableSpec, ids: List[int]) -> Dict[str, Any]:
    conn.row_factory = sqlite3.Row
    marks = ",".join("?" * len(ids))
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM {spec.table} WHERE id IN ({marks}) ORDER BY id", tuple(ids)
    ).fetchall()]
    survivor, dups = rows[0]["id"], [r["id"] for r in rows[1:]]
    values = merged_values(spec, rows)
    cols = list(values)
    conn.execute(
        f"UPDATE {spec.table} SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
        tuple(values[k] for k in cols) + (survivor,),
    )
    remapped: Dict[str, int] = {}
    for dup in dups:
        for table, n in remap_references(conn, spec, dup, survivor).items():
            remapped[table] = remapped.get(table, 0) + n
    conn.execute(
        f"DELETE FROM {spec.table} WHERE id IN ({','.join('?' * len(dups))})", tuple(dups)
    )
    return {"survivor": survivor, "merged": dups, "remapped": remapped,
            spec.counter: values[spec.counter]}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def create_index_if_clean(conn: sqlite3.Connection, spec: pu.TableSpec) -> str:
    """Schema-bootstrap path (quality_db_init v33): create the natural-key index
    only when the table holds nothing to merge. Never merges, never deletes.
    Returns ``created`` / ``present`` / ``absent-table`` / ``blocked``."""
    if not _keyed(conn, spec):
        return "absent-table"
    if pu.has_natural_key_index(conn, spec.table):
        return "present"
    if duplicate_groups(conn, spec) or blank_key_collisions(conn, spec):
        return "blocked"
    pu.create_natural_key_index(conn, spec)
    return "created"


def run(db_path: Path, *, apply: bool = False) -> Dict[str, Any]:
    """Dry run (read-only) by default; ``apply=True`` merges, remaps and indexes."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"DB not found: {db_path}")
    if not apply:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            before = measure(conn)
            plan = {
                spec.table: [{"survivor": g[0], "merged": g[1:]} for g in duplicate_groups(conn, spec)]
                for spec in pu.SPECS if _keyed(conn, spec)
            }
        finally:
            conn.close()
        return {"db": str(db_path), "mode": "dry-run", "before": before, "plan": plan}

    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            before = measure(conn)
            merges: Dict[str, List[dict]] = {}
            indexes: Dict[str, bool] = {}
            for spec in pu.SPECS:
                if not _keyed(conn, spec):
                    continue
                merges[spec.table] = [merge_group(conn, spec, g) for g in duplicate_groups(conn, spec)]
                collisions = blank_key_collisions(conn, spec)
                if collisions:
                    raise RuntimeError(
                        f"{spec.table}: rows with an empty key part block the unique index: {collisions}"
                    )
                indexes[spec.index_name] = pu.create_natural_key_index(conn, spec)
            after = measure(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    changes = sum(len(v) for v in merges.values()) + sum(1 for v in indexes.values() if v)
    return {"db": str(db_path), "mode": "apply", "before": before, "merges": merges,
            "indexes_created": indexes, "after": after, "changes": changes}
