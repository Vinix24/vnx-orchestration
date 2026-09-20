#!/usr/bin/env python3
"""Intelligence injection join + adoption-rate report.

PUNT 2 (join):  per dispatch, show offered / used / ignored-with-reason by
ATTACHing the runtime coordination DB (intelligence_injections) to the
quality intelligence DB (dispatch_pattern_offered + pattern_injection_outcome).
All three tables carry ``dispatch_id``; the key exists, the join did not.

PUNT 3a (adoption): adoption rate = used=1 / total offered, per pattern and
over time, with the counts printed alongside. A metric you cannot print is
not a metric.

Usage:
    python3 scripts/intel_injection_join.py join   [--qi <path>] [--rc <path>] [--limit N]
    python3 scripts/intel_injection_join.py adoption [--qi <path>] [--limit N]
    python3 scripts/intel_injection_join.py placebo [--qi <path>]

Default DB paths resolve from VNX_STATE_DIR (falling back to
~/.vnx-data/vnx-dev/state).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


def _default_state_dir() -> Path:
    env = (
        __import__("os").environ.get("VNX_STATE_DIR")
        or __import__("os").environ.get("VNX_DATA_DIR")
    )
    if env:
        return Path(env) if Path(env).name == "state" else Path(env) / "state"
    return Path.home() / ".vnx-data" / "vnx-dev" / "state"


def _resolve_db(arg: str | None, default_name: str, state_dir: Path) -> Path:
    if arg:
        return Path(arg)
    return state_dir / default_name


# ---------------------------------------------------------------------------
# PUNT 2 — the join (ATTACH)
# ---------------------------------------------------------------------------

JOIN_SQL = """
SELECT
    o.dispatch_id                                        AS dispatch_id,
    o.pattern_id                                         AS pattern_id,
    o.pattern_title                                      AS pattern_title,
    CASE WHEN pio.used IS NOT NULL THEN pio.used ELSE -1 END AS used,
    COALESCE(pio.reason, '')                             AS reason,
    COALESCE(pio.evidence, '')                           AS evidence,
    COALESCE(i.items_injected, 0)                        AS items_injected,
    COALESCE(i.items_suppressed, 0)                      AS items_suppressed,
    COALESCE(i.injection_point, '')                      AS injection_point
FROM dispatch_pattern_offered o
LEFT JOIN pattern_injection_outcome pio
    ON pio.dispatch_id = o.dispatch_id
   AND pio.pattern_id  = o.pattern_id
LEFT JOIN {inj_table} i
    ON i.dispatch_id = o.dispatch_id
