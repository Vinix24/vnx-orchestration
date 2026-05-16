"""Tests for scripts/aggregator/state_aggregator.py (Wave 5 PR-5.1)."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.aggregator.state_aggregator import ProjectStateUpdate, StateAggregator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agg(tmp_path: Path) -> StateAggregator:
    return StateAggregator(vnx_data_dir=tmp_path / ".vnx-data")


def _make_update(
    project_id: str = "test-proj",
    event_type: str = "dispatch_created",
    dispatch_id: str = "disp-001",
    source_t0: str | None = "T0-test",
) -> ProjectStateUpdate:
    return ProjectStateUpdate(
        project_id=project_id,
        timestamp="2026-05-16T12:00:00.000+00:00",
        event_type=event_type,
        payload={"dispatch_id": dispatch_id, "track": "A"},
        source_t0=source_t0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_submit_writes_central_facet_event(tmp_path: Path) -> None:
    agg = _make_agg(tmp_path)
    vnx = tmp_path / ".vnx-data"
    agg.submit(_make_update())

    central_path = vnx / "aggregator" / "central_state.json"
    facet_path = vnx / "aggregator" / "projects" / "test-proj.json"
    events_path = vnx / "events" / "state_aggregator.ndjson"

    assert central_path.exists(), "central_state.json not written"
    assert facet_path.exists(), "project facet not written"
    assert events_path.exists(), "events NDJSON not written"

    central = json.loads(central_path.read_text())
    assert "test-proj" in central["projects"]
    assert central["projects"]["test-proj"]["event_counts"]["dispatch_created"] == 1

    facet = json.loads(facet_path.read_text())
    assert len(facet["events"]) == 1
    assert facet["events"][0]["event_type"] == "dispatch_created"

    lines = [ln for ln in events_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["provider"] == "aggregator"
    assert record["event_type"] == "dispatch_created"
    assert record["sub_provider"] == "test-proj"


def test_submit_atomic_under_concurrent_writes(tmp_path: Path) -> None:
    agg = _make_agg(tmp_path)
    errors: list[Exception] = []

    def _worker(n: int) -> None:
        try:
            for i in range(10):
                agg.submit(_make_update(
                    project_id=f"proj-{n}",
                    event_type="t0_heartbeat",
                    dispatch_id=f"disp-{n}-{i}",
                ))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent submit raised: {errors}"

    events_path = tmp_path / ".vnx-data" / "events" / "state_aggregator.ndjson"
    lines = [ln for ln in events_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 100, f"expected 100 events, got {len(lines)}"

    central = (tmp_path / ".vnx-data" / "aggregator" / "central_state.json")
    data = json.loads(central.read_text())
    total_events = sum(
        sum(counts.values())
        for counts in (p["event_counts"] for p in data["projects"].values())
    )
    assert total_events == 100


def test_corrupt_central_recovers(tmp_path: Path) -> None:
    agg = _make_agg(tmp_path)
    central_path = tmp_path / ".vnx-data" / "aggregator" / "central_state.json"
    central_path.parent.mkdir(parents=True, exist_ok=True)
    central_path.write_text("{ not valid json }", encoding="utf-8")

    agg.submit(_make_update())

    data = json.loads(central_path.read_text())
    assert "test-proj" in data["projects"]


def test_ring_buffer_caps_at_100(tmp_path: Path) -> None:
    agg = _make_agg(tmp_path)
    for i in range(200):
        agg.submit(_make_update(dispatch_id=f"disp-{i}"))

    facet_path = tmp_path / ".vnx-data" / "aggregator" / "projects" / "test-proj.json"
    facet = json.loads(facet_path.read_text())
    assert len(facet["events"]) == 100


def test_canonical_event_emitted_per_submit(tmp_path: Path) -> None:
    agg = _make_agg(tmp_path)
    for i in range(7):
        agg.submit(_make_update(dispatch_id=f"disp-{i}"))

    events_path = tmp_path / ".vnx-data" / "events" / "state_aggregator.ndjson"
    lines = [ln for ln in events_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 7

    for line in lines:
        record = json.loads(line)
        assert "event_id" in record
        assert record["schema_version"] == 1
        assert record["provider"] == "aggregator"


def test_phase6_read_pad_compatibility(tmp_path: Path) -> None:
    agg = _make_agg(tmp_path)
    agg.submit(_make_update(project_id="vnx-dev", event_type="dispatch_created"))
    agg.submit(_make_update(project_id="seocrawler", event_type="dispatch_completed"))

    from scripts.aggregator.build_central_view import main

    vnx_data_dir = tmp_path / ".vnx-data"
    rc = main(["--read-write-pad", str(vnx_data_dir)])
    assert rc == 0

    result = agg.read_central()
    assert "vnx-dev" in result["projects"]
    assert "seocrawler" in result["projects"]
    assert result["projects"]["vnx-dev"]["event_counts"]["dispatch_created"] == 1
    assert result["projects"]["seocrawler"]["event_counts"]["dispatch_completed"] == 1
