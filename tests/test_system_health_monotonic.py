"""tests/test_system_health_monotonic.py — D2 (absence-is-loud).

`_build_system_health` (scripts/build_t0_state.py) used to compute `status`
once from db_health/build_degraded/artifact-presence and never revisit it
after reading `beacon_health` -- so `beacon_health.overall == "fail"` could
sit right next to `status: "healthy"` in the same object. Measured live on
this tree before the fix (see the D2 dispatch report for the exact
reproduction), and reproduced deterministically here.

The rule: a summary can never report healthier than the worst thing it
summarizes. This applies to every nested health signal `_build_system_health`
carries, including the new `daemon_liveness` field this same PR adds -- a
summary that ignores one of its own sources is precisely the defect closed
here.

Also covers the "second reader" gap: `_state_to_brief` (scripts/build_t0_state.py
~2462) hand-picks fields out of `system_health` for the backward-compat
t0_brief.json format. A field that lands only in the builder and not in the
brief is a half repair.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import build_t0_state as bts  # noqa: E402
from health_beacon import HealthBeacon  # noqa: E402


def _state_dir_with_terminal_marker(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "terminal_state.json").write_text("{}", encoding="utf-8")
    return state_dir


class TestStatusCannotBeHealthierThanBeacon:
    def test_fail_beacon_forbids_healthy_status(self, tmp_path: Path) -> None:
        """THE red test: a failing beacon next to status=healthy is the bug.

        Reproduced live on this tree pre-fix:
            {"status": "healthy", ..., "beacon_health": {"overall": "fail", ...}}
        """
        state_dir = _state_dir_with_terminal_marker(tmp_path)
        HealthBeacon(state_dir.parent, "broken_comp", expected_interval_seconds=3600).heartbeat(
            status="fail", details={}
        )

        health = bts._build_system_health(state_dir, db_initialized=True)

        assert health["beacon_health"]["overall"] == "fail"
        assert health["status"] != "healthy", (
            f"status={health['status']!r} sits next to beacon_health.overall='fail' "
            "-- a summary reported healthier than its own nested signal"
        )

    def test_stale_beacon_forbids_healthy_status(self, tmp_path: Path) -> None:
        import json as _json

        state_dir = _state_dir_with_terminal_marker(tmp_path)
        health_dir = state_dir.parent / "health"
        health_dir.mkdir(parents=True)
        stale_payload = {
            "component": "stale_comp",
            "last_run_ts": int(time.time() - 7200),
            "last_run_iso": "2020-01-01T00:00:00Z",
            "status": "ok",
            "details": {},
            "expected_interval_seconds": 3600,
        }
        (health_dir / "stale_comp.json").write_text(_json.dumps(stale_payload), encoding="utf-8")

        # expected_beacon_components=() isolates from this repo's real
        # beacon-writer register (D3a) the same way daemon_liveness is
        # explicitly injected below to isolate from real daemon-process
        # state — this test is about stale-beacon propagation, not absence.
        health = bts._build_system_health(
            state_dir, db_initialized=True, expected_beacon_components=(),
        )
        assert health["beacon_health"]["overall"] == "stale"
        assert health["status"] != "healthy"

    def test_ok_beacon_allows_healthy_status(self, tmp_path: Path) -> None:
        # daemon_liveness is injected as neutral "ok" so this test isolates
        # beacon behavior instead of depending on which real daemons happen
        # to be running on the machine executing the suite. Same reasoning
        # for expected_beacon_components=() (D3a): the real repo's 9
        # beacon-writer names would otherwise show up as "absent" against
        # this bare tmp_path and force overall to "fail".
        state_dir = _state_dir_with_terminal_marker(tmp_path)
        HealthBeacon(state_dir.parent, "good_comp", expected_interval_seconds=3600).heartbeat(
            status="ok", details={}
        )
        health = bts._build_system_health(
            state_dir, db_initialized=True,
            daemon_liveness={"overall": "ok", "daemons": {}},
            expected_beacon_components=(),
        )
        assert health["beacon_health"]["overall"] == "ok"
        assert health["status"] == "healthy"

    def test_db_failed_still_wins_over_beacon(self, tmp_path: Path) -> None:
        """DB health (R6.1) keeps precedence: a db failure stays 'failed'
        even when the beacon layer would only have forced 'degraded'."""
        state_dir = _state_dir_with_terminal_marker(tmp_path)
        HealthBeacon(state_dir.parent, "good_comp", expected_interval_seconds=3600).heartbeat(
            status="ok", details={}
        )
        health = bts._build_system_health(
            state_dir, db_initialized=True, db_health="failed",
            daemon_liveness={"overall": "ok", "daemons": {}},
        )
        assert health["status"] == "failed"


class TestExpectedBeaconComponentsMonotonic:
    """D3a gap 2: a component expected to write a beacon but never has is
    the most suspect case of all -- invisible to a plain glob over what
    exists in health/. expected_beacon_components is the injection point
    (mirrors daemon_liveness's own test-isolation pattern above)."""

    def test_missing_expected_beacon_component_is_absent_and_forbids_healthy_status(
        self, tmp_path: Path,
    ) -> None:
        state_dir = _state_dir_with_terminal_marker(tmp_path)
        # No beacon is ever written for "never_ran" -- only declared expected.
        health = bts._build_system_health(
            state_dir, db_initialized=True,
            daemon_liveness={"overall": "ok", "daemons": {}},
            expected_beacon_components=("never_ran",),
        )
        bh = health["beacon_health"]
        assert bh["beacons"]["never_ran"]["health"] == "absent"
        assert bh["overall"] == "fail"
        assert health["status"] != "healthy"

    def test_real_register_runs_when_not_injected(self, tmp_path: Path) -> None:
        """Default (no expected_beacon_components param) exercises the real
        beacon_register discovery over this repo's scripts/ tree -- must not
        raise, and t0_state_builder (this very module's own beacon name) is
        always a real, statically-discoverable writer, so on a bare tmp_path
        with no beacons written at all it is reported absent."""
        state_dir = _state_dir_with_terminal_marker(tmp_path)
        health = bts._build_system_health(
            state_dir, db_initialized=True, daemon_liveness={"overall": "ok", "daemons": {}},
        )
        bh = health["beacon_health"]
        assert bh["beacons"]["t0_state_builder"]["health"] == "absent"
        assert bh["overall"] == "fail"


class TestDaemonLivenessMonotonic:
    def test_injected_daemon_fail_forbids_healthy_status(self, tmp_path: Path) -> None:
        state_dir = _state_dir_with_terminal_marker(tmp_path)
        daemon_liveness = {
            "overall": "fail",
            "daemons": {"dispatcher": {"expected": True, "state": "absent", "pid": None, "since": None}},
        }
        health = bts._build_system_health(
            state_dir, db_initialized=True, daemon_liveness=daemon_liveness,
        )
        assert health["daemon_liveness"]["overall"] == "fail"
        assert health["status"] != "healthy"

    def test_injected_daemon_ok_allows_healthy_status(self, tmp_path: Path) -> None:
        # expected_beacon_components=() isolates from this repo's real
        # beacon-writer register (D3a) -- this test writes no beacon at all,
        # so without the override every one of the real names would read
        # "absent" and force status to "degraded".
        state_dir = _state_dir_with_terminal_marker(tmp_path)
        daemon_liveness = {
            "overall": "ok",
            "daemons": {"dispatcher": {"expected": True, "state": "running", "pid": 1, "since": "2026-01-01T00:00:00Z"}},
        }
        health = bts._build_system_health(
            state_dir, db_initialized=True, daemon_liveness=daemon_liveness,
            expected_beacon_components=(),
        )
        assert health["status"] == "healthy"

    def test_unknown_daemon_liveness_does_not_force_degraded(self, tmp_path: Path) -> None:
        """'unknown' (could not measure) is a third branch -- it must not be
        silently treated as a failure that drags status down."""
        state_dir = _state_dir_with_terminal_marker(tmp_path)
        daemon_liveness = {"overall": "unknown", "daemons": {}, "reason": "psutil unavailable"}
        health = bts._build_system_health(
            state_dir, db_initialized=True, daemon_liveness=daemon_liveness,
            expected_beacon_components=(),
        )
        assert health["status"] == "healthy"

    def test_real_measurement_runs_when_not_injected(self, tmp_path: Path) -> None:
        """Default (no daemon_liveness param) exercises the real psutil-based
        measurement -- must not raise, and must surface a daemon_liveness key
        with a recognized overall value."""
        state_dir = _state_dir_with_terminal_marker(tmp_path)
        health = bts._build_system_health(state_dir, db_initialized=True)
        assert "daemon_liveness" in health
        assert health["daemon_liveness"]["overall"] in ("ok", "fail", "unknown")


class TestBriefCarriesDaemonLiveness:
    def test_state_to_brief_includes_daemon_liveness(self) -> None:
        state = {
            "generated_at": "2026-08-30T10:00:00Z",
            "system_health": {
                "status": "degraded",
                "uptime_seconds": 0,
                "db_initialized": True,
                "beacon_health": {"overall": "fail"},
                "daemon_liveness": {"overall": "fail", "daemons": {"dispatcher": {"state": "absent"}}},
            },
        }
        brief = bts._state_to_brief(state)
        assert brief["system_health"].get("daemon_liveness") == {
            "overall": "fail", "daemons": {"dispatcher": {"state": "absent"}},
        }

    def test_state_to_brief_status_reflects_aggregated_value(self) -> None:
        state = {
            "generated_at": "2026-08-30T10:00:00Z",
            "system_health": {
                "status": "degraded",
                "uptime_seconds": 0,
                "db_initialized": True,
                "beacon_health": {"overall": "fail"},
            },
        }
        brief = bts._state_to_brief(state)
        assert brief["system_health"]["status"] == "degraded"
