#!/usr/bin/env python3
"""Pattern dedup utility — collapses byte-identical success_patterns.

Background (claudedocs/2026-04-30-intelligence-system-audit.md):
  54 of 55 successful injections returned byte-identical content
  ("gate gate_pr0_input_ready_contract passed"). Many duplicate
  ``success_patterns`` rows were created over time, each fragmenting the
  pattern_usage learning signal. This utility collapses duplicates by SHA-256
  of normalized content (lowercased, whitespace-stripped) and merges the
  related learning state onto the surviving canonical row (the oldest by id).

The companion migration (schemas/migrations/0011_add_pattern_category.sql)
adds the ``pattern_category`` column used by the selector to enforce
diversity. ``ensure_pattern_category_columns`` applies that migration
idempotently from Python so test fixtures and the production CLI converge.

CLI:
  python3 scripts/lib/pattern_dedup.py --db <path> --dry-run
  python3 scripts/lib/pattern_dedup.py --db <path> --apply

Both flags are mutually exclusive; one is required. The ``--apply`` form
mutates the database and must therefore be run intentionally by an operator.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def normalize_content(text: Optional[str]) -> str:
    """Lowercase + collapse whitespace so trivial reformatting doesn't hide dups."""
    if not text:
        return ""
    return " ".join(text.lower().split())


