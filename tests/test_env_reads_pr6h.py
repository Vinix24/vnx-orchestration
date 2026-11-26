"""PR-6h: enforcement precedence and retired environment readers."""
import ast
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts/lib")]
import check_ci_slug_match as slug
import config_registry as registry
import config_runtime as runtime
import config_store_db
import project_root
import worker_permissions as worker
from test_env_reads_t0_rotation_flag import python_reads
from test_t0_context_guard import _logging_git
from test_worker_scope_hook_registration import _load_hook_module

READERS = [
    ("VNX_SLUG_ENFORCEMENT", lambda: slug._build_parser().parse_args([]).enforce),
    ("VNX_ENFORCE_WORKER_PERMISSIONS", worker.worker_permission_enforcement_enabled),
]
DELETED = ("VNX_DEFAULT_BRANCHES", "VNX_RECEIPT_FALLBACK")


@pytest.fixture
def store(tmp_path, monkeypatch):
    for key in [k for k, _ in READERS] + list(DELETED) + ["VNX_WORKER_ENFORCEMENT_SKIP"]:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("VNX_OVERRIDE_" + key[4:], raising=False)
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_PROJECT_ID", "pr6h")
    monkeypatch.setattr(runtime, "_wired_for", {})
    monkeypatch.setattr(runtime, "_wire_status", None)
    monkeypatch.setattr(registry, "_db_resolver", None)
    monkeypatch.setattr(registry, "_default_project_id", None)

    def put(key, value):
        with sqlite3.connect(tmp_path / "runtime_coordination.db") as conn:
            config_store_db.set_config(conn, "pr6h", key, value, actor="test", approval_id="pr6h")
    return put


@pytest.mark.parametrize("key,read", READERS)
@pytest.mark.parametrize("value", ["0", "1"])
@pytest.mark.parametrize("source", ["row", "override", "env"])
def test_precedence(store, monkeypatch, key, read, value, source):
    opposite = str(1 - int(value))
    monkeypatch.setenv(key, opposite if source != "env" else value)
    if source != "env":
        store(key, value if source == "row" else opposite)
    if source == "override":
        monkeypatch.setenv("VNX_OVERRIDE_" + key[4:], value)
    assert read() is (value == "1")


@pytest.mark.parametrize("key,read", READERS)
def test_unreadable_wired_store_keeps_enforcement(store, monkeypatch, tmp_path, caplog, key, read):
    store(key, "1")
    monkeypatch.setenv(key, "0")
    assert runtime.autowire()
    (tmp_path / "runtime_coordination.db").write_bytes(b"not sqlite")
    assert read() is True
    assert key in caplog.text and "project_config" in caplog.text


@pytest.fixture
def central_hook(store, tmp_path, monkeypatch):
    hook = _load_hook_module()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    central = project_root.resolve_central_data_dir("pr6h")
    (central / "state").mkdir(parents=True)
    store("VNX_ENFORCE_WORKER_PERMISSIONS", "1")
    (tmp_path / "runtime_coordination.db").rename(central / "state/runtime_coordination.db")
    root = tmp_path / "project"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".vnx-data").symlink_to(central, target_is_directory=True)
    monkeypatch.setattr(hook, "__file__", str(root / "hook.py"))
    monkeypatch.chdir(tmp_path)
    for key in ("VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_STATE_DIR", "VNX_PROJECT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VNX_WORKER_ROLE", "backend-developer")
    return hook, central


@pytest.mark.parametrize("pin", ["inherited", "explicit", "state_only", "neither"])
def test_hook_data_dir_matches_fabric(central_hook, monkeypatch, tmp_path, pin):
    hook, central = central_hook
    if pin in ("inherited", "explicit"):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "foreign"))
    if pin == "explicit":
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    if pin == "state_only":
        monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "foreign/state"))
    expected = project_root.resolve_data_dir(hook.__file__)
    assert expected.resolve() == (tmp_path / "foreign" if pin == "explicit" else central)
    assert hook._hook_data_dir() == expected


@pytest.mark.parametrize("unreadable", [False, True])
def test_hook_central_store_enforces_without_env(central_hook, unreadable):
    hook, central = central_hook
    if unreadable:
        (central / "state/runtime_coordination.db").write_bytes(b"not sqlite")
    decision, reason = hook.evaluate({"tool_name": "Write", "tool_input": {"file_path": "/etc/passwd"}})
    assert decision == "block" and "file_write_scope" in reason


@pytest.mark.parametrize("value", ["0", "1"])
@pytest.mark.parametrize("pin", ["state", "state_only", "data", "local"])
@pytest.mark.parametrize("tool", ["Bash", "Write"])
def test_hook_stored_switch(store, tmp_path, monkeypatch, value, pin, tool):
    key = "VNX_ENFORCE_WORKER_PERMISSIONS"
    store(key, value)
    monkeypatch.setenv(key, str(1 - int(value)))
    state = tmp_path
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    if pin != "state":
        state = tmp_path / (".vnx-data/state" if pin == "local" else "state")
        state.mkdir(parents=True)
        (tmp_path / "runtime_coordination.db").rename(state / "runtime_coordination.db")
        monkeypatch.delenv("VNX_STATE_DIR")
        if pin in ("local", "state_only"):
            monkeypatch.delenv("VNX_DATA_DIR")
        if pin == "state_only":
            monkeypatch.setenv("VNX_STATE_DIR", str(state))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    bin_dir, log = _logging_git(tmp_path)
    payload = {"tool_name": tool, "tool_input": {"command": "git push --force origin main",
                                               "file_path": "/etc/passwd"}}
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", VNX_WORKER_ROLE="backend-developer")
    script = ("import sys; from test_worker_scope_hook_registration import _load_hook_module; "
              "hook = _load_hook_module(); hook.__file__ = sys.argv[1]; hook.main()")
    env["PYTHONPATH"] = str(ROOT / "tests")
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path / "hook.py")],
                            input=json.dumps(payload), env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    if value == "1":
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        data = tmp_path / ".vnx-data" if pin in ("local", "state_only") else tmp_path
        assert json.loads((data / "events/worker_scope_block.ndjson").read_text())["decision"] == "block"
    else:
        assert result.stdout == ""
    if pin in ("state", "data"):
        assert not log.exists(), log.read_text()


def test_zero_environment_reads():
    counts = dict.fromkeys((*DELETED, *(k for k, _ in READERS)), 0)
    modules = {"check_ci_slug_match.py", "worker_permissions.py",
               "ndjson_hash_chain.py", "dispatch_govern.py"}
    for top in ("scripts", "vnx_cli", "dashboard"):
        for path in (ROOT / top).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for key in counts:
                if key in DELETED or path.name in modules:
                    counts[key] += python_reads(tree, key)
        for path in (ROOT / top).rglob("*.sh"):
            for key in DELETED:
                counts[key] += path.read_text().count("${" + key) + path.read_text().count("$" + key)
    assert counts == dict.fromkeys(counts, 0), counts
