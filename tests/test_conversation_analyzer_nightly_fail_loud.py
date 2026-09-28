"""The nightly conversation-analyzer job must fail for real on an error.

Measured 2026-09-28: `scripts/conversation_analyzer_nightly.sh` called
`ensure_env`, a function that exists in no shell file. `lib/vnx_paths.sh`
snapshots the caller's shell options inside `$(...)` (where bash clears errexit)
and restores that snapshot on exit, so the script's `set -e` was silently gone
after the `source`. The missing function only wrote "command not found" to
stderr and every phase still logged "complete".

These tests run the REAL script (copied into a tmp tree next to the real
`lib/vnx_paths.sh`) under bash with a tmp state dir. Every Python phase is a
stub; no analyzer, ollama or mail is started.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "conversation_analyzer_nightly.sh"
_PHASES = (
    "quality_db_init",
    "conversation_analyzer",
    "link_sessions_dispatches",
    "generate_t0_session_brief",
    "governance_aggregator",
    "learning_loop_nightly",
    "generate_suggested_edits",
    "send_digest_email",
)
_SOURCE_LINE = 'source "$SCRIPT_DIR/lib/vnx_paths.sh"\n'
_PRELUDE_END = "# ── Interpreter resolution"
COMPLETE = "Nightly analysis pipeline complete"


def _build_tree(tmp: Path, script_text: str) -> tuple[Path, Path]:
    scripts = tmp / "scripts"
    scripts.mkdir()
    (scripts / "lib").symlink_to(_REPO_ROOT / "scripts" / "lib")
    script = scripts / "conversation_analyzer_nightly.sh"
    script.write_text(script_text, encoding="utf-8")
    for phase in _PHASES:
        stub = scripts / f"{phase}.py"
        stub.write_text(
            "import os, sys\n"
            f"sys.exit(int(os.environ.get('STUB_EXIT_{phase}', '0')))\n",
            encoding="utf-8",
        )
    # pgrep reports ollama as running, so the script never starts a real one.
    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    pgrep = fakebin / "pgrep"
    pgrep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pgrep.chmod(pgrep.stat().st_mode | stat.S_IEXEC)
    return script, fakebin


def _run(tmp_path: Path, script_text: str, **stub_exits: int) -> tuple[subprocess.CompletedProcess, str]:
    script, fakebin = _build_tree(tmp_path, script_text)
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "HOME": str(home),
        "VNX_DATA_DIR": str(data),
        "VNX_STATE_DIR": str(data / "state"),
    }
    env.update({f"STUB_EXIT_{k}": str(v) for k, v in stub_exits.items()})
    proc = subprocess.run(
        ["/bin/bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    log = data / "state" / "conversation_analyzer.log"
    return proc, log.read_text(encoding="utf-8") if log.exists() else ""


def _script_with_missing_function() -> str:
    text = _SCRIPT.read_text(encoding="utf-8")
    assert _PRELUDE_END in text
    return text.replace(_PRELUDE_END, "ensure_env_that_does_not_exist\n\n" + _PRELUDE_END, 1)


def test_missing_function_after_prelude_fails_the_job(tmp_path):
    proc, log = _run(tmp_path, _script_with_missing_function())
    assert proc.returncode != 0
    assert COMPLETE not in log
    assert "command not found" in proc.stderr


def test_normal_run_completes(tmp_path):
    proc, log = _run(tmp_path, _SCRIPT.read_text(encoding="utf-8"))
    assert proc.returncode == 0, proc.stderr
    assert "ensure_env" not in proc.stderr
    assert COMPLETE in log
    for n in ("Phase 0 complete", "Phase 1 complete", "Phase 2 complete", "Phase 3 complete"):
        assert n in log


def test_errexit_is_reasserted_right_after_the_vnx_paths_source():
    after_source = _SCRIPT.read_text(encoding="utf-8").split(_SOURCE_LINE, 1)[1]
    prelude = after_source.split("VNX_PYTHON=", 1)[0]
    assert "\nset -euo pipefail\n" in prelude


def test_phase0_failure_aborts_without_complete_line(tmp_path):
    proc, log = _run(tmp_path, _SCRIPT.read_text(encoding="utf-8"), quality_db_init=1)
    assert proc.returncode != 0
    assert "Phase 0 FAILED" in log
    assert COMPLETE not in log
    assert "Phase 1" not in log


def test_analyzer_failure_exits_nonzero_without_complete_line(tmp_path):
    proc, log = _run(tmp_path, _SCRIPT.read_text(encoding="utf-8"), conversation_analyzer=3)
    assert proc.returncode == 3
    assert "Phase 1 FAILED (exit=3)" in log
    assert "Phase 1 complete" not in log
    assert COMPLETE not in log
    assert "Nightly analysis pipeline FAILED" in log


def test_non_fatal_phase_failure_still_completes(tmp_path):
    proc, log = _run(tmp_path, _SCRIPT.read_text(encoding="utf-8"), generate_suggested_edits=1)
    assert proc.returncode == 0, proc.stderr
    assert "Phase 3 WARNING" in log
    assert COMPLETE in log


@pytest.fixture(autouse=True)
def _no_real_state_dir(monkeypatch):
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
