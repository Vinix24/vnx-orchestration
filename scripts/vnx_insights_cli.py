#!/usr/bin/env python3
"""
VNX Insights CLI — Read-only view of F57 dispatch parameter + behavioral intelligence signals.

Surfaces Karpathy-style dispatch performance insights and per-role/terminal context
load signals. No apply path — informational only.

Usage:
  vnx insights              Show all available insights
  vnx insights --json       Machine-readable JSON output
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

PATHS = ensure_env()
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
DB_PATH = STATE_DIR / "quality_intelligence.db"


class Colors:
    BOLD = "\033[1m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    RESET = "\033[0m"


def _get_parameter_insights() -> list[str]:
    """Return top dispatch parameter insights via DispatchParameterTracker."""
    try:
        from dispatch_parameter_tracker import DispatchParameterTracker
        tracker = DispatchParameterTracker(STATE_DIR)
        stats = tracker.stats()
        if not stats.get("insights_available"):
            completed = stats.get("completed", 0)
            return [f"Insufficient data: {completed} completed experiments (need 20 for analysis)"]
        return tracker.top_insights_for_t0(n=7)
    except ImportError as exc:
        return [f"dispatch_parameter_tracker unavailable: {exc}"]
    except Exception as exc:
        return [f"Parameter analysis error: {exc}"]


def _get_context_load_signals() -> list[str]:
    """Surface per-role/terminal context load signals from dispatch_experiments."""
    if not DB_PATH.exists():
        return []

    signals: list[str] = []
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT AVG(context_items) FROM dispatch_experiments WHERE context_items IS NOT NULL"
            ).fetchone()
            overall_avg = float(row[0]) if row and row[0] is not None else None

            if overall_avg is None or overall_avg == 0:
                return []

            rows = con.execute(
                """
                SELECT terminal, role, AVG(context_items) AS avg_ctx, COUNT(*) AS n
                FROM dispatch_experiments
                WHERE context_items IS NOT NULL
                  AND terminal IS NOT NULL
                  AND role IS NOT NULL
                GROUP BY terminal, role
                HAVING n >= 3
                ORDER BY avg_ctx DESC
                """
            ).fetchall()

            for row in rows:
                terminal = row["terminal"]
                role = row["role"]
                avg_ctx = float(row["avg_ctx"])
                pct_diff = (avg_ctx - overall_avg) / overall_avg * 100
                if abs(pct_diff) >= 20:
                    direction = "extra" if pct_diff > 0 else "less"
                    signals.append(
                        f"{terminal} worker has {abs(pct_diff):.0f}% {direction} context "
                        f"on {role} role (avg {avg_ctx:.1f} items, n={row['n']})"
                    )
        finally:
            con.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        pass

    return signals


def _get_behavioral_signals() -> dict:
    """Return behavioral summary from intelligence_dashboard_data."""
    try:
        from intelligence_dashboard_data import get_behavioral_summary
        return get_behavioral_summary()
    except ImportError:
        return {}
    except Exception:
        return {}


def _collect_all_insights() -> dict:
    """Collect all signal types and return structured dict."""
    param_insights = _get_parameter_insights()
    context_signals = _get_context_load_signals()
    behavioral = _get_behavioral_signals()

    return {
        "parameter_insights": param_insights,
        "context_load_signals": context_signals,
        "behavioral": {
            "rework_files": behavioral.get("rework_files", [])[:5],
            "common_errors": behavioral.get("common_errors", [])[:5],
            "duration_baselines": behavioral.get("duration_baselines", []),
            "exploration_insight": behavioral.get("exploration_insight", ""),
            "total_dispatches_analyzed": behavioral.get("total_dispatches_analyzed", 0),
        },
    }


def print_insights(data: dict) -> None:
    param_insights = data["parameter_insights"]
    context_signals = data["context_load_signals"]
    behavioral = data["behavioral"]

    print(f"\n{Colors.BOLD}VNX Insights — last 7d signals{Colors.RESET}\n")

    print(f"{Colors.CYAN}── Dispatch Parameter Insights{Colors.RESET}")
    if param_insights:
        for insight in param_insights:
            print(f"  • {insight}")
    else:
        print("  (no insights available)")

    print()
    print(f"{Colors.BLUE}── Context Load Signals{Colors.RESET}")
    if context_signals:
        for signal in context_signals:
            print(f"  • {signal}")
    else:
        print("  (no significant context load differences detected)")

    print()
    print(f"{Colors.YELLOW}── Behavioral Patterns{Colors.RESET}")

    rework = behavioral.get("rework_files", [])
    if rework:
        print("  High-rework files:")
        for f in rework[:3]:
            print(f"    {f.get('file', '?')} ({f.get('rework_count', 0)} reworks)")

    errors = behavioral.get("common_errors", [])
    if errors:
        print("  Recurring errors:")
        for e in errors[:3]:
            print(f"    {e.get('error', '?')} ({e.get('count', 0)}x)")

    baselines = behavioral.get("duration_baselines", [])
    if baselines:
        print("  Role duration baselines:")
        for b in baselines[:4]:
            mins = int(b.get("avg_seconds", 0) // 60)
            print(f"    {b.get('role', '?')}: ~{mins}m avg")

    exploration = behavioral.get("exploration_insight", "")
    if exploration:
        print(f"\n  {Colors.GREEN}Exploration:{Colors.RESET} {exploration}")

    analyzed = behavioral.get("total_dispatches_analyzed", 0)
    print(f"\n  Based on {analyzed} analyzed dispatches.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="VNX Insights — read-only dispatch parameter and behavioral intelligence"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    data = _collect_all_insights()

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print_insights(data)

    return 0


if __name__ == "__main__":
    sys.exit(main())
