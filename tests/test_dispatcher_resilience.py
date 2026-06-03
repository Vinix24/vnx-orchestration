#!/usr/bin/env python3
"""Daemon-loop resilience tests for scripts/dispatcher_minimal.sh.

Regression coverage for the `set -euo pipefail` leak (OIs filed 2026-06-03):
the dispatcher daemon (`while true; do ... process_dispatches; sleep 2; done`)
died after a single scan whenever that scan delivered 0 dispatches — every
dispatch rejected, every dispatch skipped ("No track found"), or an empty
pending glob — because `process_dispatches` ended on `[ $count -gt 0 ] && log`,
which returns non-zero when count==0, and that status propagated to the bare
`process_dispatches` call in the loop, tripping `set -e`.

Same class as the documented `((count++))` -> `count=$((count+1))` fix.

These tests run the REAL `process_dispatches` function extracted verbatim from
the script (its control flow — the for-loop, the count logic, and the final
return — is the code under test) against stubbed collaborators, and assert the
function returns 0 on every quiet-scan outcome. A static guard check binds the
daemon-loop call site to the `|| ...` defense-in-depth guard.
"""
from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = REPO_ROOT / "scripts" / "dispatcher_minimal.sh"


def _extract_function(name: str, source: str) -> str:
    """Extract a top-level bash function definition (`name() { ... }`).

    Relies on the project convention that top-level functions open with
    `name() {` and close with `}` at column 0. Returns the verbatim text so the
    test runs the actual shipped logic, not a reimplementation.
    """
    pattern = re.compile(
        r"^" + re.escape(name) + r"\(\) \{\n.*?^\}\n",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(source)
    if not match:
        raise AssertionError(f"could not extract function {name}() from {DISPATCHER}")
    return match.group(0)


def _run_scan(scenario_setup: str) -> subprocess.CompletedProcess:
    """Build a harness that sources the real process_dispatches() and runs one
    scan under `set -euo pipefail`, exactly as the daemon loop calls it (bare,
    not guarded — so a non-zero return aborts the harness before SURVIVED prints).
    """
    source = DISPATCHER.read_text()
    process_dispatches = _extract_function("process_dispatches", source)

    harness = textwrap.dedent(
        """\
        set -euo pipefail

        # --- stubbed collaborators (the function under test is real) ---
        log() { :; }
        sleep() { :; }  # skip the inter-dispatch delay
        _maybe_runtime_supervise() { :; }
        _cleanup_stuck_dispatches() { :; }
        _unified_supervisor_lease_sweep_tick() { :; }
        _maybe_auto_seed_tracks() { :; }
        extract_agent_role() { echo "backend-developer"; }

        PENDING_DIR="$1"
        REJECTED_DIR="$2"

        __SCENARIO__

        # --- real function under test (extracted verbatim) ---
        __PROCESS_DISPATCHES__

        # Call it exactly as the daemon loop's bare call would. Under set -e a
        # non-zero return aborts here and SURVIVED is never printed.
        process_dispatches
        echo "SURVIVED:$?"
        """
    )
    harness = harness.replace("__SCENARIO__", scenario_setup)
    harness = harness.replace("__PROCESS_DISPATCHES__", process_dispatches)
    return harness


def _execute(scenario_setup: str, tmp_path: Path) -> subprocess.CompletedProcess:
    pending = tmp_path / "pending"
    rejected = tmp_path / "rejected"
    pending.mkdir(parents=True, exist_ok=True)
    rejected.mkdir(parents=True, exist_ok=True)
    script = _run_scan(scenario_setup)
    return subprocess.run(
        ["bash", "-c", script, "bash", str(pending), str(rejected)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_empty_pending_dir_survives(tmp_path):
    """Empty pending glob: for-loop body never runs, count stays 0."""
    proc = _execute("# pending dir left empty\n", tmp_path)
    assert proc.returncode == 0, f"daemon would have died: stderr={proc.stderr!r}"
    assert "SURVIVED:0" in proc.stdout, proc.stdout


def test_all_rejected_scan_survives(tmp_path):
    """Every dispatch rejected/skipped: validate returns 1, continue fires, count=0.

    This is the exact failure mode from the OI: a scan that only rejects/skips
    must not abort the daemon.
    """
    scenario = textwrap.dedent(
        """\
        validate_dispatch_preconditions() { return 1; }
        gather_dispatch_intelligence() { return 0; }
        execute_and_classify_dispatch() { return 0; }
        # two pending files that all get rejected
        : > "$PENDING_DIR/a.md"
        : > "$PENDING_DIR/b.md"
        """
    )
    proc = _execute(scenario, tmp_path)
    assert proc.returncode == 0, f"daemon would have died: stderr={proc.stderr!r}"
    assert "SURVIVED:0" in proc.stdout, proc.stdout


def test_all_skipped_scan_survives(tmp_path):
    """Dispatches skipped at the gather stage (count never increments)."""
    scenario = textwrap.dedent(
        """\
        # validate passes and sets the _PD_* globals exactly as the real
        # validator does (so $_PD_TRACK etc. are bound under set -u).
        validate_dispatch_preconditions() {
            _PD_MAPPED_ROLE=""; _PD_TRACK="A"; _PD_DISPATCH_ID="c"; _PD_GATE=""
            return 0
        }
        gather_dispatch_intelligence() { return 1; }
        execute_and_classify_dispatch() { return 0; }
        : > "$PENDING_DIR/c.md"
        """
    )
    proc = _execute(scenario, tmp_path)
    assert proc.returncode == 0, f"daemon would have died: stderr={proc.stderr!r}"
    assert "SURVIVED:0" in proc.stdout, proc.stdout


def test_successful_dispatch_still_returns_zero(tmp_path):
    """No regression: a scan that delivers >0 dispatches also returns 0."""
    scenario = textwrap.dedent(
        """\
        validate_dispatch_preconditions() {
            _PD_MAPPED_ROLE=""; _PD_TRACK="A"; _PD_DISPATCH_ID="d"; _PD_GATE=""
            return 0
        }
        gather_dispatch_intelligence() { _PD_INTEL_RESULT=""; return 0; }
        execute_and_classify_dispatch() { return 0; }
        : > "$PENDING_DIR/d.md"
        """
    )
    proc = _execute(scenario, tmp_path)
    assert proc.returncode == 0, f"daemon would have died: stderr={proc.stderr!r}"
    assert "SURVIVED:0" in proc.stdout, proc.stdout


def test_process_dispatches_ends_with_explicit_return_zero():
    """The real function must not end on a bare `[ ] && log` (the leak)."""
    source = DISPATCHER.read_text()
    func = _extract_function("process_dispatches", source)
    # Last non-empty, non-comment statement before the closing brace must be
    # `return 0`, guaranteeing a quiet scan never returns non-zero.
    body_lines = [
        ln.strip()
        for ln in func.splitlines()
        if ln.strip() and not ln.strip().startswith("#") and ln.strip() != "}"
    ]
    assert body_lines[-1] == "return 0", (
        "process_dispatches must end with explicit `return 0`; "
        f"found {body_lines[-1]!r}"
    )


def test_daemon_loop_guards_process_dispatches_call():
    """The daemon-loop call site must guard process_dispatches with `||`.

    Defense in depth against any future set -e leak inside the per-scan path.
    """
    source = DISPATCHER.read_text()
    # Find the bare-ish call inside the `while true` loop and assert it is guarded.
    assert re.search(r"process_dispatches \|\|", source), (
        "daemon loop must call `process_dispatches || ...` so a non-zero scan "
        "cannot abort the while-true loop under set -e"
    )


def test_set_e_is_active_in_dispatcher():
    """Sanity: the leak only matters because the script runs under set -e."""
    source = DISPATCHER.read_text()
    assert re.search(r"^set -euo pipefail$", source, re.MULTILINE)
