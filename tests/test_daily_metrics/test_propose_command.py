"""The ``propose`` command tests (doorloop PR-11c, decisions 4, 14 and 26).

Every run goes through ``subprocess`` with ``--store``, ``--state-dir``,
``VNX_DAILY_METRICS_SOURCES_DIR`` and ``VNX_DAILY_METRICS_COMMANDS_DIR`` pinned
under ``tmp_path`` and the clock fixed through ``--now``. The day files are
written by the test through the real ``DayStore``; nothing touches the real
store. On ``origin/main`` the first test fails: ``propose`` is not a command yet.
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

from daily_metrics.store import DayStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "daily_metrics.py"
COMMANDS = REPO_ROOT / "scripts" / "lib" / "daily_metrics" / "commands"
NOW = "2026-11-08T03:00:00Z"
METRIC_DAY = date(2026, 11, 7)
LEDGER_NAME = "proposals.ndjson"
KEY = "pr_open_to_merge_median_hours"


def _scalar(value: float, n: int = 8) -> dict:
    return {"kind": "scalar", "value": value, "source": "fixture",
            "sample_size": n, "details": {"samples": [value] * n}}


def _doc(value: float = 1.0, n: int = 8) -> dict:
    return {"metrics": {KEY: _scalar(value, n)}}


def _window_days() -> list[date]:
    """The metric day and the seven days before it, oldest first."""
    return [METRIC_DAY - timedelta(days=offset) for offset in range(7, -1, -1)]


def _store(tmp_path: Path) -> DayStore:
    return DayStore(tmp_path / "store", "p")


def _seed_window(tmp_path: Path, newest_value: float = 5.0) -> DayStore:
    """Seven final days at 1.0 and a metric day far above their pooled baseline."""
    store = _store(tmp_path)
    for day in _window_days():
        value = newest_value if day == METRIC_DAY else 1.0
        store.write_final(day.isoformat(), _doc(value))
    return store


def _empty(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(tmp_path: Path, args: list[str], commands: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("VNX_PROJECT_ID", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    env["VNX_DAILY_METRICS_SOURCES_DIR"] = str(_empty(tmp_path / "sources"))
    env["VNX_DAILY_METRICS_COMMANDS_DIR"] = str(commands or COMMANDS)
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        env=env, capture_output=True, text=True, cwd=str(REPO_ROOT),
    )


def _args(tmp_path: Path) -> list[str]:
    return ["--store", str(tmp_path / "store"), "--state-dir", str(tmp_path / "state"),
            "--project-id", "p", "--now", NOW]


def _ledger(tmp_path: Path) -> Path:
    return tmp_path / "store" / LEDGER_NAME


def _events(tmp_path: Path) -> list[dict]:
    path = _ledger(tmp_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_propose_help_exits_zero(tmp_path):
    """Red run: exits 2 on origin/main, where ``propose`` is not a command."""
    proc = _run(tmp_path, ["propose", "--help"])
    assert proc.returncode == 0, proc.stderr
    assert "propose" in proc.stdout
    assert "--store" in proc.stdout


def test_the_newest_final_day_above_baseline_writes_one_proposal(tmp_path):
    _seed_window(tmp_path)
    proc = _run(tmp_path, ["propose", *_args(tmp_path)])
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["metric_day"] == METRIC_DAY.isoformat()
    assert payload["candidates"] == 1
    assert len(payload["proposed"]) == 1
    events = _events(tmp_path)
    assert [event["proposal_id"] for event in events] == payload["proposed"]
    assert events[0]["event"] == "proposed"
    assert events[0]["key"] == KEY
    assert events[0]["metric_day"] == METRIC_DAY.isoformat()
    assert events[0]["project_id"] == "p"


def test_a_second_run_writes_no_new_event(tmp_path):
    _seed_window(tmp_path)
    first = _run(tmp_path, ["propose", *_args(tmp_path)])
    assert first.returncode == 0, first.stderr
    before = _ledger(tmp_path).read_bytes()
    second = _run(tmp_path, ["propose", *_args(tmp_path)])
    assert second.returncode == 0, second.stderr
    payload = json.loads(second.stdout)
    assert payload["proposed"] == []
    assert payload["candidates"] == 1
    assert _ledger(tmp_path).read_bytes() == before


@pytest.mark.parametrize("status", ["incomplete", "missing"])
def test_a_newest_day_that_is_not_final_proposes_nothing(tmp_path, status):
    store = _store(tmp_path)
    for day in _window_days():
        if day != METRIC_DAY:
            store.write_final(day.isoformat(), _doc())
    if status == "incomplete":
        store.write_incomplete(METRIC_DAY.isoformat(), _doc(5.0))
    proc = _run(tmp_path, ["propose", *_args(tmp_path)])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "metric_day": METRIC_DAY.isoformat(), "proposed": [], "reason": "day_not_final"}
    assert not _ledger(tmp_path).exists()


def test_daily_over_a_five_day_catch_up_proposes_only_for_the_newest_settled_day(tmp_path):
    store = _seed_window(tmp_path)
    # One extra settled final day, so exactly five earlier settled days are missing.
    store.write_final((METRIC_DAY - timedelta(days=8)).isoformat(), _doc())
    missing = [METRIC_DAY - timedelta(days=offset) for offset in range(9, 14)]
    commands = _empty(tmp_path / "commands")
    shutil.copy(COMMANDS / "propose.py", commands / "propose.py")
    proc = _run(tmp_path, ["daily", *_args(tmp_path)], commands=commands)
    assert proc.returncode == 0, proc.stderr
    caught_up = [day for day in missing
                 if (tmp_path / "store" / f"{day.isoformat()}.json").is_file()]
    assert caught_up == missing
    events = _events(tmp_path)
    assert events, proc.stdout
    assert {event["event"] for event in events} == {"proposed"}
    assert {event["metric_day"] for event in events} == {METRIC_DAY.isoformat()}


def test_a_damaged_ledger_line_exits_one_and_leaves_the_files_alone(tmp_path):
    _seed_window(tmp_path)
    ledger = _ledger(tmp_path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("not-json\n", encoding="utf-8")
    ledger_before = ledger.read_bytes()
    day_file = tmp_path / "store" / f"{METRIC_DAY.isoformat()}.json"
    day_before = day_file.read_bytes()
    proc = _run(tmp_path, ["propose", *_args(tmp_path)])
    assert proc.returncode == 1, proc.stderr
    assert str(ledger) in proc.stderr
    assert "line 1" in proc.stderr
    assert ledger.read_bytes() == ledger_before
    assert day_file.read_bytes() == day_before


def test_no_project_id_exits_two_with_one_stderr_line(tmp_path):
    proc = _run(tmp_path, ["propose", "--store", str(tmp_path / "store"), "--now", NOW])
    assert proc.returncode == 2
    assert len(proc.stderr.strip().splitlines()) == 1
    assert "project-id" in proc.stderr
