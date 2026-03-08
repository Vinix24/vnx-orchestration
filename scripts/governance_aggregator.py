#!/usr/bin/env python3
"""Nightly Governance Aggregator (Layer 2).

Computes FPY, rework rate, gate velocity, mean CQS per scope,
updates SPC control limits, and detects anomalies via Western Electric rules.

Usage:
    python3 governance_aggregator.py [--dry-run] [--backfill] [--baseline-days 30]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env
from cqs_calculator import calculate_cqs


def get_db(paths: Dict[str, str]) -> Path:
    return Path(paths["VNX_STATE_DIR"]) / "quality_intelligence.db"


def ensure_governance_schema(conn: sqlite3.Connection) -> None:
    """Create governance tables if they don't exist (idempotent)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS governance_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            scope_type TEXT NOT NULL,
            scope_value TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            metric_value REAL NOT NULL,
            sample_size INTEGER NOT NULL,
            computed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_gov_metrics_lookup
            ON governance_metrics (period_start, scope_type, metric_name);

        CREATE TABLE IF NOT EXISTS spc_control_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_name TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            scope_value TEXT NOT NULL,
            center_line REAL NOT NULL,
            ucl REAL NOT NULL,
            lcl REAL NOT NULL,
            sigma REAL NOT NULL,
            sample_count INTEGER NOT NULL,
            baseline_start DATE,
            baseline_end DATE,
            computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(metric_name, scope_type, scope_value)
        );

        CREATE TABLE IF NOT EXISTS spc_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            scope_value TEXT NOT NULL,
            observed_value REAL NOT NULL,
            control_limit REAL,
            description TEXT,
            severity TEXT DEFAULT 'warning',
            detected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            acknowledged_at DATETIME
        );
        CREATE INDEX IF NOT EXISTS idx_spc_alerts_lookup
            ON spc_alerts (detected_at DESC, severity);
    """)


def ensure_cqs_columns(conn: sqlite3.Connection) -> None:
    """Add CQS columns to dispatch_metadata if missing."""
    cursor = conn.execute("PRAGMA table_info(dispatch_metadata)")
    cols = {row[1] for row in cursor.fetchall()}
    if "cqs" not in cols:
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN cqs REAL")
    if "normalized_status" not in cols:
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN normalized_status TEXT")
    if "cqs_components" not in cols:
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN cqs_components TEXT")
    if "target_open_items" not in cols:
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN target_open_items TEXT")
    if "open_items_created" not in cols:
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN open_items_created INTEGER DEFAULT 0")
    if "open_items_resolved" not in cols:
        conn.execute("ALTER TABLE dispatch_metadata ADD COLUMN open_items_resolved INTEGER DEFAULT 0")
    conn.commit()


# ---------------------------------------------------------------------------
# Backfill: compute CQS for all dispatches with outcome but no CQS
# ---------------------------------------------------------------------------


def backfill_cqs(conn: sqlite3.Connection, db_path: Path, dry_run: bool = False) -> int:
    """Retroactively compute CQS for dispatches missing it."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM dispatch_metadata WHERE outcome_status IS NOT NULL AND cqs IS NULL"
    ).fetchall()

    count = 0
    for row in rows:
        receipt = {
            "status": row["outcome_status"],
            "report_path": row["outcome_report_path"],
            "role": row["role"],
            "gate": row["gate"],
            "pr_id": row["pr_id"],
        }
        session = None
        sa = conn.execute(
            "SELECT * FROM session_analytics WHERE dispatch_id = ?", (row["dispatch_id"],)
        ).fetchone()
        if sa:
            session = dict(sa)

        result = calculate_cqs(receipt, session, db_path, row["dispatch_id"])

        if not dry_run:
            conn.execute(
                "UPDATE dispatch_metadata SET cqs=?, normalized_status=?, cqs_components=? WHERE dispatch_id=?",
                (result["cqs"], result["normalized_status"], json.dumps(result["components"]), row["dispatch_id"]),
            )
        count += 1
        if dry_run:
            print(f"  [dry-run] {row['dispatch_id']}: CQS={result['cqs']} status={result['normalized_status']}")

    if not dry_run:
        conn.commit()
    return count


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------


