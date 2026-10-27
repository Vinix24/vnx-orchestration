"""Seam + characterization tests for the pane-injection move (P05).

``scripts/lib/pane_injection.sh`` now owns the provider-aware injection that
used to sit in ``dispatch_deliver.sh``. Every test starts a real ``bash`` with
a recording ``tmux`` stub first on ``PATH`` and asserts the argv that bash
actually produced — nothing here reimplements the sequence in Python.

Three seams: the provider branch (claude sends the skill command first, codex
pastes skill+prompt as one payload), the Enter keystroke (the orchestrator's
job, a separate ``send-keys``), and the failure substep (``load_buffer``,
which crosses the new file boundary as ``PANE_INJECT_FAILED_SUBSTEP``).  The
``test_delivery_*`` cases also pass on the code before the move: they are the
baseline the move preserves.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PANE_INJECTION = REPO_ROOT / "scripts" / "lib" / "pane_injection.sh"
DISPATCH_DELIVER = REPO_ROOT / "scripts" / "lib" / "dispatch_deliver.sh"

# A `load-buffer` with a trailing "-" reads its payload from stdin, so the stub
# drains stdin before it may fail: a closed pipe would be an unrelated error.
_TMUX_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> {log}
for arg in "$@"; do
    if [ "$arg" = "-" ]; then cat >/dev/null 2>&1 || true; fi
done
{maybe_fail}exit 0
"""
_PID_IN_BUFFER_NAME = re.compile(r"vnx-deliver-\d+-")


def _write_tmux_stub(tmp_path: Path, fail_on: str | None = None) -> tuple[Path, Path]:
    """A bin dir holding a recording ``tmux`` stub, and the log it writes."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "tmux.log"
    log.write_text("", encoding="utf-8")
    maybe_fail = ""
    if fail_on is not None:
        maybe_fail = f'if [ "$1" = {shlex.quote(fail_on)} ]; then exit 1; fi\n'
    stub = bin_dir / "tmux"
    stub.write_text(_TMUX_STUB.format(log=shlex.quote(str(log)), maybe_fail=maybe_fail), encoding="utf-8")
    stub.chmod(0o755)
    return bin_dir, log


def _run_harness(tmp_path: Path, script: str) -> subprocess.CompletedProcess:
    harness = tmp_path / "harness.sh"
    harness.write_text(script, encoding="utf-8")
    return subprocess.run(["bash", str(harness)], capture_output=True, text=True, timeout=120, cwd=str(tmp_path))


def _calls(log: Path) -> list[str]:
    """Recorded argv lines, with the caller's pid normalized out of the names."""
    lines = log.read_text(encoding="utf-8").splitlines()
    return [_PID_IN_BUFFER_NAME.sub("vnx-deliver-PID-", line) for line in lines]


def _injection_harness(bin_dir: Path, tmp_path: Path, *args: str) -> str:
    call = " ".join(shlex.quote(arg) for arg in args)
    return f"""set -uo pipefail
export PATH={shlex.quote(str(bin_dir))}:$PATH
VNX_DISPATCH_MAX_INLINE=50000
VNX_DISPATCH_PAYLOAD_DIR={shlex.quote(str(tmp_path / 'payload'))}
log() {{ :; }}
log_structured_failure() {{ :; }}
# shellcheck source=/dev/null
source {shlex.quote(str(PANE_INJECTION))}
pane_inject_prompt {call}
echo "rc=$?"
"""


