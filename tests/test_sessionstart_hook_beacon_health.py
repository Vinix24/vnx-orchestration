#!/usr/bin/env python3
"""tests/test_sessionstart_hook_beacon_health.py — D3b (absence-is-loud): the
SessionStart hook surfaces component beacon health so a human sees it without
opening anything.

Defect this closes: `scripts/lib/health_beacon.all_beacons()` /
`beacon_summary()` already aggregate every component's heartbeat correctly,
but none of the five pre-existing readers
(scripts/build_t0_state.py, dashboard/api_health.py,
dashboard/api_subsystems.py, vnx_cli/commands/subsystems.py,
vnx_cli/commands/doctor.py — plus scripts/health_check.py, a sixth this test
file's own measurement found the plan's table had missed) sit in a human's
path at the moment it matters. Two live behind a dashboard nobody has open,
two behind a CLI nobody is running, and the build_t0_state.py projection
(t0_state.json) is only as fresh as its own refresh hook — which is a
separate, unmerged concern (D1).

hooks/sessionstart.sh fires on every session, for every T0 terminal, without
the human doing anything extra. This test drives the REAL hook (bash) against
a fixture beacon store (never the live project store — the live store moves
under a test's feet, see the D3b dispatch's "rood-voor-tests draaien op een
FIXTURE-store" rule) and asserts a non-ok beacon is visible in the injected
additionalContext.

Isolation: VNX_DATA_HOME is redirected to a tmp_path, exactly like
tests/test_sessionstart_hook_central_store.py — the whole point is exercising
the same "no repo-local state, central-store resolution" path production
hits, without ever touching this repo's own .vnx-data or the developer's
live ~/.vnx-data/<project>/ store.

Regression guard included: the number of *.py files calling
health_beacon.all_beacons()/beacon_summary() outside tests/ must not grow —
this hook shells out to the EXISTING scripts/health_check.py CLI rather than
importing health_beacon() directly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "sessionstart.sh"
LIB = REPO / "scripts" / "lib"
sys.path.insert(0, str(LIB))

import vnx_paths  # noqa: E402

_VNX_ENV_VARS = (
    "VNX_DATA_DIR",
    "VNX_STATE_DIR",
    "VNX_DATA_DIR_EXPLICIT",
    "VNX_DATA_HOME",
    "VNX_PROJECT_ID",
    "VNX_HOME",
    "VNX_BIN",
    "VNX_EXECUTABLE",
    "VNX_CANONICAL_ROOT",
    "VNX_PROJECT_ROOT",
)


def _clean_env(monkeypatch) -> None:
    for var in _VNX_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _make_terminal_project(tmp_path: Path, name: str = "project") -> Path:
    root = tmp_path / name
    (root / ".vnx").mkdir(parents=True)
    t0_dir = root / ".claude" / "terminals" / "T0"
    t0_dir.mkdir(parents=True)
    return t0_dir


def _run_hook(cwd: Path, env: dict) -> dict:
    r = subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True, text=True, cwd=str(cwd), env=env, timeout=10,
    )
    assert r.returncode == 0, f"hook must exit 0: rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    return json.loads(r.stdout)


def _write_beacon(data_dir: Path, component: str, status: str, age_seconds: float = 0.0,
                   expected_interval_seconds: float | None = 86400) -> None:
    health_dir = data_dir / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    payload = {
        "component": component,
        "last_run_ts": int(now - age_seconds),
        "last_run_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age_seconds)),
        "status": status,
        "details": {},
        "expected_interval_seconds": expected_interval_seconds,
    }
    (health_dir / f"{component}.json").write_text(json.dumps(payload), encoding="utf-8")


class TestBeaconHealthDigest:
    def test_fail_beacon_surfaces_in_sessionstart_context(self, tmp_path, monkeypatch):
        """The named consumer (Klaar-wanneer #1): a fail beacon must appear in
        the SessionStart additionalContext without the human opening anything."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)
        monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "vnx-data-home"))
        env = dict(os.environ)

        data_dir = Path(vnx_paths.resolve_paths()["VNX_DATA_DIR"])
        _write_beacon(data_dir, "t0_state_builder", status="fail", age_seconds=3600)

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "[fail] t0_state_builder" in ctx, ctx
        assert "Beacon health:" in ctx, ctx
        assert "1 NOT ok" in ctx, ctx

    def test_stale_beacon_surfaces_even_though_it_self_reported_ok(self, tmp_path, monkeypatch):
        """A beacon older than its own expected_interval_seconds is
        classified 'stale' by all_beacons() regardless of the status it
        wrote — the hook must show the DERIVED health, not the self-reported
        one."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)
        monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "vnx-data-home"))
        env = dict(os.environ)

        data_dir = Path(vnx_paths.resolve_paths()["VNX_DATA_DIR"])
        # Wrote "ok" 64.6 days ago with a 1-day expected interval — the exact
        # learning_loop scenario measured in the D3a/D3b plan.
        _write_beacon(
            data_dir, "learning_loop", status="ok",
            age_seconds=64.6 * 86400, expected_interval_seconds=86400,
        )

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "[stale] learning_loop" in ctx, ctx

    def test_all_ok_says_so_explicitly(self, tmp_path, monkeypatch):
        """A healthy fleet must say 'all ok', not stay silent — silence here
        would be indistinguishable from 'beacon health unavailable'."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)
        monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "vnx-data-home"))
        env = dict(os.environ)

        data_dir = Path(vnx_paths.resolve_paths()["VNX_DATA_DIR"])
        _write_beacon(data_dir, "phantom_guard", status="ok", age_seconds=10)

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "Beacon health: 1 components, all ok" in ctx, ctx

    def test_zero_beacons_is_a_third_state_not_silent_ok(self, tmp_path, monkeypatch):
        """Niet-gemeten is een derde tak: zero beacons found must not read as
        'all ok' — it must say it cannot distinguish never-ran from
        nothing-to-report."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)
        monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "vnx-data-home-empty"))
        env = dict(os.environ)

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "0 component beacons found" in ctx, ctx
        assert "all ok" not in ctx, ctx

    def test_hook_is_valid_bash(self):
        result = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


class TestCallSiteCountDoesNotGrow:
    """D3b Klaar-wanneer #2: "Het aantal wachters is niet gegroeid" has a
    count rule — the dispatch's own prescribed command, run verbatim:

        grep -rn "beacon_summary\\|all_beacons" --include='*.py' . \\
            --exclude-dir=tests | cut -d: -f1 | sort -u

    The dispatch states this measured 5 files before this PR. Re-measured
    here rather than trusted (per this fase's own working rule: a number
    from a handoff is a claim, not a measurement) — it is actually 10, not
    5: the dispatch's own count (and the D3a plan table it draws from) only
    counted real `all_beacons(...)`/`beacon_summary(...)` call sites (and
    even then missed scripts/health_check.py — 6 real callers, not 5).
    scripts/lib/effectiveness_probe.py and
    scripts/lib/report_to_receipt_converter.py mention the function names in
    a docstring/comment with no call, and scripts/ledger_health.py and
    scripts/lib/health_beacon.py (the aggregator's own definition) round out
    the other 4. The literal command the dispatch gives counts all of them.

    This PR must not add a file to that set. It doesn't: the beacon digest
    lives in hooks/sessionstart.sh, a .sh file the --include='*.py' filter
    never sees, regardless of what it shells out to."""

    _BASELINE = {
        "./dashboard/api_health.py",
        "./dashboard/api_subsystems.py",
        "./scripts/build_t0_state.py",
        "./scripts/health_check.py",
        "./scripts/ledger_health.py",
        "./scripts/lib/effectiveness_probe.py",
        "./scripts/lib/health_beacon.py",
        "./scripts/lib/report_to_receipt_converter.py",
        "./vnx_cli/commands/doctor.py",
        "./vnx_cli/commands/subsystems.py",
    }

    def test_grep_count_matches_the_dispatchs_own_command(self):
        result = subprocess.run(
            "grep -rn \"beacon_summary\\|all_beacons\" --include='*.py' . "
            "--exclude-dir=tests | cut -d: -f1 | sort -u",
            shell=True, capture_output=True, text=True, cwd=str(REPO),
        )
        matched = {line for line in result.stdout.splitlines() if line.strip()}
        assert matched == self._BASELINE, (
            f"caller set changed: added={matched - self._BASELINE}, "
            f"removed={self._BASELINE - matched}"
        )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