def content_hash(*parts: Optional[str]) -> str:
    """SHA-256 over normalized concatenation of the supplied content parts."""
    joined = "\n".join(normalize_content(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def ensure_pattern_category_columns(conn: sqlite3.Connection) -> None:
    """Apply migration 0011 idempotently.

    Adds ``pattern_category`` to ``success_patterns`` and ``antipatterns``
    when missing, plus the supporting indexes. Backfill is left to the
    operator-run migration SQL; this Python helper exists so test fixtures
    and the dedup CLI can run against a fresh DB without manual ALTER.
    """
    if not _column_exists(conn, "success_patterns", "pattern_category"):
        conn.execute(
            "ALTER TABLE success_patterns "
            "ADD COLUMN pattern_category TEXT NOT NULL DEFAULT 'code'"
        )
    if _table_exists(conn, "antipatterns") and not _column_exists(
        conn, "antipatterns", "pattern_category"
    ):
        conn.execute(
            "ALTER TABLE antipatterns "
            "ADD COLUMN pattern_category TEXT NOT NULL DEFAULT 'antipattern_evidence'"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_success_patterns_pattern_category "
        "ON success_patterns (pattern_category, confidence_score DESC)"
    )
    if _table_exists(conn, "antipatterns"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_antipatterns_pattern_category "
            "ON antipatterns (pattern_category, occurrence_count DESC)"
        )
    conn.commit()


# 16-char prefix of SHA-256 — enough entropy to avoid collisions across the
# population of success_patterns rows we expect to ever see (millions before
# collision risk reaches 1%) while keeping pattern_id readable.
CONTENT_HASH_PREFIX_LEN = 16


def _short_content_hash(*parts: Optional[str]) -> str:
    """Stable 16-char prefix of the normalized SHA-256 used by the selector.

    Selector ``IntelligenceItem.content_hash`` is the full SHA-256 hex string,
    but the on-row column is the 16-char prefix so the canonical-id lookup
    can use a short index without sacrificing collision resistance.
    """
    return content_hash(*parts)[:CONTENT_HASH_PREFIX_LEN]


def ensure_content_hash_column(conn: sqlite3.Connection) -> None:
    """Apply migration 0012 idempotently.

    Adds ``content_hash`` (TEXT) to ``success_patterns`` plus the supporting
    composite index on (content_hash, project_id). Backfill of existing rows
    is delegated to :func:`backfill_content_hash` so a fresh DB acquires the
    column without paying for hashing work it doesn't need yet.

    SQLite has no SHA-256 builtin, hence the column is added empty here and
    populated by the Python helper. The index is non-unique because legacy
    duplicates may share a hash until ``dedup_success_patterns --apply``
    collapses them; ``pattern_dedup`` enforces application-level uniqueness
    after the collapse.
    """
    if not _column_exists(conn, "success_patterns", "content_hash"):
        conn.execute(
            "ALTER TABLE success_patterns ADD COLUMN content_hash TEXT"
        )
    # project_id arrived in migration 0010; older snapshots may pre-date it,
    # so fall back to a single-column index when the project_id column is
    # missing rather than failing the whole migration.
    if _column_exists(conn, "success_patterns", "project_id"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_success_patterns_content_hash "
            "ON success_patterns (content_hash, project_id)"
        )
    else:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_success_patterns_content_hash "
            "ON success_patterns (content_hash)"
        )
    conn.commit()


def backfill_content_hash(conn: sqlite3.Connection) -> int:
    """Populate ``content_hash`` for rows where it is NULL.

    Returns the count of rows updated. Idempotent — rows whose hash is
    already populated are skipped, so re-running is a no-op.
    """
    ensure_content_hash_column(conn)
    rows = conn.execute(
        "SELECT id, title, description FROM success_patterns "
        "WHERE content_hash IS NULL OR content_hash = ''"
    ).fetchall()
    updated = 0
    for row in rows:
        h = _short_content_hash(row[1], row[2])
        conn.execute(
            "UPDATE success_patterns SET content_hash = ? WHERE id = ?",
            (h, row[0]),
        )
        updated += 1
    if updated:
        conn.commit()
    return updated


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def classify_pattern(title: Optional[str], description: Optional[str]) -> str:
    """Assign a pattern_category based on title/description heuristics.

    Order matters: governance gate-pass shape wins over generic process keywords.
    """
    haystack = f"{normalize_content(title)} :: {normalize_content(description)}"
    if "gate " in haystack and "passed" in haystack:
        return "governance"
    if any(token in haystack for token in (
        "receipt processor",
        "dispatch lifecycle",
        "lease release",
    )):
        return "process"
    return "code"


def backfill_pattern_category(conn: sqlite3.Connection) -> Dict[str, int]:
    """Re-classify rows where pattern_category is still the default."""
    counts: Dict[str, int] = {"governance": 0, "process": 0, "code": 0}
    rows = conn.execute(
        "SELECT id, title, description, pattern_category FROM success_patterns"
    ).fetchall()
    for row in rows:
        new_cat = classify_pattern(row[1], row[2])
        if new_cat != row[3]:
            conn.execute(
                "UPDATE success_patterns SET pattern_category = ? WHERE id = ?",
                (new_cat, row[0]),
            )
        counts[new_cat] = counts.get(new_cat, 0) + 1
    conn.commit()
    return counts


# ---------------------------------------------------------------------------
# Dedup core
# ---------------------------------------------------------------------------

def _merge_source_dispatch_ids(values: List[Optional[str]]) -> str:
    """Union JSON-encoded source_dispatch_ids lists, preserving recency cap."""
    merged: List[str] = []
    seen = set()
    for raw in values:
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, list):
            continue
        for entry in parsed:
            key = str(entry)
            if key in seen:
                continue
            seen.add(key)
            merged.append(key)
    return json.dumps(merged[-50:])


def _group_duplicates(
    conn: sqlite3.Connection,
) -> Dict[Tuple[str, str], List[sqlite3.Row]]:
    """Group success_patterns rows for dedup.

    Phase 1.5 PR-2 / OI-1315 / OI-1321: dedup keys are
    ``(project_id, content_hash)`` — NOT ``content_hash`` alone — so that
    two tenants with identical pattern text never collapse into a single
    canonical row. When ``project_id`` does not exist on the table (pre-
    migration-0010 DBs), the grouping degrades to ``("", content_hash)`` so
    legacy single-tenant deployments keep working.
    """
    has_project_id = _column_exists(conn, "success_patterns", "project_id")
    if has_project_id:
        rows = conn.execute(
            """
            SELECT id, title, description, usage_count, source_dispatch_ids,
                   first_seen, last_used, confidence_score, project_id
            FROM   success_patterns
            ORDER  BY id ASC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, title, description, usage_count, source_dispatch_ids,
                   first_seen, last_used, confidence_score
            FROM   success_patterns
            ORDER  BY id ASC
            """
        ).fetchall()
    groups: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
    for row in rows:
        project_id = row["project_id"] if has_project_id else ""
        key = (str(project_id or ""), content_hash(row["title"], row["description"]))
        groups.setdefault(key, []).append(row)
    return groups