ORDER BY o.offered_at DESC
LIMIT ?
"""


def run_join(qi_db: Path, rc_db: Path, limit: int) -> int:
    """Print offered / used / ignored-with-reason per dispatch via ATTACH.

    Returns 0 on success, 1 if the quality DB is missing. (The number of rows
    printed is surfaced in the report header, not as the return value, so the
    CLI boundary can distinguish success from failure by exit code.)
    """
    if not qi_db.exists():
        print(f"quality_intelligence.db not found: {qi_db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(qi_db))
    conn.row_factory = sqlite3.Row
    try:
        # vnx-atomic-write: ATTACH is a read-only side-DB attach; the guard
        # prevents accidentally attaching a path that resolves to the same
        # file as the primary quality DB (double-write / lock churn). When the
        # rc DB is absent or identical, the intelligence_injections LEFT JOIN
        # is omitted entirely so the query cannot reference a missing table.
        attached = False
        if rc_db.exists() and qi_db.resolve() != rc_db.resolve():
            # Bind the path as a parameter, never interpolate it into the SQL
            # literal: a path containing a quote or space would otherwise break
            # the ATTACH statement (sqlite3 supports ?-binding for ATTACH).
            conn.execute("ATTACH DATABASE ? AS rc", (str(rc_db),))
            attached = True
        elif rc_db.exists() and qi_db.resolve() == rc_db.resolve():
            print(
                "WARNING: runtime DB resolves to the same file as quality DB; "
                "intelligence_injections join column will be empty.",
                file=sys.stderr,
            )
        inj_table = "rc.intelligence_injections" if attached else "(SELECT NULL AS dispatch_id, NULL AS items_injected, NULL AS items_suppressed, NULL AS injection_point LIMIT 0)"
        sql = JOIN_SQL.format(inj_table=inj_table)

        total_offers = conn.execute(
            "SELECT COUNT(*) FROM dispatch_pattern_offered"
        ).fetchone()[0]
        total_outcomes = conn.execute(
            "SELECT COUNT(*) FROM pattern_injection_outcome"
        ).fetchone()[0]

        rows = conn.execute(sql, (limit,)).fetchall()
        print(f"# Intelligence injection join — offers: {total_offers}, "
              f"outcome rows: {total_outcomes} (showing {len(rows)})\n")
        header = (
            f"{'dispatch_id':40} {'pattern_id':36} {'used':>4} "
            f"{'reason':22} {'items_inj':>9}"
        )
        print(header)
        print("-" * len(header))
        for r in rows:
            used = r["used"]
            used_str = {1: "yes", 0: "no", -1: "?"}.get(used, str(used))
            print(
                f"{r['dispatch_id'][:40]:40} {r['pattern_id'][:36]:36} "
                f"{used_str:>4} {r['reason'][:22]:22} {r['items_injected']:>9}"
            )
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# PUNT 3a — adoption rate (used=1 / offered), per pattern + over time
# ---------------------------------------------------------------------------

ADOPTION_OVERALL_SQL = """
SELECT
    o.pattern_id                                        AS pattern_id,
    MAX(o.pattern_title)                                AS pattern_title,
    COUNT(*)                                            AS offered,
    SUM(CASE WHEN pio.used = 1 THEN 1 ELSE 0 END)       AS used,
    SUM(CASE WHEN pio.used = 0 THEN 1 ELSE 0 END)      AS ignored,
    CASE WHEN COUNT(*) > 0
         THEN ROUND(100.0 * SUM(CASE WHEN pio.used = 1 THEN 1 ELSE 0 END) / COUNT(*), 1)
         ELSE NULL END                                  AS adoption_pct
FROM dispatch_pattern_offered o
LEFT JOIN pattern_injection_outcome pio
    ON pio.dispatch_id = o.dispatch_id
   AND pio.pattern_id  = o.pattern_id
GROUP BY o.pattern_id
ORDER BY offered DESC
LIMIT ?
"""

ADOPTION_OVER_TIME_SQL = """
SELECT
    strftime('%Y-W%W', o.offered_at)                     AS week,
    COUNT(*)                                             AS offered,
    SUM(CASE WHEN pio.used = 1 THEN 1 ELSE 0 END)        AS used,
    SUM(CASE WHEN pio.used = 0 THEN 1 ELSE 0 END)        AS ignored,
    CASE WHEN COUNT(*) > 0
         THEN ROUND(100.0 * SUM(CASE WHEN pio.used = 1 THEN 1 ELSE 0 END) / COUNT(*), 1)
         ELSE NULL END                                   AS adoption_pct
FROM dispatch_pattern_offered o
LEFT JOIN pattern_injection_outcome pio
    ON pio.dispatch_id = o.dispatch_id
   AND pio.pattern_id  = o.pattern_id
