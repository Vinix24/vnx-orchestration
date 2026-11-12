"""PR-10 ratchet: the legacy dispatcher lane forwards its dispatch id to the gather.

``gather_dispatch_intelligence`` in ``scripts/dispatcher_minimal.sh`` is the parked
rollback lane (``VNX_DISPATCH_LEGACY=1`` / ``VNX_SINGLE_ENTRY_DISPATCH=0``). It called
``gather_intelligence.py gather <task> <terminal> <role> <gate>`` — four positional
arguments, no dispatch id — so an injection row written through that lane could never be
tied back to its dispatch. This file pins the fifth argument (the dispatch id, last) and
pins that the single-entry door does not route through ``dispatcher_minimal.sh`` at all.

Both tests execute the shipped code: the gather function is extracted verbatim from
``dispatcher_minimal.sh`` and run in a bash harness with a stub ``gather_intelligence.py``
that records its argv; the door test runs the real ``scripts/commands/dispatch.sh``
routing with a stub door (``dispatch_cli.py``) and a sentinel legacy lane. Nothing here
reimplements the logic under test.
"""
from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = REPO_ROOT / "scripts" / "dispatcher_minimal.sh"
DISPATCH_SH = REPO_ROOT / "scripts" / "commands" / "dispatch.sh"

DISPATCH_ID = "20261110-dlv-f4db674ff5e3"
TASK = "fix(dispatch): the legacy lane passes dispatch_id"
ROLE = "backend-developer"
GATE = "codex"
TRACK = "A"
EXPECTED_TERMINAL = "T1"