def dedup_success_patterns(
    db_path: Path,
    *,
    apply: bool = False,
) -> Dict[str, int]:
    """Collapse duplicates by SHA-256 of normalized content.

    For each group with >1 member:
      * keep the oldest (smallest ``id``) as the canonical row
      * sum ``usage_count`` and merge ``source_dispatch_ids``
      * rewrite ``pattern_usage.pattern_id`` rows pointing at duplicate
        ``intel_sp_<id>`` keys onto the canonical id, summing counters
      * rewrite ``dispatch_pattern_offered`` to the canonical id when the
        junction table exists
      * delete the duplicate rows from ``success_patterns``

    Returns:
      Mapping of dedup_key (content hash, 12-char prefix) -> count_collapsed.
      A count of 0 means the group was already singleton; counts are only
      reported for groups that had duplicates so callers can iterate over
      meaningful work.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ensure_pattern_category_columns(conn)
        ensure_content_hash_column(conn)
        backfill_content_hash(conn)
        groups = _group_duplicates(conn)
        report: Dict[str, int] = {}
        for group_key, members in groups.items():
            if len(members) <= 1:
                continue
            canonical = members[0]
            duplicates = members[1:]
            project_id_part, hash_part = group_key
            short_hash = hash_part[:12]
            short_key = (
                f"{project_id_part}:{short_hash}" if project_id_part else short_hash
            )
            report[short_key] = len(duplicates)

            if not apply:
                continue

            merged_usage = sum(int(m["usage_count"] or 0) for m in members)
            merged_sources = _merge_source_dispatch_ids(
                [m["source_dispatch_ids"] for m in members]
            )
            best_confidence = max(
                float(m["confidence_score"] or 0.0) for m in members
            )
            latest_last_used = max(
                (m["last_used"] for m in members if m["last_used"]),
                default=canonical["last_used"],
            )
            conn.execute(
                """
                UPDATE success_patterns
                SET    usage_count          = ?,
                       source_dispatch_ids  = ?,
                       confidence_score     = ?,
                       last_used            = ?
                WHERE  id = ?
                """,
                (
                    merged_usage,
                    merged_sources,
                    best_confidence,
                    latest_last_used,
                    canonical["id"],
                ),
            )

            canonical_pid = f"intel_sp_{canonical['id']}"
            for dup in duplicates:
                dup_pid = f"intel_sp_{dup['id']}"
                _merge_pattern_usage(conn, dup_pid, canonical_pid)
                _redirect_dispatch_pattern_offered(conn, dup_pid, canonical_pid)

            placeholders = ",".join("?" * len(duplicates))
            conn.execute(
                f"DELETE FROM success_patterns WHERE id IN ({placeholders})",
                tuple(d["id"] for d in duplicates),
            )

        if apply:
            conn.commit()
        return report
    finally:
        conn.close()


def _merge_pattern_usage(
    conn: sqlite3.Connection,
    duplicate_pattern_id: str,
    canonical_pattern_id: str,
) -> None:
    """Fold counters from duplicate row into canonical, then drop duplicate."""
    if not _table_exists(conn, "pattern_usage"):
        return
    dup = conn.execute(
        "SELECT used_count, ignored_count, success_count, failure_count, "
        "       confidence, last_used, last_offered "
        "FROM   pattern_usage WHERE pattern_id = ?",
        (duplicate_pattern_id,),
    ).fetchone()
    if dup is None:
        return
    canonical = conn.execute(
        "SELECT used_count, ignored_count, success_count, failure_count, "
        "       confidence, last_used, last_offered "
        "FROM   pattern_usage WHERE pattern_id = ?",
        (canonical_pattern_id,),
    ).fetchone()
    if canonical is None:
        conn.execute(
            "UPDATE pattern_usage SET pattern_id = ? WHERE pattern_id = ?",
            (canonical_pattern_id, duplicate_pattern_id),
        )
        return
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
        WHERE  pattern_id = ?
        """,
        (
            int(dup["used_count"] or 0),
            int(dup["ignored_count"] or 0),
            int(dup["success_count"] or 0),
            int(dup["failure_count"] or 0),
            float(dup["confidence"] or 0.0),
            dup["last_used"],
            dup["last_used"],
            dup["last_offered"],
            dup["last_offered"],
            canonical_pattern_id,
        ),
    )
    conn.execute(
        "DELETE FROM pattern_usage WHERE pattern_id = ?",
        (duplicate_pattern_id,),
    )


