"""The 14-day effect re-measurement of an applied suggested edit (ochtendronde D3d, ADR-095 decision 13).

Outcome codes written by code (ADR-095 names them in Dutch): ``kept`` (behouden), ``revert_proposed``
(terugdraaien voorgesteld), ``unmeasurable`` (niet meetbaar). Unknown is never 0 (ADR-063): a missing, malformed or
unknown input is ``unmeasurable``, never ``kept``.

Two quantities are measured over the window after ``applied_at``, each against the 14 days before it (lower is
better): the distinct dispatches that showed the pattern (the baseline's own quantity, ``count_dispatches_in_window``)
and the codex FAIL rounds of ``review_gates/rounds.ndjson``. Both better or equal -> ``kept``; both worse ->
``revert_proposed``; one each, or either unreadable -> ``unmeasurable`` with both values in the entry.
Each outcome opens one inbox point (source 4, ref ``effect:<dispatch id>``) before the outcome is written.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import operator_queue as oq
import review_rounds as rr
import suggested_edit_dispatch as sed

BRON_ANALYZER = 4
EFFECT_PREFIX = "effect:"
KEPT, REVERT, UNMEASURABLE = "kept", "revert_proposed", "unmeasurable"
_UTC = timezone.utc


def _iso(ts: datetime) -> str:
    return ts.astimezone(_UTC).isoformat().replace("+00:00", "Z")


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def rework_counts(rounds_path: Path, project_id: str, applied_at: datetime, end: datetime) -> dict:
    """Codex FAIL rounds before and after ``applied_at``: ``{status, before, after, reason}``; unknown is never 0."""
    def unknown(reason: str) -> dict:
        return {"status": "unknown", "before": None, "after": None, "reason": reason}

    try:
        ledger = rr.read_ledger(rounds_path)
    except rr.RoundLedgerError as exc:
        return unknown(f"rounds ledger unreadable: {exc}")
    if not ledger.exists:
        return unknown("rounds ledger not found")
    if ledger.bad:
        return unknown(f"rounds ledger holds {len(ledger.bad)} unparsable line(s)")
    start = applied_at - timedelta(days=sed.REMEASURE_DAYS)
    before = after = 0
    for line in rr.codex_rounds(l for l in ledger.lines if l.project_id == project_id):
        ts = sed._parse_ts(line.recorded_at)
        if ts is None:
            return unknown(f"recorded_at unparsable: {line.recorded_at!r}")
        if line.verdict == rr.VERDICT_FAIL:
            if start <= ts < applied_at:
                before += 1
            elif applied_at <= ts < end:
                after += 1
    return {"status": "ok", "before": before, "after": after, "reason": None}


def decide(entry: dict, project_id: str, db_path: Path, ledger: Path, rounds_path: Path) -> dict:
    """The outcome fields for one applied entry: ``outcome``, ``reason``, ``measured`` and ``rework``."""
    applied_at, end = sed._parse_ts(entry.get("applied_at")), sed._parse_ts(entry.get("remeasure_at"))
    base = entry.get("baseline")
    base_count = _count(base.get("count")) if isinstance(base, dict) and base.get("status") == "ok" else None
    measured = rework = None

    def result(outcome: str, reason: Optional[str]) -> dict:
        return {"outcome": outcome, "reason": reason, "measured": measured, "rework": rework}

    if applied_at is None or end is None:
        return result(UNMEASURABLE, "applied_at or remeasure_at missing or unparsable")
    if base_count is None:
        return result(UNMEASURABLE, "baseline unknown")
    measured = sed.count_dispatches_in_window(db_path, project_id, sed.edit_source_text(entry), applied_at, end, ledger)
    after = _count(measured.get("count")) if measured.get("status") == "ok" else None
    if after is None:
        return result(UNMEASURABLE, f"after-window unknown: {measured.get('reason')}")
    rework = rework_counts(rounds_path, project_id, applied_at, end)
    r_before, r_after = _count(rework.get("before")), _count(rework.get("after"))
    if rework["status"] != "ok" or r_before is None or r_after is None:
        return result(UNMEASURABLE, f"rework unknown: {rework['reason']}")
    patterns_worse, rework_worse = after > base_count, r_after > r_before
    if patterns_worse != rework_worse:
        return result(UNMEASURABLE, f"the two measures disagree: patterns {base_count}->{after}, rework {r_before}->{r_after}")
    return result(REVERT if patterns_worse else KEPT, None)


def open_effect_point(project_id: str, entry: dict, fields: dict, now: datetime,
                      queue_state_dir: Optional[Path] = None) -> bool:
    """One point per entry (the point id is fixed by project, source and ref): True when opened, False when it exists.

    Raises ``operator_queue.QueueRefused`` or ``QueueUnreadable`` when the inbox cannot take it."""
    detail = fields["reason"] or f"patterns {entry['baseline'].get('count')}->{fields['measured']['count']}"
    question = f"Edit #{entry.get('id')} {entry.get('target')}: effect after 14 days is {fields['outcome']} ({detail})"
    line = oq.open_point(project_id, bron=BRON_ANALYZER, ref=EFFECT_PREFIX + str(entry["dispatch_id"]),
                         vraag=question[:200], sluit_bij="operator decides to keep or revert the edit",
                         opened_by="effect_remeasure", now=_iso(now), state_dir=queue_state_dir)
    return line is not None


def remeasure(history: list, project_id: str, db_path: Path, ledger: Path, rounds_path: Path, now: datetime,
              queue_state_dir: Optional[Path] = None,
              save: Optional[Callable[[list], None]] = None) -> Dict[str, Any]:
    """Measure every applied entry due and without an outcome; ``save`` runs after each entry that got one.

    An entry whose point cannot be opened keeps no outcome and is retried by the next run."""
    done, problems = [], []
    for entry in history:
        if not isinstance(entry, dict) or entry.get("status") != "applied" or "outcome" in entry:
            continue
        due = sed._parse_ts(entry.get("remeasure_at"))
        if due is not None and due > now:
            continue
        if not isinstance(entry.get("dispatch_id"), str) or not entry["dispatch_id"].strip():
            problems.append(f"#{entry.get('id')}: no dispatch_id to key the point on")
            continue
        fields = decide(entry, project_id, db_path, ledger, rounds_path)
        try:
            open_effect_point(project_id, entry, fields, now, queue_state_dir)
        except (oq.QueueRefused, oq.QueueUnreadable) as exc:
            problems.append(f"#{entry.get('id')}: inbox point not opened: {exc}")
            continue
        entry.update(outcome=fields["outcome"], outcome_reason=fields["reason"], measured_at=_iso(now),
                     measured=fields["measured"], rework=fields["rework"])
        if save is not None:
            save(history)
        done.append((entry.get("id"), fields["outcome"]))
    return {"done": done, "problems": problems}
