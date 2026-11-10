#!/usr/bin/env python3
"""The intelligence A/B cohort: outcome classes, dedup, the cohort controls and the hard floor.

Effect PR-6 of ``intelligence-effect-measurable``, plan decisions 5, 6, 7, 14 and 17. It counts and
refuses; it never computes an effect, a success rate or a verdict (that is PR-7). It reads only
``runtime_coordination.db`` (decision 4), read-only; every query filters ``project_id = ?`` and joins
on ``(dispatch_id, project_id)`` (decision 17, ADR-007). Decision 7, verbatim:

HARDE GRENS: een verdict bestaat alleen bij n ≥ 200 opgeloste dispatches per arm. De grens is de
constante `MIN_PER_ARM = 200` in de code en is door geen vlag, omgevingsvariabele of
configuratierij te verlagen. De deadline 01-12-2026 verlengt het venster en houdt de track open.
Onder de grens is de uitkomst altijd `geen verdict` (exit 3, reden `below_floor`), nooit een
verdict bij lagere n.

Split 1 of 2. Split 2 adds the re-derivation of every stamp (``assignment_not_reproducible``), the
72-hour maturity, differential attrition and the coverage line. Until then ``resolved`` counts every
success and failure, immature ones included.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config_store_db import read_config
from coordination_db import DISPATCH_STATES, TERMINAL_DISPATCH_STATES
from intelligence_selector import load_window_config

MIN_PER_ARM = 200
WINDOW_KEY = "VNX_INTEL_AB_WINDOW"
ARMS = ("control", "treatment")
SUCCESS, FAILURE, EXCLUDED, UNRESOLVED, UNKNOWN = "success", "failure", "excluded", "unresolved", "unknown"
COUNT_KEYS = ("resolved", UNRESOLVED, EXCLUDED, UNKNOWN)

# Decision 5. Each class is cut with the imported sets: a state coordination_db adds, drops or
# moves between terminal and open lands in no class here and reads unknown until classified.
_OPEN_STATES = DISPATCH_STATES - TERMINAL_DISPATCH_STATES
CLASS_STATES: Dict[str, frozenset] = {
    SUCCESS: frozenset({"completed"}) & TERMINAL_DISPATCH_STATES,
    FAILURE: frozenset({"expired", "dead_letter"}) & TERMINAL_DISPATCH_STATES,
    EXCLUDED: frozenset({"withdrawn"}) & TERMINAL_DISPATCH_STATES,
    UNRESOLVED: frozenset({"proposed", "ready", "queued", "claimed", "delivering", "accepted",
                           "running", "timed_out", "failed_delivery", "recovered"}) & _OPEN_STATES,
}

_INJECTIONS_SQL = """
SELECT i.id, i.dispatch_id, i.injected_at, i.task_class, i.ab_arm, i.ab_window, d.state AS state
FROM intelligence_injections i
LEFT JOIN dispatches d ON d.dispatch_id = i.dispatch_id AND d.project_id = i.project_id
WHERE i.project_id = ?
"""


def parse_utc(value: object) -> Optional[datetime]:
    """An aware UTC datetime, or None for anything that is not an ISO timestamp (naive reads UTC)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def floor_rule() -> str:
    """Decision 7 as it stands in this module's docstring, on one line: the one copy in code."""
    doc = " ".join((__doc__ or "").split())
    start = doc.index("HARDE GRENS:")
    return doc[start:doc.index("lagere n.", start) + len("lagere n.")]


def classify_state(state: object) -> str:
    """Decision 5: outside DISPATCH_STATES, NULL or in no class here is unknown, never an outcome."""
    if not isinstance(state, str) or state not in DISPATCH_STATES:
        return UNKNOWN
    return next((cls for cls, states in CLASS_STATES.items() if state in states), UNKNOWN)


@dataclass(frozen=True)
class CohortDispatch:
    """One cohort dispatch, read from its first injection row (decision 6)."""
    dispatch_id: str
    arm: str
    task_class: str
    outcome: str


@dataclass
class Cohort:
    project_id: str
    as_of: datetime
    window: Optional[dict] = None
    dispatches: List[CohortDispatch] = field(default_factory=list)
    deduplicated: int = 0
    refusals: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 3 if self.refusals else 0

    @property
    def reasons(self) -> List[str]:
        return [reason for reason, _ in self.refusals]

    def refuse(self, reason: str, detail: str) -> None:
        self.refusals.append((reason, detail))

    def arms(self) -> Dict[str, Dict[str, int]]:
        """Per arm: resolved (success + failure, this is n), unresolved, excluded, unknown."""
        counts = {arm: dict.fromkeys(COUNT_KEYS, 0) for arm in ARMS}
        for d in self.dispatches:
            counts[d.arm]["resolved" if d.outcome in (SUCCESS, FAILURE) else d.outcome] += 1
        return counts


