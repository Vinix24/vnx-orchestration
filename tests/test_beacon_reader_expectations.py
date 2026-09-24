"""tests/test_beacon_reader_expectations.py — absence-is-loud, punt 1.

Every reader of the beacon store has to expect what the writers actually do.
Measured on main 264e9460 (2026-09-23), three places where it did not:

  A. ``build_t0_state._build_system_health`` never passed ``parked=``, so
     learning_loop and intelligence_daemon (parked by operator decision,
     ``beacon_register.PARKED_COMPONENTS``) read ``stale`` in t0_state.json while
     the SessionStart hook, which does pass it, called them ``parked``.
  B. ``cleanup_worker_exit`` writes only when a worker exits
     (``expected_interval_seconds=None``). ``expected_component_names()`` still
     listed it, so silence between two events read ``absent``, which
     ``beacon_summary`` counts as ``fail``.
  (C, the fleet_role_drift beacon written to the wrong root, is pinned in
  ``test_oi1478_fleet_role_drift.py``.)

The last class drives the two Python readers side by side over the same store
and demands the same verdict per component: two lezers, one uitkomst.

Everything runs against tmp_path, never the live store.
"""
from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path
from typing import Dict, Optional

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import beacon_register as br  # noqa: E402
import build_t0_state as bts  # noqa: E402
import health_check  # noqa: E402
from health_beacon import beacon_summary  # noqa: E402

_DAY = 86400
_NEUTRAL_LIVENESS = {
    "daemon_liveness": {"overall": "ok", "daemons": {}},
    "launchd_liveness": {"overall": "ok", "jobs": {}},
}


def _data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "state" / "terminal_state.json").write_text("{}", encoding="utf-8")
    return data_dir


def _write_beacon(
    data_dir: Path,
    component: str,
    *,
    status: str = "ok",
    age_seconds: float = 0.0,
    interval: Optional[int] = _DAY,
) -> None:
    health_dir = data_dir / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    now = time.time() - age_seconds
    (health_dir / f"{component}.json").write_text(
        json.dumps({
            "component": component,
            "last_run_ts": int(now),
            "last_run_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "status": status,
            "details": {},
            "expected_interval_seconds": interval,
        }),
        encoding="utf-8",
    )


def _t0_beacons(data_dir: Path, **kwargs) -> Dict[str, dict]:
    """The beacon map exactly as t0_state.json carries it, with the LIVE
    register (no ``expected_beacon_components`` injection)."""
    health = bts._build_system_health(
        data_dir / "state", db_initialized=True, **_NEUTRAL_LIVENESS, **kwargs,
    )
    return health["beacon_health"]


# ---------------------------------------------------------------------------
# A. t0_state honours the parked components
# ---------------------------------------------------------------------------


class TestT0StateHonoursParkedComponents:
    def test_old_learning_loop_beacon_reads_parked(self, tmp_path: Path) -> None:
        """The measured defect: t0_state.json said ``stale`` for a component the
        operator parked on 2026-09-09."""
        data_dir = _data_dir(tmp_path)
        _write_beacon(data_dir, "learning_loop", age_seconds=90 * _DAY, interval=_DAY)

        bh = _t0_beacons(data_dir, expected_beacon_components=())

        assert bh["beacons"]["learning_loop"]["health"] == "parked"
        assert bh["counts"]["parked"] == 1
        assert bh["counts"]["stale"] == 0

    def test_old_intelligence_daemon_beacon_reads_parked(self, tmp_path: Path) -> None:
        data_dir = _data_dir(tmp_path)
        _write_beacon(data_dir, "intelligence_daemon", age_seconds=90 * _DAY, interval=300)

        bh = _t0_beacons(data_dir, expected_beacon_components=())

        assert bh["beacons"]["intelligence_daemon"]["health"] == "parked"

    def test_parked_component_that_never_wrote_reads_parked_not_absent(self, tmp_path: Path) -> None:
        """Live register: learning_loop is an expected writer. With no beacon on
        disk the SessionStart hook says parked; t0_state used to say absent."""
        data_dir = _data_dir(tmp_path)

        bh = _t0_beacons(data_dir)

        assert bh["beacons"]["learning_loop"]["health"] == "parked"
        assert bh["beacons"]["intelligence_daemon"]["health"] == "parked"

    def test_parked_does_not_floor_the_overall_verdict(self, tmp_path: Path) -> None:
        data_dir = _data_dir(tmp_path)
        _write_beacon(data_dir, "learning_loop", age_seconds=90 * _DAY, interval=_DAY)

        bh = _t0_beacons(data_dir, expected_beacon_components=())

        assert bh["overall"] == "ok"

    def test_parking_suppresses_silence_not_a_real_failure(self, tmp_path: Path) -> None:
        """A parked component that writes a fresh ``fail`` is still a defect."""
        data_dir = _data_dir(tmp_path)
        _write_beacon(data_dir, "learning_loop", status="fail", age_seconds=10, interval=_DAY)

        bh = _t0_beacons(data_dir, expected_beacon_components=())

        assert bh["beacons"]["learning_loop"]["health"] == "fail"
        assert bh["overall"] == "fail"


# ---------------------------------------------------------------------------
# B. an event-driven writer's silence is not a finding
# ---------------------------------------------------------------------------


