"""Rollback proof for T-G1 (plan-gate point 1).

T-G1 keeps the legacy entry point because the hard-parked rollback net uses it: under
``VNX_SINGLE_ENTRY_DISPATCH=0`` / ``VNX_DISPATCH_LEGACY=1`` the pool worker runs
``python -m scripts.lib.subprocess_dispatch`` and ``_ddt_subprocess_delivery`` calls
``scripts/lib/subprocess_dispatch.py``. One test per flag pins that route.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "scripts" / "lib"
DISPATCH_DELIVER = LIB / "dispatch_deliver.sh"
FLAGS = [pytest.param({"VNX_SINGLE_ENTRY_DISPATCH": "0"}, id="single-entry-off"),
         pytest.param({"VNX_DISPATCH_LEGACY": "1"}, id="legacy-flag")]


def _lib_path() -> None:
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))


@pytest.mark.parametrize("flags", FLAGS)
def test_pool_worker_command_names_the_legacy_entrypoint(monkeypatch, flags):
    """Each flag keeps the pool worker on `-m scripts.lib.subprocess_dispatch`."""
    for key, value in flags.items():
        monkeypatch.setenv(key, value)
    _lib_path()
    from pool_manager import _pool_worker_command

    command = _pool_worker_command("proj-1", "default", "T1", "backend-developer")

    assert "scripts.lib.subprocess_dispatch" in command, command
    assert "scripts.lib.pool_worker_runner" not in command, command


@pytest.mark.parametrize("entry", [["-m", "scripts.lib.subprocess_dispatch"],
                                   [str(LIB / "subprocess_dispatch.py")]],
                         ids=["module", "path"])
def test_legacy_entrypoint_still_runs(entry):
    """The entry point the rollback route names still starts and prints its usage."""
    result = subprocess.run([sys.executable, *entry, "--help"], cwd=str(ROOT),
                            capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr
    assert "Deliver dispatch via SubprocessAdapter" in result.stdout


def test_pool_heartbeat_loop_stays_for_the_rollback_route():
    """The legacy lease renewal belongs to the legacy route and stays with it."""
    _lib_path()
    import subprocess_dispatch

    assert callable(subprocess_dispatch._pool_heartbeat_loop)


def _run_delivery(tmp_path: Path, flags: dict) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Source the real helper with python3 stubbed: `-c` runs, a script call is logged."""
    call_log = tmp_path / "python_calls.log"
    call_log.write_text("", encoding="utf-8")
    dispatch_file = tmp_path / "dispatch.md"
    dispatch_file.write_text("# dispatch\n", encoding="utf-8")
    exports = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in flags.items())
    harness = tmp_path / "harness.sh"
    harness.write_text(f"""set -o pipefail
unset VNX_SINGLE_ENTRY_DISPATCH VNX_DISPATCH_LEGACY
{exports}
VNX_DIR={shlex.quote(str(ROOT))}
CALL_LOG={shlex.quote(str(call_log))}
_DL_RC_ATTEMPT_ID=""
_DL_RC_GENERATION=0
log() {{ :; }}
log_structured_failure() {{ :; }}
rc_release_on_failure() {{ :; }}
release_terminal_claim() {{ return 0; }}
python3() {{
    if [ "${{1:-}}" = "-c" ]; then command python3 "$@"; return $?; fi
    printf '%s\\n' "$*" >> "$CALL_LOG"
}}
source {shlex.quote(str(DISPATCH_DELIVER))}
_ddt_subprocess_delivery "T1" "dispatch-1" "body" "sonnet" {shlex.quote(str(dispatch_file))} \\
    "backend-developer" "claude_code"
echo "rc=$?"
""", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("VNX_SINGLE_ENTRY_DISPATCH", "VNX_DISPATCH_LEGACY")}
    result = subprocess.run(["bash", str(harness)], capture_output=True, text=True,
                            timeout=120, cwd=str(tmp_path), env=env)
    return result, call_log.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("flags", FLAGS)
def test_delivery_calls_the_legacy_entrypoint_under_a_flag(tmp_path, flags):
    """Under each flag the delivery helper calls subprocess_dispatch.py exactly once."""
    result, calls = _run_delivery(tmp_path, flags)

    assert result.returncode == 0, result.stderr
    assert "rc=0" in result.stdout, result.stdout
    entrypoint_calls = [c for c in calls if "scripts/lib/subprocess_dispatch.py" in c]
    assert len(entrypoint_calls) == 1, calls
    assert [c for c in calls if "dispatch_bridge.py" in c] == [], calls
    assert "--terminal-id T1" in entrypoint_calls[0], entrypoint_calls