def open_read_only(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_cohort(rc_db: Path, project_id: str, as_of: datetime) -> Cohort:
    """Read the project's window, injection rows and states from ``rc_db``; apply every control."""
    cohort = Cohort(project_id=project_id, as_of=as_of)
    with closing(open_read_only(rc_db)) as conn:
        raw = read_config(conn, project_id, WINDOW_KEY)
        cohort.window = load_window_config(raw) if raw is not None else None
        if cohort.window is None:
            cohort.refuse("window_invalid", f"{WINDOW_KEY} for {project_id} is missing or invalid: {raw!r}")
            return cohort
        rows = [dict(r) for r in conn.execute(_INJECTIONS_SQL, (project_id,))]
    _fill(cohort, rows)
    _check_outcomes(cohort)
    return cohort


def _fill(cohort: Cohort, rows: List[dict]) -> None:
    """Decision 14 (the stamped rows inside [opened_utc, as_of]) and decision 6 (the first row)."""
    opened = parse_utc(cohort.window["opened_utc"])
    for row in rows:
        row["at"] = parse_utc(row["injected_at"])
        row["stamp"] = row["ab_window"] if isinstance(row["ab_window"], str) else ""
    malformed = [row["id"] for row in rows if row["at"] is None]
    if malformed:
        cohort.refuse("malformed_timestamp", f"{len(malformed)} row(s), ids {malformed[:5]}: injected_at unreadable")
    rows = [row for row in rows if row["at"] is not None]
    inside = [row for row in rows if opened <= row["at"] <= cohort.as_of]
    unstamped = [row["dispatch_id"] for row in inside if not row["stamp"].strip()]
    if unstamped:
        cohort.refuse("unstamped_after_open", f"{len(unstamped)} row(s) without ab_window, first {unstamped[0]}")
    members = [row for row in inside if row["stamp"].startswith(f"{cohort.window['window_id']}:")]
    _check_rows(cohort, members)
    first = _first_rows(rows)
    member_ids = {row["id"] for row in members}
    counted = [first[did] for did in sorted({row["dispatch_id"] for row in members}) if first[did]["id"] in member_ids]
    counted_ids = {row["dispatch_id"] for row in counted}
    cohort.deduplicated = sum(1 for row in members if row["dispatch_id"] in counted_ids) - len(counted)
    cohort.dispatches = [  # any other arm is refused as arm_invalid
        CohortDispatch(row["dispatch_id"], row["ab_arm"], row["task_class"], classify_state(row["state"]))
        for row in counted if row["ab_arm"] in ARMS]


def _first_rows(rows: List[dict]) -> Dict[str, dict]:
    """Decision 6: per dispatch the first injection row, min(injected_at), tiebreak min(id)."""
    first: Dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: (r["at"], r["id"])):
        first.setdefault(row["dispatch_id"], row)
    return first


def _check_rows(cohort: Cohort, members: List[dict]) -> None:
    """Decision 14: one configuration and two arms (re-deriving each stamp is split 2)."""
    windows = sorted({row["ab_window"] for row in members})
    if len(windows) > 1:
        cohort.refuse("config_diverged", f"{len(windows)} ab_window values in the cohort: {windows[:5]}")
    invalid = [row["dispatch_id"] for row in members if row["ab_arm"] not in ARMS]
    if invalid:
        cohort.refuse("arm_invalid", f"{len(invalid)} row(s) with an arm outside {ARMS}, first {invalid[0]}")


def _check_outcomes(cohort: Cohort) -> None:
    """Decision 5, then decision 7: a dispatch in no outcome class, then the floor."""
    unknown = [d.dispatch_id for d in cohort.dispatches if d.outcome == UNKNOWN]
    if unknown:
        cohort.refuse("unknown_states", f"{len(unknown)} cohort dispatch(es) in no outcome class, first {unknown[0]}")
    low = [f"{arm} resolved {c['resolved']}" for arm, c in cohort.arms().items() if c["resolved"] < MIN_PER_ARM]
    if low:
        cohort.refuse("below_floor", f"{', '.join(low)}; the floor is {MIN_PER_ARM} resolved per arm")


def as_dict(cohort: Cohort) -> dict:
    return {
        "project_id": cohort.project_id, "as_of": cohort.as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": cohort.window, "min_per_arm": MIN_PER_ARM, "cohort_dispatches": len(cohort.dispatches),
        "deduplicated_rows": cohort.deduplicated, "arms": cohort.arms(),
        "refusals": [{"reason": reason, "detail": detail} for reason, detail in cohort.refusals],
        "floor_rule": floor_rule() if "below_floor" in cohort.reasons else None, "exit_code": cohort.exit_code,
    }


def render(cohort: Cohort) -> str:
    data = as_dict(cohort)
    out = [f"# Intelligence A/B cohort - project {cohort.project_id}, "
           f"window {(cohort.window or {}).get('window_id', '-')}, as_of {data['as_of']}",
           f"#   cohort dispatches {data['cohort_dispatches']}, deduplicated rows {cohort.deduplicated}; "
           f"counts only, never a verdict; floor {MIN_PER_ARM} resolved per arm", ""]
    header = f"{'arm':10}" + "".join(f" {key:>10}" for key in COUNT_KEYS)
    out += [header, "-" * len(header)]
    out += [f"{arm:10}" + "".join(f" {c[key]:>10}" for key in COUNT_KEYS) for arm, c in data["arms"].items()]
    out += [f"REFUSED {r['reason']}: {r['detail']}" for r in data["refusals"]]
    return "\n".join(out + ([data["floor_rule"]] if data["floor_rule"] else []))


def run_cohort(qi_db: Path, rc_db: Path, project_id: str, as_of: datetime, as_json: bool) -> int:
    """CLI body: exit 0 with the per-arm table, 3 with the refusals, 1 when a store is unusable. QI
    is opened read-only to prove the path its siblings read; the cohort reads no table of it (decision 4)."""
    if qi_db.resolve() == rc_db.resolve():
        print(f"cohort: --qi and --rc are the same file: {rc_db}", file=sys.stderr)
        return 1
    try:
        with closing(open_read_only(qi_db)) as conn:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        cohort = build_cohort(rc_db, project_id, as_of)
    except sqlite3.Error as exc:
        print(f"cohort: --qi {qi_db} or --rc {rc_db} is unreadable or not at v38 (migration 0038): {exc}",
              file=sys.stderr)
        return 1
    print(json.dumps(as_dict(cohort), indent=2) if as_json else render(cohort))
    return cohort.exit_code
