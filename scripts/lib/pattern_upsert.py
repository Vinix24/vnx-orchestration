#!/usr/bin/env python3
"""pattern_upsert.py — the one write path into success_patterns / antipatterns.

Every writer of the two pattern tables goes through :func:`upsert_success_pattern`
or :func:`upsert_antipattern`. A pattern is identified by its natural key
``(project_id, pattern_type, title)``; ``id`` is an autoincrement and says
nothing about whether two rows describe the same pattern. ADR-007: the key
always carries ``project_id``.

The merge rules are defined once, here, and shared with the natural-key
migration (``pattern_natural_key.py``) that collapses existing duplicates:

* counter (``usage_count`` / ``occurrence_count``): summed (``counter="add"``)
* ``first_seen``: minimum; ``last_used`` / ``last_seen``: maximum
* ``source_dispatch_ids`` / ``source_receipts``: de-duplicated union
  (``pattern_dedup._merge_source_dispatch_ids``)
* ``confidence_score``: weighted by the counter
* every other column: taken from the side with the higher counter

Two writers read a cumulative counter rather than a delta, and summing it on
every run would inflate the count. They pick another counter mode:

* ``counter="max"`` — the counter is the larger of the two and the incoming
  confidence wins (learning_loop re-reads cumulative pattern_usage counters).
* ``counter="replace"`` — the incoming row is a snapshot that replaces the
  stored one: counter, confidence, sources and the other columns all come from
  the incoming side (pattern_extractor owns its behaviour-analysis rows).

Both modes keep the first_seen minimum and the last-seen maximum.

The write is ``INSERT ... ON CONFLICT(project_id, pattern_type, title) DO
UPDATE`` when the natural-key unique index exists. A DB that has not run the
migration yet has no such index, and SQLite refuses an ON CONFLICT target that
matches no unique index, so the same SET clause is then applied through
``UPDATE ... FROM`` onto the lowest-id row with that key. One SET clause, two
statements: the merge rules cannot drift between the paths.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Union

try:
    from pattern_dedup import _merge_source_dispatch_ids
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pattern_dedup import _merge_source_dispatch_ids

NATURAL_KEY = ("project_id", "pattern_type", "title")
COUNTER_MODES = ("add", "max", "replace")


@dataclass(frozen=True)
class TableSpec:
    table: str
    counter: str
    last_seen: str
    has_confidence: bool
    id_lists: tuple
    index_name: str
    ref_prefix: str


SUCCESS = TableSpec(
    table="success_patterns",
    counter="usage_count",
    last_seen="last_used",
    has_confidence=True,
    id_lists=("source_dispatch_ids", "source_receipts"),
    index_name="ux_success_patterns_natural_key",
    ref_prefix="intel_sp_",
)
ANTI = TableSpec(
    table="antipatterns",
    counter="occurrence_count",
    last_seen="last_seen",
    has_confidence=False,
    id_lists=("source_dispatch_ids",),
    index_name="ux_antipatterns_natural_key",
    ref_prefix="intel_ap_",
)
SPECS = (SUCCESS, ANTI)

# Columns the merge never treats as "other columns": the key, the id and the
# columns that carry their own rule.
_RULED = {"id", "first_seen"} | set(NATURAL_KEY)


@dataclass(frozen=True)
class UpsertResult:
    id: int
    inserted: bool


# ---------------------------------------------------------------------------
# Merge primitives (shared with the migration)
# ---------------------------------------------------------------------------

def _ts_key(value: Any) -> str:
    # CURRENT_TIMESTAMP writes "YYYY-MM-DD HH:MM:SS", isoformat() writes a "T".
    return str(value).replace(" ", "T")


def ts_min(a: Any, b: Any) -> Any:
    if a is None or a == "":
        return b
    if b is None or b == "":
        return a
    return a if _ts_key(a) <= _ts_key(b) else b


def ts_max(a: Any, b: Any) -> Any:
    if a is None or a == "":
        return b
    if b is None or b == "":
        return a
    return a if _ts_key(a) >= _ts_key(b) else b


def merge_ids(a: Optional[str], b: Optional[str]) -> str:
    return _merge_source_dispatch_ids([a, b])


def weighted_confidence(c1: Any, n1: Any, c2: Any, n2: Any) -> float:
    c1, c2 = float(c1 or 0.0), float(c2 or 0.0)
    n1, n2 = max(int(n1 or 0), 0), max(int(n2 or 0), 0)
    if n1 + n2 == 0:
        return max(c1, c2)
    return (c1 * n1 + c2 * n2) / (n1 + n2)


def register_merge_functions(conn: sqlite3.Connection) -> None:
    conn.create_function("vnx_ts_min", 2, ts_min, deterministic=True)
    conn.create_function("vnx_ts_max", 2, ts_max, deterministic=True)
    conn.create_function("vnx_merge_ids", 2, merge_ids, deterministic=True)
    conn.create_function("vnx_weighted_conf", 4, weighted_confidence, deterministic=True)


# ---------------------------------------------------------------------------
# Schema probes
# ---------------------------------------------------------------------------

def table_columns(conn: sqlite3.Connection, table: str) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def has_natural_key_index(conn: sqlite3.Connection, table: str) -> bool:
    """True when a UNIQUE index on exactly (project_id, pattern_type, title) exists."""
    for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
        name, unique, partial = row[1], row[2], row[4] if len(row) > 4 else 0
        if not unique or partial:
            continue
        cols = tuple(r[2] for r in conn.execute(f"PRAGMA index_info({name})").fetchall())
        if cols == NATURAL_KEY:
            return True
    return False


def create_natural_key_index(conn: sqlite3.Connection, spec: TableSpec) -> bool:
    """Create the natural-key unique index. Returns True when it was created now."""
    if has_natural_key_index(conn, spec.table):
        return False
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {spec.index_name} "
        f"ON {spec.table}(project_id, pattern_type, title)"
    )
    return True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _require(name: str, value: Any) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"pattern upsert refused: {name} is NULL or empty")
    return str(value)


def _as_id_list(value: Union[None, str, Iterable[str]]) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        return value
    return json.dumps([str(v) for v in value if v])


# ---------------------------------------------------------------------------
# The upsert
# ---------------------------------------------------------------------------

def _set_clause(spec: TableSpec, columns: Sequence[str], counter: str,
                always_update: Sequence[str]) -> str:
    t, c = spec.table, spec.counter
    old_n, new_n = f"COALESCE({t}.{c}, 0)", f"COALESCE(excluded.{c}, 0)"
    newer_wins = f"{new_n} > {old_n}"
    parts = []
    if counter == "add":
        parts.append(f"{c} = {old_n} + {new_n}")
    elif counter == "max":
        parts.append(f"{c} = MAX({old_n}, {new_n})")
    else:
        parts.append(f"{c} = excluded.{c}")
    parts.append(f"first_seen = vnx_ts_min({t}.first_seen, excluded.first_seen)")
    parts.append(f"{spec.last_seen} = vnx_ts_max({t}.{spec.last_seen}, excluded.{spec.last_seen})")
    for col in spec.id_lists:
        if col not in columns:
            continue
        if counter == "replace":
            parts.append(f"{col} = excluded.{col}")
        else:
            parts.append(f"{col} = vnx_merge_ids({t}.{col}, excluded.{col})")
    if spec.has_confidence and "confidence_score" in columns:
        if counter == "add":
            parts.append(
                f"confidence_score = vnx_weighted_conf({t}.confidence_score, {old_n}, "
                f"excluded.confidence_score, {new_n})"
            )
        else:
            parts.append("confidence_score = excluded.confidence_score")
    ruled = _RULED | {c, spec.last_seen, "confidence_score"} | set(spec.id_lists)
    for col in columns:
        if col in ruled:
            continue
        if counter == "replace" or col in always_update:
            parts.append(f"{col} = excluded.{col}")
        else:
            parts.append(f"{col} = CASE WHEN {newer_wins} THEN excluded.{col} ELSE {t}.{col} END")
    return ",\n    ".join(parts)


def _upsert(conn: sqlite3.Connection, spec: TableSpec, row: dict, counter: str,
            always_update: Sequence[str]) -> UpsertResult:
    if counter not in COUNTER_MODES:
        raise ValueError(f"counter must be one of {COUNTER_MODES}, got {counter!r}")
    for key in NATURAL_KEY:
        row[key] = _require(key, row.get(key))
    table_cols = table_columns(conn, spec.table)
    if "project_id" not in table_cols:
        raise ValueError(
            f"{spec.table} has no project_id column; ADR-007 forbids an unscoped pattern write"
        )
    unknown = [k for k in list(row) + list(always_update) if k not in table_cols]
    if unknown:
        raise ValueError(f"{spec.table} has no column(s) {unknown}")
    cols = [k for k in row]
    register_merge_functions(conn)
    set_clause = _set_clause(spec, cols, counter, always_update)
    existing = conn.execute(
        f"SELECT id FROM {spec.table} WHERE project_id = ? AND pattern_type = ? "
        "AND title = ? ORDER BY id LIMIT 1",
        (row["project_id"], row["pattern_type"], row["title"]),
    ).fetchone()
    values = tuple(row[k] for k in cols)
    col_list = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)

    if has_natural_key_index(conn, spec.table):
        rid = conn.execute(
            f"INSERT INTO {spec.table} ({col_list}) VALUES ({marks})\n"
            f"ON CONFLICT(project_id, pattern_type, title) DO UPDATE SET\n    {set_clause}\n"
            "RETURNING id",
            values,
        ).fetchone()[0]
        return UpsertResult(id=int(rid), inserted=existing is None)

    if existing is None:
        cur = conn.execute(f"INSERT INTO {spec.table} ({col_list}) VALUES ({marks})", values)
        return UpsertResult(id=int(cur.lastrowid), inserted=True)

    target = int(existing[0])
    aliased = ", ".join(f"? AS {k}" for k in cols)
    conn.execute(
        f"UPDATE {spec.table} SET\n    {set_clause}\n"
        f"FROM (SELECT {aliased}) AS excluded WHERE {spec.table}.id = ?",
        values + (target,),
    )
    return UpsertResult(id=target, inserted=False)


def _now() -> str:
    return datetime.now().isoformat()


def upsert_success_pattern(
    conn: sqlite3.Connection,
    *,
    project_id: Optional[str],
    title: Optional[str],
    description: str,
    pattern_data: str,
    pattern_type: str = "approach",
    category: str = "",
    usage_count: int = 1,
    confidence_score: float = 0.0,
    source_dispatch_ids: Union[None, str, Iterable[str]] = None,
    source_receipts: Union[None, str, Iterable[str]] = None,
    first_seen: Optional[str] = None,
    last_used: Optional[str] = None,
    counter: str = "add",
    always_update: Sequence[str] = (),
    **extra: Any,
) -> UpsertResult:
    """Write one success pattern under its natural key. Raises ValueError on a
    NULL/empty project_id, pattern_type or title."""
    now = _now()
    row = {
        "project_id": project_id,
        "pattern_type": pattern_type,
        "title": title,
        "category": category,
        "description": description,
        "pattern_data": pattern_data,
        "usage_count": usage_count,
        "confidence_score": confidence_score,
        "source_dispatch_ids": _as_id_list(source_dispatch_ids),
        "first_seen": first_seen or now,
        "last_used": last_used or first_seen or now,
    }
    if source_receipts is not None:
        row["source_receipts"] = _as_id_list(source_receipts)
    row.update(extra)
    return _upsert(conn, SUCCESS, row, counter, always_update)


def upsert_antipattern(
    conn: sqlite3.Connection,
    *,
    project_id: Optional[str],
    title: Optional[str],
    description: str,
    pattern_data: str,
    why_problematic: str,
    pattern_type: str = "approach",
    category: str = "",
    severity: str = "medium",
    occurrence_count: int = 1,
    source_dispatch_ids: Union[None, str, Iterable[str]] = None,
    first_seen: Optional[str] = None,
    last_seen: Optional[str] = None,
    counter: str = "add",
    always_update: Sequence[str] = (),
    **extra: Any,
) -> UpsertResult:
    """Write one antipattern under its natural key. Raises ValueError on a
    NULL/empty project_id, pattern_type or title."""
    now = _now()
    row = {
        "project_id": project_id,
        "pattern_type": pattern_type,
        "title": title,
        "category": category,
        "description": description,
        "pattern_data": pattern_data,
        "why_problematic": why_problematic,
        "severity": severity,
        "occurrence_count": occurrence_count,
        "source_dispatch_ids": _as_id_list(source_dispatch_ids),
        "first_seen": first_seen or now,
        "last_seen": last_seen or first_seen or now,
    }
    row.update(extra)
    return _upsert(conn, ANTI, row, counter, always_update)
