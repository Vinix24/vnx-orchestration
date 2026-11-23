#!/usr/bin/env python3
"""The intelligence A/B effect: the stratified risk difference, its interval and the verdict class.

Effect PR-7 of ``intelligence-effect-measurable``, plan decisions 15 and 21. It builds on the PR-6
cohort and never goes around it: every cohort refusal is a refusal here, and a verdict, an estimate
or a success rate exists only when there is no refusal at all. The floor is ``MIN_PER_ARM`` of
``intel_effect_cohort`` (decision 7, ``floor_rule()``), imported and never redefined. Standard
library only; it reads no flag and no process variable. Every query filters ``project_id = ?``
(decision 17, ADR-007).

Decision 15. The unit is a resolved, mature dispatch; ``d = p_treatment - p_control``, positive
favours injection. Strata: ``task_class`` of the first injection row crossed with the ISO week of
``injected_at`` (``date.isocalendar()``). Weight ``w_s = n_C n_T / (n_C + n_T)`` (Mantel-Haenszel),
``d = sum(w_s d_s) / sum(w_s)``, variance ``sum(w_s^2 (v_T + v_C)) / sum(w_s)^2`` with
``v = p~ (1 - p~) / n`` and ``p~ = (a + 2) / (n + 4)`` (Agresti-Coull), interval
``d +- 1.96 sqrt(variance)``. A stratum with an empty arm drops out; more than 10 % of the resolved
dispatches in dropped strata refuses as ``strata_coverage``. The raw difference stands beside it as
a control number and never moves the verdict.

Decision 21, margin 5 pp, in this order: ``treatment_better`` (lower bound > 0), ``control_better``
(upper bound < 0), ``equivalent_within_5pp`` (inside (-5, +5) pp and containing 0), else
``inconclusive``.

The balance check (plan open question 7, T0 decision 10-11) counts ``terminal_id`` and ``track`` of
the resolved dispatches per arm. An unreadable value counts under ``unknown``, never as a value and
never dropped. It reports only: it never refuses and never moves the verdict.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import intel_effect_cohort as co
from intel_effect_cohort import ARMS, FAILURE, MIN_PER_ARM, SUCCESS, Cohort, CohortDispatch

Z_95 = 1.96
MARGIN = 0.05
MAX_DROPPED_SHARE = Fraction(10, 100)
VERDICTS = ("treatment_better", "control_better", "equivalent_within_5pp")
BALANCE_FIELDS = ("terminal_id", "track")
_BALANCE_SQL = "SELECT dispatch_id, terminal_id, track FROM dispatches WHERE project_id = ?"

StratumKey = Tuple[Optional[str], str]
Strata = Dict[StratumKey, Dict[str, List[float]]]


def readable(value: object) -> Optional[str]:
    """The value when it is a string with text in it, else None: the one test for unreadable."""
    return value if isinstance(value, str) and value.strip() else None


def iso_week(at: datetime) -> str:
    year, week, _ = at.date().isocalendar()
    return f"{year}-W{week:02d}"


def resolved(cohort: Cohort) -> List[CohortDispatch]:
    """Decision 16: the mature success and failure dispatches; their count per arm is n."""
    return [d for d in cohort.dispatches if d.mature and d.outcome in (SUCCESS, FAILURE)]


def stratify(dispatches: List[CohortDispatch]) -> Strata:
    """Per (task_class, ISO week) and arm ``[n, successes]``. An unreadable task_class is the stratum None."""
    strata: Strata = {}
    for d in dispatches:
        cell = strata.setdefault((readable(d.task_class), iso_week(d.injected_at)), {arm: [0, 0] for arm in ARMS})
        cell[d.arm][0] += 1
        cell[d.arm][1] += d.outcome == SUCCESS
    return strata


def partition(strata: Strata) -> Tuple[Strata, int]:
    """The strata with both arms filled, and the number of dispatches in the strata that drop out."""
    kept = {key: cell for key, cell in strata.items() if all(cell[arm][0] for arm in ARMS)}
    return kept, sum(cell[arm][0] for key, cell in strata.items() if key not in kept for arm in ARMS)


def ac_variance(n: float, a: float) -> float:
    """Agresti-Coull: ``p~ = (a + 2) / (n + 4)``, ``v = p~ (1 - p~) / n``; never zero at 100 %."""
    p = (a + 2) / (n + 4)
    return p * (1 - p) / n


@dataclass(frozen=True)
class Estimate:
    d: float
    half_width: float

    @property
    def interval(self) -> Tuple[float, float]:
        return self.d - self.half_width, self.d + self.half_width


def estimate(kept: Strata) -> Estimate:
    """Decision 15 over strata that each have both arms filled (``partition``)."""
    total_w = weighted_d = weighted_v = 0.0
    for cell in kept.values():
        (n_c, a_c), (n_t, a_t) = cell["control"], cell["treatment"]
        w = n_c * n_t / (n_c + n_t)
        total_w += w
        weighted_d += w * (a_t / n_t - a_c / n_c)
        weighted_v += w * w * (ac_variance(n_t, a_t) + ac_variance(n_c, a_c))
    return Estimate(weighted_d / total_w, Z_95 * math.sqrt(weighted_v) / total_w)


def raw_difference(units: List[CohortDispatch]) -> float:
    """The unstratified ``p_treatment - p_control`` over every resolved dispatch: a control number only."""
    rate = {arm: [d.outcome == SUCCESS for d in units if d.arm == arm] for arm in ARMS}
    return sum(rate["treatment"]) / len(rate["treatment"]) - sum(rate["control"]) / len(rate["control"])


def verdict_flags(lower: float, upper: float) -> Dict[str, bool]:
    """Decision 21: every class that holds on the interval; ``inconclusive`` when none of the three does."""
    flags = {"treatment_better": lower > 0, "control_better": upper < 0,
             "equivalent_within_5pp": -MARGIN < lower <= 0 <= upper < MARGIN}
    return {**flags, "inconclusive": not any(flags.values())}


def verdict_class(flags: Dict[str, bool]) -> str:
    return next((name for name in VERDICTS if flags[name]), "inconclusive")


def strata_refusal(dropped: int, total: int) -> Optional[str]:
    """The ``strata_coverage`` detail, or None when at most 10 % of the resolved dispatches dropped out."""
    if total == 0:
        return "not computable: no resolved dispatches"
    share = Fraction(dropped, total)
    if share > MAX_DROPPED_SHARE:
        return f"{dropped} of {total} resolved dispatches ({float(share) * 100:.2f}%) in dropped strata (max 10%)"
    return None


def balance(rc_db: Path, project_id: str, dispatches: List[CohortDispatch]) -> Dict[str, Dict[str, dict]]:
    """Report only: per arm and field the value counts and ``unknown`` (NULL, empty, not text, no row)."""
    with closing(co.open_read_only(rc_db)) as conn:
        rows = {row["dispatch_id"]: row for row in conn.execute(_BALANCE_SQL, (project_id,))}
    out = {arm: {name: {"counts": {}, "unknown": 0} for name in BALANCE_FIELDS} for arm in ARMS}
    for d in dispatches:
        row = rows.get(d.dispatch_id)
        for name in BALANCE_FIELDS:
            value = None if row is None else readable(row[name])
            slot = out[d.arm][name]
            if value is None:
                slot["unknown"] += 1
            else:
                slot["counts"][value] = slot["counts"].get(value, 0) + 1
    return out


@dataclass
class Effect:
    cohort: Cohort
    units: List[CohortDispatch]
    strata: Strata
    kept: Strata
    dropped: int
    balance: Dict[str, Dict[str, dict]]
    refusals: List[Tuple[str, str]] = field(default_factory=list)
    estimate: Optional[Estimate] = None
    raw_d: Optional[float] = None

    @property
    def exit_code(self) -> int:
        return 3 if self.refusals else 0

    @property
    def flags(self) -> Optional[Dict[str, bool]]:
        return None if self.estimate is None else verdict_flags(*self.estimate.interval)


def build_effect(rc_db: Path, project_id: str, as_of: datetime) -> Effect:
    """The cohort, its strata and the balance; estimate, raw difference and verdict only when nothing refuses."""
    cohort = co.build_cohort(rc_db, project_id, as_of)
    units = resolved(cohort)
    strata = stratify(units)
    kept, dropped = partition(strata)
    effect = Effect(cohort, units, strata, kept, dropped, balance(rc_db, project_id, units), list(cohort.refusals))
    detail = strata_refusal(dropped, len(units))
    if detail is not None:
        effect.refusals.append(("strata_coverage", detail))
    if not effect.refusals:  # no refusal: the cohort holds >= MIN_PER_ARM per arm and kept is not empty
        effect.estimate, effect.raw_d = estimate(kept), raw_difference(units)
    return effect


def _pp(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 100, 2)


def as_dict(effect: Effect) -> dict:
    est, counts, cohort = effect.estimate, effect.cohort.arms(), effect.cohort
    wins = {arm: sum(d.outcome == SUCCESS for d in effect.units if d.arm == arm) for arm in ARMS}
    arms = {arm: {"n": c["resolved"], "successes": None if est is None else wins[arm],
                  "success_rate": None if est is None else round(wins[arm] / c["resolved"], 4),
                  **{key: c[key] for key in co.COUNT_KEYS if key != "resolved"}} for arm, c in counts.items()}
    lower, upper = est.interval if est is not None else (None, None)
    total = len(effect.units)
    return {
        "project_id": cohort.project_id, "as_of": cohort.as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_id": (cohort.window or {}).get("window_id"), "min_per_arm": MIN_PER_ARM,
        "margin_pp": _pp(MARGIN), "arms": arms,
        "estimate": None if est is None else {"d_pp": _pp(est.d), "lower_pp": _pp(lower), "upper_pp": _pp(upper),
                                              "half_width_pp": _pp(est.half_width), "raw_d_pp": _pp(effect.raw_d)},
        "verdict": None if est is None else verdict_class(effect.flags), "verdict_flags": effect.flags,
        "strata": {"total": len(effect.strata), "dropped": len(effect.strata) - len(effect.kept),
                   "dropped_dispatches": effect.dropped,
                   "dropped_pct": _pp(effect.dropped / total) if total else None,
                   "task_class_unknown": sum(readable(d.task_class) is None for d in effect.units)},
        "coverage": {**cohort.coverage, "deduplicated_rows": cohort.deduplicated},
        "balance": effect.balance,
        "refusals": [{"reason": reason, "detail": detail} for reason, detail in effect.refusals],
        "floor_rule": co.floor_rule() if "below_floor" in cohort.reasons else None, "exit_code": effect.exit_code,
    }


def render(effect: Effect) -> str:
    data = as_dict(effect)
    out = [f"# Intelligence A/B effect - project {data['project_id']}, window {data['window_id'] or '-'}, "
           f"as_of {data['as_of']}", f"#   floor {MIN_PER_ARM} resolved per arm, margin 5 pp, "
           "d = p_treatment - p_control (positive favours injection)", ""]
    keys = ("n", "successes", "success_rate", *(key for key in co.COUNT_KEYS if key != "resolved"))
    header = f"{'arm':10}" + "".join(f" {key:>12}" for key in keys)
    out += [header, "-" * len(header)]
    out += [f"{arm:10}" + "".join(f" {'-' if c[key] is None else c[key]!s:>12}" for key in keys)
            for arm, c in data["arms"].items()]
    est, strata = data["estimate"], data["strata"]
    if est is not None:
        out += ["", f"# d {est['d_pp']:+.2f} pp, 95% interval [{est['lower_pp']:+.2f}, {est['upper_pp']:+.2f}] pp "
                f"(stratified); raw {est['raw_d_pp']:+.2f} pp (control number, never the verdict)",
                f"VERDICT {data['verdict']} (true flags: "
                f"{', '.join(k for k, v in data['verdict_flags'].items() if v)})"]
    out += ["", f"# strata {strata['total']}, dropped {strata['dropped']} ({strata['dropped_dispatches']} dispatches, "
            f"{'-' if strata['dropped_pct'] is None else strata['dropped_pct']}%), "
            f"task_class unknown {strata['task_class_unknown']}",
            "# coverage: " + ", ".join(f"{key} {value}" for key, value in data["coverage"].items())]
    for arm, fields in data["balance"].items():
        out += [f"# balance {arm} {name}: " + ", ".join(
            [f"{value} {n}" for value, n in sorted(slot["counts"].items())] + [f"unknown {slot['unknown']}"])
            for name, slot in fields.items()]
    out += [f"REFUSED {r['reason']}: {r['detail']}" for r in data["refusals"]]
    return "\n".join(out + ([data["floor_rule"]] if data["floor_rule"] else []))


def run_effect(qi_db: Path, rc_db: Path, project_id: str, as_of: datetime, as_json: bool) -> int:
    """CLI body: exit 0 with a verdict, 3 with the refusals and no verdict, 1 when a store is unusable."""
    if qi_db.resolve() == rc_db.resolve():
        print(f"effect: --qi and --rc are the same file: {rc_db}", file=sys.stderr)
        return 1
    try:
        with closing(co.open_read_only(qi_db)) as conn:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        effect = build_effect(rc_db, project_id, as_of)
    except sqlite3.Error as exc:
        print(f"effect: --qi {qi_db} or --rc {rc_db} is unreadable or not at v38 (migration 0038): {exc}",
              file=sys.stderr)
        return 1
    print(json.dumps(as_dict(effect), indent=2) if as_json else render(effect))
    return effect.exit_code
