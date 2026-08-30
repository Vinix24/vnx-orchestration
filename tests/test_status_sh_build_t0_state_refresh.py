"""tests/test_status_sh_build_t0_state_refresh.py — D1 poort E regression coverage.

`vnx status` fires a background build_t0_state.py refresh
(scripts/commands/status.sh, "Legacy bash paths" block) and used to discard
BOTH streams and the exit code (`>/dev/null 2>&1 || true`): a crash on this
invocation path left zero evidence anywhere. This extracts and runs the
REAL block from status.sh (not a reimplementation) against a fake
build_t0_state.py, so the test exercises the actual shipped code.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATUS_SH = REPO / "scripts" / "commands" / "status.sh"

_BLOCK_START = "# Legacy bash paths for filter flags and fallback."


def _extract_refresh_block() -> str:
    text = STATUS_SH.read_text(encoding="utf-8")
    start = text.index(_BLOCK_START)
    # The outer `if` closes at a line that is EXACTLY "  fi" (2-space indent,
    # matching the outer `if`'s own indent) — the inner if/fi guarding the
    # log-append is indented 4 spaces, so this regex only matches the outer one.
    match = re.search(r"\n  fi\n", text[start:])
    assert match, "could not find the end of the build_t0_state refresh block in status.sh"
    return text[start:start + match.end()]


def _run_block(tmp_path: Path, builder_source: str) -> subprocess.CompletedProcess:
    vnx_home = tmp_path / "engine"
    (vnx_home / "scripts").mkdir(parents=True, exist_ok=True)
    (vnx_home / "scripts" / "build_t0_state.py").write_text(builder_source, encoding="utf-8")
    data_dir = tmp_path / "data"
    logs_dir = data_dir / "logs"

    script = (
        "set -u\n"
        f'VNX_HOME="{vnx_home}"\n'
        f'VNX_DATA_DIR="{data_dir}"\n'
        f'VNX_LOGS_DIR="{logs_dir}"\n'
        + _extract_refresh_block()
        + "\necho DONE\n"
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
    )


def test_block_is_valid_bash() -> None:
    block = _extract_refresh_block()
    assert block.strip().startswith(_BLOCK_START)
    rc = subprocess.run(["bash", "-n", "-c", "VNX_HOME=x\nVNX_DATA_DIR=y\n" + block], capture_output=True, text=True)
    assert rc.returncode == 0, f"extracted block is not valid bash: {rc.stderr}"


def test_refresh_never_fails_the_calling_shell_on_a_crashing_build(tmp_path: Path) -> None:
    builder = (
        "import sys\n"
        "sys.stderr.write('simulated crash: missing module\\n')\n"
        "sys.exit(3)\n"
    )
    r = _run_block(tmp_path, builder)
    assert r.returncode == 0, f"a background refresh crash must never fail `vnx status`: {r.stderr}"
    assert "DONE" in r.stdout


def test_crash_leaves_evidence_in_shared_error_log(tmp_path: Path) -> None:
    """D1: this used to be `>/dev/null 2>&1 || true` — a crash here left
    zero trace anywhere. It must now append to the same log the hook uses."""
    builder = (
        "import sys\n"
        "sys.stderr.write('simulated crash: missing module\\n')\n"
        "sys.exit(3)\n"
    )
    r = _run_block(tmp_path, builder)

    err_log = tmp_path / "data" / "logs" / "build_t0_state.err"
    assert err_log.is_file(), "a crashing refresh must leave evidence in the shared error log"
    content = err_log.read_text(encoding="utf-8")
    assert "rc=3" in content
    assert "missing module" in content
    assert "via vnx status" in content


def test_successful_build_does_not_touch_the_error_log(tmp_path: Path) -> None:
    builder = (
        "import sys, os, json\n"
        "out = sys.argv[sys.argv.index('--output') + 1] if '--output' in sys.argv else None\n"
        "print('ok')\n"
        "sys.exit(0)\n"
    )
    r = _run_block(tmp_path, builder)
    assert r.returncode == 0
    err_log = tmp_path / "data" / "logs" / "build_t0_state.err"
    assert not err_log.exists(), "a clean run must not create/append to the error log"


def test_multiple_crashes_append_rather_than_overwrite(tmp_path: Path) -> None:
    builder = (
        "import sys\n"
        "sys.stderr.write('boom\\n')\n"
        "sys.exit(1)\n"
    )
    _run_block(tmp_path, builder)
    _run_block(tmp_path, builder)

    err_log = tmp_path / "data" / "logs" / "build_t0_state.err"
    content = err_log.read_text(encoding="utf-8")
    assert content.count("boom") == 2, f"second crash must APPEND, not overwrite: {content!r}"
