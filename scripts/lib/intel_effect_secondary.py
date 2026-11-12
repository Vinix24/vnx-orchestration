#!/usr/bin/env python3
"""The falsifications and the secondary measures of the intelligence A/B window, each with its coverage.

Effect PR-8 of ``intelligence-effect-measurable``, plan decision 19. ``secondary`` reports F1 (adoption
rises without effect), F2 (adoption rises through dilution) and F3 (the control arm does better), plus two
secondary measures per arm from ``dispatch_metadata``: ``cqs`` and ``open_items`` (created and resolved per
dispatch). F1-F3 have one status: ``fired``, ``clear`` or ``not_evaluable``; the two measures are ``evaluated``
or ``not_evaluable``. The measures only read: they never change the verdict, the refusals or the exit code
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

FIRED, CLEAR, NOT_EVALUABLE, EVALUATED = "fired", "clear", "not_evaluable", "evaluated"
BLOCK = 500
MIN_META_COVERAGE = Fraction(80, 100)
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
_CQS_SQL = "SELECT dispatch_id, cqs FROM dispatch_metadata WHERE project_id = ? AND dispatch_id IN ({marks})"
_OI_SQL = ("SELECT dispatch_id, open_items_created, open_items_resolved FROM dispatch_metadata "
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


def _score(value: object) -> bool:
    """A readable CQS: a finite int or float (not a bool) of 0 or more. Text, None and negatives are not."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and value == value
            and abs(value) != float("inf") and value >= 0)


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


def _adoption(weeks: List[dict]) -> tuple:
    """(first week, last week, rise) of the first and last evaluable week, or (None, reason). Evaluable means
    >= MIN_WEEK_OFFERS offers with an outcome (week_stats): both denominators below are > 0."""
    good = [w for w in weeks if w["evaluable"]]
    if len(good) < 2:
        return None, f"fewer than 2 evaluable weeks ({len(good)} of {len(weeks)})"
    first, last = good[0], good[-1]
    rise = Fraction(last["used"], last["with_outcome"]) - Fraction(first["used"], first["with_outcome"])
    return (first["week"], last["week"], rise), None


def f1(weeks: List[dict], verdict: Optional[str], no_verdict: str) -> Measure:
    data = {"weeks": weeks, "rule": _rule()}
    found, why = _adoption(weeks)
    if found is None:
        return Measure("F1", NOT_EVALUABLE, why, data)
    first, last, rise = found
    data.update(first_week=first, last_week=last, rise_pp=_pct(rise))
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


def f2(adoption: tuple, rows: List[tuple]) -> Measure:
    """Dilution stands on the adoption rise alone: no verdict, F1's or any other, is read here."""
    first, last, rise = adoption
    per_week = _chars_per_item(rows, [first, last])
    if per_week is None:
        return Measure("F2", NOT_EVALUABLE, "an injection row has an unreadable payload_chars or items_injected", {})
    start, end = per_week[first], per_week[last]
    if start is None or end is None:
        return Measure("F2", NOT_EVALUABLE, "no injected items in the first or the last evaluable week", {})
    if start == 0:  # guards the division below
        return Measure("F2", NOT_EVALUABLE, "zero characters per item in the first evaluable week", {})
    drop = (start - end) / start
    data = {"first_week": first, "last_week": last, "chars_per_item_first": round(float(start), 2),
            "chars_per_item_last": round(float(end), 2), "drop_pct": _pct(drop)}
    return Measure("F2", FIRED if rise >= ADOPTION_RISE and drop >= DILUTION_DROP else CLEAR, None, data)


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


def _resolved_ids(cohort: co.Cohort) -> Dict[str, List[str]]:
    """The resolved cohort dispatches per arm (mature success or failure): the denominator of the coverage."""
    return {arm: sorted(d.dispatch_id for d in cohort.dispatches
                        if d.arm == arm and d.mature and d.outcome in (co.SUCCESS, co.FAILURE)) for arm in co.ARMS}


def _metadata_values(qi: sqlite3.Connection, sql: str, project_id: str, ids: List[str], readable) -> Dict[str, tuple]:
    """dispatch_id -> its readable values. A dispatch with no row, an unreadable value or more than one row is
    absent: it stays in the coverage denominator and out of the mean."""
    seen: Dict[str, Optional[tuple]] = {}
    for did, *values in _rows(qi, sql, project_id, ids):
        seen[did] = None if did in seen or not all(readable(v) for v in values) else tuple(values)
    return {did: values for did, values in seen.items() if values is not None}


