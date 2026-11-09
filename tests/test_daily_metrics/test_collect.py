"""Collector tests for the daily-metrics track (PR-3a).

Everything here imports ``daily_metrics.collect`` directly and drives it in
process: discovery of the source modules, the day document, the store run
lock and the event-driven beacon. The CLI and its subprocess tests live in
``daily_metrics`` PR-3b. Every clock is fixed and injected; no test reads
``datetime.now()``, and the sources directory is always pinned under
``tmp_path``, never the real package sources.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from daily_metrics import metric
from daily_metrics.store import DayStore

REPO_ROOT = Path(__file__).resolve().parents[2]
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

DATACLASS_SOURCE = '''\
from __future__ import annotations
from dataclasses import dataclass

from daily_metrics.metric import ScalarMetric

@dataclass
class Sample:
    metric_id: str
    value: float

def collect(ctx):
    sample = Sample("merges", 3.0)
    return {sample.metric_id: ScalarMetric(value=sample.value, source="dataclass")}
'''


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
    assert "daily_metrics.sources.broken" not in sys.modules


def test_source_module_with_future_annotations_and_dataclass_is_discovered(tmp_path: Path):
    from daily_metrics import collect as collect_mod

    src = tmp_path / "sources"
    src.mkdir()
    (src / "dataclass_source.py").write_text(DATACLASS_SOURCE, encoding="utf-8")
    modules, failures = collect_mod.discover_sources(src)
    assert [m.__name__.rsplit(".", 1)[-1] for m in modules] == ["dataclass_source"]
    assert failures == []


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


def test_source_whose_collect_raises_marks_its_ids(tmp_path: Path, monkeypatch):
    from daily_metrics import collect as collect_mod

    src = tmp_path / "sources"
    src.mkdir()
    (src / "exploding.py").write_text(
        "def collect(ctx):\n    raise RuntimeError('boom')\n", encoding="utf-8"
    )
    monkeypatch.setenv(collect_mod.SOURCES_DIR_ENV, str(src))
    ctx = collect_mod.CollectContext(
        "p", date(2026, 11, 6), datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
    )
    doc = collect_mod.collect_day(ctx)
    assert doc["status"] == "incomplete"
    for record in doc["metrics"].values():
        assert record["unknown_reason"].startswith("source_import_failed")
        assert record["details"]["failed_sources"] == {"exploding": "RuntimeError"}


def test_run_lock_is_exclusive(tmp_path: Path):
    from daily_metrics import collect as collect_mod

    root = tmp_path / "store"
    with collect_mod.run_lock(root) as first:
        assert first is True
        with collect_mod.run_lock(root) as second:
            assert second is False
    with collect_mod.run_lock(root) as third:
        assert third is True


def test_write_beacon_ok_and_fail(tmp_path: Path):
    from daily_metrics import collect as collect_mod

    now = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
    store = DayStore(tmp_path / "store", "p")
    for day in ("2026-11-05", "2026-11-06", "2026-11-07"):
        store.write_final(day, {"day": day})

    state = tmp_path / "tmp_state"
    collect_mod.write_beacon(state, store, now)
    payload = json.loads((state / "health" / "daily_metrics.json").read_text(encoding="utf-8"))
    assert payload["component"] == "daily_metrics"
    assert payload["status"] == "ok"
    assert payload["expected_interval_seconds"] is None
    assert payload["details"]["final_count"] == 3

    empty_state = tmp_path / "tmp_state_empty"
    collect_mod.write_beacon(empty_state, DayStore(tmp_path / "empty_store", "p"), now)
    payload = json.loads(
        (empty_state / "health" / "daily_metrics.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "fail"


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