def _delivery_harness(bin_dir: Path, tmp_path: Path) -> str:
    """The tmux delivery path with its outside world stubbed: a dispatch file,
    an empty VNX_DIR (so no worktree resolves and no `cd` is sent), a state dir
    and the lifecycle hooks the failure handler calls."""
    dispatch_file = tmp_path / "dispatch.md"
    dispatch_file.write_text("# dispatch\n", encoding="utf-8")
    vnx_dir = tmp_path / "vnxdir"
    (vnx_dir / "scripts").mkdir(parents=True)
    project_root = tmp_path / "project"
    project_root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return f"""set -uo pipefail
export PATH={shlex.quote(str(bin_dir))}:$PATH
export VNX_ADAPTER_T1=tmux
STATE_DIR={shlex.quote(str(state_dir))}
VNX_DIR={shlex.quote(str(vnx_dir))}
PROJECT_ROOT={shlex.quote(str(project_root))}
VNX_DISPATCH_MAX_INLINE=50000
VNX_DISPATCH_PAYLOAD_DIR={shlex.quote(str(tmp_path / 'payload'))}
_DL_RC_ATTEMPT_ID=""
_DL_RC_GENERATION=""
log() {{ :; }}
log_structured_failure() {{ :; }}
# shellcheck source=/dev/null
source {shlex.quote(str(DISPATCH_DELIVER))}
check_pane_input_ready() {{ return 0; }}
rc_release_on_failure() {{ :; }}
release_terminal_claim() {{ return 0; }}
deliver_dispatch_to_terminal {shlex.quote(str(dispatch_file))} "A" "backend-developer" \\
    "dispatch-1" "pane1" "T1" "claude_code" "body" "/skill"
echo "rc=$?"
echo "_DDT_FAILED_SUBSTEP=$_DDT_FAILED_SUBSTEP"
"""


def test_claude_injection_sends_skill_command_then_pastes(tmp_path):
    """Claude Code detects a slash command from `send-keys -l`; the prompt then
    arrives through the named buffer, which is deleted afterwards. No Enter."""
    bin_dir, log = _write_tmux_stub(tmp_path)
    result = _run_harness(
        tmp_path,
        _injection_harness(bin_dir, tmp_path, "pane1", "claude_code", "/skill", "body", "dispatch-1"),
    )
    assert result.returncode == 0, result.stderr
    assert "rc=0" in result.stdout, result.stdout
    assert _calls(log) == [
        "send-keys -t pane1 -l /skill",
        "load-buffer -b vnx-deliver-PID-dispatch_1 -",
        "paste-buffer -b vnx-deliver-PID-dispatch_1 -t pane1",
        "delete-buffer -b vnx-deliver-PID-dispatch_1",
    ]


def test_codex_injection_pastes_skill_and_prompt_as_one_payload(tmp_path):
    """codex gets no separate skill keystroke: one buffer holds skill + prompt."""
    bin_dir, log = _write_tmux_stub(tmp_path)
    result = _run_harness(
        tmp_path,
        _injection_harness(bin_dir, tmp_path, "pane1", "codex", "/model", "hello", "dispatch-1"),
    )
    assert result.returncode == 0, result.stderr
    assert "rc=0" in result.stdout, result.stdout
    assert _calls(log) == [
        "load-buffer -b vnx-deliver-PID-dispatch_1 -",
        "paste-buffer -b vnx-deliver-PID-dispatch_1 -t pane1",
        "delete-buffer -b vnx-deliver-PID-dispatch_1",
    ]


def test_delivery_uses_the_same_sequence_and_enters_separately(tmp_path):
    """The public delivery path produces the lib's sequence and then submits
    with one `send-keys ... Enter` of its own (Enter never rides on a paste)."""
    bin_dir, log = _write_tmux_stub(tmp_path)
    result = _run_harness(tmp_path, _delivery_harness(bin_dir, tmp_path))
    assert result.returncode == 0, result.stderr
    assert "rc=0" in result.stdout, result.stdout
    assert _calls(log) == [
        "send-keys -t pane1 -l /skill",
        "load-buffer -b vnx-deliver-PID-dispatch_1 -",
        "paste-buffer -b vnx-deliver-PID-dispatch_1 -t pane1",
        "delete-buffer -b vnx-deliver-PID-dispatch_1",
        "send-keys -t pane1 Enter",
    ]


def test_delivery_maps_a_failed_load_to_the_load_buffer_substep(tmp_path):
    """The substep travels from the lib to the delivery failure handler, so a
    failed load still reports `tx_load_buffer` and still sends no Enter."""
    bin_dir, log = _write_tmux_stub(tmp_path, fail_on="load-buffer")
    result = _run_harness(tmp_path, _delivery_harness(bin_dir, tmp_path))
    assert result.returncode == 0, result.stderr
    assert "rc=1" in result.stdout, result.stdout
    assert "_DDT_FAILED_SUBSTEP=load_buffer" in result.stdout, result.stdout
    calls = _calls(log)
    assert [call for call in calls if "Enter" in call] == []
    assert calls.count("load-buffer -b vnx-deliver-PID-dispatch_1 -") == 3
    assert "code=tx_load_buffer" in (tmp_path / "dispatch.md").read_text(encoding="utf-8")
