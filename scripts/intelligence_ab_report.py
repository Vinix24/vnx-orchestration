#!/usr/bin/env python3
"""Weekly A/B intelligence-injection report: treatment/control pairs per task class and ISO week.

This module computes nothing itself. The cohort (``intel_effect_cohort``) and the estimator
(``intel_effect_estimator``) are the one implementation (plan ``intelligence-effect-measurable``,
decision 17): the arm of a row is the stored, re-derived arm, a row without an arm never counts as
treatment, and every cohort refusal is a refusal here. Pairs exist only when nothing refuses.

Usage:
    python3 scripts/intelligence_ab_report.py --qi <path> --rc <path> [--project-id <id>] [--as-of <ISO>]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_LIB_DIR = Path(__file__).resolve().parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


def _pair(key: tuple, cell: dict) -> dict:
    (n_c, a_c), (n_t, a_t) = cell["control"], cell["treatment"]
    task_class, week = key
    return {"week": week, "task_class": task_class,
            "treatment_n": n_t, "treatment_rate": a_t / n_t,
            "control_n": n_c, "control_rate": a_c / n_c,
            "lift": round(a_t / n_t - a_c / n_c, 4)}


def weekly_ab_lift(qi_db: Path, rc_db: Path, project_id: str, as_of: Optional[datetime] = None) -> dict:
    """The effect report of ``intel_effect_estimator`` plus ``pairs`` per (week, task_class) stratum.

    ``pairs`` is empty whenever ``refusals`` is not. Raises ``ValueError`` when both paths are one
    file and ``RuntimeError`` when a store is unreadable or not at v38: never an empty report.
    """
    import intel_effect_cohort as co
    import intel_effect_estimator as es

    if Path(qi_db).resolve() == Path(rc_db).resolve():
        raise ValueError(f"qi and rc are the same file: {rc_db}")
    try:
        with closing(co.open_read_only(Path(qi_db))) as conn:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        effect = es.build_effect(Path(rc_db), project_id, as_of or datetime.now(timezone.utc))
    except sqlite3.Error as exc:
        raise RuntimeError(f"qi {qi_db} or rc {rc_db} is unreadable or not at v38 (migration 0038): {exc}") from exc
    report = es.as_dict(effect)
    report["pairs"] = [] if effect.refusals else [
        _pair(key, cell) for key, cell in sorted(effect.kept.items(), key=lambda kv: (kv[0][1], kv[0][0] or ""), reverse=True)]
    return report


def _render_markdown(report: dict) -> str:
    if not report["pairs"]:
        reasons = [f"REFUSED {r['reason']}: {r['detail']}" for r in report["refusals"]]
        return "\n".join(["No treatment/control pairs: no verdict.", *reasons]) + "\n"
    lines = [
        "# A/B Intelligence Injection Lift Report",
        "",
        "| Week | Task Class | Treatment rate | n | Control rate | n | Lift |",
        "|------|------------|---------------|---|-------------|---|------|",
    ]
    for p in report["pairs"]:
        lines.append(f"| {p['week']} | {p['task_class'] or 'unknown'} | {p['treatment_rate']:.2%} | {p['treatment_n']} "
                     f"| {p['control_rate']:.2%} | {p['control_n']} | {p['lift']:+.1%} |")
    est = report["estimate"]
    lines += ["", f"Verdict {report['verdict']}: d {est['d_pp']:+.2f} pp, 95% interval "
              f"[{est['lower_pp']:+.2f}, {est['upper_pp']:+.2f}] pp (stratified)"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly A/B intelligence injection lift report")
    parser.add_argument("--qi", required=True, help="Path to quality_intelligence.db")
    parser.add_argument("--rc", required=True, help="Path to runtime_coordination.db (v38)")
    parser.add_argument("--project-id", help="tenant (default: project_root.resolve_project_id())")
    parser.add_argument("--as-of", help="ISO timestamp (default: now, UTC)")
    args = parser.parse_args()

    import intel_effect_cohort as co
    from project_root import resolve_project_id

    as_of = co.parse_utc(args.as_of) if args.as_of else None
    if args.as_of and as_of is None:
        parser.error(f"not an ISO timestamp: {args.as_of!r}")
    try:
        project_id = args.project_id or resolve_project_id()
        report = weekly_ab_lift(Path(args.qi), Path(args.rc), project_id, as_of)
    except (RuntimeError, ValueError) as exc:
        print(f"intelligence_ab_report: {exc}", file=sys.stderr)
        return 1
    print(_render_markdown(report))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