def compute_metrics(conn: sqlite3.Connection, period_start: date, period_end: date) -> List[Dict[str, Any]]:
    """Compute all governance metrics for a period."""
    metrics: List[Dict[str, Any]] = []

    scopes = _get_scopes(conn, period_start, period_end)

    for scope_type, scope_value in scopes:
        where, params = _scope_filter(scope_type, scope_value, period_start, period_end)

        # Mean CQS
        row = conn.execute(
            f"SELECT AVG(cqs), COUNT(*) FROM dispatch_metadata WHERE cqs IS NOT NULL AND {where}", params
        ).fetchone()
        if row and row[1] > 0:
            metrics.append(_metric(period_start, period_end, scope_type, scope_value, "mean_cqs", row[0], row[1]))

        # Dispatch count
        total = conn.execute(
            f"SELECT COUNT(*) FROM dispatch_metadata WHERE {where}", params
        ).fetchone()[0]
        if total > 0:
            metrics.append(_metric(period_start, period_end, scope_type, scope_value, "dispatch_count", total, total))

        # FPY (First-Pass Yield)
        fpy_data = _compute_fpy(conn, where, params)
        if fpy_data:
            metrics.append(_metric(period_start, period_end, scope_type, scope_value, "fpy", fpy_data[0], fpy_data[1]))

        # Rework rate
        rework = _compute_rework_rate(conn, where, params)
        if rework:
            metrics.append(_metric(period_start, period_end, scope_type, scope_value, "rework_rate", rework[0], rework[1]))

        # OI resolution rate
        oi_row = conn.execute(
            f"""SELECT SUM(COALESCE(open_items_resolved, 0)),
                       SUM(COALESCE(open_items_created, 0)) + SUM(COALESCE(open_items_resolved, 0))
                FROM dispatch_metadata WHERE {where}""",
            params,
        ).fetchone()
        if oi_row and oi_row[1] and oi_row[1] > 0:
            oi_rate = oi_row[0] / oi_row[1] * 100
            metrics.append(_metric(period_start, period_end, scope_type, scope_value, "oi_resolution_rate", oi_rate, int(oi_row[1])))

        # Gate velocity (only for gate scope)
        if scope_type == "gate":
            velocity = _compute_gate_velocity(conn, scope_value, period_start, period_end)
            if velocity:
                metrics.append(_metric(period_start, period_end, scope_type, scope_value, "gate_velocity_hours", velocity[0], velocity[1]))

    return metrics


def _get_scopes(conn: sqlite3.Connection, start: date, end: date) -> List[Tuple[str, str]]:
    """Get all scope (type, value) pairs for the period."""
    scopes = [("system", "all")]
    start_s, end_s = str(start), str(end)

    for col, stype in [("terminal", "terminal"), ("role", "role"), ("gate", "gate")]:
        rows = conn.execute(
            f"SELECT DISTINCT {col} FROM dispatch_metadata WHERE {col} IS NOT NULL AND date(dispatched_at) BETWEEN ? AND ?",
            (start_s, end_s),
        ).fetchall()
        scopes.extend((stype, r[0]) for r in rows if r[0])

    # Model scope from session_analytics
    rows = conn.execute(
        """SELECT DISTINCT sa.session_model FROM session_analytics sa
           JOIN dispatch_metadata dm ON sa.dispatch_id = dm.dispatch_id
           WHERE sa.session_model IS NOT NULL AND date(dm.dispatched_at) BETWEEN ? AND ?""",
        (start_s, end_s),
    ).fetchall()
    scopes.extend(("model", r[0]) for r in rows if r[0] and r[0] != "unknown")

    return scopes


def _scope_filter(scope_type: str, scope_value: str, start: date, end: date) -> Tuple[str, tuple]:
    """Build WHERE clause and params for scope filtering."""
    base = "date(dispatched_at) BETWEEN ? AND ?"
    params: list = [str(start), str(end)]

    if scope_type == "system":
        return base, tuple(params)
    elif scope_type == "model":
        return (
            f"{base} AND dispatch_id IN (SELECT dispatch_id FROM session_analytics WHERE session_model = ?)",
            tuple(params + [scope_value]),
        )
    else:
        return f"{base} AND {scope_type} = ?", tuple(params + [scope_value])


