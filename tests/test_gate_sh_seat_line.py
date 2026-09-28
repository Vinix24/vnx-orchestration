#!/usr/bin/env python3
"""OI-1888 — `vnx gate --only <seat>` must name whoever actually read the
seat, not the literal --only argument.

PR #1952 (2026-09-27) merged while kimi_gate was unavailable: the takeover
chain handed the seat to glm_gate, which passed, and the exit code correctly
reflected that PASS — but `scripts/commands/gate.sh` printed
`Gate 'kimi_gate': PASS` from the literal --only argument and the exit code
alone, never checking who the report said actually answered.

These tests source the real gate.sh and run `cmd_gate` against a stubbed
`review_gate_manager.py` (prints a canned request-and-execute report, no real
provider, no network) and a stubbed `gh` (only `pr view --json headRefName`,
which gate.sh needs to resolve the PR's branch). Only the takeover
interpretation (`gate_seat_line.py` / `gate_enforcement_verify.py`) and the
bash plumbing around it are real.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from _gate_harness import GATE_SH, install_gate_lib_modules  # noqa: E402
from test_t0_gate_enforcement import _entry, _report  # noqa: E402


STUB_MANAGER = '''\
"""Stub review_gate_manager: prints the canned request-and-execute report at
STUB_REPORT and exits STUB_RC -- same shape test_t0_gate_enforcement.py's
stub uses for the enforcement wrapper. No real gate, no network."""
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    assert "request-and-execute" in sys.argv, f"unexpected invocation: {sys.argv}"
    sys.stdout.write(Path(os.environ["STUB_REPORT"]).read_text(encoding="utf-8") + "\\n")
    rc = int(os.environ.get("STUB_RC", "0"))
    if rc:
        sys.stderr.write("ERROR: required gates did not PASS: kimi_gate\\n")
    sys.exit(rc)
'''

STUB_GH = """\
#!/usr/bin/env bash
# Only the one call gate.sh makes to resolve a PR's head branch.
if [ "$1" = "pr" ] && [ "$2" = "view" ] && [ "$4" = "--json" ] && [ "$5" = "headRefName" ]; then
  echo "feat/x"
  exit 0
fi
echo "stub gh: unexpected invocation: $*" >&2
exit 1
"""

DRIVER = """\
#!/usr/bin/env bash
set -euo pipefail
log() {{ printf '%s\\n' "$*"; }}
err() {{ printf 'ERROR: %s\\n' "$*" >&2; }}
export VNX_HOME={vnx_home}
export PROJECT_ROOT={project}
export VNX_STATE_DIR={state_dir}
source {gate_sh}
cmd_gate {pr} --only kimi
"""


@pytest.fixture
def harness(tmp_path: Path):
    """A fake VNX_HOME carrying the real gate_seat_line.py + its deps and a
    stub review_gate_manager.py, plus a stub `gh` on PATH."""
    vnx_home = tmp_path / "vnx-home"
    install_gate_lib_modules(vnx_home / "scripts" / "lib")
    (vnx_home / "scripts" / "review_gate_manager.py").write_text(STUB_MANAGER, encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    state_dir = tmp_path / "state"
    (state_dir / "review_gates" / "requests").mkdir(parents=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(STUB_GH, encoding="utf-8")
    gh_stub.chmod(0o755)

    driver = tmp_path / "run_cmd_gate.sh"
    driver.write_text(
        DRIVER.format(vnx_home=vnx_home, project=project, state_dir=state_dir, gate_sh=GATE_SH, pr=7),
        encoding="utf-8",
    )
    driver.chmod(0o755)
    return {"driver": driver, "vnx_home": vnx_home, "bin_dir": bin_dir}


def _run(harness, report: Dict[str, Any], *, rc: int = 0) -> subprocess.CompletedProcess:
    report_file = harness["vnx_home"].parent / "stub_report.json"
    report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env["PATH"] = f"{harness['bin_dir']}:{env['PATH']}"
    env.update(STUB_REPORT=str(report_file), STUB_RC=str(rc))
    return subprocess.run(
        ["bash", str(harness["driver"])],
        env=env, capture_output=True, text=True, timeout=60,
    )


# (a) takeover: glm reads for kimi with PASS
def test_a_takeover_reader_is_named_with_its_own_verdict(harness) -> None:
    entry = _entry("glm_gate", takeover_path=["kimi_gate"])
    entry["detail"]["takeover_path"] = [
        {"gate": "kimi_gate", "reason": "lane_exhausted", "detail": "quota spent", "status": "unavailable"},
    ]
    proc = _run(harness, _report(entry), rc=0)
    assert proc.returncode == 0, proc.stderr
    assert "Gate 'kimi_gate' -> glm_gate (takeover, lane_exhausted): PASS" in proc.stdout
    assert "Gate 'kimi_gate': PASS" not in proc.stdout


# (b) no takeover: the line is unchanged
def test_b_no_takeover_line_is_unchanged(harness) -> None:
    proc = _run(harness, _report(_entry("kimi_gate")), rc=0)
    assert proc.returncode == 0, proc.stderr
    assert "Gate 'kimi_gate': PASS" in proc.stdout


# (c) unavailable, no successor: never a PASS, and the process must not exit 0
def test_c_unavailable_without_a_successor_is_never_pass(harness) -> None:
    entry = {
        "gate": "kimi_gate",
        "request_status": "not_executable",
        "execution_status": "not_executable",
        "passed": False,
        "reason_detail": "kimi binary not found on PATH",
        "detail": {"gate": "kimi_gate"},
    }
    proc = _run(harness, _report(entry), rc=1)
    assert proc.returncode != 0
    assert "Gate 'kimi_gate': UNAVAILABLE (kimi binary not found on PATH)" in proc.stderr
    assert "Gate 'kimi_gate': PASS" not in proc.stdout
    assert "Gate 'kimi_gate': PASS" not in proc.stderr


# (d) diff_truncated is surfaced
def test_d_diff_truncated_is_shown(harness) -> None:
    entry = _entry("kimi_gate")
    entry["detail"]["diff_truncated"] = True
    proc = _run(harness, _report(entry), rc=0)
    assert proc.returncode == 0, proc.stderr
    assert "Gate 'kimi_gate': PASS (diff truncated)" in proc.stdout


def test_bash_syntax_is_clean() -> None:
    result = subprocess.run(["bash", "-n", str(GATE_SH)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
