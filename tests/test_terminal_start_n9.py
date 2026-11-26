"""N9: terminal permissions and the T0 provider use the real config store."""
import sqlite3
from contextlib import closing

import pytest

import config_registry as cr
import config_runtime as crt
import config_store_db as cs
from vnx_start_runtime import StartConfig, build_provider_command

TERMINALS = ("T0", "T1", "T2", "T3")
SKIP_KEYS = tuple(f"VNX_{tid}_SKIP_PERMISSIONS" for tid in TERMINALS)
PID = "n9-project"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    for key in SKIP_KEYS + tuple(f"VNX_{tid}_PROVIDER" for tid in TERMINALS):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.replace("VNX_", "VNX_OVERRIDE_", 1), raising=False)
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    monkeypatch.setattr(crt, "_wired_for", {})
    monkeypatch.setattr(crt, "_wire_status", None)
    monkeypatch.setattr(cr, "_db_resolver", None)
    monkeypatch.setattr(cr, "_default_project_id", None)
    with closing(sqlite3.connect(tmp_path / "runtime_coordination.db")) as conn:
        cs.ensure_config_tables(conn)
        yield conn


@pytest.mark.parametrize("tid", TERMINALS)
@pytest.mark.parametrize("stored,env,expected", [
    ("0", "1", False), ("1", "0", True),
    (None, "0", False), (None, "1", True), (None, None, False),
])
def test_skip_precedence(isolated_config, monkeypatch, tid, stored, env, expected):
    key = f"VNX_{tid}_SKIP_PERMISSIONS"
    if stored is not None:
        cs.set_config(isolated_config, PID, key, stored, actor="op", approval_id="n9-approval")
    if env is not None:
        monkeypatch.setenv(key, env)
    terminal = StartConfig.from_env().terminals[tid]
    assert terminal.skip_permissions is expected
    terminal.provider = "claude_code"
    assert ("--dangerously-skip-permissions" in build_provider_command(terminal)) is expected


@pytest.mark.parametrize("stored,env,expected", [
    ("codex", "claude_code", "codex"), ("claude_code", "codex", "claude_code"),
    (None, "codex", "codex"), (None, "claude_code", "claude_code"),
    (None, None, "claude_code"),
])
def test_t0_provider_precedence(isolated_config, monkeypatch, stored, env, expected):
    key = "VNX_T0_PROVIDER"
    if stored is not None:
        cs.set_config(isolated_config, PID, key, stored, actor="op")
    if env is not None:
        monkeypatch.setenv(key, env)
    terminal = StartConfig.from_env().terminals["T0"]
    assert terminal.provider == expected
    assert build_provider_command(terminal).startswith("codex " if expected == "codex" else "claude ")


@pytest.mark.parametrize("key", SKIP_KEYS)
def test_skip_write_requires_approval(isolated_config, key):
    with pytest.raises(PermissionError, match=f"{key} requires an approval_id"):
        cs.set_config(isolated_config, PID, key, "1", actor="op")
    assert cs.read_config(isolated_config, PID, key) is None
    assert isolated_config.execute("SELECT COUNT(*) FROM project_config_audit").fetchone() == (0,)
    assert StartConfig.from_env().terminals[key.split("_")[1]].skip_permissions is False


@pytest.mark.parametrize("env", [None, "0", "garbled", "unknown"])
@pytest.mark.parametrize("store", ["corrupt", "unopenable"])
def test_unreadable_store_never_enables_skip(tmp_path, monkeypatch, env, store):
    broken = tmp_path / "broken"
    broken.mkdir()
    db = broken / "runtime_coordination.db"
    if store == "corrupt":
        db.write_bytes(b"not a sqlite database")
    else:
        db.mkdir()  # SQLite cannot open a directory, even when tests run as root.
    monkeypatch.setenv("VNX_STATE_DIR", str(broken))
    for key in SKIP_KEYS:
        if env is not None:
            monkeypatch.setenv(key, env)
    strict = cs.make_db_resolver(lambda _pid: broken, raise_on_error=True)
    with pytest.raises(sqlite3.Error):
        strict(PID, SKIP_KEYS[0])
    # N9 preserves config_runtime's non-strict fallback; an error supplies no opt-in.
    assert all(not t.skip_permissions for t in StartConfig.from_env().terminals.values())
