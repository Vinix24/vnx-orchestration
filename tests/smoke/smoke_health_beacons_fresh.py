"""Smoke test — assert beacons exist and are fresh after dispatcher startup.

Designed to be run after the dispatcher / intelligence daemons have had a
chance to fire at least one heartbeat. It checks that each "critical"
component listed below has a beacon file and that its classified health
is ``ok``.

Run as a pytest module:

    pytest tests/smoke/smoke_health_beacons_fresh.py -xvs

or directly (exit 0 on green, 1 on any non-ok component, 2 on framework
errors). The component list intentionally includes only the long-lived,
always-on services. Event-driven beacons (cleanup_worker_exit) are not
asserted here — by definition they do not run on a schedule.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from health_beacon import all_beacons  # noqa: E402

CRITICAL_COMPONENTS: List[str] = [
    "intelligence_daemon",
    "t0_state_builder",
]


def _resolve_data_dir() -> Path:
    try:
        from project_root import resolve_data_dir
        return resolve_data_dir(__file__)
    except Exception:
        return _REPO_ROOT / ".vnx-data"


@pytest.mark.skipif(
    os.environ.get("VNX_RUN_SMOKE_HEALTH") != "1",
    reason="Smoke test only runs when VNX_RUN_SMOKE_HEALTH=1",
)
def test_critical_beacons_present_and_fresh() -> None:
    data_dir = _resolve_data_dir()
    beacons = all_beacons(data_dir)

    missing: list[str] = []
    not_ok: list[tuple[str, str]] = []

    for name in CRITICAL_COMPONENTS:
        if name not in beacons:
            missing.append(name)
            continue
        health = beacons[name].get("health", "unknown")
        if health != "ok":
            not_ok.append((name, health))

    assert not missing, f"missing beacons: {missing}"
    assert not not_ok, f"non-ok beacons: {not_ok}"


def main() -> int:
    data_dir = _resolve_data_dir()
    try:
        beacons = all_beacons(data_dir)
    except Exception as exc:
        print(f"FRAMEWORK_ERROR: {exc}", file=sys.stderr)
        return 2

    bad = []
    for name in CRITICAL_COMPONENTS:
        b = beacons.get(name)
        if b is None:
            bad.append(f"{name}=missing")
        elif b.get("health") != "ok":
            bad.append(f"{name}={b.get('health')}")

    if bad:
        print("UNHEALTHY: " + ", ".join(bad))
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