def _redirect_dispatch_pattern_offered(
    conn: sqlite3.Connection,
    duplicate_pattern_id: str,
    canonical_pattern_id: str,
) -> None:
    if not _table_exists(conn, "dispatch_pattern_offered"):
        return
    # Phase 1.5 PR-2 fix-forward: carry the source row's project_id through
    # the redirect insert. Without this, the re-inserted canonical row falls
    # back to the table default ('vnx-dev' from migration 0010) and a tenant-
    # scoped offering can be silently rebound to the wrong project.
    if _column_exists(conn, "dispatch_pattern_offered", "project_id"):
        conn.execute(
            """
            INSERT OR IGNORE INTO dispatch_pattern_offered
                (dispatch_id, pattern_id, pattern_title, offered_at, project_id)
            SELECT dispatch_id, ?, pattern_title, offered_at, project_id
            FROM   dispatch_pattern_offered
            WHERE  pattern_id = ?
            """,
            (canonical_pattern_id, duplicate_pattern_id),
        )
    else:
        conn.execute(
            """
            INSERT OR IGNORE INTO dispatch_pattern_offered
                (dispatch_id, pattern_id, pattern_title, offered_at)
            SELECT dispatch_id, ?, pattern_title, offered_at
            FROM   dispatch_pattern_offered
            WHERE  pattern_id = ?
            """,
            (canonical_pattern_id, duplicate_pattern_id),
        )
    conn.execute(
        "DELETE FROM dispatch_pattern_offered WHERE pattern_id = ?",
        (duplicate_pattern_id,),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pattern_dedup",
        description="Dedup success_patterns by content hash.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to quality_intelligence.db (default: ${VNX_STATE_DIR}/quality_intelligence.db via vnx_paths)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report duplicates without mutating the database.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply dedup (mutates the database).",
    )
    parser.add_argument(
        "--backfill-category",
        action="store_true",
        help="Re-run pattern_category classification on every row.",
    )
    parser.add_argument(
        "--backfill-content-hash",
        action="store_true",
        help="Populate the content_hash column for rows missing it (idempotent).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if args.db is None:
        from project_root import resolve_state_dir
        args.db = resolve_state_dir() / "quality_intelligence.db"

    if not args.db.exists():
        print(f"[pattern_dedup] DB not found: {args.db}", file=sys.stderr)
        return 2

    if args.backfill_category:
        with sqlite3.connect(str(args.db)) as conn:
            ensure_pattern_category_columns(conn)
            counts = backfill_pattern_category(conn)
        print(f"[pattern_dedup] backfill counts: {counts}")

    if args.backfill_content_hash:
        with sqlite3.connect(str(args.db)) as conn:
            updated = backfill_content_hash(conn)
        print(f"[pattern_dedup] content_hash backfill: {updated} rows updated")

    report = dedup_success_patterns(args.db, apply=args.apply)
    if not report:
        print("[pattern_dedup] no duplicate groups found")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    total = sum(report.values())
    print(f"[pattern_dedup] {mode}: {len(report)} duplicate groups, "
          f"{total} rows would-be-collapsed")
    for key, count in sorted(report.items(), key=lambda kv: -kv[1]):
        print(f"  {key}  collapses {count} duplicates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