def _extract_function(name: str, source: str) -> str:
    """Extract a top-level bash function definition (`name() { ... }`) verbatim.

    Relies on the project convention that top-level functions open with `name() {`
    and close with `}` at column 0 (same helper shape as
    tests/test_dispatcher_resilience.py). Returns the shipped text, so the harness runs
    the real logic rather than a copy.
    """
    pattern = re.compile(
        r"^" + re.escape(name) + r"\(\) \{\n.*?^\}\n",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(source)
    if not match:
        raise AssertionError(f"could not extract function {name}() from {DISPATCHER}")
    return match.group(0)


# Stub gather CLI: records its argv (NUL-separated) and answers like a quiet gather so the
# lane proceeds past the "0 candidates" warning without writing anything.
_STUB_GATHER = (
    "import json, os, sys\n"
    'open(os.environ["VNX_TEST_ARGV_MARKER"], "w").write("\\0".join(sys.argv[1:]))\n'
    'print(json.dumps({"pattern_count": 0, "prevention_rule_count": 0}))\n'
)

# Stub door: records argv and exits 0. Never dispatches anything real.
_STUB_DOOR = (
    "import os, sys\n"
    'open(os.environ["VNX_TEST_DOOR_MARKER"], "w").write("\\0".join(sys.argv[1:]))\n'
)

# Sentinel legacy lane: only writes its marker if someone actually executes it.
_STUB_LEGACY_LANE = (
    "#!/bin/bash\n"
    'printf "reached\\n" > "${VNX_TEST_LEGACY_MARKER}"\n'
)


def _run_gather_harness(tmp_path: Path) -> list[str]:
    """Run the real gather_dispatch_intelligence against a stub gather CLI; return its argv."""
    stub_home = tmp_path / "stubhome"
    (stub_home / "scripts").mkdir(parents=True)
    (stub_home / "scripts" / "gather_intelligence.py").write_text(
        _STUB_GATHER, encoding="utf-8"
    )

    marker = tmp_path / "argv.marker"
    dispatch_file = tmp_path / "dispatch.md"
    dispatch_file.write_text("[[TARGET:T1]]\nRole: backend-developer\n", encoding="utf-8")

    fn_body = _extract_function("gather_dispatch_intelligence", DISPATCHER.read_text(encoding="utf-8"))

    harness = textwrap.dedent(
        f"""\
        set -euo pipefail

        # --- stubbed collaborators (the function under test is the shipped text) ---
        VNX_DIR='{stub_home}'
        export VNX_TEST_ARGV_MARKER='{marker}'
        log() {{ :; }}
        log_structured_failure() {{ :; }}
        extract_instruction_content() {{ printf '%s' "{TASK}"; }}
        track_to_terminal() {{ printf '%s' "{EXPECTED_TERMINAL}"; }}
        _PD_INTEL_RESULT=""

        {fn_body}

        gather_dispatch_intelligence '{dispatch_file}' '{ROLE}' '{TRACK}' '{DISPATCH_ID}' '{GATE}'
        """
    )
    proc = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"gather harness exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert marker.exists(), (
        f"stub gather_intelligence.py was never invoked\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return marker.read_text(encoding="utf-8").split("\0")


def _run_door_harness(tmp_path: Path) -> tuple[subprocess.CompletedProcess, Path, Path]:
    """Run the real cmd_dispatch door routing; return (proc, door_marker, legacy_marker)."""
    stub_home = tmp_path / "stubhome"
    (stub_home / "scripts" / "lib").mkdir(parents=True)
    (stub_home / "scripts" / "lib" / "dispatch_cli.py").write_text(_STUB_DOOR, encoding="utf-8")
    (stub_home / "scripts" / "dispatcher_minimal.sh").write_text(
        _STUB_LEGACY_LANE, encoding="utf-8"
    )
    (stub_home / "scripts" / "dispatcher_minimal.sh").chmod(0o755)

    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    dispatch_dir = tmp_path / "dispatches"
    pending_id = "20261110-item-door"
    bundle = dispatch_dir / "pending" / pending_id
    bundle.mkdir(parents=True)
    (bundle / "dispatch-spec.json").write_text("{}", encoding="utf-8")

    door_marker = tmp_path / "door.marker"
    legacy_marker = tmp_path / "legacy.marker"

    harness = textwrap.dedent(
        f"""\
        set -e
        VNX_HOME='{stub_home}'
        VNX_DATA_DIR='{data_dir}'
        VNX_DISPATCH_DIR='{dispatch_dir}'
        VNX_STATE_DIR='{data_dir}/state'
        export VNX_TEST_DOOR_MARKER='{door_marker}'
        export VNX_TEST_LEGACY_MARKER='{legacy_marker}'
        log() {{ :; }}
        err() {{ echo "[ERR] $*" >&2; }}
        source '{DISPATCH_SH}'
        VNX_SINGLE_ENTRY_DISPATCH=1
        unset VNX_DISPATCH_LEGACY
        cmd_dispatch '{pending_id}'
        """
    )
    proc = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    return proc, door_marker, legacy_marker


# --------------------------------------------------------------------------- #
# 1. The legacy lane forwards the dispatch id as the fifth gather argument
# --------------------------------------------------------------------------- #

def test_legacy_gather_passes_dispatch_id_as_fifth_argument(tmp_path: Path) -> None:
    """gather <task> <terminal> <role> <gate> <dispatch_id> — five positional args, id last."""
    argv = _run_gather_harness(tmp_path)

    assert argv[0] == "gather", f"unexpected subcommand in {argv!r}"
    positional = argv[1:]
    assert len(positional) == 5, (
        "the legacy lane must pass five positional arguments to "
        f"`gather_intelligence.py gather` (task, terminal, role, gate, dispatch_id); got {positional!r}"
    )
    assert positional[-1] == DISPATCH_ID, (
        f"the dispatch id must be the fifth (last) argument; got {positional!r}"
    )
    assert positional == [TASK, EXPECTED_TERMINAL, ROLE, GATE, DISPATCH_ID], (
        f"unexpected gather arguments: {positional!r}"
    )


# --------------------------------------------------------------------------- #
# 2. With the single-entry door on, the door never reaches dispatcher_minimal.sh
# --------------------------------------------------------------------------- #

def test_door_does_not_reach_dispatcher_minimal(tmp_path: Path) -> None:
    """VNX_SINGLE_ENTRY_DISPATCH=1 routes a staged dispatch through the door stub and never
    executes the legacy lane sentinel at VNX_HOME/scripts/dispatcher_minimal.sh."""
    proc, door_marker, legacy_marker = _run_door_harness(tmp_path)

    assert proc.returncode == 0, f"rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert door_marker.exists(), (
        f"the single-entry door (dispatch_cli.py) was NOT invoked\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert not legacy_marker.exists(), (
        "the door reached dispatcher_minimal.sh — the legacy lane must not run under "
        "VNX_SINGLE_ENTRY_DISPATCH=1"
    )
