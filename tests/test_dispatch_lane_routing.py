"""Lane-routing tests for scripts/commands/dispatch.sh (cmd_dispatch), raw-file form.

These run the ACTUAL bash function via subprocess (not a reimplementation):
the real dispatch.sh is sourced, the delivery script is replaced with a stub that
records that the lane fired and with which argv, and we assert the resolved lane
across the precedence chain:

    --adapter flag  >  'Adapter:' header  >  VNX_ADAPTER env  >  VNX_AUTO_ROUTE=1

The raw-file form has ONE lane: the headless subprocess lane. There is no default.
The tmux-spawn lane that used to be the default was removed on 2026-09-18, so a raw
file that asks for it, or names no lane at all, is refused loud and runs NO delivery
script (silently sending it to the subprocess lane would change its isolation and
lease behaviour). No tmux stub is created on purpose: nothing may try to run it.
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


def _make_env(
    tmp_path: Path,
    *,
    header_adapter: str | None = None,
    header_requires_mcp: bool = False,
) -> dict:
    """Build a fake VNX_HOME with a stub delivery script + a pending dispatch file."""
    vnx_home = tmp_path / "vnx_home"
    lib = vnx_home / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "subprocess_dispatch.py").write_text(_STUB.format(lane="subprocess"))

    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    dispatch_dir = data_dir / "dispatches"
    (dispatch_dir / "pending").mkdir(parents=True)
    state_dir.mkdir(parents=True)

    header = "[[TARGET:T1]]\nRole: backend-developer\nGate: G1\nFeature: demo\n"
    if header_adapter is not None:
        header += f"Adapter: {header_adapter}\n"
    if header_requires_mcp:
        header += "Requires-MCP: true\n"
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


def _run(
    env_paths: dict,
    *cli_args: str,
    vnx_adapter: str | None = None,
    extra_env: dict | None = None,
):
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
    if extra_env:
        run_env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=60,
    )


def _lane(env_paths: dict) -> dict:
    return json.loads(env_paths["marker"].read_text())


def _assert_subprocess_lane(env_paths: dict) -> None:
    rec = _lane(env_paths)
    assert rec["lane"] == "subprocess"
    # the subprocess lane is leased: passes --terminal-id, never --worker-label
    assert "--terminal-id" in rec["argv"]
    assert "--worker-label" not in rec["argv"]
    assert "--dispatch-id" in rec["argv"]


def test_no_adapter_is_refused_and_runs_no_delivery(tmp_path):
    """No flag, no header, no env: the old default (tmux) is gone, so the raw form is refused."""
    e = _make_env(tmp_path)
    res = _run(e)
    assert res.returncode != 0
    assert not e["marker"].exists()
    assert "removed on 2026-09-18" in res.stderr
    assert "--adapter subprocess" in res.stderr


@pytest.mark.parametrize("how", ["flag", "header", "env"])
def test_tmux_adapter_is_refused_loud_however_it_is_asked_for(tmp_path, how):
    """--adapter tmux, an 'Adapter: tmux' header, or VNX_ADAPTER=tmux: refused, nothing runs."""
    e = _make_env(tmp_path, header_adapter="tmux" if how == "header" else None)
    res = _run(
        e,
        *(("--adapter", "tmux") if how == "flag" else ()),
        vnx_adapter="tmux" if how == "env" else None,
    )
    assert res.returncode != 0
    assert not e["marker"].exists()
    assert "adapter 'tmux' was removed on 2026-09-18" in res.stderr


def test_flag_selects_subprocess(tmp_path):
    e = _make_env(tmp_path)
    res = _run(e, "--adapter", "subprocess")
    assert res.returncode == 0, res.stderr
    _assert_subprocess_lane(e)


def test_env_selects_subprocess(tmp_path):
    e = _make_env(tmp_path)
    res = _run(e, vnx_adapter="subprocess")
    assert res.returncode == 0, res.stderr
    _assert_subprocess_lane(e)


def test_flag_overrides_env(tmp_path):
    """--adapter subprocess beats VNX_ADAPTER=tmux (flag has highest precedence)."""
    e = _make_env(tmp_path)
    res = _run(e, "--adapter", "subprocess", vnx_adapter="tmux")
    assert res.returncode == 0, res.stderr
    _assert_subprocess_lane(e)


def test_header_selects_subprocess(tmp_path):
    """'Adapter: subprocess' in the file header selects the subprocess lane."""
    e = _make_env(tmp_path, header_adapter="subprocess")
    res = _run(e)
    assert res.returncode == 0, res.stderr
    _assert_subprocess_lane(e)


def test_flag_overrides_header(tmp_path):
    """--adapter subprocess beats an 'Adapter: tmux' header."""
    e = _make_env(tmp_path, header_adapter="tmux")
    res = _run(e, "--adapter", "subprocess")
    assert res.returncode == 0, res.stderr
    _assert_subprocess_lane(e)


def test_header_overrides_env(tmp_path):
    """Header 'Adapter: subprocess' wins over env tmux (header > env)."""
    e = _make_env(tmp_path, header_adapter="subprocess")
    res = _run(e, vnx_adapter="tmux")
    assert res.returncode == 0, res.stderr
    _assert_subprocess_lane(e)


def test_adapter_case_insensitive(tmp_path):
    e = _make_env(tmp_path)
    res = _run(e, "--adapter", "SubProcess")
    assert res.returncode == 0, res.stderr
    _assert_subprocess_lane(e)


def test_unknown_adapter_errors(tmp_path):
    """Unknown lane must fail loudly and run NO delivery script."""
    e = _make_env(tmp_path)
    res = _run(e, "--adapter", "bogus")
    assert res.returncode != 0
    assert not e["marker"].exists()
    assert "expected 'subprocess'" in res.stderr


def test_dry_run_no_delivery(tmp_path):
    """--dry-run resolves the lane but runs no delivery script."""
    e = _make_env(tmp_path)
    res = _run(e, "--adapter", "subprocess", "--dry-run")
    assert res.returncode == 0, res.stderr
    assert not e["marker"].exists()
    assert "Adapter:" in res.stderr  # lane logged even on dry-run


def test_auto_route_selects_subprocess_when_no_adapter_chosen(tmp_path):
    """VNX_AUTO_ROUTE=1 with no explicit adapter -> subprocess (smart routing honoured)."""
    e = _make_env(tmp_path)
    res = _run(e, extra_env={"VNX_AUTO_ROUTE": "1"})
    assert res.returncode == 0, res.stderr
    rec = _lane(e)
    assert rec["lane"] == "subprocess", f"expected subprocess, got {rec['lane']!r}"
    assert "--auto-route" in rec["argv"], "--auto-route must be forwarded to subprocess"


def test_auto_route_does_not_rescue_an_explicit_tmux_flag(tmp_path):
    """An explicit --adapter tmux is refused even with VNX_AUTO_ROUTE=1."""
    e = _make_env(tmp_path)
    res = _run(e, "--adapter", "tmux", extra_env={"VNX_AUTO_ROUTE": "1"})
    assert res.returncode != 0
    assert not e["marker"].exists()
    assert "adapter 'tmux' was removed on 2026-09-18" in res.stderr


def test_auto_route_does_not_rescue_an_explicit_tmux_env(tmp_path):
    """An explicit VNX_ADAPTER=tmux is refused even with VNX_AUTO_ROUTE=1."""
    e = _make_env(tmp_path)
    res = _run(e, vnx_adapter="tmux", extra_env={"VNX_AUTO_ROUTE": "1"})
    assert res.returncode != 0
    assert not e["marker"].exists()
    assert "adapter 'tmux' was removed on 2026-09-18" in res.stderr


# ---------------------------------------------------------------------------
# OI-865 — requires_mcp forwarding (--requires-mcp flag + Requires-MCP: header)
# ---------------------------------------------------------------------------

def test_requires_mcp_flag_forwarded_to_subprocess(tmp_path):
    """--requires-mcp on the raw-file lane reaches the subprocess lane argv."""
    e = _make_env(tmp_path)
    res = _run(e, "--requires-mcp", "--adapter", "subprocess")
    assert res.returncode == 0, res.stderr
    rec = _lane(e)
    assert rec["lane"] == "subprocess"
    assert "--requires-mcp" in rec["argv"], "subprocess CLI must receive --requires-mcp"


def test_requires_mcp_header_forwarded(tmp_path):
    """'Requires-MCP: true' in the dispatch file header is honoured without a flag."""
    e = _make_env(tmp_path, header_requires_mcp=True)
    res = _run(e, "--adapter", "subprocess")
    assert res.returncode == 0, res.stderr
    assert "--requires-mcp" in _lane(e)["argv"], (
        "Requires-MCP: true header must forward --requires-mcp to the delivery script"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
