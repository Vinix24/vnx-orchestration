"""Smoke test - assert critical component beacons are present and fresh.

Catches silent component failures (e.g. the conversation_analyzer
20-day silent failure) by verifying that each component listed in
``CRITICAL_COMPONENTS`` has written a heartbeat within its allowed
freshness window.

Per the PR-T4 CI plan (claudedocs/2026-04-30-vnx-ci-test-plan.md):

    learning_loop          24h
    intelligence_daemon    5min
    build_t0_state         30min
    compact_state          24h (nightly)
    conversation_analyzer  24h

The test imports ``health_beacon.all_beacons`` to read the JSON
heartbeat files under ``$VNX_DATA_DIR/health/``. Age is computed against
the wall clock at test time. The test gates on the explicit per-component
max age, not on the beacon's own ``expected_interval_seconds``, so the
contract here is independent of writer-side configuration.

When no health directory or no critical beacons are present (bare
checkout / fresh CI runner without state), the test is skipped. To
force a failure-on-missing in monitoring contexts, set
``VNX_SMOKE_REQUIRE_BEACONS=1``.

Run:
    pytest tests/smoke/smoke_health_beacons_fresh.py -xvs
or directly:
    python3 tests/smoke/smoke_health_beacons_fresh.py
Exit codes for direct mode: 0 ok, 1 stale/missing, 2 framework error.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from health_beacon import all_beacons  # noqa: E402

# (component_name, max_age_seconds)
CRITICAL_COMPONENTS: List[Tuple[str, int]] = [
    ("learning_loop", 24 * 3600),
    ("intelligence_daemon", 5 * 60),
    ("build_t0_state", 30 * 60),
    ("compact_state", 24 * 3600),
    ("conversation_analyzer", 24 * 3600),
]


def _resolve_data_dir() -> Path:
    try:
        from project_root import resolve_data_dir
        return resolve_data_dir(__file__)
    except Exception:
        env = os.environ.get("VNX_DATA_DIR")
        if env:
            return Path(env)
        return _REPO_ROOT / ".vnx-data"


def _format_failure(name: str, age_seconds: float, max_seconds: int) -> str:
    age_h = age_seconds / 3600.0
    max_h = max_seconds / 3600.0
    return (
        f"Component {name} has stale heartbeat "
        f"({age_h:.2f} hours old, expected <{max_h:.2f} hours)"
    )


def _classify(beacons: Dict[str, dict]) -> Tuple[List[str], List[str]]:
    missing: List[str] = []
    stale: List[str] = []
    now = time.time()
    for name, max_age in CRITICAL_COMPONENTS:
        beacon = beacons.get(name)
        if beacon is None:
            missing.append(name)
            continue
        last_ts = beacon.get("last_run_ts")
        try:
            last_ts_f = float(last_ts) if last_ts is not None else None
        except (TypeError, ValueError):
            last_ts_f = None
        if last_ts_f is None:
            stale.append(_format_failure(name, float("inf"), max_age))
            continue
        age = now - last_ts_f
        if age > max_age:
            stale.append(_format_failure(name, age, max_age))
        if beacon.get("status") == "fail":
            stale.append(f"Component {name} reported status=fail")
    return missing, stale


def _should_require_beacons() -> bool:
    return os.environ.get("VNX_SMOKE_REQUIRE_BEACONS") == "1"


def test_critical_beacons_present_and_fresh() -> None:
    data_dir = _resolve_data_dir()
    health_dir = data_dir / "health"

    if not health_dir.exists():
        if _should_require_beacons():
            pytest.fail(
                f"health directory missing at {health_dir} "
                "(VNX_SMOKE_REQUIRE_BEACONS=1)"
            )
        pytest.skip(f"no beacons present at {health_dir} (skip in bare env)")

    beacons = all_beacons(data_dir)
    if not beacons and not _should_require_beacons():
        pytest.skip("beacon directory empty (skip in bare env)")

    missing, stale = _classify(beacons)

    if missing and not _should_require_beacons():
        all_critical_missing = len(missing) == len(CRITICAL_COMPONENTS)
        if all_critical_missing:
            pytest.skip(
                "no critical beacons present yet — likely fresh checkout"
            )

    assert not missing, f"missing beacons: {missing}"
    assert not stale, "stale heartbeats:\n  - " + "\n  - ".join(stale)


def main() -> int:
    data_dir = _resolve_data_dir()
    try:
        beacons = all_beacons(data_dir)
    except Exception as exc:
        print(f"FRAMEWORK_ERROR: {exc}", file=sys.stderr)
        return 2

    missing, stale = _classify(beacons)
    bad = [f"{n}=missing" for n in missing] + stale
    if bad:
        print("UNHEALTHY:")
        for line in bad:
            print(f"  - {line}")
        return 1
    print("OK: all critical beacons fresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
