#!/usr/bin/env python3
"""F57 insights reader — surfaces Karpathy-style dispatch parameter insights for T0.

CLI:
    python3 -m f57_insights_reader [--days 7]
    python3 scripts/lib/f57_insights_reader.py --days 30

BILLING SAFETY: No Anthropic SDK. SQLite + stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from dispatch_parameter_tracker import (
    DispatchParameterTracker,
    _STATE_DIR,
    _connect,
)


def _fetch_rows_since(state_dir: Path, cutoff: datetime) -> list[dict]:
    """Return completed experiment rows with timestamp >= cutoff."""
    try:
        with _connect(state_dir) as conn:
            rows = conn.execute(
                """
                SELECT * FROM dispatch_experiments
                WHERE success IS NOT NULL
                  AND timestamp >= ?
                ORDER BY timestamp DESC
                """,
                (cutoff.isoformat(),),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _window_insights(rows: list[dict], days: int) -> list[str]:
    """Derive insight strings from a date-filtered experiment subset."""
    if len(rows) < 10:
        return []

    def _avg(subset: list[dict], key: str) -> tuple[float, int]:
        vals = [r[key] for r in subset if r.get(key) is not None]
        if not vals:
            return 0.0, 0
        return round(statistics.mean(vals), 2), len(vals)

    insights: list[str] = []

    large = [r for r in rows if (r.get("instruction_chars") or 0) > 2000]
    small = [r for r in rows if (r.get("instruction_chars") or 0) <= 2000]
    if len(large) >= 3 and len(small) >= 3:
        cqs_l, n_l = _avg(large, "cqs")
        cqs_s, n_s = _avg(small, "cqs")
        diff = cqs_l - cqs_s
        direction = "higher" if diff > 0 else "lower"
        insights.append(
            f"[{days}d] instruction_chars: >2000 (cqs={cqs_l:.1f}, n={n_l})"
            f" vs <=2000 (cqs={cqs_s:.1f}, n={n_s}) — {abs(diff):.1f} {direction}"
        )

    success_total = sum(1 for r in rows if r.get("success"))
    success_rate = round(success_total / len(rows) * 100, 1)
    insights.append(
        f"[{days}d] {len(rows)} experiments, success_rate={success_rate}%"
    )
    return insights


def read_insights(days: int = 7, state_dir: Optional[Path] = None) -> dict:
    """Return structured JSON-serialisable dict with F57 dispatch insights.

    Args:
        days: Look-back window in days for window_insights / window_experiment_count.
        state_dir: Override VNX state directory (used in tests).
    """
    sdir = state_dir or _STATE_DIR
    tracker = DispatchParameterTracker(state_dir=sdir)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    window_rows = _fetch_rows_since(sdir, cutoff)

    all_insights = tracker.top_insights_for_t0(n=5)
    stats = tracker.stats()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "all_time_insights": all_insights,
        "window_insights": _window_insights(window_rows, days),
        "window_experiment_count": len(window_rows),
        "stats": stats,
    }


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="F57 dispatch parameter insights reader")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        metavar="N",
        help="Look-back window in days (default: 7)",
    )
    args = parser.parse_args(argv)
    result = read_insights(days=args.days)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
