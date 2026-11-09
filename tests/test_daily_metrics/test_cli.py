"""CLI subprocess tests for the daily-metrics track (PR-3b).

Each test runs ``scripts/daily_metrics.py`` with ``--store``, ``--state-dir``
and ``VNX_DAILY_METRICS_SOURCES_DIR`` pinned under ``tmp_path`` and every clock
fixed through ``--now``; no test touches the real central store.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from daily_metrics import metric

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "daily_metrics.py"
NOW = "2026-11-08T03:00:00Z"

HEALTHY_SOURCE = '''\
from daily_metrics.metric import KeyedMetric, REQUIRED, ScalarMetric
_SCALARS = sorted(REQUIRED - {"gate_duration_seconds_by_seat"})


def collect(ctx):
    out = {m: ScalarMetric(value=float(i + 1), source="fake", sample_size=1)
           for i, m in enumerate(_SCALARS)}
    out["gate_duration_seconds_by_seat"] = KeyedMetric(
        source="fake", entries={"codex_gate": ScalarMetric(value=10.0, source="fake", sample_size=1)})
    return out
'''

BROKEN_SOURCE = "raise RuntimeError('boom')\n"


def _run_cli(sources_dir: Path, args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("VNX_PROJECT_ID", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    env["VNX_DAILY_METRICS_SOURCES_DIR"] = str(sources_dir)
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        env=env, capture_output=True, text=True, cwd=str(REPO_ROOT),
    )


def _args(tmp_path: Path, extra: list[str], now: str = NOW) -> list[str]:
    """Shared argument list for the one-day and window tests."""
    return ["--store", str(tmp_path / "store"), "--state-dir", str(tmp_path / "state"),
            "--project-id", "p", *extra, "--now", now]


def _day_file(tmp_path: Path, name: str) -> Path:
    return tmp_path / "store" / f"{name}.json"


@pytest.fixture()
def healthy_sources(tmp_path: Path) -> Path:
    src = tmp_path / "healthy_sources"
    src.mkdir()
    (src / "healthy.py").write_text(HEALTHY_SOURCE, encoding="utf-8")
    return src


@pytest.fixture()
def broken_sources(tmp_path: Path) -> Path:
    src = tmp_path / "broken_sources"
    src.mkdir()
    (src / "broken.py").write_text(BROKEN_SOURCE, encoding="utf-8")
    return src


@pytest.fixture()
def empty_sources(tmp_path: Path) -> Path:
    src = tmp_path / "empty_sources"
    src.mkdir()
    return src


def test_cli_script_exists():
    """Red run: fails on the merge base before ``scripts/daily_metrics.py`` exists."""
    assert CLI.is_file()


def test_daily_has_no_command_discovery():
    """Red run: fails on afee541e, where ``discover_commands`` is still exported.

    The command-discovery round is deferred to its own PR: the CLI only catches up
    the window and writes the beacon, so no command module is imported or skipped.
    """
    spec = importlib.util.spec_from_file_location("daily_metrics_cli_under_test", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not hasattr(module, "discover_commands")
    assert not (REPO_ROOT / "scripts" / "lib" / "daily_metrics" / "commands").exists()


def test_collect_one_day_returns_all_required_ids(empty_sources, tmp_path):
    proc = _run_cli(empty_sources, ["collect", *_args(tmp_path, ["--day", "2026-11-06"])])
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(_day_file(tmp_path, "2026-11-06").read_text(encoding="utf-8"))
    assert set(doc["metrics"]) == set(metric.REQUIRED)


def test_source_import_failure_makes_the_day_incomplete(broken_sources, tmp_path):
    proc = _run_cli(broken_sources, ["collect", *_args(tmp_path, ["--day", "2026-11-06"])])
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(_day_file(tmp_path, "2026-11-06").read_text(encoding="utf-8"))
    assert doc["status"] == "incomplete"
    assert all(
        record["unknown_reason"].startswith("source_import_failed")
        for record in doc["metrics"].values()
    )


def test_second_healthy_run_replaces_incomplete_with_final(broken_sources, healthy_sources, tmp_path):
    args = ["collect", *_args(tmp_path, ["--day", "2026-11-06"])]
    first = _run_cli(broken_sources, args)
    assert first.returncode == 0, first.stderr
    second = _run_cli(healthy_sources, args)
    assert second.returncode == 0, second.stderr
    doc = json.loads(_day_file(tmp_path, "2026-11-06").read_text(encoding="utf-8"))
    assert doc["status"] == "final"
    assert all(record.get("unknown_reason") is None for record in doc["metrics"].values())


def test_third_run_on_final_day_is_skipped_with_unchanged_bytes(broken_sources, healthy_sources, tmp_path):
    args = ["collect", *_args(tmp_path, ["--day", "2026-11-06"])]
    assert _run_cli(broken_sources, args).returncode == 0
    assert _run_cli(healthy_sources, args).returncode == 0
    day_file = _day_file(tmp_path, "2026-11-06")
    digest = hashlib.sha256(day_file.read_bytes()).hexdigest()
    third = _run_cli(healthy_sources, args)
    assert third.returncode == 0, third.stderr
    assert '"skipped": "final"' in third.stdout
    assert hashlib.sha256(day_file.read_bytes()).hexdigest() == digest


def test_missing_project_id_exits_two(empty_sources, tmp_path):
    proc = _run_cli(empty_sources, [
        "collect", "--store", str(tmp_path / "store"), "--state-dir", str(tmp_path / "state"),
        "--day", "2026-11-06", "--now", NOW,
    ])
    assert proc.returncode == 2


def test_locked_run_prints_skipped_locked_and_writes_nothing(healthy_sources, tmp_path):
    (tmp_path / "store").mkdir(parents=True)
    fd = os.open(tmp_path / "store" / ".lock", os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        proc = _run_cli(healthy_sources, ["collect", *_args(tmp_path, ["--day", "2026-11-06"])])
        assert proc.returncode == 0, proc.stderr
        assert "skipped: locked" in proc.stdout
        assert not _day_file(tmp_path, "2026-11-06").exists()
    finally:
        os.close(fd)


def test_daily_writes_missing_settled_days_oldest_first_never_today(healthy_sources, tmp_path):
    proc = _run_cli(healthy_sources, ["daily", *_args(tmp_path, [])])
    assert proc.returncode == 0, proc.stderr
    days = sorted(p.stem for p in (tmp_path / "store").glob("*.json"))
    assert days == sorted(days)
    assert days[0] == "2026-10-25"
    assert days[-1] == "2026-11-07"
    assert "2026-11-08" not in days


def test_collect_unsettled_day_exits_two_without_writing(healthy_sources, tmp_path):
    proc = _run_cli(healthy_sources, ["collect", *_args(tmp_path, ["--day", "2026-11-08"])])
    assert proc.returncode == 2
    assert not _day_file(tmp_path, "2026-11-08").exists()


def test_recollect_writes_revision_and_rejects_invalid(broken_sources, healthy_sources, tmp_path):
    base = _args(tmp_path, [])
    assert _run_cli(healthy_sources, ["collect", *base, "--day", "2026-11-06"]).returncode == 0

    recollected = _run_cli(healthy_sources, ["collect", "--recollect", "2026-11-06", "--reason", "fix", *base])
    assert recollected.returncode == 0, recollected.stderr
    assert _day_file(tmp_path, "2026-11-06.r2").exists()

    assert _run_cli(healthy_sources, ["collect", "--recollect", "2026-11-06", "--reason", "   ", *base]).returncode == 2

    # no file at all
    missing = _run_cli(healthy_sources, ["collect", "--recollect", "2026-11-05", "--reason", "x", *base])
    assert missing.returncode == 2
    assert not _day_file(tmp_path, "2026-11-05.r2").exists()

    # only an incomplete file
    assert _run_cli(broken_sources, ["collect", *base, "--day", "2026-11-05"]).returncode == 0
    incomplete = _run_cli(healthy_sources, ["collect", "--recollect", "2026-11-05", "--reason", "x", *base])
    assert incomplete.returncode == 2
    assert not _day_file(tmp_path, "2026-11-05.r2").exists()


def test_failed_recollect_is_retried_and_a_revised_day_stays_final(
        broken_sources, healthy_sources, tmp_path):
    """Finality is the base file's: a failed revision neither locks out nor re-collects the day."""
    base = _args(tmp_path, [])
    assert _run_cli(healthy_sources, ["collect", *base, "--day", "2026-11-06"]).returncode == 0
    a = _run_cli(broken_sources, ["collect", "--recollect", "2026-11-06", "--reason", "a", *base])
    assert a.returncode == 0, a.stderr
    r2 = json.loads(_day_file(tmp_path, "2026-11-06.r2").read_text(encoding="utf-8"))
    assert r2["status"] == "incomplete"
    daily = _run_cli(healthy_sources, ["daily", *base])
    assert daily.returncode == 0, daily.stderr
    lines = [json.loads(line) for line in daily.stdout.splitlines()]
    day_lines = [item for item in lines if item["day"] == "2026-11-06"]
    assert all("skipped" in item for item in day_lines)
    assert not _day_file(tmp_path, "2026-11-06.r3").exists()
    b = _run_cli(healthy_sources, ["collect", "--recollect", "2026-11-06", "--reason", "b", *base])
    assert b.returncode == 0, b.stderr
    r3 = json.loads(_day_file(tmp_path, "2026-11-06.r3").read_text(encoding="utf-8"))
    assert r3["status"] == "final"


def test_beacon_file_exists_after_daily(healthy_sources, tmp_path):
    proc = _run_cli(healthy_sources, ["daily", *_args(tmp_path, [])])
    assert proc.returncode == 0, proc.stderr
    beacon = tmp_path / "state" / "health" / "daily_metrics.json"
    assert beacon.exists()
    payload = json.loads(beacon.read_text(encoding="utf-8"))
    assert payload["component"] == "daily_metrics"
    assert payload["expected_interval_seconds"] is None
    assert payload["status"] == "ok"
    assert payload["details"]["final_count"] == 3
