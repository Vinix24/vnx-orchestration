#!/usr/bin/env python3
"""intelligence_dashboard_data.py — Aggregated behavioral intelligence for the dashboard.

Provides get_behavioral_summary() — queries quality_intelligence.db for all
behavioral patterns written by pattern_extractor.py.

Usage (as library):
    from intelligence_dashboard_data import get_behavioral_summary
    data = get_behavioral_summary()
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def _db_path() -> Path | None:
    """Locate quality_intelligence.db, return None if absent."""
    state_dir = os.environ.get("VNX_STATE_DIR", "")
    if state_dir:
        p = Path(state_dir) / "quality_intelligence.db"
        if p.exists():
            return p
    # Fallback: repo-relative
    here = Path(__file__).resolve()
    candidate = here.parent.parent.parent / ".vnx-data" / "state" / "quality_intelligence.db"
    return candidate if candidate.exists() else None


def get_behavioral_summary() -> dict[str, Any]:
    """Return behavioral intelligence summary for the dashboard.

    Shape:
        {
            "rework_files": [{"file": "...", "rework_count": N}, ...],
            "common_errors": [{"error": "...", "count": N}, ...],
            "file_affinities": [{"files": ["A", "B"], "co_occurrence": 0.85}, ...],
            "duration_baselines": [{"role": "...", "avg_seconds": 312}, ...],
            "exploration_insight": "...",
            "total_dispatches_analyzed": N,
            "patterns_generated": N,
        }
    """
    empty: dict[str, Any] = {
        "rework_files": [],
        "common_errors": [],
        "file_affinities": [],
        "duration_baselines": [],
        "exploration_insight": "",
        "total_dispatches_analyzed": 0,
        "patterns_generated": 0,
    }

    db = _db_path()
    if not db:
        return empty

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except Exception:
        return empty

    try:
        result = dict(empty)

        # --- File affinities ---
        try:
            rows = con.execute(
                """
                SELECT pattern_data, confidence_score
                FROM success_patterns
                WHERE category='behavior_analysis' AND title LIKE 'Files%co-occur%'
                ORDER BY confidence_score DESC
                LIMIT 20
                """
            ).fetchall()
            for row in rows:
                try:
                    data = json.loads(row["pattern_data"] or "{}")
                    result["file_affinities"].append({
                        "files": data.get("files", []),
                        "co_occurrence": data.get("co_occurrence", 0.0),
                        "count": data.get("count", 0),
                    })
                except (json.JSONDecodeError, TypeError):
                    pass
        except sqlite3.OperationalError:
            pass

        # --- Duration baselines ---
        try:
            rows = con.execute(
                """
                SELECT pattern_data
                FROM success_patterns
                WHERE category='behavior_analysis' AND title LIKE 'Expected duration:%'
                ORDER BY usage_count DESC
                """
            ).fetchall()
            for row in rows:
                try:
                    data = json.loads(row["pattern_data"] or "{}")
                    result["duration_baselines"].append({
                        "role": data.get("role", ""),
                        "avg_seconds": data.get("avg_seconds", 0),
                        "count": data.get("count", 0),
                    })
                except (json.JSONDecodeError, TypeError):
                    pass
        except sqlite3.OperationalError:
            pass

        # --- Common errors (from prevention_rules) ---
        try:
            col_names = {
                row[1]
                for row in con.execute("PRAGMA table_info(prevention_rules)").fetchall()
            }
            if "source" in col_names:
                rows = con.execute(
                    """
                    SELECT description, triggered_count
                    FROM prevention_rules
                    WHERE source='behavior_analysis'
                    ORDER BY triggered_count DESC
                    LIMIT 20
                    """
                ).fetchall()
                for row in rows:
                    result["common_errors"].append({
                        "error": (row["description"] or "")[:200],
                        "count": int(row["triggered_count"] or 0),
                    })
        except sqlite3.OperationalError:
            pass

        # --- Rework files: files with highest edit cycle counts ---
        # Derived from top-written files in success_patterns source data
        # Use dispatch_quality_context if available, otherwise derive from affinities
        try:
            rows = con.execute(
                """
                SELECT pattern_data
                FROM success_patterns
                WHERE category='behavior_analysis' AND title LIKE 'Files%co-occur%'
                ORDER BY usage_count DESC
                LIMIT 50
                """
            ).fetchall()
            file_counts: dict[str, int] = {}
            for row in rows:
                try:
                    data = json.loads(row["pattern_data"] or "{}")
                    for f in data.get("files", []):
                        file_counts[f] = file_counts.get(f, 0) + data.get("count", 1)
                except (json.JSONDecodeError, TypeError):
                    pass
            result["rework_files"] = [
                {"file": f, "rework_count": c}
                for f, c in sorted(file_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            ]
        except sqlite3.OperationalError:
            pass

        # --- Total patterns generated ---
        try:
            row = con.execute(
                "SELECT COUNT(*) AS n FROM success_patterns WHERE category='behavior_analysis'"
            ).fetchone()
            result["patterns_generated"] = int(row["n"] or 0) if row else 0
        except sqlite3.OperationalError:
            pass

        # --- Total dispatches analyzed (from source_dispatch_ids in any pattern) ---
        try:
            row = con.execute(
                """
                SELECT source_dispatch_ids
                FROM success_patterns
                WHERE category='behavior_analysis'
                LIMIT 1
                """
            ).fetchone()
            if row and row["source_dispatch_ids"]:
                ids = json.loads(row["source_dispatch_ids"] or "[]")
                result["total_dispatches_analyzed"] = len(ids)
        except (sqlite3.OperationalError, json.JSONDecodeError):
            pass

        # --- Exploration insight ---
        # Check duration baselines for reads_before_write insight
        if result["duration_baselines"]:
            best = result["duration_baselines"][0]
            avg_min = round(best["avg_seconds"] / 60, 1) if best["avg_seconds"] else 0
            role = best.get("role", "workers")
            if avg_min:
                result["exploration_insight"] = (
                    f"{role} tasks average {avg_min} min based on {best['count']} dispatches"
                )

        return result

    finally:
        con.close()