def _per_arm(name: str, qi: sqlite3.Connection, sql: str, project_id: str, ids: Dict[str, List[str]], mean) -> Measure:
    """Mean per arm over the dispatches with a readable value, evaluated only at >= MIN_META_COVERAGE in both arms."""
    arms, enough = {}, True
    for arm, arm_ids in ids.items():
        got = _metadata_values(qi, sql, project_id, arm_ids, _score if name == "cqs" else _whole)
        coverage = Fraction(len(got), len(arm_ids)) if arm_ids else None
        enough = enough and coverage is not None and coverage >= MIN_META_COVERAGE
        arms[arm] = {"resolved": len(arm_ids), "with_value": len(got), "means": None,
                     "coverage_pct": None if coverage is None else _pct(coverage), "_values": list(got.values())}
    pcts = {arm: a["coverage_pct"] for arm, a in arms.items()}
    if enough:
        for a in arms.values():
            a["means"] = mean(a["_values"])
    for a in arms.values():
        del a["_values"]
    if not enough:
        none = all(a["with_value"] == 0 for a in arms.values())
        why = ("dekking 0 %" if none else "dekking " + ", ".join(f"{arm} {'-' if p is None else f'{p:g}'} %" for arm, p in pcts.items())
               ) + f" (minimum {_pct(MIN_META_COVERAGE):g} % in both arms)"
        return Measure(name, NOT_EVALUABLE, why, {"arms": arms})
    return Measure(name, EVALUATED, None, {"arms": arms})


def _mean_cqs(values: List[tuple]) -> float:
    return round(sum(v[0] for v in values) / len(values), 4)  # enough coverage means >= 1 value


def _mean_oi(values: List[tuple]) -> dict:
    return {"created": round(sum(v[0] for v in values) / len(values), 4),
            "resolved": round(sum(v[1] for v in values) / len(values), 4)}


def metadata_measures(qi: sqlite3.Connection, project_id: str, cohort: co.Cohort, refused: Optional[str]) -> List[Measure]:
    """cqs and open_items, one guard each: a missing table or column makes only that measure not_evaluable."""
    if refused is not None:
        return [Measure(name, NOT_EVALUABLE, refused, {}) for name in ("cqs", "open_items")]
    ids = _resolved_ids(cohort)
    out = []
    for name, sql, mean in (("cqs", _CQS_SQL, _mean_cqs), ("open_items", _OI_SQL, _mean_oi)):
        got, err = _guard(lambda: _per_arm(name, qi, sql, project_id, ids, mean))
        out.append(got or Measure(name, NOT_EVALUABLE, err, {}))
    return out


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
        if other or weeks is None:
            why = refused if other else weeks_err
            line1, adoption = Measure("F1", NOT_EVALUABLE, why), None
        else:
            line1 = f1(weeks, verdict, no_verdict)
            adoption, why = _adoption(weeks)
        rows, rows_err = ([], None) if adoption is None else _guard(lambda: _payload_rows(rc, project_id, week_of))
        meta = metadata_measures(qi, project_id, cohort, refused if other else None)
    line2 = Measure("F2", NOT_EVALUABLE, why or rows_err, {}) if adoption is None or rows_err else f2(adoption, rows)
    return Secondary(effect, [line1, line2, f3(effect), *meta])


def as_dict(sec: Secondary) -> dict:
    cohort = sec.effect.cohort
    return {"project_id": cohort.project_id, "as_of": cohort.as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_id": (cohort.window or {}).get("window_id"),
            "measures": {m.name: {"status": m.status, "reason": m.reason, **m.data} for m in sec.measures},
            "effect_exit_code": sec.effect.exit_code}


def _shown(value: object, absent: str = "-", spec: str = "") -> str:
    """The value as text (through `spec`), or `absent` for None: plain text never formats an absent value."""
    return absent if value is None else format(value, spec)


def render(sec: Secondary) -> str:
    data = as_dict(sec)
    out = [f"# Intelligence A/B secondary measures - project {data['project_id']}, window {data['window_id'] or '-'}, "
           f"as_of {data['as_of']}", "#   reported lines only: they never change the verdict or the exit code of effect", ""]
    for m in sec.measures:
        out.append(f"{m.name:11} {m.status}" + (f"  ({m.reason})" if m.reason else ""))
        out += [f"#   week {w['week']}: offers {w['offers']}, with outcome {w['with_outcome']} "
                f"({_shown(w['coverage_pct'], spec='g')}% coverage), adoption {_shown(w['adoption_pct'], 'n/a')}"
                for w in m.data.get("weeks", [])]
        out += [f"#   {arm}: resolved {a['resolved']}, with value {a['with_value']} ({_shown(a['coverage_pct'], spec='g')}%), "
                f"means {_shown(a['means'])}" for arm, a in m.data.get("arms", {}).items()]
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
