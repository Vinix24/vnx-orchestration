#!/usr/bin/env python3
"""Generate weekly A/B intelligence-injection lift report.

Compares success rates between 'treatment' (injection on) and 'control'
(injection skipped) arms, matched on role + task_class + week.

Usage:
    python3 scripts/intelligence_ab_report.py [--db <path>] [--days 30]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


def weekly_ab_lift(db_path: Path, days: int = 30) -> list[dict]:
    """Compute success-rate per ab_arm, matched on role + task_class + week.

    Returns list of dicts with keys: week, role, task_class, arm, success_rate, n.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                strftime('%Y-W%W', i.injected_at) AS week,
                COALESCE(d.role, 'unknown')        AS role,
                COALESCE(i.task_class, 'unknown')  AS task_class,
                COALESCE(i.ab_arm, 'treatment')    AS ab_arm,
                AVG(CASE WHEN d.outcome_status = 'success' THEN 1.0 ELSE 0.0 END) AS success_rate,
                COUNT(*) AS n
            FROM intelligence_injections i
            LEFT JOIN dispatch_metadata d USING (dispatch_id)
            WHERE i.injected_at >= datetime('now', ?)
            GROUP BY week, role, task_class, ab_arm
            HAVING n >= 5
            ORDER BY week DESC, role, task_class
            """,
            (f"-{days} days",),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"Query failed (ab_arm column may not exist — run migration first): {exc}") from exc
    finally:
        conn.close()

    return [dict(r) for r in rows]


def _compute_lift(rows: list[dict]) -> list[dict]:
    """Compute treatment - control lift per (week, role, task_class) cell."""
    cells: dict[tuple, dict] = {}
    for row in rows:
        key = (row["week"], row["role"], row["task_class"])
        cells.setdefault(key, {})
        cells[key][row["ab_arm"]] = {"success_rate": row["success_rate"], "n": row["n"]}

    results = []
    for (week, role, task_class), arms in sorted(cells.items(), reverse=True):
        treatment = arms.get("treatment")
        control = arms.get("control")
        lift = None
        if treatment and control:
            lift = round(treatment["success_rate"] - control["success_rate"], 4)
        results.append({
            "week": week,
            "role": role,
            "task_class": task_class,
            "treatment_rate": treatment["success_rate"] if treatment else None,
            "treatment_n": treatment["n"] if treatment else 0,
            "control_rate": control["success_rate"] if control else None,
            "control_n": control["n"] if control else 0,
            "lift": lift,
        })
    return results


def _render_markdown(results: list[dict]) -> str:
    if not results:
        return "No matched pairs with n >= 5 in the requested window.\n"

    lines = [
        "# A/B Intelligence Injection Lift Report",
        "",
        "| Week | Role | Task Class | Treatment rate | n | Control rate | n | Lift |",
        "|------|------|------------|---------------|---|-------------|---|------|",
    ]
    for r in results:
        t_rate = f"{r['treatment_rate']:.2%}" if r["treatment_rate"] is not None else "-"
        c_rate = f"{r['control_rate']:.2%}" if r["control_rate"] is not None else "-"
        lift = f"{r['lift']:+.1%}" if r["lift"] is not None else "-"
        lines.append(
            f"| {r['week']} | {r['role']} | {r['task_class']} "
            f"| {t_rate} | {r['treatment_n']} "
            f"| {c_rate} | {r['control_n']} "
            f"| {lift} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly A/B intelligence injection lift report")
    parser.add_argument("--db", required=True, help="Path to quality_intelligence.db or runtime_coordination.db containing intelligence_injections")
    parser.add_argument("--days", type=int, default=30, help="Look-back window in days (default: 30)")
    args = parser.parse_args()

    db_path = Path(args.db)
    raw_rows = weekly_ab_lift(db_path, days=args.days)
    results = _compute_lift(raw_rows)
    print(_render_markdown(results))


if __name__ == "__main__":
    main()