def _compute_fpy(conn: sqlite3.Connection, where: str, params: tuple) -> Optional[Tuple[float, int]]:
    """First-Pass Yield: ratio of unique gate+pr_id combos that succeeded on first try."""
    rows = conn.execute(
        f"""SELECT gate, pr_id, MIN(dispatched_at) as first_at, normalized_status
            FROM dispatch_metadata
            WHERE normalized_status IS NOT NULL AND normalized_status != 'timeout' AND normalized_status != 'unknown'
            AND gate IS NOT NULL AND {where}
            GROUP BY gate, pr_id
            HAVING dispatched_at = first_at""",
        params,
    ).fetchall()
    if not rows:
        return None
    success = sum(1 for r in rows if r[3] == "success")
    return (success / len(rows) * 100, len(rows))


def _compute_rework_rate(conn: sqlite3.Connection, where: str, params: tuple) -> Optional[Tuple[float, int]]:
    """Rework rate: total dispatches / unique (gate, pr_id) combos."""
    total = conn.execute(
        f"SELECT COUNT(*) FROM dispatch_metadata WHERE gate IS NOT NULL AND {where}", params
    ).fetchone()[0]
    unique = conn.execute(
        f"SELECT COUNT(DISTINCT gate || '|' || COALESCE(pr_id, '')) FROM dispatch_metadata WHERE gate IS NOT NULL AND {where}",
        params,
    ).fetchone()[0]
    if unique == 0:
        return None
    return (total / unique, total)


def _compute_gate_velocity(conn: sqlite3.Connection, gate: str, start: date, end: date) -> Optional[Tuple[float, int]]:
    """Hours from first dispatch to gate completion for a specific gate."""
    rows = conn.execute(
        """SELECT pr_id,
                  MIN(dispatched_at) as first_dispatch,
                  MAX(completed_at) as last_complete
           FROM dispatch_metadata
           WHERE gate = ? AND date(dispatched_at) BETWEEN ? AND ?
           AND completed_at IS NOT NULL AND normalized_status = 'success'
           GROUP BY pr_id""",
        (gate, str(start), str(end)),
    ).fetchall()
    if not rows:
        return None
    hours = []
    for r in rows:
        try:
            t_start = datetime.fromisoformat(r[1])
            t_end = datetime.fromisoformat(r[2])
            hours.append((t_end - t_start).total_seconds() / 3600)
        except (ValueError, TypeError):
            continue
    if not hours:
        return None
    return (statistics.mean(hours), len(hours))


def _metric(start: date, end: date, scope_type: str, scope_value: str, name: str, value: float, sample: int) -> Dict[str, Any]:
    return {
        "period_start": str(start),
        "period_end": str(end),
        "scope_type": scope_type,
        "scope_value": scope_value,
        "metric_name": name,
        "metric_value": round(value, 4),
        "sample_size": sample,
    }


# ---------------------------------------------------------------------------
# SPC Control Limits
# ---------------------------------------------------------------------------


def update_control_limits(conn: sqlite3.Connection, baseline_days: int = 30) -> int:
    """Recompute control limits from recent governance_metrics data."""
    end = date.today()
    start = end - timedelta(days=baseline_days)

    rows = conn.execute(
        """SELECT DISTINCT metric_name, scope_type, scope_value
           FROM governance_metrics
           WHERE period_start >= ?""",
        (str(start),),
    ).fetchall()

    updated = 0
    for metric_name, scope_type, scope_value in rows:
        values = [
            r[0]
            for r in conn.execute(
                """SELECT metric_value FROM governance_metrics
                   WHERE metric_name=? AND scope_type=? AND scope_value=?
                   AND period_start >= ? ORDER BY period_start""",
                (metric_name, scope_type, scope_value, str(start)),
            ).fetchall()
        ]
        if len(values) < 3:
            continue

        mean = statistics.mean(values)
        sigma = statistics.stdev(values)
        ucl = mean + 3 * sigma
        lcl = max(0, mean - 3 * sigma)

        conn.execute(
            """INSERT INTO spc_control_limits (metric_name, scope_type, scope_value, center_line, ucl, lcl, sigma, sample_count, baseline_start, baseline_end)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(metric_name, scope_type, scope_value)
               DO UPDATE SET center_line=excluded.center_line, ucl=excluded.ucl, lcl=excluded.lcl,
                             sigma=excluded.sigma, sample_count=excluded.sample_count,
                             baseline_start=excluded.baseline_start, baseline_end=excluded.baseline_end,
                             computed_at=CURRENT_TIMESTAMP""",
            (metric_name, scope_type, scope_value, mean, ucl, lcl, sigma, len(values), str(start), str(end)),
        )
        updated += 1

    conn.commit()
    return updated


