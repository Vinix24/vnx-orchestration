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

D3b2 fix-forward: `.claude/vnx-system/` is also excluded from that guard's
grep. CI's own VNX CI workflow rsyncs the whole repo into that directory
(`rsync -a --exclude '.git' --exclude '.claude' ./
"$GITHUB_WORKSPACE/.claude/vnx-system/"`) before running these tests, so on
CI it holds a full second copy of every caller in the baseline below. Those
are not additional call sites of the fabric -- they are copies of the ones
already counted at repo root -- so counting them would double the number on
every CI run while a local run (no rollout copy) still saw the true count.

OI-1594 fix-forward, two more defects in the same guard: (1) the grep matched
*mentions* of the function names — a docstring or comment referencing
`all_beacons()`/`beacon_summary()` counted the same as an actual call, which
is why landing scripts/lib/beacon_register.py (a docstring mention only)
turned this guard red on the very commit that introduced it. Replaced with
an AST scan for real `ast.Call` nodes. (2) the grep never excluded
`.vnx-data/` — a worktree nested inside the repo (this project's normal
local layout, see `.vnx-data/worktrees/`) doubled the count the same way
`.claude/vnx-system/` did on CI. Added to the exclusion set alongside
`tests` and `.claude`.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

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


_CALL_SITE_TARGETS = frozenset({"all_beacons", "beacon_summary"})
_CALL_SITE_EXCLUDE_DIRS = frozenset({"tests", ".claude", ".vnx-data"})


