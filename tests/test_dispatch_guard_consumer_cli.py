"""tests/test_dispatch_guard_consumer_cli.py — dispatch_guard.sh in a consumer project.

A consumer has no ``bin/vnx``. The CLI on its PATH may be the pip package, whose
``status --json`` speaks a different schema (``active_dispatches``, ``agents``) than the
guard reads (``vnx_status/1.0``). The guard must find a CLI that speaks the schema, refuse
one that does not (naming the CLI and the schema it answered), and read the state of its own
repo, not of the caller's cwd or an inherited ``VNX_PROJECT_ROOT``.

OI-2268: GO requires live_work and queues measured at the call. A CLI from 1.7.5 on stamps its
``status --json --live`` answer (``_live``); the 1.7.2 shape without that stamp is WAIT.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD_SRC = ROOT / "skills" / "t0-orchestrator" / "scripts" / "dispatch_guard.sh"

PIP_STATUS = {
    "active_dispatches": 0,
    "agent_count": 0,
    "agents": [],
    "initialized": True,
    "project_dir": "/x",
    "recent_completions": [],
}


def _v172(busy: bool) -> dict:
    live = [{"dispatch_id": "20261021-busy", "state": "running", "lock": "held", "age_seconds": 5}] if busy else []
    return {
        "schema": "vnx_status/1.0",
        "t0_state_available": True,
        "system_health": {"status": "healthy"},
        "queues": {"pending_count": 0, "conflict_count": 0},
        "live_work": {"available": True, "live": live, "starting": [], "stale": [], "unmeasured": []},
    }


def _live(busy: bool) -> dict:
    """The same answer from a CLI that measured live_work and queues at the call.
    ``__NOW__`` becomes the time of the call (see _fake_cli)."""
    payload = _v172(busy)
    payload["live_measured_at"] = "__NOW__"
    payload["live_measurement"] = {
        "mode": "live", "sections": ["live_work", "queues"], "duration_seconds": 0.01, "unmeasured": [],
    }
    return payload


def _fake_cli(path: Path, payload: dict, only_cwd: Path | None = None) -> Path:
    """A `vnx` that prints *payload* for `status --json`; with *only_cwd* it answers busy
    only when both its cwd and VNX_PROJECT_ROOT are that directory. ``__NOW__`` in the
    answer becomes the time of the call."""
    path.parent.mkdir(parents=True, exist_ok=True)
    idle = json.dumps(_live(False))
    body = json.dumps(payload)
    stamp = 'sed "s/__NOW__/$(date -u +%Y-%m-%dT%H:%M:%SZ)/"'
    if only_cwd is None:
        answer = f"{stamp} <<'JSON'\n{body}\nJSON\n"
    else:
        answer = (
            f'if [ "$(pwd -P)" = "{only_cwd.resolve()}" ] && [ "${{VNX_PROJECT_ROOT:-}}" = "{only_cwd.resolve()}" ]; then\n'
            f"{stamp} <<'JSON'\n{body}\nJSON\nelse\n{stamp} <<'JSON'\n{idle}\nJSON\nfi\n"
        )
    path.write_text(f'#!/usr/bin/env bash\nif [ "$1 $2" = "status --json" ]; then\n{answer}exit 0\nfi\nexit 1\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def _consumer(tmp_path: Path) -> Path:
    repo = tmp_path / "consumer"
    scripts = repo / ".claude" / "skills" / "t0-orchestrator" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(GUARD_SRC, scripts / "dispatch_guard.sh")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _run(repo: Path, tmp_path: Path, cwd: Path, path_dir: Path | None = None, extra: dict | None = None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env["HOME"] = str(home)
    if path_dir is not None:
        env["PATH"] = f"{path_dir}{os.pathsep}{env['PATH']}"
    env.update(extra or {})
    script = repo / ".claude" / "skills" / "t0-orchestrator" / "scripts" / "dispatch_guard.sh"
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, cwd=cwd)


def test_pip_schema_on_path_plus_install_uses_the_install(tmp_path: Path) -> None:
    repo = _consumer(tmp_path)
    pip_dir = tmp_path / "pipbin"
    _fake_cli(pip_dir / "vnx", PIP_STATUS)
    _fake_cli(tmp_path / "home" / ".vnx-system" / "current" / "bin" / "vnx", _live(False))
    result = _run(repo, tmp_path, repo, path_dir=pip_dir)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.startswith("GO")


def test_vnx_home_install_wins_over_path_cli(tmp_path: Path) -> None:
    repo = _consumer(tmp_path)
    pip_dir = tmp_path / "pipbin"
    _fake_cli(pip_dir / "vnx", PIP_STATUS)
    vnx_home = tmp_path / "vnxhome"
    _fake_cli(vnx_home / "bin" / "vnx", _live(True))
    result = _run(repo, tmp_path, repo, path_dir=pip_dir, extra={"VNX_HOME": str(vnx_home)})
    assert result.returncode == 2, result.stderr + result.stdout
    assert result.stdout.startswith("WAIT")


def test_only_pip_schema_fails_closed_naming_cli_and_schema(tmp_path: Path) -> None:
    repo = _consumer(tmp_path)
    pip_dir = tmp_path / "pipbin"
    cli = _fake_cli(pip_dir / "vnx", PIP_STATUS)
    result = _run(repo, tmp_path, repo, path_dir=pip_dir)
    assert result.returncode == 1
    assert str(cli) in result.stderr
    assert "vnx_status/1.0" in result.stderr
    assert "live_work missing" not in result.stderr


def test_caller_cwd_and_inherited_project_root_do_not_leak(tmp_path: Path) -> None:
    repo = _consumer(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _fake_cli(tmp_path / "home" / ".vnx-system" / "current" / "bin" / "vnx", _live(True), only_cwd=repo)
    result = _run(repo, tmp_path, other, extra={"VNX_PROJECT_ROOT": str(other)})
    assert result.returncode == 2, result.stderr + result.stdout
    assert "busy" in result.stdout


def test_intelligence_sh_reads_status_from_the_install_in_a_consumer(tmp_path: Path) -> None:
    repo = _consumer(tmp_path)
    shutil.copy(
        ROOT / "skills" / "t0-orchestrator" / "scripts" / "intelligence.sh",
        repo / ".claude" / "skills" / "t0-orchestrator" / "scripts" / "intelligence.sh",
    )
    _fake_cli(tmp_path / "home" / ".vnx-system" / "current" / "bin" / "vnx", _live(True), only_cwd=repo)
    home = tmp_path / "home"
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env["HOME"] = str(home)
    script = repo / ".claude" / "skills" / "t0-orchestrator" / "scripts" / "intelligence.sh"
    other = tmp_path / "elsewhere"
    other.mkdir()
    result = subprocess.run(
        ["bash", "-c", f'source "{script}" >/dev/null; check_live_work'],
        capture_output=True, text=True, env=env, cwd=other,
    )
    assert result.returncode == 0, result.stderr
    assert "live 20261021-busy" in result.stdout


def test_intelligence_sh_names_no_repo_local_data_dir_or_relative_script() -> None:
    text = (ROOT / "skills" / "t0-orchestrator" / "scripts" / "intelligence.sh").read_text(encoding="utf-8")
    assert ".vnx-data" not in text
    assert 'python3 "scripts/' not in text


def test_project_pin_shim_beats_the_central_current_install(tmp_path: Path) -> None:
    repo = _consumer(tmp_path)
    home = tmp_path / "home"
    _fake_cli(home / ".vnx-system" / "bin" / "vnx", _live(False))
    _fake_cli(home / ".vnx-system" / "current" / "bin" / "vnx", _live(True))
    result = _run(repo, tmp_path, repo)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.startswith("GO")


def test_explicit_vnx_bin_beats_the_central_current_install(tmp_path: Path) -> None:
    repo = _consumer(tmp_path)
    explicit = _fake_cli(tmp_path / "explicit" / "vnx", _live(False))
    _fake_cli(tmp_path / "home" / ".vnx-system" / "current" / "bin" / "vnx", _live(True))
    result = _run(repo, tmp_path, repo, extra={"VNX_BIN": str(explicit)})
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.startswith("GO")


def test_explicit_vnx_bin_beats_the_project_pin_shim(tmp_path: Path) -> None:
    repo = _consumer(tmp_path)
    explicit = _fake_cli(tmp_path / "explicit" / "vnx", _live(False))
    _fake_cli(tmp_path / "home" / ".vnx-system" / "bin" / "vnx", _live(True))
    result = _run(repo, tmp_path, repo, extra={"VNX_BIN": str(explicit)})
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.startswith("GO")