GROUP BY week
ORDER BY week DESC
LIMIT ?
"""


def run_adoption(qi_db: Path, limit: int) -> int:
    if not qi_db.exists():
        print(f"quality_intelligence.db not found: {qi_db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(qi_db))
    conn.row_factory = sqlite3.Row
    try:
        total_offers = conn.execute(
            "SELECT COUNT(*) FROM dispatch_pattern_offered"
        ).fetchone()[0]
        total_used = conn.execute(
            "SELECT COUNT(*) FROM pattern_injection_outcome WHERE used = 1"
        ).fetchone()[0]
        total_ignored = conn.execute(
            "SELECT COUNT(*) FROM pattern_injection_outcome WHERE used = 0"
        ).fetchone()[0]
        overall = conn.execute(ADOPTION_OVERALL_SQL, (limit,)).fetchall()
        over_time = conn.execute(ADOPTION_OVER_TIME_SQL, (limit,)).fetchall()
    finally:
        conn.close()

    print(f"# Adoption rate — offered: {total_offers}, used: {total_used}, "
          f"ignored: {total_ignored}\n")
    print("## Per pattern\n")
    header = f"{'pattern_id':36} {'offered':>7} {'used':>5} {'ignored':>7} {'adopt%':>6}"
    print(header)
    print("-" * len(header))
    for r in overall:
        pct = "n/a" if r["adoption_pct"] is None else f"{r['adoption_pct']}"
        print(f"{r['pattern_id'][:36]:36} {r['offered']:>7} {r['used']:>5} "
              f"{r['ignored']:>7} {pct:>6}")

    print(f"\n## Over time (per ISO week)\n")
    header = f"{'week':10} {'offered':>7} {'used':>5} {'ignored':>7} {'adopt%':>6}"
    print(header)
    print("-" * len(header))
    for r in over_time:
        pct = "n/a" if r["adoption_pct"] is None else f"{r['adoption_pct']}"
        print(f"{r['week']:10} {r['offered']:>7} {r['used']:>5} {r['ignored']:>7} {pct:>6}")
    return 0


# ---------------------------------------------------------------------------
# PUNT 3b — adoption rate per arm (placebo discrimination test)
#
# Side-by-side adoption rate per injection arm, with the counts that make the
# rate readable. A rate without its n is a number you cannot interpret, and a
# placebo arm with 0 rows is the expected state until the arm is activated —
# the command must print cleanly against zero rows (dispatch
# 20260920-191500-placebo-arm). Reads ab_arm from pattern_injection_outcome
# when the column exists (v32); otherwise every row is read as 'treatment'.
# ---------------------------------------------------------------------------

_PLACEBO_ARM_SQL_TEMPLATE = """
SELECT
    COALESCE(pio.ab_arm, 'treatment')                       AS ab_arm,
    COUNT(*)                                                AS offered,
    SUM(CASE WHEN pio.used = 1 THEN 1 ELSE 0 END)           AS used,
    SUM(CASE WHEN pio.used = 0 THEN 1 ELSE 0 END)           AS ignored,
    CASE WHEN COUNT(*) > 0
         THEN ROUND(100.0 * SUM(CASE WHEN pio.used = 1 THEN 1 ELSE 0 END) / COUNT(*), 1)
         ELSE NULL END                                     AS adoption_pct
FROM dispatch_pattern_offered o
LEFT JOIN pattern_injection_outcome pio
    ON pio.dispatch_id = o.dispatch_id
   AND pio.pattern_id  = o.pattern_id
GROUP BY COALESCE(pio.ab_arm, 'treatment')
ORDER BY ab_arm
"""

_PLACEBO_ARM_SQL_NO_COLUMN = """
SELECT
    'treatment'                                             AS ab_arm,
    COUNT(*)                                                AS offered,
    SUM(CASE WHEN pio.used = 1 THEN 1 ELSE 0 END)           AS used,
    SUM(CASE WHEN pio.used = 0 THEN 1 ELSE 0 END)           AS ignored,
    CASE WHEN COUNT(*) > 0
         THEN ROUND(100.0 * SUM(CASE WHEN pio.used = 1 THEN 1 ELSE 0 END) / COUNT(*), 1)
         ELSE NULL END                                     AS adoption_pct
FROM dispatch_pattern_offered o
LEFT JOIN pattern_injection_outcome pio
    ON pio.dispatch_id = o.dispatch_id
   AND pio.pattern_id  = o.pattern_id
