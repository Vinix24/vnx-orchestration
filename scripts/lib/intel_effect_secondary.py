#!/usr/bin/env python3
"""The falsifications and the secondary measures of the intelligence A/B window, each with its coverage.

Effect PR-8 of ``intelligence-effect-measurable``, plan decision 19. ``secondary`` reports F1 (adoption
rises without effect), F2 (adoption rises through dilution) and F3 (the control arm does better). CQS
and open items follow in the next PR of the track (PR-8b). Every line has one status: ``fired``, ``clear`` or
``not_evaluable``. The measures only read: they never change the verdict, the refusals or the exit code
of ``effect`` (the exit code here is 0 for any set of statuses, 1 only for an unusable store).

Fail closed: a measure is ``not_evaluable`` with its reason and coverage whenever its evidence is
missing, unreadable, below the minimum coverage or has no verdict behind it. Only a readable value
counts, and nothing is ever dropped silently. Cohort, arms, verdict and weeks come from the PR-6 and PR-7
modules; every query filters ``project_id = ?`` (decision 17, ADR-007) and the id lists go to the stores
in blocks of ``BLOCK``, no ATTACH.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import intel_effect_cohort as co
import intel_effect_estimator as es

FIRED, CLEAR, NOT_EVALUABLE = "fired", "clear", "not_evaluable"
BLOCK = 500
MIN_WEEK_OFFERS = 30
MIN_WEEK_COVERAGE = Fraction(1, 2)
ADOPTION_RISE = Fraction(5, 100)
DILUTION_DROP = Fraction(10, 100)

_OFFERS_SQL = """
SELECT o.dispatch_id, pio.used FROM dispatch_pattern_offered o
LEFT JOIN pattern_injection_outcome pio ON pio.dispatch_id = o.dispatch_id
  AND pio.pattern_id = o.pattern_id AND pio.project_id = o.project_id
WHERE o.project_id = ? AND o.dispatch_id IN ({marks})
"""
_PAYLOAD_SQL = ("SELECT id, dispatch_id, injected_at, payload_chars, items_injected FROM intelligence_injections "
                "WHERE project_id = ? AND dispatch_id IN ({marks})")


@dataclass
class Measure:
    name: str
    status: str
    reason: Optional[str] = None
    data: dict = field(default_factory=dict)


def _rows(conn: sqlite3.Connection, sql: str, project_id: str, ids: List[str]) -> Iterator[tuple]:
    for start in range(0, len(ids), BLOCK):
        chunk = ids[start:start + BLOCK]
        yield from conn.execute(sql.format(marks=",".join("?" * len(chunk))), (project_id, *chunk))


def _whole(value: object) -> bool:
    """A readable count: an int (not a bool) of 0 or more. The one test for unreadable here."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _pct(fraction: Fraction) -> float:
    return round(float(fraction) * 100, 2)


def week_stats(qi: sqlite3.Connection, project_id: str, week_of: Dict[str, str]) -> List[dict]:
    """Per ISO week the offers, the offers with a readable outcome row, the used ones and the coverage."""
    stats: Dict[str, dict] = {}
    for did, used in _rows(qi, _OFFERS_SQL, project_id, sorted(week_of)):
        slot = stats.setdefault(week_of[did], {"offers": 0, "with_outcome": 0, "used": 0})
        slot["offers"] += 1
        if _whole(used) and used in (0, 1):
            slot["with_outcome"] += 1
            slot["used"] += used
    out = []
    for week in sorted(stats):
        slot = stats[week]
        coverage = Fraction(slot["with_outcome"], slot["offers"])
        ok = slot["with_outcome"] >= MIN_WEEK_OFFERS and coverage >= MIN_WEEK_COVERAGE
        out.append({"week": week, **slot, "coverage_pct": _pct(coverage), "evaluable": ok,
                    "adoption_pct": _pct(Fraction(slot["used"], slot["with_outcome"])) if ok else None})
    return out


