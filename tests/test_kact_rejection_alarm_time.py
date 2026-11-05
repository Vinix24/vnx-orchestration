"""K-act (OI-2565, OI-2566): the quarantine deadline counts the alarm time that
was actually OBSERVED, not wall-clock age.

#2395 dead-lettered a permanent lifecycle refusal after an UNBROKEN day of
alarm, so a host that sleeps more than the 2 h continuity gap every night never
collects 24 h and retries the refusal for ever (OI-2565). The promise is "a day
of alarm somebody could have seen": the gaps that were seen continuously are
summed on the history entry (``alarm_seconds``), a longer silence adds nothing,
and an entry written before the field existed starts at zero (OI-2566 point 1).
Driven through the real entry point, ``scan_and_convert``.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

from health_beacon import HealthBeacon  # noqa: E402
import report_to_receipt_converter as rtc  # noqa: E402

_TENANT = "proj-quarantine"
_BASE = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc).timestamp()  # 08:00 day 1


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
    state = {"epoch": _BASE}  # whole seconds: stamps and deadline agree
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


def test_a_nightly_sleep_still_ends_the_alarm(reports_dir: Path, state_dir: Path, clock):
    """OI-2565: 15 h awake, 8 h asleep, then awake again reaches the day on day 2.

    On #2395 the unbroken run only starts at 07:00 on day 2, so 17:00 is still
    under 24 h and the permanent refusal is retried for ever."""
    did = "20260601-nightly"
    report, _ = _refuse_once(reports_dir, state_dir, did)

    for _ in range(15):  # 08:00 -> 23:00 on day 1: 15 h of observed alarm
        clock["epoch"] += 3600
        rtc.scan_and_convert([reports_dir], state_dir)

    clock["epoch"] += 8 * 3600  # asleep 23:00 -> 07:00: no alarm time accrues
    stats = rtc.scan_and_convert([reports_dir], state_dir)
    assert (stats.error_count, stats.rejected_count) == (1, 0)

    for _ in range(8):  # 07:00 -> 15:00 on day 2: 23 h of alarm
        clock["epoch"] += 3600
        stats = rtc.scan_and_convert([reports_dir], state_dir)
        assert (stats.error_count, stats.rejected_count) == (1, 0)
    assert report.is_file()

    clock["epoch"] += 3600  # 16:00: exactly 24 h — the deadline is exceeded, not reached
    stats = rtc.scan_and_convert([reports_dir], state_dir)
    assert (stats.error_count, stats.rejected_count) == (1, 0)
    assert report.is_file()
    assert not (state_dir / "receipt_deadletter").exists()

    clock["epoch"] += 3600  # 17:00: 25 h of observed alarm
    stats = rtc.scan_and_convert([reports_dir], state_dir)
    assert stats.rejected_count == 1
    assert not report.exists()
    assert (state_dir / "receipt_deadletter" / f"{did}.md").is_file()
    assert _history_dispatch(state_dir, did).get("alarm_seconds") == 25 * 3600


def test_an_outage_adds_no_alarm_time(reports_dir: Path, state_dir: Path, clock):
    """Seen once, then 30 h of silence, then seen again: one sighting, not a day."""
    did = "20260601-outage"
    report, _ = _refuse_once(reports_dir, state_dir, did)

    clock["epoch"] += 30 * 3600
    stats = rtc.scan_and_convert([reports_dir], state_dir)

    assert (stats.error_count, stats.rejected_count) == (1, 0)
    assert report.is_file()
    assert _history_dispatch(state_dir, did).get("alarm_seconds") == 0
    assert not (state_dir / "receipt_deadletter").exists()


def test_a_legacy_entry_starts_at_zero(reports_dir: Path, state_dir: Path, clock):
    """OI-2566 point 1: an entry written before ``alarm_seconds`` existed may
    span an outage the old merge never saw; the first scan must not act on it."""
    did = "20260601-legacy"
    report, ours = _refuse_once(reports_dir, state_dir, did)
    legacy = {key: value for key, value in ours.items() if key != "alarm_seconds"}
    _seed_history(state_dir, [{**legacy,
                               "first_rejected_at": _stamp_at(clock["epoch"] - 30 * 3600),
                               "rejected_at": _stamp_at(clock["epoch"] - 3600)}])

    stats = rtc.scan_and_convert([reports_dir], state_dir)

    assert (stats.error_count, stats.rejected_count) == (1, 0)
    assert report.is_file()
    assert not (state_dir / "receipt_deadletter").exists()
    # Only the hour this sighting actually observed counts.
    assert _history_dispatch(state_dir, did).get("alarm_seconds") == 3600


def test_a_transient_code_is_never_dead_lettered(reports_dir: Path, state_dir: Path, clock):
    """Unchanged: project_mismatch hinges on resolve_project_id(), not on alarm."""
    did = "20260601-tenant"
    report, ours = _refuse_once(reports_dir, state_dir, did, attempt=1,
                                start_project_id="proj-elsewhere")
    assert ours["reason"].startswith("ledger lifecycle project_mismatch: ")
    _seed_history(state_dir, [{**ours, "alarm_seconds": 48 * 3600,
                               "first_rejected_at": _stamp_at(clock["epoch"] - 48 * 3600),
                               "rejected_at": _stamp_at(clock["epoch"] - 3600)}])

    stats = rtc.scan_and_convert([reports_dir], state_dir)

    assert (stats.error_count, stats.rejected_count) == (1, 0)
    assert report.is_file()
    assert not (state_dir / "receipt_deadletter").exists()


def test_a_permanent_code_seen_hourly_for_a_day_is_dead_lettered(
    reports_dir: Path, state_dir: Path, clock
):
    """Unchanged: an unbroken day of hourly alarm quarantines it."""
    did = "20260601-steady"
    report, _ = _refuse_once(reports_dir, state_dir, did)

    stats = None
    for hour in range(1, 26):
        clock["epoch"] += 3600
        stats = rtc.scan_and_convert([reports_dir], state_dir)
        if hour < 25:
            assert (stats.error_count, stats.rejected_count) == (1, 0)
            assert report.is_file()
    assert stats is not None and stats.rejected_count == 1
    assert not report.exists()
    assert (state_dir / "receipt_deadletter" / f"{did}.md").is_file()
