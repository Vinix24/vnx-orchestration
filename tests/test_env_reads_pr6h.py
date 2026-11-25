"""PR-6h: enforcement precedence, hash-chain fold and retired environment readers."""
import ast
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
import ndjson_hash_chain as chain
import trace_token_validator as trace
import worker_permissions as worker
from test_env_reads_t0_rotation_flag import python_reads

READERS = [
    ("VNX_PROVENANCE_ENFORCEMENT", lambda: trace.get_enforcement_mode() == trace.EnforcementMode.ENFORCED),
    ("VNX_SLUG_ENFORCEMENT", lambda: slug._build_parser().parse_args([]).enforce),
    ("VNX_ENFORCE_WORKER_PERMISSIONS", worker.worker_permission_enforcement_enabled),
    ("VNX_PROVENANCE_LEGACY_ACCEPTED", trace.is_legacy_accepted),
    ("VNX_HASH_CHAIN_REQUIRED", chain.chain_receipts_variable_on),
]
DELETED = ("VNX_CHAIN_RECEIPTS", "VNX_DEFAULT_BRANCHES", "VNX_RECEIPT_FALLBACK")


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


@pytest.mark.parametrize("legacy", [False, True])
def test_chain_default_and_retired_flag(store, monkeypatch, legacy):
    if legacy:
        monkeypatch.setenv("VNX_CHAIN_RECEIPTS", "1")
    assert chain.append_chains(chain.TAIL_EMPTY) is False


@pytest.mark.parametrize("key,read", [READERS[0], READERS[2]])
def test_unreadable_wired_store_keeps_enforcement(store, monkeypatch, tmp_path, caplog, key, read):
    store(key, "1")
    monkeypatch.setenv(key, "0")
    assert runtime.autowire()
    (tmp_path / "runtime_coordination.db").write_bytes(b"not sqlite")
    assert read() is True
    assert key in caplog.text and "project_config" in caplog.text


def test_zero_environment_reads():
    counts = dict.fromkeys((*DELETED, *(k for k, _ in READERS[:4])), 0)
    modules = {"check_ci_slug_match.py", "trace_token_validator.py", "worker_permissions.py",
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


@pytest.mark.parametrize("script", ["ci_trace_token_check.sh", "commands/install_git_hooks.sh"])
@pytest.mark.parametrize("mode", ["env", "error", "missing"])
def test_bash_config_and_fallback(store, tmp_path, monkeypatch, script, mode):
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    for message in ("base", "feat: trace\n\nDispatch-ID: 20261125-120000-pr6h-A"):
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                        "commit", "--allow-empty", "-m", message], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.setenv("VNX_PROVENANCE_ENFORCEMENT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    if mode == "error":
        Path(os.environ["VNX_GLOBAL_CONFIG_FILE"]).parent.mkdir(parents=True, exist_ok=True)
        Path(os.environ["VNX_GLOBAL_CONFIG_FILE"]).write_text("invalid json")
    home = ROOT
    if mode == "missing":
        home = tmp_path
        (home / "scripts/lib").mkdir(parents=True)
        (home / "scripts/lib/vnx_resolve_root.sh").write_text((ROOT / "scripts/lib/vnx_resolve_root.sh").read_text())
        (home / "hooks").symlink_to(ROOT / "hooks", target_is_directory=True)
    target = home / "scripts" / script
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == "missing":
        target.write_text((ROOT / "scripts" / script).read_text())
    command = 'bash "$1" HEAD~1' if script.startswith("ci_") else 'log() { echo "$*"; }; err() { echo "$*"; }; source "$1"; cmd_install_git_hooks'
    result = subprocess.run(["bash", "-c", command, "pr6h", str(target)], cwd=tmp_path,
                            env=dict(os.environ, VNX_HOME=str(home)), capture_output=True, text=True)
    expected = ("Enforcement: ENFORCED" if mode == "env" else "Enforcement: shadow") if script.startswith("ci_") else ("Enforcement: ACTIVE" if mode == "env" else "Enforcement: SHADOW")
    assert expected in result.stdout, result.stdout + result.stderr
    assert result.returncode == 0
    if mode != "env":
        assert "Config read failed" in result.stdout + result.stderr
