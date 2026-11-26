"""Q1b cutover readers and rollback-variable regressions (parts a and b)."""
import ast
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))
FLAGS = {"VNX_" + name for name in (
    "RUNTIME_PRIMARY", "BROKER_ENABLED", "BROKER_SHADOW",
    "CANONICAL_LEASE_ACTIVE", "ADAPTER_PRIMARY", "TMUX_ADAPTER_ENABLED",
)}
FILES = (
    "lib/runtime_core.py", "lib/tmux_adapter.py", "lib/terminal_state_core.py",
    "lib/lease_manager.py", "runtime_core_cli.py", "runtime_cutover_check.py",
    "commands/recover.sh", "lib/vnx_recovery_phases.py",
    "lib/mixed_execution_router.py", "lib/dispatch_lifecycle.sh",
    "dispatcher_minimal.sh", "lib/dispatch_broker.py",
)


def test_no_cutover_env_reads():
    reads = []
    for file in FILES:
        source = (ROOT / "scripts" / file).read_text()
        if file.endswith(".sh"):
            reads.extend((file, name) for name in re.findall(r"\$\{(VNX_\w+)", source) if name in FLAGS)
            continue
        tree = ast.parse(source)
        aliases = {n.targets[0].id: n.value.value for n in ast.walk(tree)
                   if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                   and isinstance(n.value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args and isinstance(node.func, ast.Attribute):
                if ast.unparse(node.func) not in {"os.environ.get", "os.getenv"}:
                    continue
                arg = node.args[0]
                name = arg.value if isinstance(arg, ast.Constant) else aliases.get(getattr(arg, "id", ""))
                if name in FLAGS:
                    reads.append((file, name))
    # Q1bb STOP: lease_sweep is dispatcher-only and opt-in; no standalone expiry owner.
    retained = [(file, "VNX_CANONICAL_LEASE_ACTIVE") for file in (
        "dispatcher_minimal.sh", "lib/terminal_state_core.py", "lib/lease_manager.py")]
    assert sorted(reads) == sorted(retained), reads


@pytest.mark.parametrize("flag,function", [
    ("VNX_RUNTIME_PRIMARY", "runtime_core.runtime_primary_active"),
    ("VNX_TMUX_ADAPTER_ENABLED", "tmux_adapter.adapter_enabled"),
    ("VNX_ADAPTER_PRIMARY", "tmux_adapter.primary_path_active"),
])
def test_rollback_zero_cannot_disable_cutover(monkeypatch, flag, function):
    import importlib
    module, name = function.split(".")
    monkeypatch.setenv(flag, "0")
    assert getattr(importlib.import_module(module), name)() is True


def test_runtime_factory_ignores_rollback_zero(tmp_path, monkeypatch):
    from runtime_core import load_runtime_core
    monkeypatch.setenv("VNX_RUNTIME_PRIMARY", "0")
    assert load_runtime_core(tmp_path / "state", tmp_path / "dispatches") is not None


def test_lifecycle_ignores_rollback_zero():
    result = subprocess.run(["bash", "-c", 'source "$1"; _rc_enabled', "q1ba",
                             str(ROOT / "scripts/lib/dispatch_lifecycle.sh")],
                            env={**os.environ, "VNX_RUNTIME_PRIMARY": "0"}, capture_output=True)
    assert result.returncode == 0


@pytest.mark.parametrize("command", [
    ["register", "--dispatch-id", "q1ba-register", "--terminal", "T1"],
    ["check-terminal", "--terminal", "T1", "--dispatch-id", "q1ba-check"],
    ["chain-closeout"],
    ["delivery-start", "--terminal", "T1", "--dispatch-id", "q1ba-start"],
    ["acquire-lease", "--terminal", "T1", "--dispatch-id", "q1ba-lease"],
])
def test_cli_initialization_failure_blocks(tmp_path, command):
    import json
    state = tmp_path / "state"
    state.write_text("not a directory")
    runner = ("import runpy,sys; from pathlib import Path; from unittest.mock import patch; "
              "sys.path.insert(0,str(Path(sys.argv[1]).parent/'lib'))\n"
              "with patch('runtime_core.load_broker', side_effect=OSError('broker unavailable')): "
              "runpy.run_path(sys.argv.pop(1),run_name='__main__')")
    result = subprocess.run([sys.executable, "-c", runner, str(ROOT / "scripts/runtime_core_cli.py"), *command],
                            env={**os.environ, "VNX_STATE_DIR": str(state)},
                            capture_output=True, text=True)
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert not payload.get("available") and not payload.get("registered")
    assert (payload.get("error") or payload.get("reason")) == "runtime_core_unavailable"


def test_recover_runs_canonical_with_retired_flag(tmp_path):
    from runtime_coordination import init_schema
    init_schema(tmp_path / "state")
    env = {**os.environ, "VNX_HOME": str(ROOT), "VNX_RUNTIME_PRIMARY": "0"}
    for key, subdir in {"VNX_DATA_DIR": "data", "VNX_STATE_DIR": "state",
                        "VNX_LOCKS_DIR": "locks", "VNX_PIDS_DIR": "pids",
                        "VNX_DISPATCH_DIR": "dispatches"}.items():
        env[key] = str(tmp_path / subdir)
    result = subprocess.run(["bash", "-c", 'source "$1"; log() { echo "$@"; }; cmd_recover --dry-run',
                             "q1ba", str(ROOT / "scripts/commands/recover.sh")],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Running canonical runtime recovery" in result.stdout
    assert "Runtime core: ACTIVE" in result.stdout
