"""K-act pre-work (OI-2552, OI-2541): the dead-letter deadline survives the
history cap and needs an unbroken day. Driven through the real entry point,
``scan_and_convert``: ``first_rejected_at`` is the deadline, ``rejected_at``
the newest sighting.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

from health_beacon import HealthBeacon  # noqa: E402
import report_to_receipt_converter as rtc  # noqa: E402

_TENANT = "proj-quarantine"


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "state"
    sd.mkdir(parents=True)
    return sd


@pytest.fixture()
def reports_dir(tmp_path: Path) -> Path:
    rd = tmp_path / "unified_reports"
    rd.mkdir(parents=True)
    return rd


@pytest.fixture()
def clock(monkeypatch):
    """Pin ``datetime.now`` (stamps) and ``time.time`` (the deadline)."""
    state = {"epoch": float(int(time.time()))}  # whole seconds: stamps and deadline agree
    real = rtc.datetime

    class _ClockDatetime(real):
        @classmethod
        def now(cls, tz=None):
            return real.fromtimestamp(state["epoch"], tz or timezone.utc)

    monkeypatch.setattr(rtc, "datetime", _ClockDatetime)
    monkeypatch.setattr(rtc, "time", SimpleNamespace(time=lambda: state["epoch"]))
    return state


def _stamp_at(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _seed_history(state_dir: Path, entries: list) -> None:
    HealthBeacon(state_dir.parent, rtc._HEALTH_COMPONENT,
                 rtc._HEALTH_EXPECTED_INTERVAL_SECONDS).heartbeat(
        status="fail", details={"rejected": entries})


def _history_dispatch(state_dir: Path, dispatch_id: str) -> dict:
    path = state_dir.parent / "health" / f"{rtc._HEALTH_COMPONENT}.json"
    history = json.loads(path.read_text(encoding="utf-8"))["details"]["rejected"]
    [entry] = [e for e in history if e["dispatch_id"] == dispatch_id]
    return entry


def _refuse_once(reports_dir: Path, state_dir: Path, did: str, *, attempt: int = 3,
                 start_project_id: str = _TENANT) -> tuple:
    """Start a v3 dispatch, write its report, take the first sighting."""
    (state_dir / "t0_receipts.ndjson").write_text(
        json.dumps({"timestamp": "2026-06-01T10:00:00Z", "event_type": "dispatch_started",
                    "dispatch_id": did, "project_id": start_project_id, "attempt": 1,
                    "lifecycle_mode": "v3"}) + "\n", encoding="utf-8")
    report = reports_dir / f"{did}.md"
    report.write_text(
        f"---\ndispatch_id: {did}\nproject_id: {_TENANT}\nprovider: claude\n"
        f"model: claude-sonnet-4-6\nattempt: {attempt}\n---\n\n"
        "## Summary\n\nImplemented the feature per dispatch specification. "
        "All tests pass and coverage is at target.\n\n"
        "## Changes\n\n- scripts/lib/example.py: added X\n\n"
        "## Verification\n\npytest tests/ -x: 42 passed\n\n"
        "## Open Items\n\nNone\n", encoding="utf-8")
    rtc.scan_and_convert([reports_dir], state_dir)
    return report, _history_dispatch(state_dir, did)


def test_the_cap_never_evicts_a_refusal_sighted_in_this_scan(
    reports_dir: Path, state_dir: Path, clock
):
    """Sighted this scan: the 200-entry cut must keep it, or it returns fresh."""
    did = "20260601-cap"
    report, ours = _refuse_once(reports_dir, state_dir, did)

    now_stamp = _stamp_at(clock["epoch"])
    first_stamp = _stamp_at(clock["epoch"] - 25 * 3600)
    others = [
        {"dispatch_id": f"20260601-other-{i}", "file": f"20260601-other-{i}.md",
         "reason": "missing model", "rejected_at": now_stamp}
        for i in range(rtc._HEALTH_REJECTED_HISTORY_MAX)
    ]
    # Ours first: among equal stamps the stable sort drops the first entry.
    _seed_history(state_dir, [{**ours, "first_rejected_at": first_stamp,
                               "rejected_at": now_stamp}, *others])

    stats = rtc.scan_and_convert([reports_dir], state_dir)

    assert stats.rejected_count == 1
    assert not report.exists()
    assert (state_dir / "receipt_deadletter" / f"{did}.md").is_file()
    assert _history_dispatch(state_dir, did)["first_rejected_at"] == first_stamp


def test_an_outage_gap_restarts_the_deadline_and_a_continuous_day_still_ends_it(
    reports_dir: Path, state_dir: Path, clock
):
    """Seen once 30 h ago and not since: the converter was down, so this is one
    sighting, not a day. Hourly sightings from here do reach the deadline."""
    did = "20260601-continuity"
    report, ours = _refuse_once(reports_dir, state_dir, did)
    outage = _stamp_at(clock["epoch"] - 30 * 3600)
    _seed_history(state_dir, [{**ours, "first_rejected_at": outage, "rejected_at": outage}])

    stats = rtc.scan_and_convert([reports_dir], state_dir)

    assert (stats.error_count, stats.rejected_count) == (1, 0)
    assert report.is_file()
    entry = _history_dispatch(state_dir, did)
    assert entry["first_rejected_at"] == entry["rejected_at"] == _stamp_at(clock["epoch"])

    for hour in range(1, 26):
        clock["epoch"] += 3600
        stats = rtc.scan_and_convert([reports_dir], state_dir)
        if hour < 25:
            assert (stats.error_count, stats.rejected_count) == (1, 0)
            assert report.is_file()
    assert stats.rejected_count == 1
    assert not report.exists()


def test_a_permanent_refusal_seen_hourly_for_a_day_is_dead_lettered(
    reports_dir: Path, state_dir: Path, clock
):
    """Unchanged from main: an unbroken day behind a permanent code quarantines it."""
    did = "20260601-steady"
    report, ours = _refuse_once(reports_dir, state_dir, did)
    _seed_history(state_dir, [{**ours,
                               "first_rejected_at": _stamp_at(clock["epoch"] - 25 * 3600),
                               "rejected_at": _stamp_at(clock["epoch"] - 3600)}])

    stats = rtc.scan_and_convert([reports_dir], state_dir)

    assert stats.rejected_count == 1
    assert not report.exists()
    assert (state_dir / "receipt_deadletter" / f"{did}.md").is_file()


def test_a_transient_refusal_is_never_dead_lettered(
    reports_dir: Path, state_dir: Path, clock
):
    """Unchanged: project_mismatch hinges on resolve_project_id(), never on age."""
    did = "20260601-tenant"
    report, ours = _refuse_once(reports_dir, state_dir, did, attempt=1,
                                start_project_id="proj-elsewhere")
    assert ours["reason"].startswith("ledger lifecycle project_mismatch: ")
    _seed_history(state_dir, [{**ours,
                               "first_rejected_at": _stamp_at(clock["epoch"] - 25 * 3600),
                               "rejected_at": _stamp_at(clock["epoch"] - 3600)}])

    stats = rtc.scan_and_convert([reports_dir], state_dir)

    assert (stats.error_count, stats.rejected_count) == (1, 0)
    assert report.is_file()
    assert not (state_dir / "receipt_deadletter").exists()
