"""The pure verdict tests (doorloop PR-12b, split 1 of 2; decision 26).

``judge`` builds its day documents in memory; ``append_verdict`` and
``blocked_keys`` run against a real ``proposals.ndjson`` under ``tmp_path``.
``unknown`` never reads as ``not_helped``. The ``verdict`` command and its CLI
tests land in split 2 on top of this; on ``origin/main`` this module fails to
import because ``daily_metrics.verdict`` does not exist there yet.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from daily_metrics import proposal_ledger, verdict

PID = "aaaa11112222"
OTHER = "bbbb33334444"
METRIC = "pr_open_to_merge_median_hours"
CI = "ci_duration_seconds_median"
A = date(2026, 11, 1)
#: A metric day a verdict can be stamped with; derived from ``A``, never guessed.
D = A + timedelta(days=8)
WINDOW = [A + timedelta(days=offset) for offset in range(1, 8)]
#: Sentinel: the field is left out of the document entirely, not set to ``None``.
OMIT = object()


def _doc(value, *, metric=METRIC, n=8, schema=1, status="final"):
    record = {"kind": "scalar", "value": value, "source": "t", "sample_size": n,
              "details": {"samples": [value] * n}}
    document = {"status": status, "metrics": {metric: record}}
    if schema is not OMIT:
        document["schema"] = schema
    return document


def _approved(frozen=50.0, *, metric=METRIC, schema=1, proposal_id=PID, day=A):
    event = {"event": "approved", "proposal_id": proposal_id, "project_id": "p",
             "key": metric, "metric_id": metric, "seat": None,
             "approval_day": day.isoformat(), "approved_at": f"{day.isoformat()}T09:00:00Z",
             "approved_by": "operator", "evidence": {"tty": True},
             "frozen_baseline": {"value": frozen, "sample_size": 40, "unknown_reason": None}}
    if schema is not OMIT:
        event["schema"] = schema
    return event


def _post(value=45.0, **kwargs):
    return {day: _doc(value, **kwargs) for day in WINDOW}


def _ledger(tmp_path):
    return tmp_path / "store" / "proposals.ndjson"


def _events(tmp_path):
    path = _ledger(tmp_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _seed_ledger(tmp_path, *approved):
    rows = []
    for event in approved:
        rows.append({"event": "proposed", "proposal_id": event["proposal_id"],
                     "project_id": "p", "key": event["key"], "metric_id": event["metric_id"],
                     "seat": None, "metric_day": "2026-10-31", "value": 60.0})
        rows.append(event)
    path = _ledger(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                    encoding="utf-8")


@pytest.mark.parametrize(("frozen", "post", "reason"), [
    (None, _post(12.0), "baseline_unknown"),
    (50.0, {}, "post_value_unknown"),
    (50.0, {day: _doc(45.0) for day in WINDOW[:4]}, "too_few_final_days"),
    (50.0, {day: _doc(45.0, schema=2 if day == WINDOW[3] else 1) for day in WINDOW},
     "schema_changed"),
    (50.0, {day: _doc(45.0, n=1) for day in WINDOW}, "sample_too_small"),
])
def test_no_undetermined_case_reads_as_not_helped(frozen, post, reason):
    result = verdict.judge(_approved(frozen), post)
    assert result["verdict"] == "undetermined"
    assert result["reason"] == reason
    assert result["improvement"] is None


@pytest.mark.parametrize(("approved_schema", "post_schema"), [
    (OMIT, OMIT),  # neither side recorded one: 50 -> 45 would otherwise be helped
    (None, None),  # neither side recorded one, explicitly null
    (1, OMIT),     # the post days lost it while the approval kept 1
    (OMIT, 1),     # the approval lost it while the post days kept 1
    (None, 1),
    (1, None),
])
def test_a_missing_schema_is_undetermined_and_never_compares_absent_to_absent(
        approved_schema, post_schema):
    """Decision 26: no recorded schema is not evidence the schema stayed the same."""
    result = verdict.judge(_approved(schema=approved_schema), _post(45.0, schema=post_schema))
    assert result["verdict"] == "undetermined"
    assert result["reason"] == "schema_missing"
    assert result["improvement"] is None


def test_one_final_post_day_without_schema_already_leaves_the_verdict_undetermined():
    post = _post(45.0)
    post[WINDOW[3]] = _doc(45.0, schema=OMIT)
    result = verdict.judge(_approved(), post)
    assert result["verdict"] == "undetermined"
    assert result["reason"] == "schema_missing"
    assert result["improvement"] is None


@pytest.mark.parametrize(("frozen", "metric", "post", "outcome", "improvement"), [
    (50.0, METRIC, 45.0, "helped", 5.0),       # exactly ten percent, floor 0.5 met
    (1000.0, CI, 900.0, "not_helped", 100.0),  # ten percent, absolute floor 120 missed
    (50.0, METRIC, 55.0, "not_helped", -5.0),  # a worsening
])
def test_enough_data_decides_by_the_ten_percent_and_the_absolute_floor(
        frozen, metric, post, outcome, improvement):
    result = verdict.judge(_approved(frozen, metric=metric), _post(post, metric=metric))
    assert (result["verdict"], result["improvement"]) == (outcome, improvement)


def test_a_written_undetermined_verdict_silences_its_key_for_fourteen_days(tmp_path):
    """Decision 26: the written event, not a hand-built row, blocks its key."""
    _seed_ledger(tmp_path, _approved(), _approved(proposal_id=OTHER, metric=CI))
    path = _ledger(tmp_path)
    proposal_ledger.append_verdict(path, {
        "event": "verdict", "proposal_id": PID, "project_id": "p", "key": METRIC,
        "metric_id": METRIC, "metric_day": D.isoformat(), "verdict": "undetermined",
        "reason": "baseline_unknown"})
    proposal_ledger.append_verdict(path, {
        "event": "verdict", "proposal_id": OTHER, "project_id": "p", "key": CI,
        "metric_id": CI, "metric_day": D.isoformat(), "verdict": "helped",
        "reason": None})
    events = _events(tmp_path)
    last_blocked = D + timedelta(days=proposal_ledger.BLOCK_DAYS)
    first_free = D + timedelta(days=proposal_ledger.BLOCK_DAYS + 1)
    assert METRIC in proposal_ledger.blocked_keys(events, last_blocked)
    assert METRIC not in proposal_ledger.blocked_keys(events, first_free)
    assert CI not in proposal_ledger.blocked_keys(events, last_blocked)
    assert proposal_ledger.open_keys(events) == set()


def test_append_verdict_refuses_not_approved_and_already_judged(tmp_path):
    path = _ledger(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    proposed = {"event": "proposed", "proposal_id": PID, "key": METRIC}
    path.write_text(json.dumps(proposed) + "\n", encoding="utf-8")
    event = {"event": "verdict", "proposal_id": PID, "key": METRIC, "verdict": "helped"}

    def refused(payload):
        before = path.read_bytes()
        with pytest.raises(proposal_ledger.ApprovalRefused) as info:
            proposal_ledger.append_verdict(path, payload)
        assert path.read_bytes() == before
        return info.value.reason

    assert refused(event) == "not_approved"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "approved", "proposal_id": PID}) + "\n")
    proposal_ledger.append_verdict(path, event)
    assert refused(event) == "already_judged"
