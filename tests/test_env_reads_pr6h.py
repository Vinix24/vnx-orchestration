"""PR-6h: enforcement precedence and retired environment readers."""
import ast
import json
import os
import shutil
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
import trace_token_validator as trace
import worker_permissions as worker
from test_env_reads_t0_rotation_flag import python_reads
from test_t0_context_guard import _logging_git
from test_worker_scope_hook_registration import _load_hook_module

READERS = [
    ("VNX_PROVENANCE_ENFORCEMENT", lambda: trace.get_enforcement_mode() == trace.EnforcementMode.ENFORCED),
    ("VNX_SLUG_ENFORCEMENT", lambda: slug._build_parser().parse_args([]).enforce),
    ("VNX_ENFORCE_WORKER_PERMISSIONS", worker.worker_permission_enforcement_enabled),
    ("VNX_PROVENANCE_LEGACY_ACCEPTED", trace.is_legacy_accepted),
]
DELETED = ("VNX_DEFAULT_BRANCHES", "VNX_RECEIPT_FALLBACK")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
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
        with sqlite3.connect(Path(os.environ["VNX_STATE_DIR"]) / "runtime_coordination.db") as conn:
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
    assert read() is (key != "VNX_PROVENANCE_LEGACY_ACCEPTED")
    if key == "VNX_PROVENANCE_LEGACY_ACCEPTED":
        key = "VNX_PROVENANCE_ENFORCEMENT"  # First failed read of the complete pair.
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
    modules = {"check_ci_slug_match.py", "worker_permissions.py", "trace_token_validator.py",
               "ndjson_hash_chain.py", "dispatch_govern.py"}
    for top in ("scripts", "vnx_cli", "dashboard"):
        for path in (ROOT / top).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for key in counts:
                if key in DELETED or path.name in modules:
                    counts[key] += python_reads(tree, key)
        for path in (ROOT / top).rglob("*.sh"):
            for key in (*DELETED, "VNX_PROVENANCE_ENFORCEMENT", "VNX_PROVENANCE_LEGACY_ACCEPTED"):
                counts[key] += path.read_text().count("${" + key) + path.read_text().count("$" + key)
    assert counts == dict.fromkeys(counts, 0), counts
    hook = (ROOT / "hooks/git/commit-msg").read_text()
    for key in ("VNX_PROVENANCE_ENFORCEMENT", "VNX_PROVENANCE_LEGACY_ACCEPTED"):
        assert "${" + key not in hook and "$" + key not in hook


def test_unresolvable_python_store_stays_restrictive(store, monkeypatch, caplog):
    monkeypatch.setenv("VNX_STATE_DIR", "x" * 1024)
    monkeypatch.setenv("VNX_PROVENANCE_ENFORCEMENT", "0")
    assert trace.get_enforcement_mode() == trace.EnforcementMode.ENFORCED
    assert trace.is_legacy_accepted() is False
    assert "project_config_error" in caplog.text


@pytest.mark.parametrize("script", ["ci_trace_token_check.sh", "commands/install_git_hooks.sh"])
@pytest.mark.parametrize("mode", ["env", "error", "missing", "store_error", "unresolvable",
                                 "row1", "row0", "override1", "override0", "global1", "global0",
                                 "empty", "null", "malformed", "unknown", "validator_error",
                                 "validator_missing", "legacy0", "legacy1", "preferred",
                                 "missing_legacy", "error_legacy", "unresolvable_legacy",
                                 "bad_legacy_empty", "bad_legacy_null", "bad_legacy_malformed", "bad_legacy_unknown"])
