"""Lane-routing tests for scripts/commands/dispatch.sh (cmd_dispatch).

These run the ACTUAL bash function via subprocess (not a reimplementation):
the real dispatch.sh is sourced, the two delivery scripts are replaced with
stubs that record which lane fired and with which argv, and we assert the
resolved lane across the precedence chain:

    --adapter flag  >  'Adapter:' header  >  VNX_ADAPTER env  >  default 'tmux'

Default Claude lane MUST be the subscription-preserving tmux-spawn; the paid
headless SubprocessAdapter is opt-in only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DISPATCH_SH = REPO_ROOT / "scripts" / "commands" / "dispatch.sh"

_STUB = """#!/usr/bin/env python3
import os, sys, json
with open(os.environ["VNX_TEST_MARKER"], "w") as f:
    json.dump({{"lane": "{lane}", "argv": sys.argv[1:]}}, f)
sys.exit(0)
"""


def _make_env(tmp_path: Path, *, header_adapter: str | None = None) -> dict:
    """Build a fake VNX_HOME with stub delivery scripts + a pending dispatch file."""
    vnx_home = tmp_path / "vnx_home"
    lib = vnx_home / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "tmux_interactive_dispatch.py").write_text(_STUB.format(lane="tmux"))
    (lib / "subprocess_dispatch.py").write_text(_STUB.format(lane="subprocess"))

    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    dispatch_dir = data_dir / "dispatches"
    (dispatch_dir / "pending").mkdir(parents=True)
    state_dir.mkdir(parents=True)

    header = "[[TARGET:T1]]\nRole: backend-developer\nGate: G1\nFeature: demo\n"
    if header_adapter is not None:
        header += f"Adapter: {header_adapter}\n"
    df = dispatch_dir / "pending" / "demo.md"
    df.write_text(header + "\nDo the thing.\n")

    marker = tmp_path / "marker.json"
    return {
        "vnx_home": vnx_home,
        "data_dir": data_dir,
        "state_dir": state_dir,
        "dispatch_dir": dispatch_dir,
        "dispatch_file": df,
        "marker": marker,
    }


def _run(env_paths: dict, *cli_args: str, vnx_adapter: str | None = None):
    """Source the real dispatch.sh and invoke cmd_dispatch via bash."""
    args = " ".join(f"'{a}'" for a in (str(env_paths["dispatch_file"]), *cli_args))
    script = f"""
set -u
log() {{ printf '%s\\n' "$*" >&2; }}
err() {{ printf 'ERR %s\\n' "$*" >&2; }}
export VNX_HOME='{env_paths["vnx_home"]}'
export VNX_DATA_DIR='{env_paths["data_dir"]}'
export VNX_STATE_DIR='{env_paths["state_dir"]}'
export VNX_DISPATCH_DIR='{env_paths["dispatch_dir"]}'
export VNX_TEST_MARKER='{env_paths["marker"]}'
source '{DISPATCH_SH}'
cmd_dispatch {args}
"""
    run_env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    if vnx_adapter is not None:
        run_env["VNX_ADAPTER"] = vnx_adapter
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=60,
    )


def _lane(env_paths: dict) -> dict:
    return json.loads(env_paths["marker"].read_text())


def test_default_lane_is_tmux(tmp_path):
    """No flag, no header, no env -> default subscription tmux-spawn."""
    e = _make_env(tmp_path)
    res = _run(e)
    assert res.returncode == 0, res.stderr
    rec = _lane(e)
    assert rec["lane"] == "tmux"
    # tmux lane is leaseless: passes --worker-label, never --terminal-id
    assert "--worker-label" in rec["argv"]
    assert "--terminal-id" not in rec["argv"]
    assert "--dispatch-id" in rec["argv"]


def test_env_selects_subprocess(tmp_path):
    e = _make_env(tmp_path)
    res = _run(e, vnx_adapter="subprocess")
    assert res.returncode == 0, res.stderr
    rec = _lane(e)
    assert rec["lane"] == "subprocess"
    # burst lane is leased: passes --terminal-id, never --worker-label
    assert "--terminal-id" in rec["argv"]
    assert "--worker-label" not in rec["argv"]


def test_flag_overrides_env(tmp_path):
    """--adapter tmux beats VNX_ADAPTER=subprocess (flag has highest precedence)."""
    e = _make_env(tmp_path)
    res = _run(e, "--adapter", "tmux", vnx_adapter="subprocess")
    assert res.returncode == 0, res.stderr
    assert _lane(e)["lane"] == "tmux"


def test_header_selects_subprocess(tmp_path):
    """'Adapter: subprocess' in the file header opts into the burst lane."""
    e = _make_env(tmp_path, header_adapter="subprocess")
    res = _run(e)
    assert res.returncode == 0, res.stderr
    assert _lane(e)["lane"] == "subprocess"


def test_flag_overrides_header(tmp_path):
    """--adapter tmux beats 'Adapter: subprocess' header."""
    e = _make_env(tmp_path, header_adapter="subprocess")
    res = _run(e, "--adapter", "tmux")
    assert res.returncode == 0, res.stderr
    assert _lane(e)["lane"] == "tmux"


def test_env_overrides_header_off_default(tmp_path):
    """Header 'Adapter: subprocess' wins over env tmux (header > env)."""
    e = _make_env(tmp_path, header_adapter="subprocess")
    res = _run(e, vnx_adapter="tmux")
    assert res.returncode == 0, res.stderr
    assert _lane(e)["lane"] == "subprocess"


def test_adapter_case_insensitive(tmp_path):
    e = _make_env(tmp_path)
    res = _run(e, "--adapter", "SubProcess")
    assert res.returncode == 0, res.stderr
    assert _lane(e)["lane"] == "subprocess"


def test_unknown_adapter_errors(tmp_path):
    """Unknown lane must fail loudly and run NEITHER delivery script."""
    e = _make_env(tmp_path)
    res = _run(e, "--adapter", "bogus")
    assert res.returncode != 0
    assert not e["marker"].exists()


def test_dry_run_no_delivery(tmp_path):
    """--dry-run resolves the lane but runs no delivery script."""
    e = _make_env(tmp_path)
    res = _run(e, "--dry-run")
    assert res.returncode == 0, res.stderr
    assert not e["marker"].exists()
    assert "Adapter:" in res.stderr  # lane logged even on dry-run


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
