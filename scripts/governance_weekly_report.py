#!/usr/bin/env python3
"""Weekly Governance Report Generator (Layer 3).

Generates a markdown report with FPY trends, model comparison,
role effectiveness, gate bottlenecks, and actionable items.

Usage:
    python3 governance_weekly_report.py [--weeks-back 4] [--output-dir docs/governance/]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env


def get_db(paths: Dict[str, str]) -> Path:
    return Path(paths["VNX_STATE_DIR"]) / "quality_intelligence.db"


def _week_bounds(ref_date: date) -> Tuple[date, date]:
    """Return Monday-Sunday bounds for the ISO week containing ref_date."""
    monday = ref_date - timedelta(days=ref_date.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _get_metric(conn: sqlite3.Connection, metric: str, scope_type: str, scope_value: str,
                start: date, end: date) -> Optional[float]:
    row = conn.execute(
        """SELECT metric_value FROM governance_metrics
           WHERE metric_name=? AND scope_type=? AND scope_value=?
           AND period_start >= ? AND period_end <= ?
           ORDER BY computed_at DESC LIMIT 1""",
        (metric, scope_type, scope_value, str(start), str(end)),
    ).fetchone()
    return row[0] if row else None


def _get_metric_trend(conn: sqlite3.Connection, metric: str, scope_type: str, scope_value: str,
                      weeks: int) -> List[Tuple[str, float]]:
    """Get weekly metric values for trend display."""
    results = []
    today = date.today()
    for w in range(weeks - 1, -1, -1):
        ref = today - timedelta(weeks=w)
        start, end = _week_bounds(ref)
        val = _get_metric(conn, metric, scope_type, scope_value, start, end)
        iso_week = start.isocalendar()
        label = f"W{iso_week[1]:02d}"
        if val is not None:
            results.append((label, val))
    return results


def generate_report(conn: sqlite3.Connection, weeks_back: int = 4) -> str:
    """Generate the weekly governance markdown report."""
    today = date.today()
    current_start, current_end = _week_bounds(today)
    iso = current_start.isocalendar()
    week_label = f"{iso[0]}-W{iso[1]:02d}"

    prev_start = current_start - timedelta(weeks=1)
    prev_end = prev_start + timedelta(days=6)

    lines = [
        f"# Week {week_label} Governance Report",
        f"\nGenerated: {today.isoformat()}",
        f"Period: {current_start} to {current_end}",
        "",
    ]

    # --- Section 1: System Health ---
    lines.append("## System Health\n")

    fpy = _get_metric(conn, "fpy", "system", "all", current_start, current_end)
    fpy_prev = _get_metric(conn, "fpy", "system", "all", prev_start, prev_end)
    rework = _get_metric(conn, "rework_rate", "system", "all", current_start, current_end)
    rework_prev = _get_metric(conn, "rework_rate", "system", "all", prev_start, prev_end)
    mean_cqs = _get_metric(conn, "mean_cqs", "system", "all", current_start, current_end)
    dispatch_count = _get_metric(conn, "dispatch_count", "system", "all", current_start, current_end)

    def _delta(current: Optional[float], previous: Optional[float]) -> str:
        if current is None or previous is None:
            return ""
        diff = current - previous
        arrow = "+" if diff >= 0 else ""
        return f" ({arrow}{diff:.1f} vs vorige week)"

    if fpy is not None:
        lines.append(f"- **FPY**: {fpy:.1f}%{_delta(fpy, fpy_prev)}")
    else:
        lines.append("- **FPY**: Geen data")
    if rework is not None:
        lines.append(f"- **Rework Rate**: {rework:.2f}{_delta(rework, rework_prev)}")
    else:
        lines.append("- **Rework Rate**: Geen data")
    if mean_cqs is not None:
        lines.append(f"- **Mean CQS**: {mean_cqs:.1f}")
    if dispatch_count is not None:
        lines.append(f"- **Dispatches**: {int(dispatch_count)}")

    # FPY trend
    fpy_trend = _get_metric_trend(conn, "fpy", "system", "all", weeks_back)
    if fpy_trend:
        lines.append(f"\n**FPY Trend** ({weeks_back} weken): " + " -> ".join(f"{l}: {v:.1f}%" for l, v in fpy_trend))

    # SPC Alerts
    alerts = conn.execute(
        """SELECT severity, COUNT(*) FROM spc_alerts
           WHERE date(detected_at) BETWEEN ? AND ? AND acknowledged_at IS NULL
           GROUP BY severity ORDER BY severity""",
        (str(current_start), str(current_end)),
    ).fetchall()
    if alerts:
        alert_summary = ", ".join(f"{count} {sev}" for sev, count in alerts)
        lines.append(f"\n**Active SPC Alerts**: {alert_summary}")

        critical = conn.execute(
            """SELECT description FROM spc_alerts
               WHERE severity='critical' AND date(detected_at) BETWEEN ? AND ?
               AND acknowledged_at IS NULL LIMIT 5""",
            (str(current_start), str(current_end)),
        ).fetchall()
        for row in critical:
            lines.append(f"  - CRITICAL: {row[0]}")
    else:
        lines.append("\n**Active SPC Alerts**: Geen")

    lines.append("")

    # --- Section 2: Model Comparison ---
    lines.append("## Model Comparison\n")

    models = conn.execute(
        """SELECT scope_value, metric_value, sample_size FROM governance_metrics
           WHERE metric_name='mean_cqs' AND scope_type='model'
           AND period_start >= ? AND period_end <= ?
           ORDER BY metric_value DESC""",
        (str(current_start), str(current_end)),
    ).fetchall()

    if models:
        lines.append("| Model | Tasks | Mean CQS | FPY |")
        lines.append("|-------|-------|----------|-----|")
        for model_name, cqs_val, n in models:
            model_fpy = _get_metric(conn, "fpy", "model", model_name, current_start, current_end)
            fpy_str = f"{model_fpy:.0f}%" if model_fpy is not None else "N/A"
            lines.append(f"| {model_name} | {n} | {cqs_val:.1f} | {fpy_str} |")
    else:
        lines.append("Geen model-specifieke data deze week.")

    lines.append("")

    # --- Section 3: Role Effectiveness ---
    lines.append("## Role Effectiveness\n")

    roles = conn.execute(
        """SELECT scope_value, metric_value, sample_size FROM governance_metrics
           WHERE metric_name='fpy' AND scope_type='role'
           AND period_start >= ? AND period_end <= ?
           ORDER BY metric_value DESC""",
        (str(current_start), str(current_end)),
    ).fetchall()

    if roles:
        lines.append("| Role | FPY | Rework | Dispatches |")
        lines.append("|------|-----|--------|------------|")
        for role_name, fpy_val, n in roles:
            role_rework = _get_metric(conn, "rework_rate", "role", role_name, current_start, current_end)
            rework_str = f"{role_rework:.2f}" if role_rework is not None else "N/A"
            lines.append(f"| {role_name} | {fpy_val:.0f}% | {rework_str} | {n} |")
    else:
        lines.append("Geen role-specifieke data deze week.")

    lines.append("")

    # --- Section 4: Gate Bottlenecks ---
    lines.append("## Gate Bottlenecks\n")

    gates = conn.execute(
        """SELECT scope_value, metric_value, sample_size FROM governance_metrics
           WHERE metric_name='gate_velocity_hours' AND scope_type='gate'
           AND period_start >= ? AND period_end <= ?
           ORDER BY metric_value DESC LIMIT 10""",
        (str(current_start), str(current_end)),
    ).fetchall()

    if gates:
        lines.append("| Gate | Velocity (hrs) | Rework Rate | Dispatches |")
        lines.append("|------|----------------|-------------|------------|")
        for gate_name, velocity, n in gates:
            gate_rework = _get_metric(conn, "rework_rate", "gate", gate_name, current_start, current_end)
            rework_str = f"{gate_rework:.2f}" if gate_rework is not None else "N/A"
            lines.append(f"| {gate_name} | {velocity:.1f} | {rework_str} | {n} |")
    else:
        lines.append("Geen gate-specifieke data deze week.")

    lines.append("")

    # --- Section 5: Actionable Items ---
    lines.append("## Top Actions\n")

    actions = _generate_actions(conn, current_start, current_end)
    if actions:
        for i, action in enumerate(actions[:5], 1):
            lines.append(f"{i}. {action}")
    else:
        lines.append("Geen actiepunten geidentificeerd.")

    lines.append("")
    return "\n".join(lines)


def _generate_actions(conn: sqlite3.Connection, start: date, end: date) -> List[str]:
    """Generate actionable items from metrics and alerts."""
    actions = []

    # High rework gates
    high_rework = conn.execute(
        """SELECT scope_value, metric_value FROM governance_metrics
           WHERE metric_name='rework_rate' AND scope_type='gate' AND metric_value > 1.5
           AND period_start >= ? AND period_end <= ?
           ORDER BY metric_value DESC LIMIT 3""",
        (str(start), str(end)),
    ).fetchall()
    for gate, rate in high_rework:
        actions.append(f"Gate `{gate}` rework rate {rate:.1f}x -> review prompt quality and acceptance criteria")

    # Critical SPC alerts
    critical = conn.execute(
        """SELECT DISTINCT metric_name, scope_value, description FROM spc_alerts
           WHERE severity='critical' AND date(detected_at) BETWEEN ? AND ?
           AND acknowledged_at IS NULL LIMIT 3""",
        (str(start), str(end)),
    ).fetchall()
    for metric, scope, desc in critical:
        actions.append(f"SPC critical: {desc}")

    # Low FPY roles
    low_fpy = conn.execute(
        """SELECT scope_value, metric_value FROM governance_metrics
           WHERE metric_name='fpy' AND scope_type='role' AND metric_value < 50
           AND period_start >= ? AND period_end <= ?
           ORDER BY metric_value ASC LIMIT 2""",
        (str(start), str(end)),
    ).fetchall()
    for role, fpy_val in low_fpy:
        actions.append(f"Role `{role}` FPY at {fpy_val:.0f}% -> investigate recent failures")

    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly governance report generator")
    parser.add_argument("--weeks-back", type=int, default=4, help="Weeks of trend data")
    parser.add_argument("--output-dir", type=str, default=None, help="Output directory for report")
    args = parser.parse_args()

    paths = ensure_env()
    db_path = get_db(paths)
    if not db_path.exists():
        print("No quality_intelligence.db found")
        return 1

    conn = sqlite3.connect(str(db_path))
    report = generate_report(conn, args.weeks_back)
    conn.close()

    # Determine output path
    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        vnx_home = Path(paths["VNX_HOME"])
        out_dir = vnx_home / "docs" / "governance"

    out_dir.mkdir(parents=True, exist_ok=True)

    today = date.today()
    iso = today.isocalendar()
    filename = f"week_{iso[0]}_W{iso[1]:02d}.md"
    out_path = out_dir / filename

    out_path.write_text(report, encoding="utf-8")
    print(f"Governance report written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
