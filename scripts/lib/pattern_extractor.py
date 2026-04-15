#!/usr/bin/env python3
"""pattern_extractor.py — Extract behavioral patterns from dispatch_behaviors.json
and persist to quality_intelligence.db.

Reads the JSON output of event_analyzer.py --all --output and produces:
  1. File-affinity success_patterns (files that co-occur within the same dispatch)
  2. Duration-baseline success_patterns per role
  3. Common-error prevention_rules

Usage:
    python3 scripts/lib/pattern_extractor.py --input .vnx-data/state/dispatch_behaviors.json
    python3 scripts/lib/pattern_extractor.py --input behaviors.json --db /path/to/db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _default_db_path() -> Path:
    state_dir = os.environ.get("VNX_STATE_DIR", "")
    if state_dir:
        return Path(state_dir) / "quality_intelligence.db"
    here = Path(__file__).resolve()
    return here.parent.parent.parent / ".vnx-data" / "state" / "quality_intelligence.db"


def _open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    # Ensure success_patterns table exists (minimal schema; quality_db_init.py owns the full one)
    con.execute("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT DEFAULT 'behavioral',
            category TEXT,
            title TEXT,
            description TEXT,
            pattern_data TEXT,
            code_example TEXT,
            prerequisites TEXT,
            outcomes TEXT,
            success_rate REAL DEFAULT 0.0,
            usage_count INTEGER DEFAULT 0,
            avg_completion_time REAL DEFAULT 0.0,
            confidence_score REAL DEFAULT 0.5,
            source_dispatch_ids TEXT,
            source_receipts TEXT,
            first_seen TEXT,
            last_used TEXT
        )
    """)

    # Ensure prevention_rules table exists
    con.execute("""
        CREATE TABLE IF NOT EXISTS prevention_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_combination TEXT,
            rule_type TEXT,
            description TEXT,
            recommendation TEXT,
            confidence REAL DEFAULT 0.5,
            created_at TEXT,
            triggered_count INTEGER DEFAULT 0,
            last_triggered TEXT
        )
    """)

    # Add source column to prevention_rules if missing
    existing_cols = {row[1] for row in con.execute("PRAGMA table_info(prevention_rules)").fetchall()}
    if "source" not in existing_cols:
        con.execute("ALTER TABLE prevention_rules ADD COLUMN source TEXT")

    con.commit()
    return con


# ---------------------------------------------------------------------------
# Pattern extraction logic
# ---------------------------------------------------------------------------

def _file_affinities(behaviors: list[dict]) -> list[dict]:
    """Compute file co-occurrence pairs across all dispatches.

    Returns list of dicts sorted by co_occurrence descending:
        {"files": ["A", "B"], "co_occurrence": 0.85, "count": 17}
    """
    pair_counts: Counter = Counter()
    total_dispatches = len(behaviors)

    for b in behaviors:
        files_read = b.get("files_read") or []
        files_written = b.get("files_written") or []
        all_files = list(dict.fromkeys(files_read + files_written))  # dedup, preserve order

        # Only consider Python and shell files (skip paths too generic)
        relevant = [
            f for f in all_files
            if f and (f.endswith(".py") or f.endswith(".sh"))
        ]

        for a, bfile in combinations(sorted(set(relevant)), 2):
            pair_counts[(a, bfile)] += 1

    if not total_dispatches:
        return []

    results = []
    for (a, bfile), count in pair_counts.most_common(50):
        co_occ = round(count / total_dispatches, 3)
        if co_occ >= 0.1 and count >= 2:  # at least 10% co-occurrence and 2 absolute counts
            results.append({
                "files": [a, bfile],
                "co_occurrence": co_occ,
                "count": count,
            })

    return results


def _duration_baselines(behaviors: list[dict]) -> list[dict]:
    """Compute average dispatch duration per role.

    Returns list of dicts:
        {"role": "backend-developer", "avg_seconds": 312, "count": 8, "min": 120, "max": 600}
    """
    by_role: dict[str, list[float]] = defaultdict(list)
    for b in behaviors:
        role = b.get("role") or "unknown"
        dur = float(b.get("duration_seconds") or 0.0)
        if dur > 0:
            by_role[role].append(dur)

    results = []
    for role, durations in sorted(by_role.items()):
        results.append({
            "role": role,
            "avg_seconds": round(sum(durations) / len(durations), 1),
            "count": len(durations),
            "min_seconds": round(min(durations), 1),
            "max_seconds": round(max(durations), 1),
        })
    return results


def _common_errors(behaviors: list[dict]) -> list[dict]:
    """Extract most common bash errors across all dispatches.

    Returns list of dicts:
        {"error": "...", "count": N, "recommendation": "..."}
    """
    error_counts: Counter = Counter()
    for b in behaviors:
        for err in b.get("bash_errors") or []:
            cleaned = err.strip()[:200]
            if cleaned:
                error_counts[cleaned] += 1

    results = []
    for error, count in error_counts.most_common(20):
        if count >= 2:
            results.append({
                "error": error,
                "count": count,
                "recommendation": f"This error occurred {count} times. Add defensive checks or import guards.",
            })
    return results


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------

