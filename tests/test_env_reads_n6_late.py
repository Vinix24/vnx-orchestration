"""config-een-oppervlak N6a: eleven names are gone from the code; set to hostile values, the default path stays."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(VNX_ROOT / "scripts" / "lib"), str(VNX_ROOT / "scripts")]
AGENTIC = ("VNX_LITELLM_AGENTIC_CMD_TIMEOUT", "VNX_LITELLM_AGENTIC_DEADLINE",
           "VNX_LITELLM_AGENTIC_MAX_TOKENS", "VNX_LITELLM_AGENTIC_MAX_TURNS")
DELETED_NAMES = ("VNX_POOL_LEASE_HEARTBEAT_SECONDS", *AGENTIC, "VNX_LEARNING_LOOP_CUTOFF",
                 "VNX_MODE_GATING_ENABLED", "VNX_STARTER_MODE_ENABLED", "VNX_PROVIDER",
                 "VNX_WORKTREES", "VNX_VENV_PATH")


def _files():
    for d in ("scripts", "vnx_cli", "dashboard", "bin"):
        for p in sorted((VNX_ROOT / d).rglob("*")) if (VNX_ROOT / d).is_dir() else ():
            if p.is_file() and p.suffix in {".py", ".sh", ""} and "__pycache__" not in p.parts:
                yield p


def _occurrences(name: str):
    pattern = re.compile(r"(?<![A-Z0-9_])" + re.escape(name) + r"(?![A-Z0-9_])")
    hits = []
    for p in _files():
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue  # a binary file under bin/ holds no setting name
        hits += [f"{p.relative_to(VNX_ROOT)}:{no}" for no, line in enumerate(lines, 1) if pattern.search(line)]
    return hits


@pytest.mark.parametrize("name", DELETED_NAMES)
def test_deleted_name_has_zero_occurrences(name):
    hits = _occurrences(name)
    assert not hits, f"{name}: {len(hits)} occurrence(s) left: {hits}"


def test_scan_sees_the_tree():
    assert sum(1 for _ in _files()) > 100
    assert _occurrences("VNX_PROJECT_ID"), "the scan must find a name that is still read"


def test_lease_heartbeat_is_fifteen_seconds(monkeypatch):
    import pool_worker_runner

    monkeypatch.setenv("VNX_POOL_LEASE_HEARTBEAT_SECONDS", "0.01")
    assert pool_worker_runner._lease_heartbeat_interval() == 15.0


def test_agentic_spawn_limits_are_the_old_defaults(monkeypatch):
    from provider_spawns import litellm_spawn

    for name in AGENTIC:
        monkeypatch.setenv(name, "7")
    seen = {}
    proc = type("P", (), {"returncode": 0, "wait": lambda self, timeout=None: 0,
                          "communicate": lambda self, input=None, timeout=None: seen.update(
                              payload=json.loads(input), timeout=timeout) or (b"", b"")})()
    monkeypatch.setattr(litellm_spawn.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(litellm_spawn.provider_switch, "require", lambda *a, **k: None)
    litellm_spawn.spawn_litellm_agentic("p", "m", "D-1", "T1", event_writer=lambda *a, **k: None)
    payload = seen["payload"]
    assert (payload["max_turns"], payload["max_tokens"], payload["command_timeout"]) == (30, 8192, 120)
    assert seen["timeout"] == 1800.0


def test_learning_loop_cutoff_is_the_constant(monkeypatch):
    import learning_loop

    monkeypatch.setenv("VNX_LEARNING_LOOP_CUTOFF", "nonsense")
    assert learning_loop.resolve_receipt_cutoff() == datetime(2026, 9, 28, 16, 41, 11, tzinfo=timezone.utc)


def test_mode_gate_blocks_with_the_rollback_variable_set(tmp_path):
    text = (VNX_ROOT / "bin" / "vnx").read_text(encoding="utf-8")
    start = text.index("_check_mode_gate() {")
    script = text[start:text.index("\n}\n", start) + 3] + '\n_check_mode_gate "$1"\n'
    (tmp_path / "mode.json").write_text('{"mode": "starter"}', encoding="utf-8")
    env = {**os.environ, "VNX_DATA_DIR": str(tmp_path), "VNX_DATA_DIR_EXPLICIT": "1", "VNX_HOME": str(VNX_ROOT),
           "VNX_PYTHON": sys.executable, "VNX_MODE_GATING_ENABLED": "0"}
    gate = [subprocess.run(["bash", "-c", script, "gate", cmd], env=env, capture_output=True, text=True)
            for cmd in ("start", "status")]
    assert gate[0].returncode != 0, gate[0].stderr  # operator-only command refused in starter mode
    assert gate[1].returncode == 0, gate[1].stderr


def test_venv_resolution_ignores_the_override(tmp_path):
    project, other = tmp_path / "proj", tmp_path / "elsewhere"
    for root in (project / ".venv", other):
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "activate").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    script = f'source "{VNX_ROOT}/scripts/lib/vnx_paths.sh" >/dev/null 2>&1; PROJECT_ROOT="$1"; _resolve_venv_path'
    env = {**os.environ, "VNX_VENV_PATH": str(other)}
    out = subprocess.run(["bash", "-c", script, "venv", str(project)], env=env, capture_output=True, text=True)
    assert out.stdout.strip() == f"{project}/.venv/bin/activate"