def _find_call_sites(root: Path, exclude_dirs: frozenset[str] = _CALL_SITE_EXCLUDE_DIRS) -> set[str]:
    """AST scan for real `ast.Call` nodes targeting `all_beacons`/
    `beacon_summary`, rooted at `root`.

    A name that only appears in a docstring or a comment is not a call
    site — the grep this replaces could not tell the two apart (OI-1594).
    Matches on the bare function name, whether called directly
    (`all_beacons(...)`) or via attribute access
    (`health_beacon.all_beacons(...)`), which is exactly what the
    dispatch's literal `beacon_summary\\|all_beacons` grep pattern matched
    too — this only narrows *which* matches count, not what they match on.
    """
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            full_path = Path(dirpath) / filename
            tree = ast.parse(full_path.read_text(encoding="utf-8"), filename=str(full_path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                else:
                    continue
                if name in _CALL_SITE_TARGETS:
                    found.add("./" + str(full_path.relative_to(root)))
    return found


class TestCallSiteCountDoesNotGrow:
    """D3b Klaar-wanneer #2: "Het aantal wachters is niet gegroeid" has a
    count rule — originally the dispatch's own prescribed command, run
    verbatim:

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
    never sees, regardless of what it shells out to.

    D3b2 fix-forward: `--exclude-dir=.claude` is added to the dispatch's
    literal command. `.claude/vnx-system/` is a rolled-out COPY of this
    repo's fabric (see the module docstring), not a second source tree.
    VNX CI's own workflow rsyncs the whole repo into it before the test
    suite runs, so on CI every path in _BASELINE below also exists a second
    time under `./.claude/vnx-system/...`, doubling the matched set to 20
    without a single new caller. A run on a checkout that never had that
    rollout (a plain local clone) would still see 10 and pass, which is
    exactly why this only broke on CI and not locally: the grep's answer
    depended on which environment it ran in, not on the code it counted.
    Excluding `.claude` makes both environments agree.

    OI-1594 fix-forward, replacing the grep with `_find_call_sites`: the
    grep-based guard went red on the very commit that introduced it
    (scripts/lib/beacon_register.py, landed one commit earlier), for a name
    it only mentions in two docstrings, never calls. Text-matching cannot
    tell "the name appears" from "the name is called" apart; an AST scan
    for `ast.Call` nodes can. Re-running that scan drops the baseline from
    10 to 7: scripts/lib/beacon_register.py,
    scripts/lib/effectiveness_probe.py,
    scripts/lib/report_to_receipt_converter.py, and
    scripts/ledger_health.py all mention `all_beacons`/`beacon_summary` in
    a docstring or comment with zero real calls, and drop out. The
    remaining 7 (dashboard/api_health.py, dashboard/api_subsystems.py,
    scripts/build_t0_state.py, scripts/health_check.py,
    scripts/lib/health_beacon.py — its own `beacon_summary()` calls
    `all_beacons()` internally — vnx_cli/commands/doctor.py,
    vnx_cli/commands/subsystems.py) each contain a genuine
    `all_beacons(...)`/`beacon_summary(...)` call.

    Second defect fixed here: the exclusion set gains `.vnx-data`, alongside
    `tests` and `.claude`. A worktree nested under `.vnx-data/worktrees/`
    is this project's normal local layout (this file's own worktree is one),
    not an exception — the same class of double-count `.claude/vnx-system/`
    caused on CI, just triggered locally instead.

    Open design question for the operator, deliberately NOT decided here:
    whether the guard should stay a fixed namelist (current choice — it
    forces a human to look at every addition) or become a property (e.g.
    "every real caller lives under dashboard/, scripts/, or vnx_cli/").
    A property would have let scripts/lib/beacon_register.py's docstring
    mentions pass silently along with any *actual* new caller placed under
    those same trees, which is the opposite of what this guard is for."""

    _BASELINE = {
        "./dashboard/api_health.py",
        "./dashboard/api_subsystems.py",
        "./scripts/build_t0_state.py",
        "./scripts/health_check.py",
        "./scripts/lib/health_beacon.py",
        "./vnx_cli/commands/doctor.py",
        "./vnx_cli/commands/subsystems.py",
    }

    def test_grep_count_matches_the_dispatchs_own_command(self):
        matched = _find_call_sites(REPO)
        assert matched == self._BASELINE, (
            f"caller set changed: added={matched - self._BASELINE}, "
            f"removed={self._BASELINE - matched}"
        )


class TestCallSiteGuardCanActuallyFail:
    """A wachter die niet kan falen, faalt stil. `_find_call_sites` is
    exercised directly against isolated fixture trees (never the real repo)
    so this guard's *mechanism* is proven, not just today's baseline."""

    def test_a_real_new_caller_turns_the_assertion_red(self, tmp_path):
        (tmp_path / "existing.py").write_text(
            "from health_beacon import all_beacons\nall_beacons(x)\n", encoding="utf-8",
        )
        baseline = _find_call_sites(tmp_path)

        (tmp_path / "new_caller.py").write_text(
            "from health_beacon import beacon_summary\nbeacon_summary(x)\n", encoding="utf-8",
        )
        matched = _find_call_sites(tmp_path)

        assert matched != baseline
        with pytest.raises(AssertionError):
            assert matched == baseline

    def test_a_docstring_mention_in_a_new_file_does_not_trip_it(self, tmp_path):
        (tmp_path / "existing.py").write_text(
            "from health_beacon import all_beacons\nall_beacons(x)\n", encoding="utf-8",
        )
        baseline = _find_call_sites(tmp_path)

        (tmp_path / "mentions_only.py").write_text(
            '"""Eventually calls all_beacons() and beacon_summary()."""\n'
            "# see all_beacons for details\n",
            encoding="utf-8",
        )
        matched = _find_call_sites(tmp_path)

        assert matched == baseline

    def test_a_hit_under_vnx_data_does_not_trip_it(self, tmp_path):
        (tmp_path / "existing.py").write_text(
            "from health_beacon import all_beacons\nall_beacons(x)\n", encoding="utf-8",
        )
        baseline = _find_call_sites(tmp_path)

        nested = tmp_path / ".vnx-data" / "worktrees" / "dispatch-example" / "scripts"
        nested.mkdir(parents=True)
        (nested / "build_t0_state.py").write_text(
            "from health_beacon import beacon_summary\nbeacon_summary(x)\n", encoding="utf-8",
        )
        matched = _find_call_sites(tmp_path)

        assert matched == baseline

    def test_a_hit_under_claude_does_not_trip_it(self, tmp_path):
        (tmp_path / "existing.py").write_text(
            "from health_beacon import all_beacons\nall_beacons(x)\n", encoding="utf-8",
        )
        baseline = _find_call_sites(tmp_path)

        nested = tmp_path / ".claude" / "vnx-system" / "scripts"
        nested.mkdir(parents=True)
        (nested / "build_t0_state.py").write_text(
            "from health_beacon import beacon_summary\nbeacon_summary(x)\n", encoding="utf-8",
        )
        matched = _find_call_sites(tmp_path)

        assert matched == baseline


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