GROUP BY ab_arm
ORDER BY ab_arm
"""


def _pio_has_ab_arm(conn: sqlite3.Connection) -> bool:
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(pattern_injection_outcome)"
        ).fetchall()}
        return "ab_arm" in cols
    except sqlite3.Error:
        return False


def run_placebo(qi_db: Path) -> int:
    """Print adoption rate per arm, side-by-side, with counts.

    Returns 0 on a clean print (including the all-zero case, which is the
    expected state until the placebo arm is activated). Returns 1 only when
    the quality DB itself is absent.
    """
    if not qi_db.exists():
        print(f"quality_intelligence.db not found: {qi_db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(qi_db))
    conn.row_factory = sqlite3.Row
    try:
        has_arm = _pio_has_ab_arm(conn)
        sql = _PLACEBO_ARM_SQL_TEMPLATE if has_arm else _PLACEBO_ARM_SQL_NO_COLUMN
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()

    # Build a stable arm -> row map so every known arm prints even at zero.
    arms = ("placebo", "treatment", "control")
    by_arm: dict = {a: {"offered": 0, "used": 0, "ignored": 0, "adoption_pct": None} for a in arms}
    for r in rows:
        arm = r["ab_arm"] or "treatment"
        by_arm.setdefault(arm, {"offered": 0, "used": 0, "ignored": 0, "adoption_pct": None})
        by_arm[arm] = {
            "offered": int(r["offered"]),
            "used": int(r["used"]),
            "ignored": int(r["ignored"]),
            "adoption_pct": r["adoption_pct"],
        }

    # Header with global counts
    total_offered = sum(d["offered"] for d in by_arm.values())
    total_used = sum(d["used"] for d in by_arm.values())
    total_ignored = sum(d["ignored"] for d in by_arm.values())
    print(f"# Adoption rate per arm — offered: {total_offered}, "
          f"used: {total_used}, ignored: {total_ignored}")
    print(f"#   (ab_arm column {'present' if has_arm else 'ABSENT — every row read as treatment'}"
          f")\n")
    header = f"{'arm':12} {'offered':>7} {'used':>5} {'ignored':>7} {'adopt%':>6}"
    print(header)
    print("-" * len(header))
    for arm in arms:
        d = by_arm[arm]
        pct = "n/a" if d["adoption_pct"] is None else f"{d['adoption_pct']}"
        print(f"{arm:12} {d['offered']:>7} {d['used']:>5} {d['ignored']:>7} {pct:>6}")
    # Any unexpected arm (e.g. a future arm value)
    for arm, d in by_arm.items():
        if arm in arms:
            continue
        pct = "n/a" if d["adoption_pct"] is None else f"{d['adoption_pct']}"
        print(f"{arm:12} {d['offered']:>7} {d['used']:>5} {d['ignored']:>7} {pct:>6}")

    # Discrimination readout: the delta the test turns on.
    placebo = by_arm.get("placebo", {"adoption_pct": None, "offered": 0})
    treatment = by_arm.get("treatment", {"adoption_pct": None, "offered": 0})
    p_pct = placebo["adoption_pct"]
    t_pct = treatment["adoption_pct"]
    if p_pct is not None and t_pct is not None:
        delta = round(t_pct - p_pct, 1)
        print(f"\n# treatment - placebo adoption delta: {delta:+.1f} pp")
        print(f"#   treatment n={treatment['offered']}, placebo n={placebo['offered']}")
    else:
        print("\n# treatment - placebo adoption delta: n/a (one or both arms empty)")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_join = sub.add_parser("join", help="per-dispatch offered/used/ignored join")
    p_join.add_argument("--qi", help="path to quality_intelligence.db")
    p_join.add_argument("--rc", help="path to runtime_coordination.db")
    p_join.add_argument("--limit", type=int, default=50)

    p_adp = sub.add_parser("adoption", help="adoption rate per pattern and over time")
    p_adp.add_argument("--qi", help="path to quality_intelligence.db")
    p_adp.add_argument("--limit", type=int, default=20)

    p_pl = sub.add_parser("placebo", help="adoption rate per injection arm (placebo discrimination)")
    p_pl.add_argument("--qi", help="path to quality_intelligence.db")

    args = parser.parse_args(argv)
    state_dir = _default_state_dir()

    if args.cmd == "join":
        qi = _resolve_db(args.qi, "quality_intelligence.db", state_dir)
        rc = _resolve_db(args.rc, "runtime_coordination.db", state_dir)
        return run_join(qi, rc, args.limit)
    if args.cmd == "adoption":
        qi = _resolve_db(args.qi, "quality_intelligence.db", state_dir)
        return run_adoption(qi, args.limit)
    if args.cmd == "placebo":
        qi = _resolve_db(args.qi, "quality_intelligence.db", state_dir)
        return run_placebo(qi)
    return 2


if __name__ == "__main__":
    sys.exit(main())