def _upsert_affinity_patterns(con: sqlite3.Connection, affinities: list[dict],
                               dispatch_ids: list[str]) -> int:
    """Write file-affinity patterns to success_patterns. Returns inserted count."""
    now = datetime.now(timezone.utc).isoformat()
    source_ids = json.dumps(dispatch_ids[:20])
    inserted = 0

    # Clear old behavior_analysis affinity patterns before re-inserting
    con.execute(
        "DELETE FROM success_patterns WHERE category='behavior_analysis' AND title LIKE 'Files co-occur:%'"
    )

    for aff in affinities:
        files = aff["files"]
        title = f"Files co-occur: {files[0]} + {files[1]}"
        description = (
            f"These files appear in the same dispatch {aff['count']} times "
            f"(co-occurrence rate: {aff['co_occurrence']:.1%})."
        )
        pattern_data = json.dumps(aff)
        con.execute(
            """
            INSERT INTO success_patterns
                (pattern_type, category, title, description, pattern_data,
                 confidence_score, usage_count, source_dispatch_ids, first_seen, last_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "behavioral", "behavior_analysis", title, description, pattern_data,
                aff["co_occurrence"], aff["count"], source_ids, now, now,
            ),
        )
        inserted += 1

    con.commit()
    return inserted


def _upsert_duration_patterns(con: sqlite3.Connection, baselines: list[dict],
                               dispatch_ids: list[str]) -> int:
    """Write duration-baseline patterns to success_patterns. Returns inserted count."""
    now = datetime.now(timezone.utc).isoformat()
    source_ids = json.dumps(dispatch_ids[:20])
    inserted = 0

    con.execute(
        "DELETE FROM success_patterns WHERE category='behavior_analysis' AND title LIKE 'Expected duration:%'"
    )

    for baseline in baselines:
        role = baseline["role"]
        avg_min = round(baseline["avg_seconds"] / 60, 1)
        title = f"Expected duration: {role}"
        description = (
            f"Based on {baseline['count']} dispatches, {role} tasks average "
            f"{avg_min} minutes (range: {round(baseline['min_seconds']/60,1)}–"
            f"{round(baseline['max_seconds']/60,1)} min)."
        )
        pattern_data = json.dumps(baseline)
        con.execute(
            """
            INSERT INTO success_patterns
                (pattern_type, category, title, description, pattern_data,
                 confidence_score, usage_count, source_dispatch_ids, first_seen, last_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "behavioral", "behavior_analysis", title, description, pattern_data,
                0.8, baseline["count"], source_ids, now, now,
            ),
        )
        inserted += 1

    con.commit()
    return inserted


def _upsert_prevention_rules(con: sqlite3.Connection, errors: list[dict]) -> int:
    """Write common-error prevention rules. Returns inserted count."""
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0

    # Remove old behavior_analysis prevention rules
    con.execute("DELETE FROM prevention_rules WHERE source='behavior_analysis'")

    for err in errors:
        con.execute(
            """
            INSERT INTO prevention_rules
                (tag_combination, rule_type, description, recommendation,
                 confidence, created_at, triggered_count, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "bash_error", "behavior_analysis",
                err["error"][:500], err["recommendation"][:500],
                round(min(1.0, err["count"] / 10), 2), now, err["count"],
                "behavior_analysis",
            ),
        )
        inserted += 1

    con.commit()
    return inserted


# ---------------------------------------------------------------------------
# Main extraction entry point
# ---------------------------------------------------------------------------

def extract_patterns(input_path: Path, db_path: Path) -> dict:
    """Read behaviors JSON and persist patterns to DB.

    Returns summary dict with insertion counts.
    """
    if not input_path.exists():
        sys.stderr.write(f"[warn] behaviors file not found: {input_path}\n")
        return {"affinities_inserted": 0, "baselines_inserted": 0, "rules_inserted": 0}

    try:
        raw = json.loads(input_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(f"[error] failed to read {input_path}: {exc}\n")
        return {"affinities_inserted": 0, "baselines_inserted": 0, "rules_inserted": 0}

    # Support both list of behaviors and summary dict (--all vs --summary output)
    if isinstance(raw, dict):
        sys.stderr.write("[warn] input appears to be a summary dict, not a list of behaviors\n")
        return {"affinities_inserted": 0, "baselines_inserted": 0, "rules_inserted": 0}

    behaviors: list[dict] = raw if isinstance(raw, list) else []
    dispatch_ids = [b.get("dispatch_id", "") for b in behaviors if b.get("dispatch_id")]

    sys.stderr.write(f"[info] Extracting patterns from {len(behaviors)} dispatch behaviors\n")

    affinities = _file_affinities(behaviors)
    baselines = _duration_baselines(behaviors)
    errors = _common_errors(behaviors)

    sys.stderr.write(f"[info] Found: {len(affinities)} affinity pairs, "
                     f"{len(baselines)} role baselines, {len(errors)} common errors\n")

    con = _open_db(db_path)
    try:
        n_aff = _upsert_affinity_patterns(con, affinities, dispatch_ids)
        n_base = _upsert_duration_patterns(con, baselines, dispatch_ids)
        n_rules = _upsert_prevention_rules(con, errors)
    finally:
        con.close()

    result = {
        "affinities_inserted": n_aff,
        "baselines_inserted": n_base,
        "rules_inserted": n_rules,
        "behaviors_analyzed": len(behaviors),
    }
    sys.stderr.write(f"[ok] Patterns written: {result}\n")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract behavioral patterns from dispatch_behaviors.json and persist to DB."
    )
    parser.add_argument(
        "--input", metavar="PATH", required=True,
        help="Path to dispatch_behaviors.json (output of event_analyzer.py --all)",
    )
    parser.add_argument(
        "--db", metavar="PATH",
        help="Path to quality_intelligence.db (default: $VNX_STATE_DIR/quality_intelligence.db)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    db_path = Path(args.db) if args.db else _default_db_path()

    result = extract_patterns(input_path, db_path)
    print(json.dumps(result, indent=2))

    if result.get("behaviors_analyzed", 0) == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
