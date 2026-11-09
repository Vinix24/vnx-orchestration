"""Collector and CLI tests for the daily-metrics track (PR-3).

Every clock in this file is fixed and injected through ``--now``; no test reads
``datetime.now()``. The subprocess tests run ``scripts/daily_metrics.py`` with
``--store``, ``--state-dir`` and ``VNX_DAILY_METRICS_SOURCES_DIR`` all pinned
under ``tmp_path``, never the real central store.
"""

from __future__ import annotations

import ast
import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from daily_metrics import metric

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "daily_metrics.py"
COLLECT_PATH = REPO_ROOT / "scripts" / "lib" / "daily_metrics" / "collect.py"

HEALTHY_SOURCE = '''\
from daily_metrics.metric import KeyedMetric, REQUIRED, ScalarMetric

_SCALARS = sorted(REQUIRED - {"gate_duration_seconds_by_seat"})


def collect(ctx):
    out = {}
    for index, metric_id in enumerate(_SCALARS):
        out[metric_id] = ScalarMetric(value=float(index + 1), source="fake", sample_size=1)
    out["gate_duration_seconds_by_seat"] = KeyedMetric(
        source="fake",
        entries={"codex_gate": ScalarMetric(value=10.0, source="fake", sample_size=1)},
    )
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


def test_collect_module_is_importable():
    """Red run: this assert fails on the merge base before ``collect.py`` exists."""
    assert importlib.util.find_spec("daily_metrics.collect") is not None


def test_discover_sources_reports_import_failures(tmp_path: Path):
    from daily_metrics import collect as collect_mod

    src = tmp_path / "sources"
    src.mkdir()
    (src / "broken.py").write_text(BROKEN_SOURCE, encoding="utf-8")
    modules, failures = collect_mod.discover_sources(src)
    assert modules == []
    assert [f.module for f in failures] == ["broken"]
    assert failures[0].exception == "RuntimeError"


def test_discover_sources_keeps_modules_with_collect(tmp_path: Path):
    from daily_metrics import collect as collect_mod

    src = tmp_path / "sources"
    src.mkdir()
    (src / "healthy.py").write_text(HEALTHY_SOURCE, encoding="utf-8")
    (src / "not_a_source.py").write_text("value = 1\n", encoding="utf-8")
    modules, failures = collect_mod.discover_sources(src)
    assert [m.__name__.rsplit(".", 1)[-1] for m in modules] == ["healthy"]
    assert failures == []


def test_collect_day_missing_sources_is_incomplete_with_reason(tmp_path: Path, monkeypatch):
    from daily_metrics import collect as collect_mod

    src = tmp_path / "sources"
    src.mkdir()
    monkeypatch.setenv(collect_mod.SOURCES_DIR_ENV, str(src))
    ctx = collect_mod.CollectContext(
        "p", date(2026, 11, 6), datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
    )
    doc = collect_mod.collect_day(ctx)
    assert set(doc["metrics"]) == set(metric.REQUIRED)
    assert doc["status"] == "incomplete"
    assert doc["schema"] == 1
    assert doc["project_id"] == "p"
    assert doc["day"] == "2026-11-06"
    assert all(
        record["unknown_reason"] == "source_import_failed:no_source"
        for record in doc["metrics"].values()
    )


def test_collect_day_drops_unexpected_ids(tmp_path: Path, monkeypatch):
    from daily_metrics import collect as collect_mod
    from daily_metrics.metric import ScalarMetric

    src = tmp_path / "sources"
    src.mkdir()
    (src / "extra.py").write_text(
        "from daily_metrics.metric import ScalarMetric\n"
        "def collect(ctx):\n"
        "    return {'merges': ScalarMetric(value=1.0, source='s'), "
        "'not_a_metric': ScalarMetric(value=2.0, source='s')}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(collect_mod.SOURCES_DIR_ENV, str(src))
    ctx = collect_mod.CollectContext(
        "p", date(2026, 11, 6), datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
    )
    doc = collect_mod.collect_day(ctx)
    assert doc["details"]["unexpected_ids"] == ["not_a_metric"]
    assert "not_a_metric" not in doc["metrics"]
    assert doc["metrics"]["merges"]["value"] == 1.0


def test_collect_one_day_returns_all_required_ids(empty_sources, tmp_path):
    store = tmp_path / "store"
    state = tmp_path / "state"
    proc = _run_cli(empty_sources, [
        "collect", "--store", str(store), "--state-dir", str(state),
        "--project-id", "p", "--day", "2026-11-06", "--now", "2026-11-08T03:00:00Z",
    ])
    assert proc.returncode == 0, proc.stderr
    doc = json.loads((store / "2026-11-06.json").read_text(encoding="utf-8"))
    assert set(doc["metrics"]) == set(metric.REQUIRED)


def test_source_import_failure_makes_the_day_incomplete(broken_sources, tmp_path):
    store = tmp_path / "store"
    state = tmp_path / "state"
    proc = _run_cli(broken_sources, [
        "collect", "--store", str(store), "--state-dir", str(state),
        "--project-id", "p", "--day", "2026-11-06", "--now", "2026-11-08T03:00:00Z",
    ])
    assert proc.returncode == 0, proc.stderr
    doc = json.loads((store / "2026-11-06.json").read_text(encoding="utf-8"))
    assert doc["status"] == "incomplete"
    assert all(
        record["unknown_reason"].startswith("source_import_failed")
        for record in doc["metrics"].values()
    )


def test_second_healthy_run_replaces_incomplete_with_final(broken_sources, healthy_sources, tmp_path):
    store = tmp_path / "store"
    state = tmp_path / "state"
    args = ["collect", "--store", str(store), "--state-dir", str(state),
            "--project-id", "p", "--day", "2026-11-06", "--now", "2026-11-08T03:00:00Z"]
    first = _run_cli(broken_sources, args)
    assert first.returncode == 0, first.stderr
    second = _run_cli(healthy_sources, args)
    assert second.returncode == 0, second.stderr
    doc = json.loads((store / "2026-11-06.json").read_text(encoding="utf-8"))
    assert doc["status"] == "final"
    assert all(record.get("unknown_reason") is None for record in doc["metrics"].values())


def test_third_run_on_final_day_is_skipped_with_unchanged_bytes(broken_sources, healthy_sources, tmp_path):
    store = tmp_path / "store"
    state = tmp_path / "state"
    args = ["collect", "--store", str(store), "--state-dir", str(state),
            "--project-id", "p", "--day", "2026-11-06", "--now", "2026-11-08T03:00:00Z"]
    assert _run_cli(broken_sources, args).returncode == 0
    assert _run_cli(healthy_sources, args).returncode == 0
    day_file = store / "2026-11-06.json"
    digest = hashlib.sha256(day_file.read_bytes()).hexdigest()
    third = _run_cli(healthy_sources, args)
    assert third.returncode == 0, third.stderr
    assert '"skipped": "final"' in third.stdout
    assert hashlib.sha256(day_file.read_bytes()).hexdigest() == digest


def test_missing_project_id_exits_two(empty_sources, tmp_path):
    proc = _run_cli(empty_sources, [
        "collect", "--store", str(tmp_path / "store"), "--state-dir", str(tmp_path / "state"),
        "--day", "2026-11-06", "--now", "2026-11-08T03:00:00Z",
    ])
    assert proc.returncode == 2


def test_locked_run_prints_skipped_locked_and_writes_nothing(healthy_sources, tmp_path):
    store = tmp_path / "store"
    state = tmp_path / "state"
    store.mkdir(parents=True)
    lock_path = store / ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        proc = _run_cli(healthy_sources, [
            "collect", "--store", str(store), "--state-dir", str(state),
            "--project-id", "p", "--day", "2026-11-06", "--now", "2026-11-08T03:00:00Z",
        ])
        assert proc.returncode == 0, proc.stderr
        assert "skipped: locked" in proc.stdout
        assert not (store / "2026-11-06.json").exists()
    finally:
        os.close(fd)


def test_daily_writes_missing_settled_days_oldest_first_never_today(healthy_sources, tmp_path):
    store = tmp_path / "store"
    state = tmp_path / "state"
    proc = _run_cli(healthy_sources, [
        "daily", "--store", str(store), "--state-dir", str(state),
        "--project-id", "p", "--now", "2026-11-08T03:00:00Z",
    ])
    assert proc.returncode == 0, proc.stderr
    days = sorted(p.stem for p in store.glob("*.json"))
    assert days == sorted(days)
    assert days[0] == "2026-10-25"
    assert days[-1] == "2026-11-07"
    assert "2026-11-08" not in days


def test_collect_unsettled_day_exits_two_without_writing(healthy_sources, tmp_path):
    store = tmp_path / "store"
    state = tmp_path / "state"
    proc = _run_cli(healthy_sources, [
        "collect", "--store", str(store), "--state-dir", str(state),
        "--project-id", "p", "--day", "2026-11-08", "--now", "2026-11-08T03:00:00Z",
    ])
    assert proc.returncode == 2
    assert not (store / "2026-11-08.json").exists()


def test_recollect_writes_revision_and_rejects_invalid(broken_sources, healthy_sources, tmp_path):
    store = tmp_path / "store"
    state = tmp_path / "state"
    base = ["--store", str(store), "--state-dir", str(state),
            "--project-id", "p", "--now", "2026-11-08T03:00:00Z"]
    assert _run_cli(healthy_sources, ["collect", *base, "--day", "2026-11-06"]).returncode == 0

    recollected = _run_cli(healthy_sources, ["collect", "--recollect", "2026-11-06", "--reason", "fix", *base])
    assert recollected.returncode == 0, recollected.stderr
    assert (store / "2026-11-06.r2.json").exists()

    assert _run_cli(healthy_sources, ["collect", "--recollect", "2026-11-06", "--reason", "   ", *base]).returncode == 2

    # no file at all
    missing = _run_cli(healthy_sources, ["collect", "--recollect", "2026-11-05", "--reason", "x", *base])
    assert missing.returncode == 2
    assert not (store / "2026-11-05.r2.json").exists()

    # only an incomplete file
    assert _run_cli(broken_sources, ["collect", *base, "--day", "2026-11-05"]).returncode == 0
    incomplete = _run_cli(healthy_sources, ["collect", "--recollect", "2026-11-05", "--reason", "x", *base])
    assert incomplete.returncode == 2
    assert not (store / "2026-11-05.r2.json").exists()


def test_beacon_file_exists_after_daily(healthy_sources, tmp_path):
    store = tmp_path / "store"
    state = tmp_path / "state"
    proc = _run_cli(healthy_sources, [
        "daily", "--store", str(store), "--state-dir", str(state),
        "--project-id", "p", "--now", "2026-11-08T03:00:00Z",
    ])
    assert proc.returncode == 0, proc.stderr
    beacon = state / "health" / "daily_metrics.json"
    assert beacon.exists()
    payload = json.loads(beacon.read_text(encoding="utf-8"))
    assert payload["component"] == "daily_metrics"
    assert payload["expected_interval_seconds"] is None
    assert payload["status"] == "ok"
    assert payload["details"]["final_count"] == 3


def test_beacon_call_declares_a_literal_none_interval():
    tree = ast.parse(COLLECT_PATH.read_text(encoding="utf-8"), filename=str(COLLECT_PATH))
    event_driven = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "HealthBeacon"):
            continue
        interval = None
        for keyword in node.keywords:
            if keyword.arg == "expected_interval_seconds":
                interval = keyword.value
        if interval is None and len(node.args) >= 3:
            interval = node.args[2]
        if isinstance(interval, ast.Constant) and interval.value is None:
            event_driven.append(node)
    assert event_driven, "the daily_metrics beacon must pass a literal None interval"
