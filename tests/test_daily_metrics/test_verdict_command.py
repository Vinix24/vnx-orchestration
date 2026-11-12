"""The ``verdict`` command tests (doorloop PR-12b split 2, decisions 14 and 26).

Every run goes through ``subprocess`` with ``--store``, ``--state-dir``,
``VNX_DAILY_METRICS_SOURCES_DIR`` and ``VNX_DAILY_METRICS_COMMANDS_DIR`` pinned
under ``tmp_path`` and the clock fixed through ``--now``. The day files are
written by the test through the real ``DayStore``; nothing touches the real
store. The pure ``judge`` cases live in ``test_verdict.py``. On ``origin/main``
the first test fails: ``verdict`` is not a command there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from daily_metrics import proposal_ledger
from daily_metrics.store import DayStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "daily_metrics.py"
COMMANDS = REPO_ROOT / "scripts" / "lib" / "daily_metrics" / "commands"
PID = "aaaa11112222"
OTHER = "bbbb33334444"
METRIC = "pr_open_to_merge_median_hours"
A = date(2026, 11, 1)
NOW = "2026-11-09T09:00:00Z"      # a+7 = 11-08 is settled at 11-09T02:00Z
BEFORE = "2026-11-09T00:00:00Z"   # a+7 is not settled
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


def test_a_post_day_without_a_recorded_schema_is_written_as_schema_missing(tmp_path):
    """Decision 26: a schema nobody recorded never becomes a definitive verdict."""
    _seed_post(tmp_path, {day: _doc(45.0, schema=OMIT) for day in WINDOW})
    _seed_ledger(tmp_path, _approved())
    proc = _run(tmp_path, ["verdict", PID, *_args(tmp_path)])
    assert proc.returncode == 0, proc.stderr
    (event,) = [event for event in _events(tmp_path) if event["event"] == "verdict"]
    assert (event["verdict"], event["reason"]) == ("undetermined", "schema_missing")
    assert event["improvement"] is None