def _rule() -> str:
    return (f"a week needs >= {MIN_WEEK_OFFERS} offers with an outcome and >= {_pct(MIN_WEEK_COVERAGE):g}% "
            "coverage (offers with outcome / all offers)")


def f1(weeks: List[dict], verdict: Optional[str], no_verdict: str) -> Measure:
    good = [w for w in weeks if w["evaluable"]]
    data = {"weeks": weeks, "rule": _rule()}
    if len(good) < 2:
        return Measure("F1", NOT_EVALUABLE, f"fewer than 2 evaluable weeks ({len(good)} of {len(weeks)})", data)
    first, last = good[0], good[-1]
    rise = Fraction(last["used"], last["with_outcome"]) - Fraction(first["used"], first["with_outcome"])
    data.update(first_week=first["week"], last_week=last["week"], rise_pp=_pct(rise))
    if verdict is None:
        return Measure("F1", NOT_EVALUABLE, no_verdict, data)
    fired = rise >= ADOPTION_RISE and verdict != "treatment_better"
    return Measure("F1", FIRED if fired else CLEAR, None, {**data, "verdict": verdict})


def _chars_per_item(rows: List[tuple], weeks: List[str]) -> Optional[Dict[str, Optional[Fraction]]]:
    """Per week sum(payload_chars) / sum(items_injected) over the first rows with items; None if unreadable."""
    if any(not (_whole(chars) and _whole(items)) for _, _, chars, items in rows):
        return None
    sums = {week: [0, 0] for week in weeks}
    for week, _, chars, items in rows:
        if items > 0 and week in sums:
            sums[week][0] += chars
            sums[week][1] += items
    return {week: Fraction(c, i) if i else None for week, (c, i) in sums.items()}


def f2(f1_line: Measure, rows: List[tuple]) -> Measure:
    if f1_line.status == NOT_EVALUABLE:
        return Measure("F2", NOT_EVALUABLE, f"F1 is not evaluable: {f1_line.reason}", {})
    first, last = f1_line.data["first_week"], f1_line.data["last_week"]
    per_week = _chars_per_item(rows, [first, last])
    if per_week is None:
        return Measure("F2", NOT_EVALUABLE, "an injection row has an unreadable payload_chars or items_injected", {})
    start, end = per_week[first], per_week[last]
    if start is None or end is None:
        return Measure("F2", NOT_EVALUABLE, "no injected items in the first or the last evaluable week", {})
    drop = (start - end) / start
    data = {"first_week": first, "last_week": last, "chars_per_item_first": round(float(start), 2),
            "chars_per_item_last": round(float(end), 2), "drop_pct": _pct(drop)}
    return Measure("F2", FIRED if f1_line.status == FIRED and drop >= DILUTION_DROP else CLEAR, None, data)


def f3(effect: es.Effect) -> Measure:
    if effect.estimate is None:
        return Measure("F3", NOT_EVALUABLE, "no verdict: " + ", ".join(effect.cohort.reasons or ["no estimate"]), {})
    verdict = es.verdict_class(effect.flags)
    return Measure("F3", FIRED if verdict == "control_better" else CLEAR, None, {"verdict": verdict})


def _payload_rows(rc: sqlite3.Connection, project_id: str, week_of: Dict[str, str]) -> List[tuple]:
    """The first injection row per cohort dispatch (decision 6: min injected_at, then min id) with items."""
    first: Dict[str, tuple] = {}
    keyed = []
    for rid, did, at, chars, items in _rows(rc, _PAYLOAD_SQL, project_id, sorted(week_of)):
        parsed = co.parse_utc(at)
        if parsed is not None:  # an unreadable timestamp is already a cohort refusal
            keyed.append(((parsed, rid), did, chars, items))
    for _, did, chars, items in sorted(keyed, key=lambda k: k[0]):
        first.setdefault(did, (week_of[did], did, chars, items))
    return [row for row in first.values() if not _whole(row[3]) or row[3] > 0]


