#!/usr/bin/env python3
"""Tests for OI-1409 — the launchd drive behind scripts/ledger_health.py.

``ledger_health.py`` existed, worked, mutated nothing, and had no driver:
nothing ever invoked it, so its beacon (and the doctor check that reads it,
``vnx_cli/commands/doctor.py::_check_ledger_health``) only ever reflected
whatever state existed the one time a human ran it by hand. This file locks
in the two behaviors the launchd drive (``scripts/launchd/com.vnx.ledger-
health.plist`` + ``init_cmd._install_ledger_health_runner``) exists to
guarantee — not "the plist file exists" and not "a constant equals X"
(memory: a test on the constant's literal value or the file's mere presence
expires the moment either is renamed):

  1. The chosen launchd ``StartInterval`` is provably smaller than
     ``ledger_health.BEACON_EXPECTED_INTERVAL_SECONDS`` — both values read
     live (the interval from the plist actually produced by
     ``_install_ledger_health_runner``, the same install path ``vnx init``
     runs; the threshold straight from the ``ledger_health`` module), never
     copied as a literal into this file.
  2. When ``ledger_health.py`` reruns on that cadence,
     ``health_beacon.all_beacons`` never classifies its beacon as stale;
     when nothing reruns it at all, the same beacon goes stale exactly at
     ``BEACON_EXPECTED_INTERVAL_SECONDS`` — the pre-dispatch failure mode,
     reproduced here instead of just asserted away.

Both timestamps are forced via ``monkeypatch`` on ``time.time`` in the
``ledger_health`` and ``health_beacon`` modules — never read off wall-clock
reality, which would only ever prove "worked once, on this machine, today".
"""
from __future__ import annotations

import os as _os
import plistlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(VNX_ROOT))

import ledger_health as lh  # noqa: E402
import health_beacon  # noqa: E402
from vnx_cli.commands import init_cmd  # noqa: E402

PLIST_NAME = "com.vnx.ledger-health"


def _install_and_read_interval(tmp_path, monkeypatch) -> int:
    """Run the REAL install path (``init_cmd._install_ledger_health_runner``)
    against a fake engine root + fake home, then parse the ``StartInterval``
    that actually lands on disk. Exercises the same code ``vnx init`` runs —
    not a hand-copied number that could drift from what gets installed.

    Fake engine root lives under ``tmp_path`` (never under
    ``.vnx-data/worktrees/``, which this very test suite's real repo path
    is) so the OI-1117 worktree guard in ``_install_launchd_agent`` does not
    fire and silently skip the install.
    """
    real_engine = init_cmd._engine.engine_root()
    fake_engine_root = tmp_path / "fake-vnx-engine"
    fake_engine_root.mkdir()
    _os.symlink(
        real_engine / "scripts", fake_engine_root / "scripts", target_is_directory=True
    )
    monkeypatch.setattr(init_cmd._engine, "engine_root", lambda: fake_engine_root)

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = 0
        m.stderr = ""
        m.stdout = f"{PLIST_NAME}\n" if cmd == ["launchctl", "list"] else ""
        return m

    monkeypatch.setattr(init_cmd.subprocess, "run", fake_run)

    installed = init_cmd._install_ledger_health_runner(
        str(fake_engine_root), project_id="test-project"
    )
    assert installed is True, (
        "_install_ledger_health_runner returned False — the plist template "
        "or the install wiring is missing (OI-1409 not implemented)"
    )

    dest = fake_home / "Library" / "LaunchAgents" / f"{PLIST_NAME}.plist"
    with dest.open("rb") as fh:
        data = plistlib.load(fh)
    return int(data["StartInterval"])


class TestLedgerHealthLaunchdCadence:
    def test_start_interval_is_below_beacon_staleness_threshold(self, tmp_path, monkeypatch):
        """The interval the job actually installs with must stay under the
        module's own staleness window, or the beacon this job writes goes
        stale by construction between every pair of runs."""
        interval = _install_and_read_interval(tmp_path, monkeypatch)
        assert interval < lh.BEACON_EXPECTED_INTERVAL_SECONDS, (
            f"StartInterval={interval}s must be below "
            f"BEACON_EXPECTED_INTERVAL_SECONDS={lh.BEACON_EXPECTED_INTERVAL_SECONDS}s"
        )

    def test_beacon_stays_fresh_across_cadence_but_goes_stale_without_a_rerun(
        self, tmp_path, monkeypatch
    ):
        interval = _install_and_read_interval(tmp_path, monkeypatch)

        data_dir = tmp_path / "data"
        state_dir = data_dir / "state"
        state_dir.mkdir(parents=True)
        # Minimal but real (parseable, empty) ledger/register — enough for
        # compute_health to run every check to a real STATUS_OK/finding,
        # never SKIPPED_UNVERIFIED, which would leave the beacon's status
        # field ambiguous for this test's purpose (staleness, not findings).
        (state_dir / lh.REGISTER_NAME).write_text("", encoding="utf-8")
        (state_dir / lh.LEDGER_NAME).write_text("", encoding="utf-8")

        t0 = 1_800_000_000.0
        monkeypatch.setattr(lh.time, "time", lambda: t0)
        result = lh.compute_health(data_dir, state_dir)
        lh.write_health_surface(data_dir, result)

        # One tick before the NEXT scheduled launchd run: if the job fires
        # every `interval` seconds as installed, the beacon is at most
        # `interval` seconds old at any point in the cycle.
        t_within_cadence = t0 + interval - 1
        monkeypatch.setattr(health_beacon.time, "time", lambda: t_within_cadence)
        beacons = health_beacon.all_beacons(data_dir)
        assert beacons["ledger_health"]["health"] != "stale", (
            f"beacon read {interval - 1}s after its last write must not be stale — "
            f"that is well inside the {interval}s launchd cadence this job installs"
        )

        # No rerun at all, all the way past the module's own staleness
        # window — the pre-dispatch state: ledger_health.py existed, wrote a
        # beacon once, and nothing ever invoked it again.
        t_beyond_threshold = t0 + lh.BEACON_EXPECTED_INTERVAL_SECONDS + 1
        monkeypatch.setattr(health_beacon.time, "time", lambda: t_beyond_threshold)
        beacons_stale = health_beacon.all_beacons(data_dir)
        assert beacons_stale["ledger_health"]["health"] == "stale", (
            "a beacon nobody ever reruns must read stale once its age passes "
            f"BEACON_EXPECTED_INTERVAL_SECONDS={lh.BEACON_EXPECTED_INTERVAL_SECONDS}s — "
            "this is the exact 'mechanism built, never driven' defect OI-1409 fixes"
        )
