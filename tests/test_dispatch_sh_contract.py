"""test_dispatch_sh_contract.py — dispatch.sh flag-routing contract (item A, NON-dry-run).

The pre-existing flag tests only assert on --dry-run log strings; they never prove the door
is actually invoked. kimi's finding: a PATH stub cannot work because _d_single_entry_dispatch
calls ${VNX_HOME}/scripts/lib/dispatch_cli.py by ABSOLUTE path. These tests override VNX_HOME
to a stub dir whose dispatch_cli.py RECORDS its argv, so we prove the real routing decision
(door vs legacy) and that --dry-run is forwarded — not just a dry-run print.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DISPATCH_SH = _REPO_ROOT / "scripts" / "commands" / "dispatch.sh"

# Stub door: records argv to VNX_TEST_ARGV_MARKER and exits 0 (no real dispatch).
_STUB = (
    "import os, sys\n"
    'open(os.environ["VNX_TEST_ARGV_MARKER"], "w").write("\\0".join(sys.argv[1:]))\n'
)


def _make_stub_home(tmp_path: Path) -> Path:
    home = tmp_path / "stubhome"
    (home / "scripts" / "lib").mkdir(parents=True)
    (home / "scripts" / "lib" / "dispatch_cli.py").write_text(_STUB, encoding="utf-8")
    return home


def _preamble(stub_home: Path, data_dir: Path, dispatch_dir: Path, marker: Path) -> str:
    return f"""
set -e
VNX_HOME='{stub_home}'
VNX_DATA_DIR='{data_dir}'
VNX_DISPATCH_DIR='{dispatch_dir}'
VNX_STATE_DIR='{data_dir}/state'
export VNX_TEST_ARGV_MARKER='{marker}'
log() {{ echo "[LOG] $*"; }}
err() {{ echo "[ERR] $*" >&2; }}
source '{_DISPATCH_SH}'
"""


def _run(bash_cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)


def _promote_spec(dispatch_dir: Path, dispatch_id: str) -> None:
    pend = dispatch_dir / "pending" / dispatch_id
    pend.mkdir(parents=True)
    (pend / "dispatch-spec.json").write_text("{}", encoding="utf-8")


def test_door_invoked_non_dry_run(tmp_path):
    """Flag ON + a promoted spec → the REAL door (dispatch_cli.py) runs with --spec-file and
    NO --dry-run. Proves the door is taken for real, not just dry-run printed."""
    stub_home = _make_stub_home(tmp_path)
    dispatch_dir = tmp_path / "dispatches"
    _promote_spec(dispatch_dir, "20260622-itemA-door")
    marker = tmp_path / "argv.marker"
    cmd = _preamble(stub_home, tmp_path, dispatch_dir, marker) + """
VNX_SINGLE_ENTRY_DISPATCH=1
unset VNX_DISPATCH_LEGACY
cmd_dispatch '20260622-itemA-door'
"""
    r = _run(cmd)
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    assert marker.exists(), "door stub dispatch_cli.py was NOT invoked"
    argv = marker.read_text().split("\0")
    assert "--spec-file" in argv
    assert "--dry-run" not in argv


def test_door_forwards_dry_run(tmp_path):
    """Flag ON + --dry-run → the door is invoked WITH --dry-run forwarded."""
    stub_home = _make_stub_home(tmp_path)
    dispatch_dir = tmp_path / "dispatches"
    _promote_spec(dispatch_dir, "20260622-itemA-dry")
    marker = tmp_path / "argv.marker"
    cmd = _preamble(stub_home, tmp_path, dispatch_dir, marker) + """
VNX_SINGLE_ENTRY_DISPATCH=1
cmd_dispatch '20260622-itemA-dry' --dry-run
"""
    r = _run(cmd)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert marker.exists()
    assert "--dry-run" in marker.read_text().split("\0")


def test_rollback_does_not_invoke_door(tmp_path):
    """Flag ON + VNX_DISPATCH_LEGACY=1 → the door stub is NEVER invoked (rollback wins)."""
    stub_home = _make_stub_home(tmp_path)
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()
    dispatch_md = tmp_path / "d.md"
    dispatch_md.write_text("[[TARGET:T1]]\nRole: backend-developer\n\nx\n", encoding="utf-8")
    marker = tmp_path / "argv.marker"
    cmd = _preamble(stub_home, tmp_path, dispatch_dir, marker) + f"""
VNX_SINGLE_ENTRY_DISPATCH=1
VNX_DISPATCH_LEGACY=1
cmd_dispatch '{dispatch_md}' --dry-run
"""
    r = _run(cmd)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert not marker.exists(), "door invoked despite VNX_DISPATCH_LEGACY=1 rollback"
    assert "single-entry gate" not in (r.stdout + r.stderr).lower()


def test_default_off_does_not_invoke_door(tmp_path):
    """Pre-flip default (flag unset) → legacy; the door stub is never invoked."""
    stub_home = _make_stub_home(tmp_path)
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()
    dispatch_md = tmp_path / "d.md"
    dispatch_md.write_text("[[TARGET:T1]]\nRole: backend-developer\n\nx\n", encoding="utf-8")
    marker = tmp_path / "argv.marker"
    cmd = _preamble(stub_home, tmp_path, dispatch_dir, marker) + f"""
unset VNX_SINGLE_ENTRY_DISPATCH
cmd_dispatch '{dispatch_md}' --dry-run
"""
    r = _run(cmd)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert not marker.exists()