@dataclass
class Secondary:
    effect: es.Effect
    measures: List[Measure]


def _guard(build):
    """(result, None) or (None, reason): a store that lacks a table or column makes its measures
    not_evaluable with that reason, never an exit and never a pass."""
    try:
        return build(), None
    except sqlite3.Error as exc:
        return None, f"source unreadable: {exc}"


def build_secondary(qi_db: Path, rc_db: Path, project_id: str, as_of: datetime) -> Secondary:
    effect = es.build_effect(rc_db, project_id, as_of)
    cohort = effect.cohort
    verdict = None if effect.estimate is None else es.verdict_class(effect.flags)
    no_verdict = "no verdict: " + ", ".join(cohort.reasons or ["no estimate"])
    week_of = {d.dispatch_id: es.iso_week(d.injected_at) for d in cohort.dispatches if d.arm == "treatment"}
    other = sorted({r for r in cohort.reasons if r != "below_floor"})  # below the floor still reports coverage
    refused = "cohort refused: " + ", ".join(other)
    with closing(co.open_read_only(qi_db)) as qi, closing(co.open_read_only(rc_db)) as rc:
        weeks, weeks_err = _guard(lambda: week_stats(qi, project_id, week_of))
        line1 = (Measure("F1", NOT_EVALUABLE, refused if other else weeks_err) if other or weeks is None
                 else f1(weeks, verdict, no_verdict))
        rows, rows_err = ([], None) if line1.status == NOT_EVALUABLE else _guard(
            lambda: _payload_rows(rc, project_id, week_of))
    line2 = Measure("F2", NOT_EVALUABLE, rows_err, {}) if rows_err else f2(line1, rows)
    return Secondary(effect, [line1, line2, f3(effect)])


def as_dict(sec: Secondary) -> dict:
    cohort = sec.effect.cohort
    return {"project_id": cohort.project_id, "as_of": cohort.as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_id": (cohort.window or {}).get("window_id"),
            "measures": {m.name: {"status": m.status, "reason": m.reason, **m.data} for m in sec.measures},
            "effect_exit_code": sec.effect.exit_code}


def render(sec: Secondary) -> str:
    data = as_dict(sec)
    out = [f"# Intelligence A/B secondary measures - project {data['project_id']}, window {data['window_id'] or '-'}, "
           f"as_of {data['as_of']}", "#   reported lines only: they never change the verdict or the exit code of effect", ""]
    for m in sec.measures:
        out.append(f"{m.name:11} {m.status}" + (f"  ({m.reason})" if m.reason else ""))
        out += [f"#   week {w['week']}: offers {w['offers']}, with outcome {w['with_outcome']} "
                f"({w['coverage_pct']:g}% coverage), adoption {w['adoption_pct'] if w['evaluable'] else 'n/a'}"
                for w in m.data.get("weeks", [])]
        out += [f"#   {arm}: resolved {a['resolved']}, with value {a['with_value']} ({a['coverage_pct']:g}%), "
                f"means {a['means']}" for arm, a in m.data.get("arms", {}).items()]
    return "\n".join(out)


def run_secondary(qi_db: Path, rc_db: Path, project_id: str, as_of: datetime, as_json: bool) -> int:
    """CLI body: exit 0 with one status per measure, 1 when a store is unusable. Never the verdict's exit code."""
    if qi_db.resolve() == rc_db.resolve():
        print(f"secondary: --qi and --rc are the same file: {rc_db}", file=sys.stderr)
        return 1
    try:
        sec = build_secondary(qi_db, rc_db, project_id, as_of)
    except sqlite3.Error as exc:
        print(f"secondary: --qi {qi_db} or --rc {rc_db} is unreadable or not at v38 (migration 0038): {exc}",
              file=sys.stderr)
        return 1
    print(json.dumps(as_dict(sec), indent=2) if as_json else render(sec))
    return 0
