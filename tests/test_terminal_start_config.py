"""Terminal start configuration (config-een-oppervlak PR-3): registry keys and the delete defaults.

Registry keys (``VNX_T1/2/3_PROVIDER``, ``VNX_QUEUE_POPUP_ENABLED``): a stored value wins without
env. Deleted names: the variable no longer steers anything and the old default path stays.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import config_registry as cr  # noqa: E402
import config_runtime as crt  # noqa: E402
import config_store_db as cs  # noqa: E402
import vnx_paths  # noqa: E402
from vnx_start_runtime import PROVIDER_CLAUDE, PROVIDER_KIMI, StartConfig  # noqa: E402

PID = "vnx-dev"
KEYS = ("VNX_T1_PROVIDER", "VNX_T2_PROVIDER", "VNX_T3_PROVIDER", "VNX_QUEUE_POPUP_ENABLED")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in KEYS + ("VNX_T0_MODEL", "VNX_T1_MODEL", "VNX_T0_FLAGS", "VNX_GEMINI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {})
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)
    yield
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)


def _store(tmp_path, monkeypatch, **values):
    sd = tmp_path / "state"
    sd.mkdir()
    conn = sqlite3.connect(sd / "runtime_coordination.db")
    for key, value in values.items():
        cs.set_config(conn, PID, key, value, actor="op", approval_id=None)
    conn.close()
    monkeypatch.setenv("VNX_STATE_DIR", str(sd))


def test_stored_providers_win_without_env(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, VNX_T1_PROVIDER="codex_cli", VNX_T2_PROVIDER="gemini_cli",
           VNX_T3_PROVIDER="codex_cli")
    terminals = StartConfig.from_env().terminals
    assert [terminals[t].provider for t in ("T1", "T2", "T3")] == ["codex", "gemini", "codex"]


def test_stored_provider_beats_env(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, VNX_T1_PROVIDER="codex_cli")
    monkeypatch.setenv("VNX_T1_PROVIDER", "gemini_cli")
    assert StartConfig.from_env().terminals["T1"].provider == "codex"


def test_unset_providers_keep_the_start_defaults(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, VNX_QUEUE_POPUP_ENABLED="1")
    terminals = StartConfig.from_env().terminals
    assert terminals["T1"].provider == PROVIDER_KIMI
    assert terminals["T2"].provider == PROVIDER_KIMI
    assert terminals["T3"].provider == PROVIDER_CLAUDE


def test_stored_queue_popup_wins_without_env(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, VNX_QUEUE_POPUP_ENABLED="0")
    assert StartConfig.from_env().queue_popup_enabled is False


def test_queue_popup_defaults_on(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, VNX_T1_PROVIDER="codex_cli")
    assert StartConfig.from_env().queue_popup_enabled is True


def test_deleted_variables_no_longer_steer_the_start(monkeypatch):
    monkeypatch.setenv("VNX_T0_MODEL", "opus")
    monkeypatch.setenv("VNX_T1_MODEL", "opus")
    monkeypatch.setenv("VNX_T0_FLAGS", "--bad-flag")
    monkeypatch.setenv("VNX_GEMINI_MODEL", "gemini-x")
    cfg = StartConfig.from_env()
    assert cfg.terminals["T0"].model == "default" and cfg.terminals["T0"].extra_flags == ""
    assert cfg.terminals["T1"].model == "kimi-k3"
    assert cfg.gemini_model == "gemini-2.5-pro"


# ── bash: the supervisor reads through `vnx config get --raw` and stops loudly ──────────────

def _supervisor_fn() -> str:
    text = (REPO / "scripts" / "vnx_supervisor_simple.sh").read_text()
    m = re.search(r"^load_queue_popup_enabled\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, "load_queue_popup_enabled not found"
    return m.group(0)


def _run_fn(tmp_path, stub_body: str):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    stub = home / "bin" / "vnx"
    stub.write_text("#!/bin/bash\n" + stub_body + "\n")
    stub.chmod(0o755)
    script = (
        'SUPERVISOR_QUEUE_POPUP=""\nlog() { echo "$*" >&2; }\n' + _supervisor_fn()
        + 'load_queue_popup_enabled\necho "value=$SUPERVISOR_QUEUE_POPUP"\n'
    )
    env = {**os.environ, "VNX_HOME": str(home), "PATH": "/usr/bin:/bin"}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def test_supervisor_reads_the_value_through_the_cli(tmp_path):
    r = _run_fn(tmp_path, 'echo "$@" >&2; echo 0')
    assert r.returncode == 0 and "value=0" in r.stdout
    assert "config get --raw VNX_QUEUE_POPUP_ENABLED" in r.stderr


@pytest.mark.parametrize("body", ["exit 3", "exit 2", "echo boom >&2; exit 127", "echo ''"])
def test_supervisor_stops_naming_the_key_on_a_failed_or_empty_read(tmp_path, body):
    r = _run_fn(tmp_path, body)
    assert r.returncode == 1
    assert "VNX_QUEUE_POPUP_ENABLED" in r.stderr
    assert "value=" not in r.stdout


@pytest.mark.parametrize("rel", ["scripts/commands/start.sh", "scripts/commands/resume.sh",
                                 "scripts/vnx_supervisor_simple.sh"])
def test_bash_readers_use_the_fabric_binary_never_path(rel):
    text = (REPO / rel).read_text()
    assert '"$VNX_HOME/bin/vnx" config get --raw VNX_QUEUE_POPUP_ENABLED' in text
    assert "${VNX_QUEUE_POPUP_ENABLED" not in text