def _fixture_register(tmp_path: Path):
    """An event-driven writer and a daily one, discovered the way production
    discovers them: from HealthBeacon(...) call sites."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "writers.py").write_text(textwrap.dedent("""
        from health_beacon import HealthBeacon

        def on_worker_exit(data_dir):
            HealthBeacon(data_dir, "evt", expected_interval_seconds=None).heartbeat()

        def daily(data_dir):
            HealthBeacon(data_dir, "per", expected_interval_seconds=3600).heartbeat()
    """), encoding="utf-8")
    return br.read_beacon_register(scripts)


class TestEventDrivenAbsenceIsNotAFinding:
    def test_event_driven_component_without_a_beacon_is_not_absent(self, tmp_path: Path) -> None:
        register = _fixture_register(tmp_path)
        data_dir = _data_dir(tmp_path)
        _write_beacon(data_dir, "per", age_seconds=10, interval=3600)

        summary = beacon_summary(data_dir, expected=br.expected_component_names(register))

        assert summary["counts"]["absent"] == 0
        assert summary["beacons"].get("evt", {}).get("health") != "absent"
        assert summary["overall"] == "ok"

    def test_event_driven_component_with_a_failing_beacon_stays_fail(self, tmp_path: Path) -> None:
        """Only the silence stops being an alarm. A real failure stays loud."""
        register = _fixture_register(tmp_path)
        data_dir = _data_dir(tmp_path)
        _write_beacon(data_dir, "per", age_seconds=10, interval=3600)
        _write_beacon(data_dir, "evt", status="fail", age_seconds=10, interval=None)

        summary = beacon_summary(data_dir, expected=br.expected_component_names(register))

        assert summary["beacons"]["evt"]["health"] == "fail"
        assert summary["overall"] == "fail"

    def test_component_with_an_interval_that_is_absent_stays_absent(self, tmp_path: Path) -> None:
        register = _fixture_register(tmp_path)
        data_dir = _data_dir(tmp_path)

        summary = beacon_summary(data_dir, expected=br.expected_component_names(register))

        assert summary["beacons"]["per"]["health"] == "absent"
        assert summary["overall"] == "fail"

    def test_t0_state_does_not_report_cleanup_worker_exit_absent(self, tmp_path: Path) -> None:
        """Real register, real writer: cleanup_worker_exit passes
        ``expected_interval_seconds=None``."""
        data_dir = _data_dir(tmp_path)

        bh = _t0_beacons(data_dir)

        assert bh["beacons"].get("cleanup_worker_exit", {}).get("health") != "absent"

    def test_t0_state_keeps_a_failing_cleanup_worker_exit_beacon_loud(self, tmp_path: Path) -> None:
        data_dir = _data_dir(tmp_path)
        _write_beacon(data_dir, "cleanup_worker_exit", status="fail", age_seconds=60, interval=None)

        bh = _t0_beacons(data_dir)

        assert bh["beacons"]["cleanup_worker_exit"]["health"] == "fail"

    def test_periodic_real_writer_that_never_wrote_is_still_absent(self, tmp_path: Path) -> None:
        """fleet_role_drift promises a daily beacon, so its absence is a finding."""
        data_dir = _data_dir(tmp_path)

        bh = _t0_beacons(data_dir)

        assert bh["beacons"]["fleet_role_drift"]["health"] == "absent"


# ---------------------------------------------------------------------------
# Two readers, one verdict
# ---------------------------------------------------------------------------


def _health_check_verdicts(data_dir: Path, capsys: pytest.CaptureFixture) -> Dict[str, str]:
    """scripts/health_check.py as hooks/sessionstart.sh invokes it: the
    expected and parked names come from beacon_register, comma-joined."""
    capsys.readouterr()
    health_check.main([
        "--state-dir", str(data_dir), "--json",
        "--expected", ",".join(br.expected_component_names()),
        "--parked", ",".join(br.parked_component_names()),
    ])
    payload = json.loads(capsys.readouterr().out)
    return {name: b["health"] for name, b in payload["beacons"].items()}


class TestReadersAgree:
    def test_t0_state_and_the_sessionstart_reader_give_the_same_verdict(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        data_dir = _data_dir(tmp_path)
        _write_beacon(data_dir, "learning_loop", age_seconds=90 * _DAY, interval=_DAY)
        _write_beacon(data_dir, "intelligence_daemon", age_seconds=90 * _DAY, interval=300)
        _write_beacon(data_dir, "t0_state_builder", age_seconds=30, interval=1800)
        _write_beacon(data_dir, "ledger_health", status="fail", age_seconds=30, interval=_DAY)
        # cleanup_worker_exit: never wrote. fleet_role_drift: never wrote.

        t0_state = {
            name: b["health"] for name, b in _t0_beacons(data_dir)["beacons"].items()
        }
        sessionstart = _health_check_verdicts(data_dir, capsys)

        assert t0_state == sessionstart
        assert t0_state["learning_loop"] == "parked"
        assert t0_state["intelligence_daemon"] == "parked"
        assert t0_state["fleet_role_drift"] == "absent"
        assert t0_state.get("cleanup_worker_exit") != "absent"
