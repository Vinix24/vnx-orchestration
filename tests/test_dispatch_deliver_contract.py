"""Contract test for the terminal-binding selector, flipped by T-G1.

``deliver_dispatch_to_terminal`` in ``scripts/lib/dispatch_deliver.sh`` used to read
``VNX_ADAPTER_T{n}`` for every terminal, so T1/T2/T3 could opt into tmux. T-G1 removed
the worker half: T1, T2 and T3 always take the subprocess branch and no value moves them
back. T0 keeps its behaviour -- tmux by default, subprocess only on the exact value
``subprocess``.

Each case sources the real library in a real ``bash``, stubs both branch entry points
and calls the delivery function once; the assertion is the NUMBER of calls per branch.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DELIVER = REPO_ROOT / "scripts" / "lib" / "dispatch_deliver.sh"
# The red run sources an inverted scratch copy from $TMPDIR through this
# variable; CI and normal runs leave it unset, so the shipped library is what
# the suite exercises.
_OVERRIDE = os.environ.get("VNX_TEST_DISPATCH_DELIVER", "")
DISPATCH_DELIVER = Path(_OVERRIDE) if _OVERRIDE else DEFAULT_DELIVER

SUBPROCESS = "subprocess"
TMUX = "tmux"

# (terminal, VNX_ADAPTER_<terminal> value or None for unset, expected branch).
# T0 has no default-subprocess rule, so unset T0 stays on the tmux branch; it
# reaches the subprocess branch only through the exact value "subprocess".
# A worker terminal (T1-T3) is always subprocess: the variable is not read, so
# "tmux" and an unrecognised value are subprocess cases too (T-G1).
CASES = [
    ("T0", None, TMUX),
    ("T0", "subprocess", SUBPROCESS),
    ("T0", "tmux", TMUX),
    ("T0", "other", TMUX),
    ("T1", None, SUBPROCESS),
    ("T1", "subprocess", SUBPROCESS),
    ("T1", "tmux", SUBPROCESS),
    ("T1", "other", SUBPROCESS),
    ("T2", None, SUBPROCESS),
    ("T2", "tmux", SUBPROCESS),
    ("T3", None, SUBPROCESS),
    ("T3", "tmux", SUBPROCESS),
]
CASE_IDS = [f"{terminal}-{value or 'unset'}-to-{branch}" for terminal, value, branch in CASES]


def _harness(tmp_path: Path, terminal_id: str, adapter_value: str | None) -> str:
    dispatch_file = tmp_path / "dispatch.md"
    dispatch_file.write_text("# dispatch\n", encoding="utf-8")
    call_log = tmp_path / "calls.log"
    call_log.write_text("", encoding="utf-8")
    # An empty VNX_DIR keeps the pre-delivery worktree lookup from resolving a
    # path, so the tmux branch never sends a `cd`; everything outside the
    # selector is stubbed below.
    vnx_dir = tmp_path / "vnxdir"
    (vnx_dir / "scripts").mkdir(parents=True)
    project_root = tmp_path / "project"
    project_root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    set_adapter = ""
    if adapter_value is not None:
        set_adapter = f"export VNX_ADAPTER_{terminal_id}={shlex.quote(adapter_value)}\n"
    return f"""set -uo pipefail
# The caller's environment must not decide the case: start from "unset".
unset VNX_ADAPTER_T0 VNX_ADAPTER_T1 VNX_ADAPTER_T2 VNX_ADAPTER_T3
{set_adapter}STATE_DIR={shlex.quote(str(state_dir))}
VNX_DIR={shlex.quote(str(vnx_dir))}
PROJECT_ROOT={shlex.quote(str(project_root))}
VNX_DISPATCH_MAX_INLINE=50000
VNX_DISPATCH_PAYLOAD_DIR={shlex.quote(str(tmp_path / 'payload'))}
CALL_LOG={shlex.quote(str(call_log))}
log() {{ :; }}
log_structured_failure() {{ :; }}
# shellcheck source=/dev/null
source {shlex.quote(str(DISPATCH_DELIVER))}
# Both branch entry points become recorders; neither branch body runs.
_ddt_subprocess_delivery() {{ printf 'subprocess\\n' >> "$CALL_LOG"; return 0; }}
pane_inject_prompt() {{ printf 'tmux\\n' >> "$CALL_LOG"; return 0; }}
check_pane_input_ready() {{ return 0; }}
tmux_retry() {{ return 0; }}
rc_release_on_failure() {{ :; }}
release_terminal_claim() {{ return 0; }}
deliver_dispatch_to_terminal {shlex.quote(str(dispatch_file))} "A" "backend-developer" \\
    "dispatch-1" "pane1" {shlex.quote(terminal_id)} "claude_code" "body" "/skill"
echo "rc=$?"
"""


def _run_harness(tmp_path: Path, script: str) -> subprocess.CompletedProcess:
    harness = tmp_path / "harness.sh"
    harness.write_text(script, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("VNX_ADAPTER_")}
    return subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True, timeout=120, cwd=str(tmp_path), env=env
    )


@pytest.mark.parametrize(("terminal_id", "adapter_value", "expected"), CASES, ids=CASE_IDS)
def test_selector_calls_exactly_one_branch(tmp_path, terminal_id, adapter_value, expected):
    result = _run_harness(tmp_path, _harness(tmp_path, terminal_id, adapter_value))
    assert result.returncode == 0, result.stderr
    assert "rc=0" in result.stdout, result.stdout

    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    other = TMUX if expected == SUBPROCESS else SUBPROCESS
    assert calls.count(expected) == 1, f"expected exactly one {expected} call, recorded {calls}"
    assert calls.count(other) == 0, f"expected zero {other} calls, recorded {calls}"
    assert calls == [expected], calls


def test_selector_never_takes_both_branches_for_one_delivery(tmp_path):
    """The invariant behind the per-case counts: one delivery, one branch."""
    recorded = {}
    for terminal_id, adapter_value, _expected in CASES:
        case_dir = tmp_path / f"{terminal_id}-{adapter_value or 'unset'}"
        case_dir.mkdir()
        result = _run_harness(case_dir, _harness(case_dir, terminal_id, adapter_value))
        assert result.returncode == 0, result.stderr
        recorded[(terminal_id, adapter_value)] = (case_dir / "calls.log").read_text(
            encoding="utf-8"
        ).splitlines()
    assert all(len(calls) == 1 for calls in recorded.values()), recorded
