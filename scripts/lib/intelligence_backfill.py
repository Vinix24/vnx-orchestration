#!/usr/bin/env python3
"""
intelligence_backfill.py — retroactive scope_tags population for quality_intelligence.db.

Updates success_patterns and antipatterns where category is NULL or empty,
based on keyword matching in title+description. Safe to re-run (idempotent
because it only touches rows with empty category).

Usage:
    python3 scripts/lib/intelligence_backfill.py [--db PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_SCRIPTS_LIB = Path(__file__).resolve().parent
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

try:
    from project_root import resolve_project_root
    _PROJECT_ROOT = resolve_project_root(__file__)
except (ImportError, RuntimeError):
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

logger = logging.getLogger(__name__)

# Keyword patterns → primary tag assigned to matching rows.
# Priority: first matching rule wins (top = highest priority).
_KEYWORD_RULES: List[Tuple[List[str], str]] = [
    (["sql", "schema", "migration", "table"], "sql"),
    (["async", "await", "asyncio"], "async"),
    (["security", "secret", "auth"], "security"),
    (["ui", "html", "css", "dashboard", "tsx"], "ui"),
    (["runtime", "dispatch", "receipt"], "runtime"),
    (["intelligence", "pattern"], "intelligence"),
]


def _infer_tag(title: str, description: str) -> Optional[str]:
    """Return the primary tag for a pattern row, or None if no keyword matches."""
    haystack = f"{title} {description}".lower()
    for keywords, primary_tag in _KEYWORD_RULES:
        if any(re.search(rf"\b{re.escape(kw)}\b", haystack) for kw in keywords):
            return primary_tag
    return None


def backfill_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    dry_run: bool = False,
) -> Tuple[int, int]:
    """Backfill category for rows with empty category.

    Returns (checked, updated) counts.
    Only updates rows where category IS NULL or category = ''.
    """
    try:
        rows = conn.execute(
            f"SELECT id, title, description FROM {table} "
            "WHERE category IS NULL OR category = ''",
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("backfill_table: query failed on %s: %s", table, exc)
        return 0, 0

    checked = len(rows)
    updated = 0

    for row in rows:
        row_id = row[0]
        title = row[1] or ""
        description = row[2] or ""
        tag = _infer_tag(title, description)
        if tag is None:
            continue
        if not dry_run:
            try:
                conn.execute(
                    f"UPDATE {table} SET category = ? WHERE id = ?",
                    (tag, row_id),
                )
            except sqlite3.Error as exc:
                logger.warning("backfill_table: update failed for %s id=%s: %s", table, row_id, exc)
                continue
        updated += 1

    if not dry_run and updated > 0:
        try:
            conn.commit()
        except sqlite3.Error as exc:
            logger.warning("backfill_table: commit failed on %s: %s", table, exc)

    return checked, updated


def run_backfill(db_path: Path, *, dry_run: bool = False) -> Dict[str, Dict[str, int]]:
    """Run the full backfill and return a per-table summary dict."""
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        raise RuntimeError(f"Failed to open DB {db_path}: {exc}") from exc

    results: Dict[str, Dict[str, int]] = {}
    try:
        for table in ("success_patterns", "antipatterns"):
            checked, updated = backfill_table(conn, table, dry_run=dry_run)
            results[table] = {"checked": checked, "updated": updated}
            logger.info(
                "backfill %s: checked=%d updated=%d dry_run=%s",
                table, checked, updated, dry_run,
            )
    finally:
        conn.close()

    return results


def _default_db_path() -> Optional[Path]:
    """Resolve default quality_intelligence.db via VNX_STATE_DIR or canonical vnx_paths."""
    state_dir_env = os.environ.get("VNX_STATE_DIR")
    if state_dir_env:
        candidate = Path(state_dir_env) / "quality_intelligence.db"
        if candidate.exists():
            return candidate
    # _PROJECT_ROOT / ".vnx-data" is repo-local; a central install's DB lives at
    # ~/.vnx-data/<project>/state instead. Try the canonical resolver first.
    try:
        from vnx_paths import resolve_paths
        candidate = Path(resolve_paths()["VNX_STATE_DIR"]) / "quality_intelligence.db"
        if candidate.exists():
            return candidate
    except Exception:
        pass
    candidate = _PROJECT_ROOT / ".vnx-data" / "state" / "quality_intelligence.db"
    if candidate.exists():
        return candidate
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill scope_tags (category) for intelligence patterns with empty category."
    )
    parser.add_argument("--db", type=Path, help="Path to quality_intelligence.db")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    db_path = args.db or _default_db_path()
    if db_path is None:
        logger.error("No quality_intelligence.db found. Pass --db <path>.")
        sys.exit(1)

    try:
        results = run_backfill(db_path, dry_run=args.dry_run)
    except (FileNotFoundError, RuntimeError) as exc:
        logger.error("%s", exc)
        sys.exit(1)

    total_checked = sum(v["checked"] for v in results.values())
    total_updated = sum(v["updated"] for v in results.values())
    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"{prefix}Backfill complete: {total_checked} rows checked, {total_updated} rows updated")
    for table, counts in results.items():
        print(f"  {table}: checked={counts['checked']} updated={counts['updated']}")


if __name__ == "__main__":
    main()