# ---------------------------------------------------------------------------
# Anomaly Detection (Western Electric Rules)
# ---------------------------------------------------------------------------


def detect_anomalies(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Detect SPC anomalies using Western Electric rules."""
    alerts: List[Dict[str, Any]] = []

    limits = conn.execute("SELECT * FROM spc_control_limits").fetchall()
    col_names = [d[0] for d in conn.execute("SELECT * FROM spc_control_limits LIMIT 0").description]

    for limit_row in limits:
        limit = dict(zip(col_names, limit_row))
        values = [
            r[0]
            for r in conn.execute(
                """SELECT metric_value FROM governance_metrics
                   WHERE metric_name=? AND scope_type=? AND scope_value=?
                   ORDER BY period_start DESC LIMIT 20""",
                (limit["metric_name"], limit["scope_type"], limit["scope_value"]),
            ).fetchall()
        ]
        if not values:
            continue

        latest = values[0]
        center = limit["center_line"]
        ucl = limit["ucl"]
        lcl = limit["lcl"]
        sigma = limit["sigma"]

        # Rule 1: Out of control — point beyond UCL/LCL
        if latest > ucl or latest < lcl:
            violated = "UCL" if latest > ucl else "LCL"
            alerts.append({
                "alert_type": "out_of_control",
                "metric_name": limit["metric_name"],
                "scope_type": limit["scope_type"],
                "scope_value": limit["scope_value"],
                "observed_value": latest,
                "control_limit": ucl if latest > ucl else lcl,
                "description": f"{limit['metric_name']} = {latest:.2f} beyond {violated} ({ucl:.2f}/{lcl:.2f})",
                "severity": "critical",
            })

        # Rule 2: Trend — 7+ consecutive increasing or decreasing
        if len(values) >= 7:
            recent7 = list(reversed(values[:7]))
            if all(recent7[i] < recent7[i + 1] for i in range(6)):
                alerts.append({
                    "alert_type": "trend",
                    "metric_name": limit["metric_name"],
                    "scope_type": limit["scope_type"],
                    "scope_value": limit["scope_value"],
                    "observed_value": latest,
                    "control_limit": None,
                    "description": f"7-point increasing trend detected for {limit['metric_name']}",
                    "severity": "warning",
                })
            elif all(recent7[i] > recent7[i + 1] for i in range(6)):
                alerts.append({
                    "alert_type": "trend",
                    "metric_name": limit["metric_name"],
                    "scope_type": limit["scope_type"],
                    "scope_value": limit["scope_value"],
                    "observed_value": latest,
                    "control_limit": None,
                    "description": f"7-point decreasing trend detected for {limit['metric_name']}",
                    "severity": "warning",
                })

        # Rule 3: Shift — 8+ consecutive on same side of center line
        if len(values) >= 8:
            recent8 = values[:8]
            above = all(v > center for v in recent8)
            below = all(v < center for v in recent8)
            if above or below:
                side = "above" if above else "below"
                alerts.append({
                    "alert_type": "shift",
                    "metric_name": limit["metric_name"],
                    "scope_type": limit["scope_type"],
                    "scope_value": limit["scope_value"],
                    "observed_value": latest,
                    "control_limit": center,
                    "description": f"8-point shift {side} center line for {limit['metric_name']}",
                    "severity": "warning",
                })

        # Rule 4: Run — 2 of 3 beyond 2 sigma
        if len(values) >= 3 and sigma > 0:
            two_sigma_high = center + 2 * sigma
            two_sigma_low = center - 2 * sigma
            recent3 = values[:3]
            beyond_2s = sum(1 for v in recent3 if v > two_sigma_high or v < two_sigma_low)
            if beyond_2s >= 2:
                alerts.append({
                    "alert_type": "run",
                    "metric_name": limit["metric_name"],
                    "scope_type": limit["scope_type"],
                    "scope_value": limit["scope_value"],
                    "observed_value": latest,
                    "control_limit": two_sigma_high,
                    "description": f"2-of-3 points beyond 2-sigma for {limit['metric_name']}",
                    "severity": "info",
                })

    return alerts


def store_metrics(conn: sqlite3.Connection, metrics: List[Dict[str, Any]]) -> None:
    """Insert computed metrics into governance_metrics table."""
    for m in metrics:
        conn.execute(
            """INSERT INTO governance_metrics (period_start, period_end, scope_type, scope_value, metric_name, metric_value, sample_size)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (m["period_start"], m["period_end"], m["scope_type"], m["scope_value"],
             m["metric_name"], m["metric_value"], m["sample_size"]),
        )
    conn.commit()


