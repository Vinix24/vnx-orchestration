"""The verdict tests (doorloop PR-12b, decisions 14 and 26).

The ``judge`` cases build their day documents in memory; the command cases run
the real CLI through ``subprocess`` under ``tmp_path`` with a fixed ``--now``.
``unknown`` never reads as ``not_helped``. On ``origin/main`` the first test
fails: ``verdict`` is not a command yet.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from daily_metrics import proposal_ledger, verdict
from daily_metrics.store import DayStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "daily_metrics.py"
COMMANDS = REPO_ROOT / "scripts" / "lib" / "daily_metrics" / "commands"
PID = "aaaa11112222"
OTHER = "bbbb33334444"
METRIC = "pr_open_to_merge_median_hours"
CI = "ci_duration_seconds_median"
A = date(2026, 11, 1)
NOW = "2026-11-09T09:00:00Z"      # a+7 = 11-08 is settled at 11-09T02:00Z
BEFORE = "2026-11-09T00:00:00Z"   # a+7 is not settled
WINDOW = [A + timedelta(days=offset) for offset in range(1, 8)]


def _doc(value, *, metric=METRIC, n=8, schema=1, status="final"):
    record = {"kind": "scalar", "value": value, "source": "t", "sample_size": n,
              "details": {"samples": [value] * n}}
    return {"schema": schema, "status": status, "metrics": {metric: record}}


def _approved(frozen=50.0, *, metric=METRIC, schema=1, proposal_id=PID, day=A):
    return {"event": "approved", "proposal_id": proposal_id, "project_id": "p",
            "key": metric, "metric_id": metric, "seat": None,
            "approval_day": day.isoformat(), "approved_at": f"{day.isoformat()}T09:00:00Z",
            "approved_by": "operator", "evidence": {"tty": True},
            "frozen_baseline": {"value": frozen, "sample_size": 40, "unknown_reason": None},
            "schema": schema}


def _post(value=45.0, **kwargs):
    return {day: _doc(value, **kwargs) for day in WINDOW}


def _run(tmp_path, args, commands=None):
    env = dict(os.environ)
    env.pop("VNX_PROJECT_ID", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    env["VNX_DAILY_METRICS_SOURCES_DIR"] = str(tmp_path / "sources")
    env["VNX_DAILY_METRICS_COMMANDS_DIR"] = str(commands or COMMANDS)
    return subprocess.run([sys.executable, str(CLI), *args], env=env,
                          capture_output=True, text=True, cwd=str(REPO_ROOT))


def _args(tmp_path, now=NOW):
    return ["--store", str(tmp_path / "store"), "--state-dir", str(tmp_path / "state"),
            "--project-id", "p", "--now", now]


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


def _seed_post(tmp_path, docs=None):
    store = DayStore(tmp_path / "store", "p")
    for day, doc in (docs or _post()).items():
        store.write_final(day.isoformat(), doc)


def test_verdict_help_exits_zero(tmp_path):
    """Red run: exits 2 on origin/main, where ``verdict`` is not a command."""
    proc = _run(tmp_path, ["verdict", "--help"])
    assert proc.returncode == 0, proc.stderr
    assert "--store" in proc.stdout


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


@pytest.mark.parametrize(("frozen", "metric", "post", "outcome", "improvement"), [
    (50.0, METRIC, 45.0, "helped", 5.0),       # exactly ten percent, floor 0.5 met
    (1000.0, CI, 900.0, "not_helped", 100.0),  # ten percent, absolute floor 120 missed
    (50.0, METRIC, 55.0, "not_helped", -5.0),  # a worsening
])
def test_enough_data_decides_by_the_ten_percent_and_the_absolute_floor(
        frozen, metric, post, outcome, improvement):
    result = verdict.judge(_approved(frozen, metric=metric), _post(post, metric=metric))
    assert (result["verdict"], result["improvement"]) == (outcome, improvement)



def test_a_post_window_that_is_not_settled_exits_two_and_writes_nothing(tmp_path):
    _seed_post(tmp_path)
    _seed_ledger(tmp_path, _approved())
    before = _ledger(tmp_path).read_bytes()
    proc = _run(tmp_path, ["verdict", PID, *_args(tmp_path, BEFORE)])
    assert proc.returncode == 2
    assert len(proc.stderr.strip().splitlines()) == 1
    assert _ledger(tmp_path).read_bytes() == before


def test_the_command_writes_the_full_verdict_event(tmp_path):
    _seed_post(tmp_path)
    _seed_ledger(tmp_path, _approved())
    proc = _run(tmp_path, ["verdict", PID, *_args(tmp_path)])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"judged": [PID], "waiting": []}
    (event,) = [event for event in _events(tmp_path) if event["event"] == "verdict"]
    assert (event["proposal_id"], event["key"], event["metric_id"], event["seat"]) == (
        PID, METRIC, METRIC, None)
    assert (event["metric_day"], event["approval_day"]) == ("2026-11-08", A.isoformat())
    assert event["window"] == {"from": "2026-11-02", "to": "2026-11-08"}
    assert (event["final_days"], event["post_value"], event["post_n"]) == (7, 45.0, 56)
    assert (event["frozen_baseline"], event["improvement"]) == (50.0, 5.0)
    assert (event["judged_at"], event["days_after_approval"]) == (NOW, 8)


def test_the_frozen_baseline_survives_rewritten_pre_approval_days(tmp_path):
    store = DayStore(tmp_path / "store", "p")
    pre = [A - timedelta(days=offset) for offset in range(1, 8)]
    for day in pre:
        store.write_final(day.isoformat(), _doc(50.0))
    _seed_post(tmp_path)
    _seed_ledger(tmp_path, _approved(50.0))
    # After the approval the days before a are rewritten to a baseline of 44.
    for day in pre:
        (tmp_path / "store" / f"{day.isoformat()}.json").write_text(
            json.dumps(_doc(44.0)), encoding="utf-8")
    proc = _run(tmp_path, ["verdict", PID, *_args(tmp_path)])
    assert proc.returncode == 0, proc.stderr
    (event,) = [event for event in _events(tmp_path) if event["event"] == "verdict"]
    assert event["frozen_baseline"] == 50.0
    assert event["improvement"] == 5.0
    assert event["verdict"] == "helped"


def test_daily_judges_the_due_approval_and_leaves_the_waiting_one(tmp_path):
    _seed_post(tmp_path)
    _seed_ledger(tmp_path, _approved(), _approved(proposal_id=OTHER, day=date(2026, 11, 8)))
    commands = tmp_path / "commands"
    commands.mkdir()
    for name in ("propose.py", "verdict.py"):
        shutil.copy(COMMANDS / name, commands / name)
    proc = _run(tmp_path, ["daily", *_args(tmp_path)], commands=commands)
    assert proc.returncode == 0, proc.stderr
    payload = next(line for line in map(json.loads, proc.stdout.splitlines()) if "judged" in line)
    assert payload == {"judged": [PID], "waiting": [OTHER]}
    assert [event["proposal_id"] for event in _events(tmp_path)
            if event["event"] == "verdict"] == [PID]
    before = _ledger(tmp_path).read_bytes()
    second = _run(tmp_path, ["daily", *_args(tmp_path)], commands=commands)
    assert second.returncode == 0, second.stderr
    assert _ledger(tmp_path).read_bytes() == before


def test_an_undetermined_verdict_blocks_the_key_for_fourteen_days(tmp_path):
    _seed_post(tmp_path)
    _seed_ledger(tmp_path, _approved(frozen=None))
    assert _run(tmp_path, ["verdict", PID, *_args(tmp_path)]).returncode == 0
    events = _events(tmp_path)
    (event,) = [event for event in events if event["event"] == "verdict"]
    assert (event["verdict"], event["reason"]) == ("undetermined", "baseline_unknown")
    day = date.fromisoformat(event["metric_day"])
    assert event["key"] in proposal_ledger.blocked_keys(events, day + timedelta(days=10))
    assert event["key"] not in proposal_ledger.blocked_keys(events, day + timedelta(days=15))
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