def test_bash_config_and_fallback(store, tmp_path, monkeypatch, script, mode):
    consumer = tmp_path / "consumer"
    subprocess.run(["git", "init", "-q", "-b", "main", str(consumer)], check=True)
    message = ("feat: PR-9" if "legacy" in mode else "feat: no token")
    mode = mode.removesuffix("_legacy")
    if mode == "preferred":
        message += "\n\nDispatch-ID: dlv-ac12ec2948bd-hd"
    for commit_message in ("base", message):
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Test",
                        "-c", "user.email=test@example.org", "commit", "--allow-empty", "-m", commit_message],
                       cwd=consumer, check=True, capture_output=True)
    home = consumer / ".claude/vnx-system"
    for directory in ("bin", "scripts", "hooks"):
        shutil.copytree(ROOT / directory, home / directory, ignore=shutil.ignore_patterns("__pycache__"))
    assert not (home / ".git").exists() and not (consumer / "bin/vnx").exists()
    (tmp_path / "state").mkdir()
    for key, value in {"VNX_HOME": home, "VNX_PROJECT_ROOT": consumer, "VNX_DATA_DIR": tmp_path,
                       "VNX_STATE_DIR": tmp_path / "state", "VNX_PROVENANCE_ENFORCEMENT": "1"}.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    if mode.startswith("legacy"):
        store("VNX_PROVENANCE_LEGACY_ACCEPTED", mode[-1])
        monkeypatch.setenv("VNX_PROVENANCE_LEGACY_ACCEPTED", str(1 - int(mode[-1])))
    if mode.startswith(("row", "override")):
        store("VNX_PROVENANCE_ENFORCEMENT", mode[-1] if mode.startswith("row") else str(1-int(mode[-1])))
        if mode == "row1":
            monkeypatch.delenv("VNX_PROVENANCE_ENFORCEMENT")
        if mode.startswith("override"):
            monkeypatch.setenv("VNX_OVERRIDE_PROVENANCE_ENFORCEMENT", mode[-1])
    if mode in ("error", "global1", "global0"):
        path = Path(os.environ["VNX_GLOBAL_CONFIG_FILE"])
        path.parent.mkdir(parents=True)
        path.write_text("invalid json" if mode == "error" else json.dumps(
            {"version": 1, "values": {"VNX_PROVENANCE_ENFORCEMENT": {"value": mode[-1]}}}))
        monkeypatch.setenv("VNX_PROVENANCE_ENFORCEMENT", "0" if mode != "global0" else "1")
    if mode == "store_error":
        (tmp_path / "state/runtime_coordination.db").write_bytes(b"not sqlite")
    if mode == "unresolvable":
        monkeypatch.delenv("VNX_PROJECT_ID")
    validator = home / "scripts/lib/trace_token_validator.py"
    if mode == "validator_error":
        validator.write_bytes(b"\xff")
    if mode in ("missing", "validator_missing"):
        validator.rename(validator.with_suffix(".absent"))
    invalid = {"empty": "", "null": "null", "malformed": "0\n1", "unknown": "unknown"}
    if mode in invalid:
        monkeypatch.setenv("VNX_OVERRIDE_PROVENANCE_ENFORCEMENT", invalid[mode])
    if mode.startswith("bad_legacy_"):
        monkeypatch.setenv("VNX_PROVENANCE_ENFORCEMENT", "0")
        monkeypatch.setenv("VNX_OVERRIDE_PROVENANCE_LEGACY_ACCEPTED", invalid[mode.removeprefix("bad_legacy_")])
    command = ('bash "$1" HEAD~1' if script.startswith("ci_") else
               'log() { echo "$*"; }; err() { echo "$*" >&2; }; source "$1"; cmd_install_git_hooks')
    result = subprocess.run(["bash", "-c", command, "pr6h", str(home / "scripts" / script)],
                            cwd=consumer, capture_output=True, text=True)
    enforced = mode not in ("row0", "override0", "global0")
    legacy_allowed = mode == "legacy1" or (mode == "unresolvable" and message == "feat: PR-9")
    blocked = enforced and mode != "preferred" and not legacy_allowed
    if mode not in ("missing", "unresolvable"):
        assert (trace.get_enforcement_mode() == trace.EnforcementMode.ENFORCED) is enforced
    expected = ("ENFORCED" if enforced else "shadow") if script.startswith("ci_") else (
        "ACTIVE" if enforced else "SHADOW")
    assert "Enforcement: " + expected in result.stdout, result.stdout + result.stderr
    if script.startswith("ci_"):
        assert result.returncode == int(blocked), result.stdout + result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        monkeypatch.delenv("VNX_HOME")  # The installed symlink must locate its own installation.
        message_file = tmp_path / "message"
        message_file.write_text(message)
        hook = subprocess.run(["bash", str(consumer / ".git/hooks/commit-msg"), str(message_file)],
                              cwd=consumer, capture_output=True, text=True)
        assert hook.returncode == int(blocked), hook.stdout + hook.stderr
    if mode in ("error", "missing", "store_error", *invalid) or mode.startswith("bad_legacy_"):
        assert "Config read failed" in result.stdout + result.stderr


@pytest.mark.parametrize("token", [False, True])
@pytest.mark.parametrize("evidence", ["head", "missing", "null", "malformed", "unknown", "unavailable", "no_payload", "no_path"])
def test_ci_pr_head_evidence(store, tmp_path, monkeypatch, token, evidence):
    def git(*args, **kwargs):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Test",
                               "-c", "user.email=test@example.org", *args], cwd=tmp_path,
                              check=True, capture_output=True, text=True, **kwargs).stdout.strip()
    git("init", "-q")
    git("commit", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "feat: change" + ("\n\nDispatch-ID: dlv-ac12ec2948bd-hd" if token else ""))
    head = git("rev-parse", "HEAD")
    merge = git("commit-tree", "HEAD^{tree}", "-p", base, "-p", head, input="Synthetic merge\n")
    git("checkout", "--detach", merge)
    sha = {"head": head, "null": None, "malformed": [], "unknown": "unknown", "unavailable": "0" * 40}
    event = tmp_path / "event.json"
    if evidence not in ("no_payload", "no_path"):
        event.write_text(json.dumps({"pull_request": {"head": {} if evidence == "missing" else {"sha": sha[evidence]}}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    if evidence == "no_path":
        monkeypatch.delenv("GITHUB_EVENT_PATH")
    monkeypatch.setenv("VNX_PROVENANCE_ENFORCEMENT", "1")
    result = subprocess.run(["bash", str(ROOT / "scripts/ci_trace_token_check.sh"), base],
                            cwd=tmp_path, capture_output=True, text=True)
    assert (result.returncode == 0) is (token and evidence == "head"), result.stdout + result.stderr
    if evidence == "head":
        assert "Total: 1" in result.stdout
    else:
        assert result.stderr and "RESULT: PASS" not in result.stdout