def store_alerts(conn: sqlite3.Connection, alerts: List[Dict[str, Any]]) -> None:
    """Insert SPC alerts into spc_alerts table."""
    for a in alerts:
        conn.execute(
            """INSERT INTO spc_alerts (alert_type, metric_name, scope_type, scope_value, observed_value, control_limit, description, severity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (a["alert_type"], a["metric_name"], a["scope_type"], a["scope_value"],
             a["observed_value"], a.get("control_limit"), a["description"], a["severity"]),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Nightly governance metrics aggregator")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    parser.add_argument("--backfill", action="store_true", help="Backfill CQS for existing dispatches")
    parser.add_argument("--baseline-days", type=int, default=30, help="SPC baseline window in days")
    parser.add_argument("--period-days", type=int, default=1, help="Aggregation period in days")
    args = parser.parse_args()

    paths = ensure_env()
    db_path = get_db(paths)
    if not db_path.exists():
        print("No quality_intelligence.db found, skipping governance aggregation")
        return 0

    conn = sqlite3.connect(str(db_path))

    # Ensure schema exists
    ensure_governance_schema(conn)
    ensure_cqs_columns(conn)

    # Backfill CQS if requested
    if args.backfill:
        count = backfill_cqs(conn, db_path, dry_run=args.dry_run)
        print(f"Backfilled CQS for {count} dispatches")

    # Compute metrics for the period
    period_end = date.today() - timedelta(days=1)  # yesterday
    period_start = period_end - timedelta(days=args.period_days - 1)

    print(f"Computing governance metrics for {period_start} to {period_end}...")
    metrics = compute_metrics(conn, period_start, period_end)

    if args.dry_run:
        print(f"\n[dry-run] {len(metrics)} metrics computed:")
        for m in metrics:
            print(f"  {m['scope_type']}/{m['scope_value']}: {m['metric_name']}={m['metric_value']:.4f} (n={m['sample_size']})")
    else:
        store_metrics(conn, metrics)
        print(f"Stored {len(metrics)} governance metrics")

    # Update SPC control limits
    if not args.dry_run:
        updated = update_control_limits(conn, args.baseline_days)
        print(f"Updated {updated} SPC control limits")

    # Detect anomalies
    alerts = detect_anomalies(conn)
    if alerts:
        if args.dry_run:
            print(f"\n[dry-run] {len(alerts)} SPC alerts detected:")
            for a in alerts:
                print(f"  [{a['severity']}] {a['alert_type']}: {a['description']}")
        else:
            store_alerts(conn, alerts)
            print(f"Stored {len(alerts)} SPC alerts")
            for a in alerts:
                if a["severity"] == "critical":
                    print(f"  CRITICAL: {a['description']}")
    else:
        print("No SPC anomalies detected")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
